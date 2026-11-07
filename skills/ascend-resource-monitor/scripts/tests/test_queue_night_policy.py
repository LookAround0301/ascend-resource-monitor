from pathlib import Path
import sys
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'assets/runtime'))
from queue_night_policy import reserve_night,enforcement_decisions
from queue_planner import plan
from queue_store import QueueStore
from queue_unified import UnifiedQueue
from control_common import ControlError


class NightPolicyTests(unittest.TestCase):
    def test_normal_task_is_clipped_at_two_and_cannot_start_at_one(self):
        nodes={'a':list(range(8)),'b':list(range(8))}
        job={'id':'normal','cards':8,'seconds':7200,'start_before':3600,
             'clip_finish_before':7200,'finish_before':7200,'allowed_nodes':['a']}
        result=plan(nodes,[],[job],3000)
        self.assertEqual(result[0]['start'],3000);self.assertEqual(result[0]['seconds'],4200)
        self.assertEqual(plan(nodes,[],[job],3600)[0]['start'],None)
        self.assertEqual(plan(nodes,[],[job],3599)[0]['seconds'],3601)

    def test_exact_fixed_windows_and_dual_capacity(self):
        nodes={'a':list(range(8)),'b':list(range(8))}
        first=reserve_night(nodes,[],1000,3600,8,False,'a')
        self.assertEqual(first,{'a':list(range(8))})
        existing=[{'start':1000,'end':4600,'allocations':first}]
        self.assertIsNone(reserve_night(nodes,existing,1200,600,8,False,'a'))
        self.assertEqual(reserve_night(nodes,existing,1200,600,8,False,'b'),{'b':list(range(8))})
        self.assertIsNone(reserve_night(nodes,existing,1200,600,8,True))
        self.assertEqual(reserve_night(nodes,existing,4600,600,8,True),nodes)

    def test_sixteen_card_groups_and_preserving_future_assignments(self):
        nodes={'a':list(range(16)),'b':list(range(16))}
        one=reserve_night(nodes,[],1000,3000,8,False,'a')
        first=[{'start':1000,'end':4000,'allocations':one}]
        two=reserve_night(nodes,first,2000,600,8,False,'a')
        self.assertEqual(set(one['a'])&set(two['a']),set())
        first.append({'start':2000,'end':2600,'allocations':two})
        self.assertIsNone(reserve_night(nodes,first,2100,300,16,False,'a'))
        self.assertEqual(len(reserve_night(nodes,first,4000,300,16,False,'a')['a']),16)

    def test_night_can_use_free_cards_split_across_groups(self):
        nodes={'a':list(range(8)),'b':list(range(8))}
        reservations=[{'start':1000,'end':2000,'allocations':{'a':[0,1,4,5,6,7]}}]
        self.assertEqual(reserve_night(nodes,reservations,1200,300,2,False,'a'),{'a':[2,3]})
        reservations[0]['allocations']['a']=[0,1,3,4,5,7]
        self.assertEqual(reserve_night(nodes,reservations,1200,300,2,False,'a'),{'a':[2,6]})

    def test_enforcement_protects_owner_shared_and_unassigned_cards(self):
        task={'state':'active','not_before':1000,'requested_seconds':600,'employee':'owner',
              'allowed_employees':['shared'],'allocations':{'a':[0,1]}}
        def row(employee,cards,letter):
            return {'node':'a','employee':employee,'cards':cards,'control_ref':letter*64+'@started','process_key':letter}
        rows=[row('owner',[0],'a'),row('shared',[1],'b'),row('other',[1],'c'),row('other',[2],'d')]
        targets,errors=enforcement_decisions([task],rows,{'a'},1100)
        self.assertEqual([x['control_ref'] for x in targets],['c'*64+'@started'])
        self.assertEqual(errors,[])
        targets,errors=enforcement_decisions([task],rows,{'a'},1600)
        self.assertEqual({x['control_ref'] for x in targets},{c*64+'@started' for c in 'abc'})
        self.assertEqual(errors,[])

    def test_stale_or_ambiguous_data_never_issues_a_stop(self):
        task={'state':'queued','not_before':1000,'requested_seconds':600,'employee':'owner',
              'allowed_employees':[],'allocations':{'a':[0]}}
        unknown={'node':'a','employee':'未知','cards':[0],'control_ref':'a'*64+'@started','process_key':'a'}
        targets,errors=enforcement_decisions([task],[unknown],{'a'},1100)
        self.assertEqual(targets,[]);self.assertEqual(errors[0]['reason'],'unknown_owner_or_container')
        targets,errors=enforcement_decisions([task],[unknown],set(),1100)
        self.assertEqual(targets,[]);self.assertEqual(errors[0]['reason'],'inventory_unavailable')

    def test_shared_container_conflict_does_not_kill_authorized_job(self):
        task={'state':'active','not_before':1000,'requested_seconds':600,'employee':'owner',
              'allowed_employees':[],'allocations':{'a':[0,1]}}
        rows=[{'node':'a','employee':name,'cards':[card],'control_ref':'a'*64+'@started','process_key':name}
              for name,card in [('owner',0),('other',1)]]
        targets,errors=enforcement_decisions([task],rows,{'a'},1100)
        self.assertEqual(targets,[]);self.assertEqual(errors[0]['reason'],'shared_container_conflict')


class StrictQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=QueueStore(Path(self.temp.name)/'queue.db')
        self.user=self.store.user(self.store.register('u12345678','Fixture','fixture-password'))
        self.queue=UnifiedQueue(self.store,['a','b'],timezone='Asia/Shanghai',strict_nights=True)
        self.clock=patch('queue_unified.time.time').start();self.addCleanup(patch.stopall)
        self.stamp=lambda s:datetime.fromisoformat(s).replace(tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        self.clock.return_value=self.stamp('2026-11-04T23:00:00')
    def add(self,**values):
        return self.queue.enqueue(self.user,{'machine':'a','cards':8,'requested_seconds':7200,
                                              'notifications_ready':True,**values})
    def test_night_reservation_is_stable_and_conflicts_are_rejected(self):
        start=self.stamp('2026-11-05T02:00:00')
        first=self.add(night=True,not_before=start,requested_seconds=3600)
        row=next(r for r in self.queue.view(self.user)['tasks'] if r['id']==first)
        self.assertTrue(row['night_fixed']);self.assertEqual(row['allocations'],{'a':list(range(8))})
        self.assertEqual(row['estimated_start'],start)
        with self.assertRaises(ControlError):self.add(night=True,not_before=start,requested_seconds=3600)
        second=self.add(machine='b',night=True,not_before=start,requested_seconds=3600)
        self.queue.action(self.user,first,'cancel')
        row=next(r for r in self.queue.view(self.user)['tasks'] if r['id']==second)
        self.assertEqual(row['allocations'],{'b':list(range(8))})
        self.assertEqual(row['estimated_start'],start)
        self.clock.return_value=start;self.queue.tick()
        row=next(r for r in self.queue.view(self.user)['tasks'] if r['id']==second)
        self.assertEqual(row['state'],'queued')  # Takeover must confirm actual resources first.
    def test_late_normal_offer_is_cut_at_two_and_expires_at_one(self):
        self.clock.return_value=self.stamp('2026-11-05T00:50:00')
        job=self.add()
        row=next(r for r in self.queue.view(self.user)['tasks'] if r['id']==job)
        self.assertEqual(row['state'],'ready')
        self.clock.return_value=self.stamp('2026-11-05T00:52:00')
        self.queue.action(self.user,job,'claim',row['offer_id'])
        row=next(r for r in self.queue.view(self.user)['tasks'] if r['id']==job)
        self.assertEqual(row['activated']+row['requested_seconds'],self.stamp('2026-11-05T02:00:00'))
        self.queue.action(self.user,job,'finish')
        self.clock.return_value=self.stamp('2026-11-05T00:59:00')
        job=self.add();row=next(r for r in self.queue.view(self.user)['tasks'] if r['id']==job)
        self.assertEqual(row['claim_before'],self.stamp('2026-11-05T01:00:00'))
        self.clock.return_value=self.stamp('2026-11-05T01:00:00')
        row=next(r for r in self.queue.view(self.user)['tasks'] if r['id']==job)
        self.assertEqual(row['state'],'queued');self.assertTrue(row['normal_start_after_one'])

    def test_normal_requests_after_one_cannot_start_before_morning_reset(self):
        for value in ('2026-11-05T01:00:00','2026-11-05T02:00:00',
                      '2026-11-05T03:00:00','2026-11-05T08:59:00'):
            with self.subTest(value=value):
                self.clock.return_value=self.stamp(value)
                job=self.add()
                row=next(r for r in self.queue.view(self.user)['tasks'] if r['id']==job)
                self.assertEqual(row['state'],'queued')
                self.queue.action(self.user,job,'cancel')
        self.clock.return_value=self.stamp('2026-11-05T09:00:00')
        job=self.add()
        self.assertEqual(next(r for r in self.queue.view(self.user)['tasks'] if r['id']==job)['state'],'ready')

    def test_existing_offer_is_capped_by_fixed_night_on_upgrade(self):
        self.queue=UnifiedQueue(self.store,['a','b'],timezone='Asia/Shanghai')
        self.clock.return_value=self.stamp('2026-11-05T00:50:00')
        normal=self.add()
        start=self.stamp('2026-11-05T02:00:00')
        self.add(night=True,not_before=start,requested_seconds=3600)
        self.queue=UnifiedQueue(self.store,['a','b'],timezone='Asia/Shanghai',strict_nights=True)
        row=next(r for r in self.queue.view(self.user)['tasks'] if r['id']==normal)
        self.assertEqual(row['offer_end_before'],start-600)
        self.queue.action(self.user,normal,'claim',row['offer_id'])
        row=next(r for r in self.queue.view(self.user)['tasks'] if r['id']==normal)
        self.assertEqual(row['activated']+row['requested_seconds'],start-600)

    def test_normal_offer_fits_before_fixed_night_even_if_claimed_later(self):
        start=self.stamp('2026-11-05T02:00:00')
        night=self.add(night=True,not_before=start,requested_seconds=3600)
        self.clock.return_value=self.stamp('2026-11-05T00:50:00')
        normal=self.add(requested_seconds=7200)
        row=next(r for r in self.queue.view(self.user)['tasks'] if r['id']==normal)
        self.assertEqual(row['state'],'ready');self.assertEqual(row['effective_requested_seconds'],3600)
        self.clock.return_value=self.stamp('2026-11-05T00:55:00')
        self.queue.action(self.user,normal,'claim',row['offer_id'])
        rows={r['id']:r for r in self.queue.view(self.user)['tasks']}
        self.assertEqual(rows[normal]['activated']+rows[normal]['requested_seconds'],start-600)
        self.assertEqual(rows[night]['estimated_start'],start)

    def test_existing_unfixed_nights_are_adopted_only_if_exact_slots_do_not_conflict(self):
        self.queue=UnifiedQueue(self.store,['a','b'],timezone='Asia/Shanghai')
        start=self.stamp('2026-11-05T02:00:00')
        first=self.add(night=True,not_before=start,requested_seconds=3600)
        second=self.add(night=True,not_before=start,requested_seconds=3600)
        self.queue=UnifiedQueue(self.store,['a','b'],timezone='Asia/Shanghai',strict_nights=True)
        rows={r['id']:r for r in self.queue.view(self.user)['tasks']}
        self.assertTrue(rows[first]['night_fixed']);self.assertFalse(rows[first]['night_unconfirmed'])
        self.assertFalse(rows[second]['night_fixed']);self.assertTrue(rows[second]['night_unconfirmed'])
        self.assertIsNone(rows[second]['estimated_start'])


if __name__=='__main__':unittest.main()
