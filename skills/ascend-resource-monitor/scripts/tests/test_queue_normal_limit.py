"""Normal reservation limits preserve night policy and existing reservations."""
from datetime import datetime
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, os.environ.get('QUEUE_TEST_RUNTIME', str(Path(__file__).resolve().parents[2] / 'assets/runtime')))
from control_common import ControlError
from queue_store import QueueStore
from queue_unified import UnifiedQueue


class NormalReservationLimitTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = QueueStore(Path(temp.name) / 'queue.db')
        self.user = self.store.user(self.store.register('u12345678', 'Fixture', 'test-only-password'))
        self.now = datetime(2026, 11, 23, 14, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        clock = patch('queue_unified.time.time', return_value=self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.queue = self.make_queue(3)

    def make_queue(self, limit=5):
        return UnifiedQueue(self.store, ['a', 'b'], timezone='Asia/Shanghai', normal_reservation_limit=limit)

    def add(self, queue=None, **extra):
        return (queue or self.queue).enqueue(self.user, {'purpose':'测试预约','cards': 1, 'requested_seconds': 3600,
                                                       'notifications_ready': True, **extra})

    def test_three_allowed_fourth_rejected_without_inserting(self):
        jobs = [self.add() for _ in range(3)]
        with self.assertRaises(ControlError) as error:
            self.add()
        self.assertEqual(error.exception.status, 409)
        self.assertIn('3 条未结束普通预约', str(error.exception))
        state = self.queue.view(self.user)
        self.assertEqual(state['normal_reservation_limit'], 3)
        self.assertEqual({r['id'] for r in state['tasks']}, set(jobs))

    def test_queued_ready_active_all_count_and_cancel_frees_slot(self):
        jobs = [self.add() for _ in range(3)]
        with self.store.db() as db:
            for job, state in zip(jobs, ('queued', 'ready', 'active')):
                db.execute('UPDATE unified_tasks SET state=? WHERE id=?', (state, job))
        with self.assertRaises(ControlError):
            self.add()
        self.queue.action(self.user, jobs[0], 'cancel')
        self.assertTrue(self.add())

    def test_night_does_not_consume_normal_quota_and_total_five_remains(self):
        start = self.now + 8 * 3600
        self.add(night=True, not_before=start)
        for _ in range(3):
            self.add()
        self.assertTrue(self.add(night=True, not_before=start + 3600))
        with self.assertRaises(ControlError) as error:
            self.add(night=True, not_before=start + 7200)
        self.assertIn('5 个未结束预约', str(error.exception))

    def test_completed_and_cancelled_do_not_consume_quota(self):
        jobs = [self.add() for _ in range(3)]
        with self.store.db() as db:
            for job, state in zip(jobs[:2], ('done', 'cancelled')):
                db.execute('UPDATE unified_tasks SET state=? WHERE id=?', (state, job))
        self.assertTrue(self.add())
        self.assertTrue(self.add())

    def test_lowering_limit_keeps_existing_reservations(self):
        old_queue = self.make_queue()
        jobs = [self.add(old_queue) for _ in range(5)]
        new_queue = self.make_queue(3)
        self.assertEqual({r['id'] for r in new_queue.view(self.user)['tasks']}, set(jobs))
        with self.assertRaises(ControlError):
            self.add(new_queue)

    def test_invalid_limits_rejected(self):
        for limit in (0, 6, True, 3.0, '3'):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                self.make_queue(limit)


if __name__ == '__main__':
    unittest.main()
