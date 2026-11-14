import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'assets'/'runtime'))
from queue_store import QueueStore
from queue_unified import UnifiedQueue
from control_common import ControlError


class MidnightPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=QueueStore(Path(self.tmp.name)/'queue.db')
        self.user=self.store.user(self.store.register('u12345678','Test','fixture-password'))
        self.q=UnifiedQueue(self.store,['a','b'],timezone='Asia/Shanghai',daily_reset=True,
             strict_nights=True,overtime_control=True,normal_priority=True,normal_max_seconds=3600,
             night_carryover_cutoff_hour=9,midnight_separation=True)
        self.clock=patch('queue_unified.time.time').start();self.addCleanup(patch.stopall)
        self.clock.return_value=self.ts('2026-11-14T23:30:00')

    def ts(self,s):return datetime.fromisoformat(s).replace(tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
    def add(self,**kw):
        values=dict(machine='a',cards=8,requested_seconds=3600,notifications_ready=True,avoid_slots=[])
        values.update(kw)
        return self.q.enqueue(self.user,values)
    def row(self,job):return next(r for r in self.q.view(self.user)['tasks'] if r['id']==job)

    def test_normal_is_clipped_and_released_at_midnight(self):
        job=self.add();off=self.row(job);self.assertEqual(off['state'],'ready')
        self.q.action(self.user,job,'claim',off['offer_id']);r=self.row(job)
        self.assertEqual(r['activated']+r['requested_seconds'],self.ts('2026-11-15T00:00:00'))
        self.clock.return_value=self.ts('2026-11-15T00:00:00');self.q.tick()
        self.assertFalse(any(r['id']==job for r in self.q.view(self.user)['tasks']))

    def test_overnight_normal_waits_and_survives_nine_reset(self):
        self.clock.return_value=self.ts('2026-11-15T00:30:00')
        job=self.add();r=self.row(job);self.assertEqual(r['state'],'queued')
        self.assertGreaterEqual(r['estimated_start'],self.ts('2026-11-15T09:00:00'))
        self.clock.return_value=self.ts('2026-11-15T08:59:00');self.q.tick();self.assertEqual(self.row(job)['state'],'queued')
        self.clock.return_value=self.ts('2026-11-15T09:00:00');self.q.tick();self.assertEqual(self.row(job)['state'],'ready')

    def test_night_midnight_to_0900_and_conflict(self):
        start=self.ts('2026-11-15T00:00:00')
        night=self.add(night=True,not_before=start)
        r=self.row(night);self.assertEqual(r['requested_seconds'],int(self.ts('2026-11-15T09:00:00')-start))
        normal=self.add();r=self.row(normal);self.q.action(self.user,normal,'claim',r['offer_id'])
        self.assertEqual(self.row(normal)['activated']+self.row(normal)['requested_seconds'],start)
        with self.assertRaises(ControlError):self.add(night=True,not_before=start)

    def test_night_before_midnight_is_rejected_and_late_start_ends_at_0900(self):
        with self.assertRaises(ControlError):self.add(night=True,not_before=self.ts('2026-11-14T23:55:00'))
        job=self.add(night=True,not_before=self.ts('2026-11-15T08:45:00'))
        r=self.row(job);self.assertEqual(r['not_before']+r['requested_seconds'],self.ts('2026-11-15T09:00:00'))
        with self.assertRaises(ControlError):self.add(night=True,not_before=self.ts('2026-11-15T09:00:00'))

    def test_extension_cannot_cross_midnight(self):
        job=self.add(requested_seconds=1200);r=self.row(job)
        self.q.action(self.user,job,'claim',r['offer_id'])
        self.clock.return_value=self.ts('2026-11-14T23:50:00')
        with self.assertRaises(ControlError):self.q.action(self.user,job,'extend',extension_minutes=20)
        self.q.action(self.user,job,'extend',extension_minutes=10)
        r=self.row(job);self.assertEqual(r['activated']+r['requested_seconds'],self.ts('2026-11-15T00:00:00'))

    def test_night_reservation_does_not_move_behind_normal(self):
        job=self.add();r=self.row(job);self.q.action(self.user,job,'claim',r['offer_id'])
        start=self.ts('2026-11-15T00:00:00');night=self.add(night=True,not_before=start)
        self.assertEqual(self.row(night)['not_before'],start)
        self.assertEqual(self.row(job)['activated']+self.row(job)['requested_seconds'],start)

    def test_mode_requires_complete_cutoff_configuration(self):
        with self.assertRaises(ValueError):
            UnifiedQueue(self.store,['a','b'],timezone='Asia/Shanghai',strict_nights=True,
                         overtime_control=True,midnight_separation=True)

    def test_night_registration_releases_at_nine_then_normal_can_start(self):
        start=self.ts('2026-11-15T00:00:00');night=self.add(night=True,not_before=start)
        with self.store.db() as db:
            db.execute("UPDATE unified_tasks SET state='active',activated=? WHERE id=?",(start,night))
        self.clock.return_value=self.ts('2026-11-15T08:50:00')
        normal=self.add();self.assertEqual(self.row(normal)['state'],'queued')
        self.clock.return_value=self.ts('2026-11-15T09:00:00');self.q.tick()
        rows=self.q.view(self.user)['tasks']
        self.assertFalse(any(r['id']==night for r in rows))
        self.assertEqual(next(r for r in rows if r['id']==normal)['state'],'ready')
