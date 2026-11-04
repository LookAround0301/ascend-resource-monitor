from datetime import datetime
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'assets/runtime'))
from queue_store import QueueStore
from queue_unified import UnifiedQueue
from control_common import ControlError


def stamp(value):
    return datetime.fromisoformat(value).replace(tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()


class NightPriorityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = QueueStore(Path(self.temp.name) / 'queue.db')
        self.user = self.store.user(self.store.register('u12345678', 'Fixture', 'fixture-password'))
        self.queue = UnifiedQueue(self.store, ['a', 'b'], timezone='Asia/Shanghai')
        self.now = stamp('2026-11-04T19:00:00')
        self.clock = patch('queue_unified.time.time', return_value=self.now).start()
        self.addCleanup(patch.stopall)

    def add(self, **kwargs):
        return self.queue.enqueue(self.user, {'machine': 'a', 'cards': 8, 'requested_seconds': 3600,
                                              'notifications_ready': True, **kwargs})

    def test_night_end_after_nine_is_scheduled_and_can_be_claimed(self):
        start = stamp('2026-11-04T20:00:00')
        job = self.add(night=True, not_before=start, requested_seconds=14*3600)
        row = self.queue.view(self.user)['tasks'][0]
        self.assertEqual(row['id'], job)
        self.assertEqual(row['estimated_start'], start)
        self.assertTrue(row['night_end_overdue'])
        self.assertFalse(row['window_expired'])
        self.clock.return_value = start
        row = self.queue.view(self.user)['tasks'][0]
        self.assertEqual(row['state'], 'ready')
        self.queue.action(self.user, job, 'claim', row['offer_id'])
        self.assertEqual(self.queue.view(self.user)['tasks'][0]['state'], 'active')

    def test_normal_estimated_start_at_or_after_two_is_flagged(self):
        start = stamp('2026-11-04T20:00:00')
        job = self.add(night=True, not_before=start, requested_seconds=8*3600)
        self.clock.return_value = start
        row = self.queue.view(self.user)['tasks'][0]
        self.queue.action(self.user, job, 'claim', row['offer_id'])
        self.clock.return_value = stamp('2026-11-04T22:00:00')
        normal = self.add()
        row = next(r for r in self.queue.view(self.user)['tasks'] if r['id'] == normal)
        self.assertGreaterEqual(row['estimated_start'], stamp('2026-11-05T02:00:00'))
        self.assertTrue(row['normal_start_after_two'])

    def test_later_normal_task_is_planned_before_waiting_night_task(self):
        active = self.add(requested_seconds=7200)
        offer = self.queue.view(self.user)['tasks'][0]['offer_id']
        self.queue.action(self.user, active, 'claim', offer)
        night = self.add(night=True, not_before=stamp('2026-11-04T20:00:00'), requested_seconds=3600)
        normal = self.add()
        rows = {row['id']: row for row in self.queue.view(self.user)['tasks']}
        self.assertEqual(rows[normal]['estimated_start'], stamp('2026-11-04T21:10:00'))
        self.assertEqual(rows[night]['estimated_start'], stamp('2026-11-04T22:20:00'))
        self.assertLess(rows[normal]['position'], rows[night]['position'])

    def test_already_ready_night_is_not_preempted_by_normal(self):
        night = self.add(night=True, not_before=stamp('2026-11-04T20:00:00'))
        self.clock.return_value = stamp('2026-11-04T20:00:00')
        before = self.queue.view(self.user)['tasks'][0]
        normal = self.add()
        rows = {r['id']: r for r in self.queue.view(self.user)['tasks']}
        self.assertEqual(rows[night]['state'], 'ready')
        self.assertEqual(rows[night]['offer_id'], before['offer_id'])
        self.assertEqual(rows[normal]['state'], 'queued')

    def test_priority_controls_do_not_move_night_ahead_of_normal(self):
        active = self.add(requested_seconds=7200)
        self.queue.action(self.user, active, 'claim', self.queue.view(self.user)['tasks'][0]['offer_id'])
        night = self.add(night=True, not_before=stamp('2026-11-04T20:00:00'))
        normal = self.add()
        rows = {r['id']: r for r in self.queue.view(self.user)['tasks']}
        self.assertFalse(rows[normal]['can_lower_priority'])
        self.assertFalse(rows[night]['can_lower_priority'])
        with self.assertRaises(ControlError):
            self.queue.action(self.user, normal, 'priority-down')
        with self.assertRaises(ControlError):
            self.queue.action({**self.user, 'admin': True}, night, 'priority-top')

    def test_night_start_window_and_exact_nine_boundary(self):
        for value in ('2026-11-04T19:59:00', '2026-11-05T09:01:00'):
            with self.assertRaises(ControlError):
                self.add(night=True, not_before=stamp(value))
        job = self.add(night=True, not_before=stamp('2026-11-05T09:00:00'))
        row = next(r for r in self.queue.view(self.user)['tasks'] if r['id'] == job)
        self.assertEqual(row['finish_before'], stamp('2026-11-05T09:00:00'))
        self.assertTrue(row['night_end_overdue'])
        self.clock.return_value = stamp('2026-11-05T01:00:00')
        job = self.add(night=True, not_before=stamp('2026-11-05T01:30:00'))
        row = next(r for r in self.queue.view(self.user)['tasks'] if r['id'] == job)
        self.assertEqual(row['finish_before'], stamp('2026-11-05T09:00:00'))

    def test_daily_reset_keeps_night_active_and_waiting_but_clears_normal(self):
        self.clock.return_value = stamp('2026-11-05T08:50:00')
        self.queue = UnifiedQueue(self.store, ['a', 'b'], timezone='Asia/Shanghai', daily_reset=True)
        start = stamp('2026-11-05T08:51:00')
        active = self.add(night=True, not_before=start, requested_seconds=3600)
        waiting = self.add(night=True, not_before=start, requested_seconds=3600)
        normal = self.add(machine='b', requested_seconds=60)
        self.clock.return_value = start
        row = next(r for r in self.queue.view(self.user)['tasks'] if r['id'] == active)
        self.queue.action(self.user, active, 'claim', row['offer_id'])
        self.clock.return_value = stamp('2026-11-05T09:00:00')
        rows = {r['id']: r for r in self.queue.view(self.user)['tasks']}
        self.assertNotIn(normal, rows)
        self.assertEqual(rows[active]['state'], 'active')
        self.assertEqual(rows[waiting]['state'], 'queued')
        self.assertTrue(rows[active]['night_end_overdue'])
        self.assertTrue(rows[waiting]['night_end_overdue'])
        reopened = UnifiedQueue(self.store, ['a', 'b'], timezone='Asia/Shanghai', daily_reset=True)
        self.assertEqual({r['id'] for r in reopened.view(self.user)['tasks']}, {active, waiting})


if __name__ == '__main__':
    unittest.main()
