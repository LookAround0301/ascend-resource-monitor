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
