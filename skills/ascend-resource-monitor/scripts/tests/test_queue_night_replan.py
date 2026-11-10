from pathlib import Path
import sys
import unittest
import tempfile
import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'assets/runtime'))
from queue_night_replan import reschedule
from queue_store import QueueStore
from queue_unified import UnifiedQueue
from control_common import ControlError
from queue_night_coordinator import NightCoordinator


class NightReplanTests(unittest.TestCase):
    def test_normal_work_pushes_conflicting_nights_and_cascades(self):
        nights=[{'id':'first','requested':1000,'scheduled':1000,'seconds':1000,'order':1,'allocations':{'a':[0]}},
                {'id':'second','requested':2000,'scheduled':2000,'seconds':1000,'order':2,'allocations':{'a':[0]}},
                {'id':'other','requested':1000,'scheduled':1000,'seconds':500,'order':3,'allocations':{'b':[0]}}]
        blockers=[{'start':800,'end':1200,'allocations':{'a':[0]}}]
        self.assertEqual(reschedule(nights,blockers),{'first':1200,'second':2200,'other':1000})

    def test_delay_never_moves_earlier_and_unknown_release_blocks_followers(self):
        nights=[{'id':'first','requested':1000,'scheduled':1200,'seconds':1000,'order':1,'allocations':{'a':[0]}},
                {'id':'second','requested':2000,'scheduled':2200,'seconds':1000,'order':2,'allocations':{'a':[0]}}]
        self.assertEqual(reschedule(nights,[]),{'first':1200,'second':2200})
        self.assertEqual(reschedule(nights,[{'start':1100,'end':None,'allocations':{'a':[0]}}]),{'first':None,'second':None})

    def test_hard_deadline_caps_occupancy_and_blocks_late_start(self):
        nights=[{'id':'first','requested':1000,'scheduled':1000,'seconds':2000,'latest_end':2000,'order':1,'allocations':{'a':[0]}},
                {'id':'second','requested':2000,'scheduled':2000,'seconds':500,'order':2,'allocations':{'a':[0]}}]
        self.assertEqual(reschedule(nights,[]),{'first':1000,'second':2000})
        blocker=[{'start':900,'end':2000,'allocations':{'a':[0]}}]
        self.assertEqual(reschedule(nights,blocker),{'first':None,'second':2000})


class NormalPriorityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=QueueStore(Path(self.temp.name)/'queue.db')
        self.user=self.store.user(self.store.register('u12345678','Owner','fixture-password'))
        self.clock=patch('queue_unified.time.time').start();self.addCleanup(patch.stopall)
        self.stamp=lambda value:datetime.fromisoformat(value).replace(tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        self.clock.return_value=self.stamp('2026-11-07T22:00:00')
        self.queue=UnifiedQueue(self.store,['a','b'],timezone='Asia/Shanghai',strict_nights=True,
                                overtime_control=True,normal_priority=True)

    def add(self,**values):
        return self.queue.enqueue(self.user,{'machine':'a','cards':8,'requested_seconds':3600,
                                             'avoid_slots':[],'notifications_ready':True,**values})

    def test_short_backfill_requires_opt_in_and_preserves_first_slot(self):
        self.clock.return_value=self.stamp('2026-11-07T11:10:00')
        first=self.add(machine='dual',cards=16,avoid_slots=[22,23])
        later=self.add(backfill_min_seconds=0)
        rows={r['id']:r for r in self.queue.view(self.user)['tasks']}
        self.assertEqual(rows[first]['estimated_start'],self.stamp('2026-11-07T12:00:00'))
        self.assertEqual(rows[later]['state'],'queued')
        self.queue.update_backfill(self.user,later,1200)
        rows={r['id']:r for r in self.queue.view(self.user)['tasks']}
        self.assertEqual(rows[later]['state'],'ready')
        self.assertTrue(rows[later]['backfill_planned'])
        self.assertEqual(rows[later]['backfill_end_before'],self.stamp('2026-11-07T11:50:00'))
        self.assertEqual(rows[later]['effective_requested_seconds'],2400)
        self.assertEqual(rows[first]['estimated_start'],self.stamp('2026-11-07T12:00:00'))
        self.clock.return_value+=300
        offer=next(r for r in self.queue.view(self.user)['tasks'] if r['id']==later)
        self.assertEqual(offer['effective_requested_seconds'],2100)
        self.queue.action(self.user,later,'claim',offer['offer_id'])
        row=next(r for r in self.queue.view(self.user)['tasks'] if r['id']==later)
        self.assertEqual(row['requested_seconds'],2100)
        self.clock.return_value=self.stamp('2026-11-07T11:50:00')
        with self.assertRaises(ControlError):self.queue.action(self.user,later,'extend',extension_minutes=10)
        self.clock.return_value=self.stamp('2026-11-07T12:00:00')
        self.queue.tick()
        rows={r['id']:r for r in self.queue.view(self.user)['tasks']}
        self.assertNotIn(later,rows)
        self.assertEqual(rows[first]['state'],'ready')

    def test_minimum_duration_and_opt_out(self):
        self.clock.return_value=self.stamp('2026-11-07T11:10:00')
        self.add(machine='dual',cards=16,avoid_slots=[22,23])
        later=self.add(backfill_min_seconds=2700)
        self.assertEqual(next(r for r in self.queue.view(self.user)['tasks'] if r['id']==later)['state'],'queued')
        self.queue.update_backfill(self.user,later,1200)
        self.assertEqual(next(r for r in self.queue.view(self.user)['tasks'] if r['id']==later)['state'],'ready')
        self.queue.update_backfill(self.user,later,0)
        self.assertEqual(next(r for r in self.queue.view(self.user)['tasks'] if r['id']==later)['state'],'queued')
        with self.assertRaises(ControlError):self.queue.update_backfill(self.user,later,3900)

    def test_multiple_backfills_preserve_the_first_reservation(self):
        self.clock.return_value=self.stamp('2026-11-07T11:10:00')
        first=self.add(machine='dual',cards=16,avoid_slots=[22,23])
        a=self.add(machine='a',backfill_min_seconds=1200)
        b=self.add(machine='b',backfill_min_seconds=1200)
        rows={r['id']:r for r in self.queue.view(self.user)['tasks']}
        self.assertEqual(rows[first]['estimated_start'],self.stamp('2026-11-07T12:00:00'))
        for job in (a,b):
            self.assertEqual(rows[job]['state'],'ready')
            self.assertEqual(rows[job]['effective_requested_seconds'],2400)

    def test_late_confirmation_never_accepts_less_than_minimum(self):
        self.clock.return_value=self.stamp('2026-11-07T11:10:00')
        self.add(machine='dual',cards=16,avoid_slots=[22,23])
        later=self.add(backfill_min_seconds=1800)
        offer=next(r for r in self.queue.view(self.user)['tasks'] if r['id']==later)
        self.clock.return_value=self.stamp('2026-11-07T11:20:02')
        with self.assertRaises(ControlError):self.queue.action(self.user,later,'claim',offer['offer_id'])
        self.assertEqual(next(r for r in self.queue.view(self.user)['tasks'] if r['id']==later)['state'],'queued')

    def test_no_truncation_when_full_duration_fits(self):
        self.clock.return_value=self.stamp('2026-11-07T10:40:00')
        self.add(machine='dual',cards=16,avoid_slots=[21,22,23])
        later=self.add(backfill_min_seconds=1200)
        row=next(r for r in self.queue.view(self.user)['tasks'] if r['id']==later)
        self.assertEqual(row['state'],'ready')
        self.assertFalse(row['backfill_planned'])
        self.assertEqual(row['effective_requested_seconds'],3600)
        self.queue.update_backfill(self.user,later,1800)
        updated=next(r for r in self.queue.view(self.user)['tasks'] if r['id']==later)
        self.assertEqual(updated['state'],'ready')
        self.assertEqual(updated['offer_id'],row['offer_id'])
        other=self.store.user(self.store.register('u23456789','Other','fixture-password'))
        with self.assertRaises(ControlError):self.queue.update_backfill(other,later,0)

    def with_carryover_cutoff(self):
        self.queue=UnifiedQueue(self.store,['a','b'],timezone='Asia/Shanghai',strict_nights=True,
                                overtime_control=True,normal_priority=True,night_carryover_cutoff_hour=10)

    def test_night_is_shortened_only_if_it_crosses_ten(self):
        self.with_carryover_cutoff()
        start=self.stamp('2026-11-07T23:00:00')
        long=self.add(night=True,not_before=start,requested_seconds=13*3600)
        other=self.add(machine='b',night=True,not_before=start,requested_seconds=10*3600+1800)
        rows={r['id']:r for r in self.queue.view(self.user)['tasks']}
        self.assertEqual(rows[long]['requested_seconds'],11*3600)
        self.assertEqual(rows[other]['requested_seconds'],10*3600+1800)

    def test_existing_running_night_releases_at_ten_and_preserves_containers(self):
        start=self.stamp('2026-11-08T01:00:00')
        job=self.add(night=True,not_before=start,requested_seconds=10*3600)
        self.clock.return_value=start
        self.queue.lock_due_night_takeovers(start)
        snapshots=self.queue.due_night_tasks(start)
        self.queue.finish_night_takeovers(snapshots,set(),{},start)
        self.with_carryover_cutoff()
        self.clock.return_value=self.stamp('2026-11-08T09:30:00')
        row=next(r for r in self.queue.view(self.user)['tasks'] if r['id']==job)
        self.assertEqual(row['activated']+row['requested_seconds'],self.stamp('2026-11-08T10:00:00'))
        self.clock.return_value=self.stamp('2026-11-08T10:00:00')
        monitor=SimpleNamespace(current=lambda now:[],fresh_nodes=lambda now:{'a','b'},
                                inventory_times={'a':self.clock.return_value,'b':self.clock.return_value},cards=lambda row:set())
        async def no_stop(*args):raise AssertionError('cutoff must not stop containers')
        asyncio.run(NightCoordinator(self.queue,monitor,no_stop,lambda event:None).poll())
        view=self.queue.view(self.user)
        self.assertFalse(any(r['id']==job for r in view['tasks']))
        self.assertTrue(any('上午 10 点' in a['message'] and '不会停止容器' in a['message'] for a in view['alerts']))
        self.assertFalse(self.queue.due_night_tasks(self.clock.return_value))

    def test_unstarted_night_never_takes_over_at_cutoff(self):
        self.with_carryover_cutoff()
        start=self.stamp('2026-11-08T08:55:00')
        job=self.add(night=True,not_before=start,requested_seconds=3600)
        self.clock.return_value=self.stamp('2026-11-08T10:00:00')
        self.queue.lock_due_night_takeovers(self.clock.return_value)
        self.assertFalse(self.queue.due_night_tasks(self.clock.return_value))
        self.assertFalse(any(r['id']==job for r in self.queue.view(self.user)['tasks']))

    def test_waiting_normal_has_priority_and_nights_cascade(self):
        first=self.add(night=True,not_before=self.stamp('2026-11-07T23:55:00'))
        second=self.add(night=True,not_before=self.stamp('2026-11-08T00:55:00'))
        normal=self.add(cards=1,requested_seconds=7200,avoid_slots=[44,45,46])
        rows={row['id']:row for row in self.queue.view(self.user)['tasks']}
        self.assertEqual(rows[normal]['estimated_start'],self.stamp('2026-11-07T23:30:00'))
        self.assertEqual(rows[normal]['effective_requested_seconds'],5400)
        self.assertEqual(rows[first]['estimated_start'],self.stamp('2026-11-08T01:00:00'))
        self.assertEqual(rows[second]['estimated_start'],self.stamp('2026-11-08T02:00:00'))
        self.assertEqual(rows[first]['night_requested_start'],self.stamp('2026-11-07T23:55:00'))
        self.queue.action(self.user,normal,'cancel')
        rows={row['id']:row for row in self.queue.view(self.user)['tasks']}
        self.assertEqual(rows[first]['estimated_start'],self.stamp('2026-11-08T01:00:00'))

    def test_no_normal_start_at_midnight_and_release_at_one(self):
        self.clock.return_value=self.stamp('2026-11-07T23:50:00')
        job=self.add(requested_seconds=7200)
        row=next(row for row in self.queue.view(self.user)['tasks'] if row['id']==job)
        self.queue.action(self.user,job,'claim',row['offer_id'])
        row=next(row for row in self.queue.view(self.user)['tasks'] if row['id']==job)
        self.assertEqual(row['activated']+row['requested_seconds'],self.stamp('2026-11-08T01:00:00'))
        self.clock.return_value=self.stamp('2026-11-08T01:00:00')
        self.assertFalse(any(row['id']==job for row in self.queue.view(self.user)['tasks']))
        self.assertTrue(any('凌晨 1 点' in alert['message'] for alert in self.queue.view(self.user)['alerts']))
        later=self.add();row=next(row for row in self.queue.view(self.user)['tasks'] if row['id']==later)
        self.assertEqual(row['state'],'queued')

    def test_ordinary_duration_is_not_shortened_by_unstarted_night(self):
        self.clock.return_value=self.stamp('2026-11-07T23:00:00')
        night=self.add(night=True,not_before=self.stamp('2026-11-07T23:30:00'))
        job=self.add(requested_seconds=7200)
        rows={row['id']:row for row in self.queue.view(self.user)['tasks']}
        self.assertEqual(rows[job]['state'],'ready')
        self.assertEqual(rows[job]['effective_requested_seconds'],7200)
        self.assertEqual(rows[night]['estimated_start'],self.stamp('2026-11-08T01:00:00'))
        self.queue.action(self.user,job,'claim',rows[job]['offer_id'])
        row=next(row for row in self.queue.view(self.user)['tasks'] if row['id']==job)
        self.assertEqual(row['requested_seconds'],7200)

    def test_disjoint_night_is_not_delayed_by_normal_work(self):
        night=self.add(machine='b',night=True,not_before=self.stamp('2026-11-07T23:00:00'))
        self.add(machine='a',requested_seconds=7200)
        row=next(row for row in self.queue.view(self.user)['tasks'] if row['id']==night)
        self.assertEqual(row['estimated_start'],self.stamp('2026-11-07T23:00:00'))

    def test_shift_alert_includes_shared_night_users(self):
        shared=self.store.user(self.store.register('u23456789','Shared','fixture-password'))
        night=self.add(night=True,not_before=self.stamp('2026-11-07T23:55:00'))
        self.queue.allowed_user(self.user,night,'u23456789','add')
        self.add(cards=1,requested_seconds=7200,avoid_slots=[44,45,46])
        for person in (self.user,shared):
            alerts=self.queue.view(person)['alerts']
            self.assertTrue(any(a['kind']=='night-rescheduled' and '01:00' in a['message'] for a in alerts))

    def test_takeover_locks_night_against_later_normal_work(self):
        self.clock.return_value=self.stamp('2026-11-07T20:00:00')
        start=self.stamp('2026-11-07T20:05:00')
        night=self.add(night=True,not_before=start)
        self.clock.return_value=start
        self.assertFalse(self.queue.due_night_tasks(start))
        self.queue.lock_due_night_takeovers(start)
        self.assertEqual([row['id'] for row in self.queue.due_night_tasks(start)],[night])
        with self.assertRaises(ControlError):self.queue.action(self.user,night,'cancel')
        normal=self.add()
        rows={row['id']:row for row in self.queue.view(self.user)['tasks']}
        self.assertEqual(rows[night]['not_before'],start)
        self.assertTrue(rows[night]['night_takeover'])
        self.assertNotEqual(rows[normal]['state'],'ready')

    def test_started_night_is_not_interrupted_by_later_normal(self):
        self.clock.return_value=self.stamp('2026-11-07T20:00:00')
        start=self.stamp('2026-11-07T20:05:00')
        night=self.add(night=True,not_before=start)
        self.clock.return_value=start
        monitor=SimpleNamespace(current=lambda now:[],fresh_nodes=lambda now:{'a','b'},
                                inventory_times={'a':start,'b':start},cards=lambda row:set())
        async def no_stop(*args):raise AssertionError('no container should be stopped')
        asyncio.run(NightCoordinator(self.queue,monitor,no_stop,lambda event:None).poll())
        self.assertEqual(next(r for r in self.queue.view(self.user)['tasks'] if r['id']==night)['state'],'active')
        normal=self.add()
        rows={row['id']:row for row in self.queue.view(self.user)['tasks']}
        self.assertEqual(rows[night]['state'],'active')
        self.assertNotEqual(rows[normal]['state'],'ready')


if __name__=='__main__':unittest.main()
