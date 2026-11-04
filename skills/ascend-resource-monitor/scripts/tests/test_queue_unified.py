"""Reservation minute precision and the per-request next-morning deadline."""
from datetime import datetime
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'assets/runtime'))
from control_common import ControlError
from queue_store import QueueStore
from queue_unified import UnifiedQueue
from queue_planner import plan


def stamp(value):
    return datetime.fromisoformat(value).replace(tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()


class UnifiedWindowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = QueueStore(Path(self.temp.name) / 'queue.db')
        self.user = self.store.user(self.store.register('u12345678', 'Fixture', 'test-only-password'))
        self.queue = UnifiedQueue(self.store, ['a', 'b'], timezone='Asia/Shanghai')
        self.now = stamp('2026-11-04T14:00:00')
        self.deadline = stamp('2026-11-05T09:00:00')

    def add(self, **data):
        return self.queue.enqueue(self.user, {'cards': 1, 'requested_seconds': 90*60,
                                             'notifications_ready': True, **data})

    def test_minutes_and_normal_two_hour_limit(self):
        with patch('queue_unified.time.time', return_value=self.now):
            for seconds in (60, 90*60, 2*3600):
                self.add(requested_seconds=seconds)
            for seconds in (0, 59, 61, 2*3600+60):
                with self.assertRaises(ControlError): self.add(requested_seconds=seconds)
            rows = self.queue.view(self.user)['tasks']
        self.assertEqual([r['requested_seconds'] for r in rows], [60, 5400, 7200])
        self.assertTrue(all(r['finish_before'] == self.deadline for r in rows))

    def test_scheduled_start_and_end_use_deployment_timezone(self):
        with patch('queue_unified.time.time', return_value=self.now):
            self.assertEqual(self.queue.view(self.user)['reservation_window']['end'], self.deadline)
            self.add(night=True, not_before=self.deadline-90*60)
            for start, seconds in [(self.deadline-60, 120), (self.deadline, 60),
                                   (self.now-60, 60), (float('nan'), 60)]:
                with self.assertRaises(ControlError): self.add(night=True, not_before=start, requested_seconds=seconds)
        # Before 9am still means the following calendar day's 9am.
        self.assertEqual(self.queue.cutoff(stamp('2026-11-04T01:00:00')), self.deadline)

    def test_waiting_tasks_never_move_past_original_deadline(self):
        with patch('queue_unified.time.time', return_value=self.now):
            job = self.add()
            self.assertTrue(self.queue.view(self.user)['tasks'][0]['can_claim'])
        with patch('queue_unified.time.time', return_value=self.deadline-60):
            row = self.queue.view(self.user)['tasks'][0]
            self.assertEqual(row['state'], 'queued')
            self.assertTrue(row['window_expired'])
            self.assertFalse(row['allocations'])
            self.assertIsNone(row['estimated_start'])
            with self.assertRaises(ControlError): self.queue.action(self.user, job, 'claim')
            self.queue.action(self.user, job, 'cancel')
            self.assertEqual(self.queue.view(self.user)['tasks'], [])

    def test_expired_deadline_does_not_stop_active_task_or_change_legacy(self):
        with patch('queue_unified.time.time', return_value=self.now):
            job = self.add()
            self.queue.action(self.user, job, 'claim')
        # Reopening/migrating preserves the ongoing task and its deadline.
        reopened = UnifiedQueue(self.store, ['a', 'b'], timezone='Asia/Shanghai')
        with patch('queue_unified.time.time', return_value=self.deadline+3600):
            row = reopened.view(self.user)['tasks'][0]
            self.assertEqual(row['id'], job)
            self.assertEqual(row['state'], 'active')
            self.assertFalse(row['window_expired'])

    def test_planner_deadline_includes_duration_but_not_release_buffer(self):
        nodes = {'a': list(range(8)), 'b': list(range(8))}
        jobs = [{'id': 'exact', 'cards': 8, 'dual': True, 'seconds': 600, 'finish_before': 1600}]
        self.assertEqual(plan(nodes, [], jobs, 1000)[0]['start'], 1000)
        busy = [{'node': node, 'cards': cards, 'start': 0, 'end': 1100} for node, cards in nodes.items()]
        jobs.append({'id': 'fits', 'cards': 1, 'seconds': 60, 'finish_before': 1600})
        result = plan(nodes, busy, jobs, 1000)
        self.assertIsNone(result[0]['start'])
        self.assertEqual(result[1]['start'], 1100)

    def test_preexisting_reservation_without_deadline_is_preserved(self):
        with patch('queue_unified.time.time', return_value=self.now):
            job = self.add()
        with self.store.db() as db:
            db.execute('ALTER TABLE unified_tasks DROP COLUMN finish_before')
        reopened = UnifiedQueue(self.store, ['a', 'b'], timezone='Asia/Shanghai')
        with patch('queue_unified.time.time', return_value=self.now):
            row = reopened.view(self.user)['tasks'][0]
        self.assertEqual(row['id'], job)
        self.assertEqual(row['requested_seconds'], 5400)
        self.assertEqual(row['finish_before'], 0)


if __name__ == '__main__': unittest.main()
