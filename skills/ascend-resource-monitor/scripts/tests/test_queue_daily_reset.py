"""Daily cleanup keeps the new ordinary queue submitted during night closure."""
import json
import inspect
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'assets' / 'runtime'))
if os.environ.get('QUEUE_TEST_RUNTIME'):
    sys.path.insert(0, os.environ['QUEUE_TEST_RUNTIME'])
from queue_store import QueueStore
from queue_unified import UnifiedQueue
from queue_agent import initialize_events


class DailyResetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = patch('queue_unified.time.time').start()
        self.addCleanup(patch.stopall)
        self.clock.return_value = self.ts('2026-11-17T17:00:00')
        self.store = QueueStore(Path(self.tmp.name) / 'queue.db')
        self.user = self.store.user(self.store.register('u12345678', 'Test', 'fixture-password'))
        self.q = self.reopen()

    def ts(self, value):
        return datetime.fromisoformat(value).replace(tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()

    def reopen(self):
        options = {'night_start_hour': 0} if 'night_start_hour' in inspect.signature(UnifiedQueue).parameters else {}
        return UnifiedQueue(self.store, ['a', 'b'], timezone='Asia/Shanghai', daily_reset=True,
                            strict_nights=True, overtime_control=True, normal_max_seconds=3600,
                            night_carryover_cutoff_hour=9, midnight_separation=True, **options)

    def add(self, **values):
        payload = dict(purpose='测试预约',machine='a', cards=8, requested_seconds=3600,
                       notifications_ready=True, avoid_slots=[])
        payload.update(values)
        return self.q.enqueue(self.user, payload)

    def rows(self):
        with self.store.db() as db:
            return {row['id']: dict(row) for row in db.execute('SELECT * FROM unified_tasks')}

    def reset(self, value):
        self.clock.return_value = self.ts(value)
        with self.store.db() as db:
            self.q._reset_if_due(db, self.clock.return_value)

    def reset_info(self):
        with self.store.db() as db:
            return json.loads(db.execute("SELECT value FROM unified_meta WHERE key='last_daily_reset'").fetchone()[0])

    def test_nine_clears_previous_ready_and_queued_before_offering_overnight_requests(self):
        old_ready = self.add()
        old_waiting = self.add()
        self.assertEqual(self.rows()[old_ready]['state'], 'ready')
        self.assertEqual(self.rows()[old_waiting]['state'], 'queued')
        self.clock.return_value = self.ts('2026-11-18T08:11:00')
        overnight = self.add()
        self.assertIn(old_ready, self.rows())
        self.assertIn(old_waiting, self.rows())
        self.clock.return_value = self.ts('2026-11-18T09:00:00')
        self.q.tick()
        rows = self.rows()
        self.assertEqual(set(rows), {overnight})
        self.assertEqual(rows[overnight]['state'], 'ready')
        self.assertEqual(self.reset_info()['count'], 2)

    def test_midnight_cutoff_inclusive_preserves_new_ordinary_and_future_night(self):
        old = self.add()
        self.clock.return_value = self.ts('2026-11-18T00:00:00')
        midnight = self.add()
        self.clock.return_value = self.ts('2026-11-18T08:11:00')
        morning = self.add(machine='b')
        night = self.add(night=True, not_before=self.ts('2026-11-19T02:00:00'))
        self.reset('2026-11-18T08:59:59')
        self.assertIn(old, self.rows())
        self.reset('2026-11-18T09:00:00')
        self.assertEqual(set(self.rows()), {midnight, morning, night})
        self.assertEqual(self.reset_info()['count'], 1)

    def test_two_am_cutoff_expires_previous_normal_period_at_local_nine(self):
        old = self.add()
        before = self.add()
        exact = self.add()
        morning = self.add()
        # The 101/103 queue's night closure begins at 02:00 Beijing time.
        self.q.night_start_hour = 2
        with self.store.db() as db:
            for job, created in ((before, '2026-11-18T01:59:59'),
                                 (exact, '2026-11-18T02:00:00'),
                                 (morning, '2026-11-18T08:11:00')):
                db.execute('UPDATE unified_tasks SET created=? WHERE id=?', (self.ts(created), job))
        self.reset('2026-11-18T09:00:00')
        self.assertEqual(set(self.rows()), {exact, morning})
        self.assertEqual(self.reset_info()['count'], 2)

    def test_delayed_restart_catches_up_and_keeps_todays_active_and_new_requests(self):
        old = self.add()
        active = self.add(machine='b')
        new = self.add()
        with self.store.db() as db:
            db.execute("UPDATE unified_tasks SET created=?,state='active',activated=? WHERE id=?",
                       (self.ts('2026-11-18T08:11:00'), self.ts('2026-11-18T09:05:00'), active))
            db.execute('UPDATE unified_tasks SET created=? WHERE id=?',
                       (self.ts('2026-11-18T09:20:00'), new))
        self.q = self.reopen()
        self.reset('2026-11-18T09:30:00')
        rows = self.rows()
        self.assertNotIn(old, rows)
        self.assertEqual(set(rows), {active, new})
        self.assertEqual(rows[active]['activated'], self.ts('2026-11-18T09:05:00'))
        self.assertEqual(self.reset_info()['at'], self.ts('2026-11-18T09:00:00'))

    def test_reset_once_per_day_then_new_period_expires_next_day(self):
        self.add()
        self.reset('2026-11-18T09:00:00')
        before = self.reset_info()
        new = self.add()
        self.q = self.reopen()
        self.reset('2026-11-18T10:00:00')
        self.assertEqual(self.reset_info(), before)
        self.assertIn(new, self.rows())
        self.reset('2026-11-19T09:00:00')
        self.assertEqual(self.rows(), {})

    def test_cleanup_removes_only_expired_confirmation_and_emits_ended_event(self):
        old = self.add()
        self.clock.return_value = self.ts('2026-11-18T08:11:00')
        new = self.add()
        initialize_events(self.store)
        with self.store.db() as db:
            for job in (old, new):
                db.execute('INSERT INTO confirmations VALUES (?,?,?,?,?,?)',
                           (job, self.user['id'], job, 'claim', '{}', self.ts('2026-11-18T10:00:00')))
        self.reset('2026-11-18T09:00:00')
        with self.store.db() as db:
            self.assertEqual([r[0] for r in db.execute('SELECT job FROM confirmations')], [new])
            self.assertEqual([r[0] for r in db.execute("SELECT job FROM agent_queue_events WHERE state='ended'")], [old])

    def test_continuing_records_exclude_preserved_active_registration(self):
        old = self.add()
        new = self.add()
        with self.store.db() as db:
            db.execute("UPDATE unified_tasks SET state='active',activated=?,allocations=?,allowed_employees=? WHERE id=?",
                       (self.ts('2026-11-17T23:00:00'), '{"a":[0]}', '["v12345678"]', old))
            db.execute("UPDATE unified_tasks SET state='active',created=?,activated=?,allocations=? WHERE id=?",
                       (self.ts('2026-11-18T08:11:00'), self.ts('2026-11-18T09:05:00'), '{"b":[0]}', new))
        self.reset('2026-11-18T09:30:00')
        self.assertEqual(set(self.rows()), {new})
        self.assertEqual({r['node'] for r in self.q.continuing()}, {'a'})
        self.assertEqual({r['employee'] for r in self.q.continuing()}, {'u12345678', 'v12345678'})

    def test_disabled_reset_keeps_previous_requests(self):
        old = self.add()
        self.q.daily_reset = False
        self.reset('2026-11-18T09:00:00')
        self.assertIn(old, self.rows())

    def test_legacy_reset_still_clears_all_normals(self):
        old = self.add()
        self.clock.return_value = self.ts('2026-11-18T08:11:00')
        overnight = self.add()
        self.q.midnight_separation = False
        self.reset('2026-11-18T09:00:00')
        self.assertNotIn(old, self.rows())
        self.assertNotIn(overnight, self.rows())


if __name__ == '__main__':
    unittest.main()
