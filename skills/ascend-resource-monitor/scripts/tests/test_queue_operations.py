from datetime import datetime
from pathlib import Path
import sys,tempfile,unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'assets/runtime'))
from queue_store import QueueStore
from queue_unified import UnifiedQueue
from control_common import ControlError

def stamp(text):return datetime.fromisoformat(text).replace(tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()

class QueueOperationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.now=stamp('2026-11-04T08:50:00');self.clock=patch('queue_unified.time.time',return_value=self.now).start();self.addCleanup(patch.stopall)
        self.store=QueueStore(Path(self.temp.name)/'queue.db')
        self.users=[self.store.user(self.store.register('u1234567'+str(i),'Person'+str(i),'fixture-password')) for i in range(3)]
        self.queue=UnifiedQueue(self.store,['a','b'],timezone='Asia/Shanghai',daily_reset=True)
    def add(self,user=None,seconds=60):return self.queue.enqueue(user or self.users[0],{'machine':'a','cards':8,'requested_seconds':seconds,'notifications_ready':True})
    def claim(self,job,user=None):
        user=user or self.users[0];row=next(r for r in self.queue.view(user)['tasks'] if r['id']==job)
        self.queue.action(user,job,'claim',row['offer_id'])
    def test_bottleneck_notifies_waiter_only_after_five_minutes(self):
        first=self.add();self.claim(first);self.add(self.users[1]);self.clock.return_value=self.now+61
        current=self.queue.view(self.users[0]);following=self.queue.view(self.users[1])
        self.assertTrue(current['tasks'][0]['overdue'])
        self.assertTrue(current['tasks'][0]['overdue_bottleneck'])
        self.assertEqual(current['alerts'][0]['kind'],'overdue')
        self.assertFalse(following['alerts'])
        self.clock.return_value=self.now+360
        self.assertFalse(self.queue.view(self.users[1])['alerts'])
        self.clock.return_value=self.now+361
        following=self.queue.view(self.users[1])
        self.assertEqual(following['alerts'][0]['kind'],'overdue-next')
        self.assertIn(self.users[0]['employee'],following['alerts'][0]['message'])
        self.assertIn('已超时 6 分钟',following['alerts'][0]['message'])
        self.queue.daily_reset=False
        self.clock.return_value=self.now+60+27*60+1
        following=self.queue.view(self.users[1])
        row=next(r for r in following['tasks'] if r['id']==first)
        self.assertEqual((row['overdue_seconds']+59)//60,28)
        self.assertIn('已超时 28 分钟',following['alerts'][0]['message'])
        self.assertFalse(self.queue.view(self.users[2])['alerts'])
    def test_active_bottleneck_is_detected_before_overdue_without_extra_alert(self):
        first=self.add(seconds=600);self.claim(first);self.add(self.users[1])
        current=self.queue.view(self.users[0]);row=next(r for r in current['tasks'] if r['id']==first)
        self.assertTrue(row['bottleneck']);self.assertFalse(row['overdue'])
        self.assertFalse(row['overdue_bottleneck']);self.assertFalse(current['alerts'])
        self.queue.action(self.users[1],next(r['id'] for r in current['tasks'] if r['state']=='queued'),'cancel')
        row=next(r for r in self.queue.view(self.users[0])['tasks'] if r['id']==first)
        self.assertFalse(row['bottleneck'])
    def test_latest_estimated_release_is_bottleneck_for_dual_waiter(self):
        first=self.queue.enqueue(self.users[0],{'machine':'a','cards':1,'requested_seconds':600,'notifications_ready':True})
        self.claim(first)
        latest=self.queue.enqueue(self.users[1],{'machine':'b','cards':8,'requested_seconds':1200,'notifications_ready':True})
        self.claim(latest,self.users[1])
        self.queue.enqueue(self.users[2],{'machine':'dual','cards':16,'requested_seconds':60,'notifications_ready':True})
        rows={r['id']:r for r in self.queue.view(self.users[1])['tasks']}
        self.assertFalse(rows[first]['bottleneck'])
        self.assertEqual(rows[first]['overtime_remaining_seconds'],600)
        self.assertTrue(rows[latest]['bottleneck'])
        self.assertFalse(rows[latest]['overdue'])
        self.assertIsNone(rows[latest]['overtime_remaining_seconds'])

    def test_equal_latest_releases_are_both_bottlenecks(self):
        first=self.add(seconds=600);self.claim(first)
        other=self.queue.enqueue(self.users[1],{'machine':'b','cards':8,'requested_seconds':600,'notifications_ready':True})
        self.claim(other,self.users[1])
        self.queue.enqueue(self.users[2],{'machine':'dual','cards':16,'requested_seconds':60,'notifications_ready':True})
        rows={r['id']:r for r in self.queue.view(self.users[0])['tasks']}
        self.assertTrue(rows[first]['bottleneck'])
        self.assertTrue(rows[other]['bottleneck'])

    def test_future_night_waiter_does_not_create_estimated_bottleneck(self):
        first=self.add(seconds=600);self.claim(first)
        self.queue.enqueue(self.users[1],{'machine':'a','cards':8,'requested_seconds':60,'notifications_ready':True,
                                         'night':True,'not_before':self.now+300})
        row=next(r for r in self.queue.view(self.users[0])['tasks'] if r['id']==first)
        self.assertFalse(row['bottleneck'])
        self.clock.return_value=self.now+300
        row=next(r for r in self.queue.view(self.users[0])['tasks'] if r['id']==first)
        self.assertTrue(row['bottleneck'])

    def test_multiple_overdue_tasks_jointly_block_dual_waiter_and_warn_owners(self):
        first=self.queue.enqueue(self.users[0],{'machine':'a','cards':1,'requested_seconds':60,'notifications_ready':True})
        self.claim(first)
        second=self.queue.enqueue(self.users[1],{'machine':'a','cards':7,'requested_seconds':120,'notifications_ready':True})
        self.claim(second,self.users[1])
        offered=self.queue.enqueue(self.users[2],{'machine':'b','cards':8,'requested_seconds':300,'notifications_ready':True})
        waiting=self.queue.enqueue(self.users[2],{'machine':'dual','cards':16,'requested_seconds':60,'notifications_ready':True})
        with self.store.db() as db:
            a=db.execute('SELECT queue_order FROM unified_tasks WHERE id=?',(offered,)).fetchone()[0]
            b=db.execute('SELECT queue_order FROM unified_tasks WHERE id=?',(waiting,)).fetchone()[0]
            db.execute('UPDATE unified_tasks SET queue_order=? WHERE id=?',(b,offered))
            db.execute('UPDATE unified_tasks SET queue_order=? WHERE id=?',(a,waiting))
        self.clock.return_value=self.now+421
        rows={r['id']:r for r in self.queue.view(self.users[0])['tasks']}
        self.assertTrue(rows[first]['overdue_joint_blocker'])
        self.assertTrue(rows[second]['overdue_joint_blocker'])
        self.assertEqual(rows[offered]['state'],'ready')
        self.assertEqual(rows[waiting]['state'],'queued')
        self.assertEqual(self.queue.view(self.users[0])['alerts'][0]['kind'],'overdue-joint')
        waiter_alerts=self.queue.view(self.users[2])['alerts']
        self.assertEqual(sum(a['kind']=='overdue-joint-next' for a in waiter_alerts),2)
        self.assertTrue(any('已超时 7 分钟' in a['message'] for a in waiter_alerts))
        self.assertTrue(any('已超时 6 分钟' in a['message'] for a in waiter_alerts))
        self.assertNotIn('可以立即开始',waiter_alerts[0]['message'])

    def test_only_nearest_waiter_gets_overdue_dependency_notice(self):
        owner=self.users[0]
        overdue=self.queue.enqueue(owner,{'machine':'a','cards':1,'requested_seconds':60,'notifications_ready':True})
        self.claim(overdue)
        offered=self.queue.enqueue(self.users[1],{'machine':'b','cards':8,'requested_seconds':600,'notifications_ready':True})
        users=[self.users[1],self.users[2]]
        for i in (3,4):
            users.append(self.store.user(self.store.register('u1234567'+str(i),'Person'+str(i),'fixture-password')))
        for user in users:
            self.queue.enqueue(user,{'machine':'dual','cards':16,'requested_seconds':60,'notifications_ready':True})
        self.clock.return_value=self.now+361
        first=self.queue.view(users[0])
        self.assertTrue(any(a['kind']=='overdue-dependent-next' for a in first['alerts']))
        message=next(a['message'] for a in first['alerts'] if a['kind']=='overdue-dependent-next')
        self.assertIn('已超时 6 分钟',message)
        self.assertIn('按队列顺序等待',message)
        self.assertNotIn('占用你的预约所需资源',message)
        for user in users[1:]:
            self.assertFalse(any(a['kind'] in ('overdue-dependent-next','overdue-joint-next','overdue-next')
                                 for a in self.queue.view(user)['alerts']))
        self.assertEqual(next(r for r in first['tasks'] if r['id']==offered)['state'],'ready')

    def test_simultaneous_independent_waiters_are_both_notified(self):
        owner=self.users[0]
        overdue=self.queue.enqueue(owner,{'machine':'dual','cards':16,'requested_seconds':60,'notifications_ready':True})
        self.claim(overdue)
        for user,node in ((self.users[1],'a'),(self.users[2],'b')):
            self.queue.enqueue(user,{'machine':node,'cards':8,'requested_seconds':60,'notifications_ready':True})
        self.clock.return_value=self.now+361
        for user in self.users[1:]:
            alerts=self.queue.view(user)['alerts']
            self.assertTrue(any(a['kind']=='overdue-next' for a in alerts))

    def test_single_overdue_dependency_is_visible_when_other_node_has_ready_offer(self):
        overdue=self.queue.enqueue(self.users[0],{'machine':'a','cards':1,'requested_seconds':60,'notifications_ready':True})
        self.claim(overdue)
        active=self.queue.enqueue(self.users[1],{'machine':'a','cards':1,'requested_seconds':600,'notifications_ready':True})
        self.claim(active,self.users[1])
        offered=self.queue.enqueue(self.users[2],{'machine':'b','cards':8,'requested_seconds':600,'notifications_ready':True})
        waiter=self.queue.enqueue(self.users[1],{'machine':'dual','cards':16,'requested_seconds':60,'notifications_ready':True})
        self.clock.return_value=self.now+61
        row=next(r for r in self.queue.view(self.users[0])['tasks'] if r['id']==overdue)
        self.assertTrue(row['overdue_pending_blocker'])
        self.assertFalse(row['overdue_joint_blocker'])
        self.assertFalse(row['overdue_bottleneck'])
        self.assertIsNone(row['overtime_remaining_seconds'])
        self.assertEqual(self.queue.view(self.users[0])['alerts'][0]['kind'],'overdue-dependent')
        rows={r['id']:r for r in self.queue.view(self.users[1])['tasks']}
        self.assertEqual(rows[offered]['state'],'ready')
        self.assertEqual(rows[waiter]['state'],'queued')

    def test_unrelated_overdue_tasks_are_not_called_joint_blockers(self):
        first=self.queue.enqueue(self.users[0],{'machine':'a','cards':1,'requested_seconds':60,'notifications_ready':True})
        self.claim(first)
        unrelated=self.queue.enqueue(self.users[1],{'machine':'b','cards':1,'requested_seconds':60,'notifications_ready':True})
        self.claim(unrelated,self.users[1])
        other=self.queue.enqueue(self.users[2],{'machine':'a','cards':7,'requested_seconds':600,'notifications_ready':True})
        self.claim(other,self.users[2])
        self.queue.enqueue(self.users[2],{'machine':'a','cards':8,'requested_seconds':60,'notifications_ready':True})
        self.clock.return_value=self.now+61
        rows={r['id']:r for r in self.queue.view(self.users[0])['tasks']}
        self.assertFalse(rows[first]['overdue_joint_blocker'])
        self.assertFalse(rows[unrelated]['overdue_joint_blocker'])

    def test_unknown_due_full_machine_waiter_blocks_later_backfill(self):
        first=self.queue.enqueue(self.users[0],{'machine':'a','cards':1,'requested_seconds':60,'notifications_ready':True})
        self.claim(first)
        self.clock.return_value=self.now+61
        waiter=self.queue.enqueue(self.users[1],{'machine':'dual','cards':16,'requested_seconds':600,'notifications_ready':True})
        later=self.queue.enqueue(self.users[2],{'machine':'b','cards':8,'requested_seconds':600,'notifications_ready':True})
        rows={r['id']:r for r in self.queue.view(self.users[1])['tasks']}
        self.assertEqual(rows[waiter]['state'],'queued')
        self.assertIsNone(rows[waiter]['estimated_start'])
        self.assertEqual(rows[later]['state'],'queued')
        self.queue.action(self.users[0],first,'finish')
        rows={r['id']:r for r in self.queue.view(self.users[1])['tasks']}
        self.assertEqual(rows[waiter]['state'],'ready')
        self.assertEqual(rows[later]['state'],'queued')

    def test_nonblocking_can_become_bottleneck_when_other_cards_release(self):
        def add_cards(user,cards,seconds):
            return self.queue.enqueue(user,{'machine':'a','cards':cards,'requested_seconds':seconds,'notifications_ready':True})
        first=add_cards(self.users[0],1,60);self.claim(first)
        other=add_cards(self.users[1],7,120);self.claim(other,self.users[1])
        waiting=add_cards(self.users[2],8,60)
        self.clock.return_value=self.now+61
        view=self.queue.view(self.users[0]);row=next(r for r in view['tasks'] if r['id']==first)
        self.assertFalse(row['overdue_bottleneck'])
        self.assertTrue(row['overdue_pending_blocker'])
        self.assertEqual(view['alerts'][0]['kind'],'overdue-dependent')
        self.assertEqual(view['alerts'][0]['repeat_seconds'],300)
        self.assertIn('其他资源',view['alerts'][0]['message'])
        self.queue.action(self.users[1],other,'finish')
        view=self.queue.view(self.users[0]);row=next(r for r in view['tasks'] if r['id']==first)
        self.assertTrue(row['overdue_bottleneck'])
        self.assertEqual(view['alerts'][0]['kind'],'overdue')
        self.assertEqual(view['alerts'][0]['repeat_seconds'],300)
        self.assertEqual(next(r for r in view['tasks'] if r['id']==waiting)['state'],'queued')
    def test_dual_waiter_requires_other_node_to_be_free(self):
        first=self.add();self.claim(first)
        other=self.queue.enqueue(self.users[1],{'machine':'b','cards':8,'requested_seconds':120,'notifications_ready':True})
        self.claim(other,self.users[1])
        self.queue.enqueue(self.users[2],{'machine':'dual','cards':16,'requested_seconds':60,'notifications_ready':True})
        self.clock.return_value=self.now+61
        self.assertFalse(next(r for r in self.queue.view(self.users[0])['tasks'] if r['id']==first)['overdue_bottleneck'])
        self.queue.action(self.users[1],other,'finish')
        self.assertTrue(next(r for r in self.queue.view(self.users[0])['tasks'] if r['id']==first)['overdue_bottleneck'])
    def test_remaining_overtime_uses_competing_task_end_without_ten_minute_buffer(self):
        first=self.queue.enqueue(self.users[0],{'machine':'a','cards':1,'requested_seconds':60,'notifications_ready':True})
        self.claim(first)
        other=self.queue.enqueue(self.users[1],{'machine':'a','cards':7,'requested_seconds':120,'notifications_ready':True})
        self.claim(other,self.users[1])
        self.queue.enqueue(self.users[2],{'machine':'a','cards':8,'requested_seconds':60,'notifications_ready':True})
        row=next(r for r in self.queue.view(self.users[0])['tasks'] if r['id']==first)
        self.assertFalse(row['bottleneck']);self.assertEqual(row['overtime_remaining_seconds'],60)
        self.clock.return_value=self.now+61
        row=next(r for r in self.queue.view(self.users[0])['tasks'] if r['id']==first)
        self.assertTrue(row['overdue']);self.assertFalse(row['bottleneck'])
        self.assertTrue(row['overdue_pending_blocker'])
        self.assertIsNone(row['overtime_remaining_seconds'])
        self.queue.action(self.users[1],other,'finish')
        row=next(r for r in self.queue.view(self.users[0])['tasks'] if r['id']==first)
        self.assertTrue(row['overdue_bottleneck']);self.assertIsNone(row['overtime_remaining_seconds'])
    def test_remaining_overtime_is_unknown_for_unrelated_resources_or_overdue_reference(self):
        first=self.queue.enqueue(self.users[0],{'machine':'a','cards':1,'requested_seconds':60,'notifications_ready':True})
        self.claim(first)
        other=self.queue.enqueue(self.users[1],{'machine':'b','cards':8,'requested_seconds':120,'notifications_ready':True})
        self.claim(other,self.users[1])
        self.queue.enqueue(self.users[2],{'machine':'b','cards':8,'requested_seconds':60,'notifications_ready':True})
        row=next(r for r in self.queue.view(self.users[0])['tasks'] if r['id']==first)
        self.assertFalse(row['bottleneck']);self.assertIsNone(row['overtime_remaining_seconds'])
        self.clock.return_value=self.now+121
        row=next(r for r in self.queue.view(self.users[0])['tasks'] if r['id']==first)
        self.assertIsNone(row['overtime_remaining_seconds'])
    def test_remaining_overtime_for_dual_waiter_uses_other_node_end(self):
        first=self.add(seconds=60);self.claim(first)
        other=self.queue.enqueue(self.users[1],{'machine':'b','cards':8,'requested_seconds':120,'notifications_ready':True})
        self.claim(other,self.users[1])
        self.queue.enqueue(self.users[2],{'machine':'dual','cards':16,'requested_seconds':60,'notifications_ready':True})
        row=next(r for r in self.queue.view(self.users[0])['tasks'] if r['id']==first)
        self.assertFalse(row['bottleneck']);self.assertEqual(row['overtime_remaining_seconds'],60)
    def test_remaining_overtime_ignores_unrelated_earlier_bottleneck(self):
        first=self.queue.enqueue(self.users[0],{'machine':'a','cards':1,'requested_seconds':60,'notifications_ready':True})
        self.claim(first)
        related=self.queue.enqueue(self.users[1],{'machine':'a','cards':7,'requested_seconds':180,'notifications_ready':True})
        self.claim(related,self.users[1])
        unrelated=self.queue.enqueue(self.users[2],{'machine':'b','cards':8,'requested_seconds':120,'notifications_ready':True})
        self.claim(unrelated,self.users[2])
        self.queue.enqueue(self.users[0],{'machine':'a','cards':8,'requested_seconds':60,'notifications_ready':True})
        self.queue.enqueue(self.users[1],{'machine':'b','cards':8,'requested_seconds':60,'notifications_ready':True})
        rows={r['id']:r for r in self.queue.view(self.users[0])['tasks']}
        self.assertTrue(rows[unrelated]['bottleneck'])
        self.assertFalse(rows[first]['bottleneck'])
        self.assertEqual(rows[first]['overtime_remaining_seconds'],120)
    def test_future_waiter_does_not_make_overdue_task_a_bottleneck_yet(self):
        first=self.add();self.claim(first)
        self.queue.enqueue(self.users[1],{'machine':'a','cards':8,'requested_seconds':60,'notifications_ready':True,
                                         'night':True,'not_before':self.now+300})
        self.clock.return_value=self.now+61
        self.assertFalse(next(r for r in self.queue.view(self.users[0])['tasks'] if r['id']==first)['overdue_bottleneck'])
        self.clock.return_value=self.now+300
        self.assertTrue(next(r for r in self.queue.view(self.users[0])['tasks'] if r['id']==first)['overdue_bottleneck'])
    def test_only_admin_reorders_waiting_and_preserves_original_times(self):
        first=self.add();self.claim(first)
        a=self.add(self.users[1]);b=self.add(self.users[2])
        before={r['id']:r['created'] for r in self.queue.view(None)['tasks']}
        with self.assertRaises(ControlError):self.queue.action(self.users[2],b,'priority-up')
        self.queue.action({**self.users[0],'admin':True},b,'priority-up')
        rows=self.queue.view(None)['tasks'];self.assertEqual([r['id'] for r in rows],[first,b,a])
        self.assertEqual({r['id']:r['created'] for r in rows},before)
        self.queue.action({**self.users[0],'admin':True},b,'priority-down')
        self.assertEqual([r['id'] for r in self.queue.view(None)['tasks']],[first,a,b])
    def test_user_can_lower_only_own_waiting_reservation(self):
        first=self.add();self.claim(first)
        a=self.add(self.users[1]);b=self.add(self.users[2]);c=self.add(self.users[1])
        rows={r['id']:r for r in self.queue.view(self.users[1])['tasks']}
        self.assertTrue(rows[a]['can_lower_priority'])
        self.assertFalse(rows[b]['can_lower_priority'])
        self.assertFalse(rows[c]['can_lower_priority'])
        with self.assertRaises(ControlError):self.queue.action(self.users[1],b,'priority-down')
        with self.assertRaises(ControlError):self.queue.action(self.users[0],first,'priority-down')
        with self.assertRaises(ControlError):self.queue.action({**self.users[0],'admin':True},first,'priority-down')
        self.queue.action(self.users[1],a,'priority-down')
        self.assertEqual([r['id'] for r in self.queue.view(None)['tasks']],[first,b,a,c])
    def test_allowed_users_are_owner_managed_persistent_and_removed_with_reservation(self):
        job=self.add()
        other=self.users[1]['employee']
        for actor in (self.users[1],{**self.users[1],'admin':True}):
            with self.assertRaises(ControlError):self.queue.allowed_user(actor,job,other,'add')
        with self.assertRaises(ControlError):self.queue.allowed_user(self.users[0],job,'bad-id','add')
        with self.assertRaises(ControlError):self.queue.allowed_user(self.users[0],job,self.users[0]['employee'],'add')
        self.queue.allowed_user(self.users[0],job,other.upper(),'add')
        self.queue.allowed_user(self.users[0],job,other,'add')
        self.assertEqual(self.queue.view(self.users[0])['tasks'][0]['allowed_employees'],[other])
        reopened=UnifiedQueue(self.store,['a','b'],timezone='Asia/Shanghai',daily_reset=True)
        self.assertEqual(reopened.view(self.users[0])['tasks'][0]['allowed_employees'],[other])
        self.queue.allowed_user(self.users[0],job,other,'remove')
        self.assertEqual(self.queue.view(self.users[0])['tasks'][0]['allowed_employees'],[])
        self.queue.allowed_user(self.users[0],job,other,'add')
        self.queue.action(self.users[0],job,'cancel')
        self.assertFalse(self.queue.view(self.users[0])['tasks'])
        with self.assertRaises(ControlError):self.queue.allowed_user(self.users[0],job,other,'remove')
    def test_ready_owner_defers_one_place_and_invalidates_offer(self):
        a=self.add(self.users[0]);b=self.add(self.users[1]);c=self.add(self.users[2])
        before=self.queue.view(self.users[0])['tasks'];offer=before[0]['offer_id']
        self.assertTrue(before[0]['can_lower_priority'])
        with self.assertRaises(ControlError):self.queue.action(self.users[1],a,'priority-down',offer)
        with self.assertRaises(ControlError):self.queue.action(self.users[0],a,'priority-down','stale')
        with self.assertRaises(ControlError):self.queue.action(self.users[0],a,'priority-down')
        self.queue.action(self.users[0],a,'priority-down',offer)
        rows=self.queue.view(self.users[0])['tasks']
        self.assertEqual([r['id'] for r in rows],[b,a,c])
        self.assertEqual([r['state'] for r in rows],['ready','queued','queued'])
        self.assertEqual(rows[1]['created'],before[0]['created'])
        self.assertEqual(rows[1]['allocations'],{})
        self.assertEqual(rows[1]['missed_claims'],0)
        with self.assertRaises(ControlError):self.queue.action(self.users[0],a,'claim',offer)
        self.claim(b,self.users[1]);self.queue.action(self.users[1],b,'finish')
        new=next(r for r in self.queue.view(self.users[0])['tasks'] if r['id']==a)
        self.assertEqual(new['state'],'ready')
        self.assertNotEqual(new['offer_id'],offer)
        with self.assertRaises(ControlError):self.queue.action(self.users[0],a,'claim',offer)
    def test_ready_tail_cannot_defer_and_other_ready_offer_is_not_preempted(self):
        a=self.add(self.users[0])
        self.assertFalse(self.queue.view(self.users[0])['tasks'][0]['can_lower_priority'])
        offer=self.queue.view(self.users[0])['tasks'][0]['offer_id']
        with self.assertRaises(ControlError):self.queue.action(self.users[0],a,'priority-down',offer)
        b=self.queue.enqueue(self.users[1],{'machine':'b','cards':8,'requested_seconds':60,'notifications_ready':True})
        before=self.queue.view(self.users[0])['tasks'];other=before[1]['offer_id']
        self.queue.action(self.users[0],a,'priority-down',offer)
        rows=self.queue.view(self.users[0])['tasks']
        self.assertEqual([r['id'] for r in rows],[b,a])
        self.assertEqual(rows[0]['offer_id'],other)
        self.assertEqual(rows[0]['state'],'ready')
    def test_top_moves_directly_to_first_waiting_preserving_other_order_and_active(self):
        first=self.add();self.claim(first)
        a=self.add(self.users[1]);b=self.add(self.users[2]);c=self.add(self.users[1]);d=self.add(self.users[2])
        before={r['id']:(r['created'],r['activated'],r['allocations']) for r in self.queue.view(None)['tasks']}
        admin={**self.users[0],'admin':True}
        with self.assertRaises(ControlError):self.queue.action(self.users[2],d,'priority-top')
        with self.assertRaises(ControlError):self.queue.action(admin,first,'priority-top')
        self.queue.action(admin,d,'priority-top')
        rows=self.queue.view(admin)['tasks']
        self.assertEqual([r['id'] for r in rows],[first,d,a,b,c])
        self.assertEqual({r['id']:(r['created'],r['activated'],r['allocations']) for r in rows},before)
        self.assertFalse(rows[1]['can_raise_priority'])
        with self.assertRaises(ControlError):self.queue.action(admin,d,'priority-top')
        reopened=UnifiedQueue(self.store,['a','b'],timezone='Asia/Shanghai',daily_reset=True)
        self.assertEqual([r['id'] for r in reopened.view(None)['tasks']],[first,d,a,b,c])
    def test_nine_am_clears_active_and_waiting_once_and_preserves_accounts(self):
        active=self.add();self.claim(active);self.queue.allowed_user(self.users[0],active,self.users[2]['employee'],'add');self.add(self.users[1]);self.clock.return_value=stamp('2026-11-04T08:59:59')
        self.assertEqual(len(self.queue.view(None)['tasks']),2)
        self.clock.return_value=stamp('2026-11-04T09:00:00')
        self.assertEqual(self.queue.view(None)['tasks'],[])
        continuing=self.queue.continuing()
        self.assertEqual(continuing[0]['employee'],self.users[0]['employee'])
        self.assertEqual(continuing[1]['employee'],self.users[2]['employee'])
        self.assertEqual(continuing[1]['cards'],list(range(8)))
        new=self.add(self.users[1]);self.assertEqual(self.queue.view(None)['tasks'][0]['id'],new)
        reopened=UnifiedQueue(self.store,['a','b'],timezone='Asia/Shanghai',daily_reset=True)
        self.assertEqual(reopened.view(None)['tasks'][0]['id'],new)
        self.assertIsNotNone(self.store.login(self.users[0]['employee'],'fixture-password'))
        self.clock.return_value=stamp('2026-11-05T09:00:00');self.assertEqual(reopened.view(None)['tasks'],[])

if __name__=='__main__':unittest.main()
