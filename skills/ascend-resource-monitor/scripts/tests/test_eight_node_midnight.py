import secrets
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'assets/runtime'))
from control_common import ControlError
from queue_agent import command, initialize_events
from queue_store import QueueStore
from queue_unified import UnifiedQueue


class EightNodeMidnightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = patch('queue_unified.time.time').start()
        self.addCleanup(patch.stopall)
        self.clock.return_value = self.ts('2026-11-18T23:30:00')
        self.store = QueueStore(Path(self.tmp.name) / 'queue.db')
        self.user = self.store.user(self.store.login_without_password('a00000001', 'Test User'))
        self.nodes = ['A5-' + str(n) for n in (179, 183, 187, 191, 220, 224, 228, 232)]
        self.q = UnifiedQueue(self.store, self.nodes, timezone='Asia/Shanghai',
            daily_reset=True, strict_nights=True, overtime_control=True,
            normal_max_seconds=28800, night_carryover_cutoff_hour=9,
            midnight_separation=True, night_tomorrow_only=True,
            flexible_scheduling=True, daily_reset_normal_cutoff_hour=0)

    def ts(self, value):
        return datetime.fromisoformat(value).replace(tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()

    def add(self, **kwargs):
        return self.q.enqueue(self.user, dict(machine='multi:8', cards=64,
            requested_seconds=28800, notifications_ready=True, avoid_slots=[], **kwargs))

    def rows(self):
        return {r['id']: r for r in self.q.view(self.user)['tasks']}

    def test_eight_node_normal_released_at_midnight_and_waits_until_nine(self):
        job = self.add()
        self.q.action(self.user, job, 'claim', self.rows()[job]['offer_id'])
        active = self.rows()[job]
        midnight = self.ts('2026-11-19T00:00:00')
        self.assertEqual(set(active['allocations']), set(self.nodes))
        self.assertEqual(active['activated'] + active['requested_seconds'], midnight)
        self.clock.return_value = midnight
        self.assertNotIn(job, self.rows())
        new = self.add()
        self.assertEqual(self.rows()[new]['state'], 'queued')
        self.clock.return_value = self.ts('2026-11-19T08:59:59')
        self.assertEqual(self.rows()[new]['state'], 'queued')
        self.clock.return_value += 1
        self.assertEqual(self.rows()[new]['state'], 'ready')
        self.assertEqual(self.rows()[new]['effective_requested_seconds'], 28800)

    def test_date_and_windows_always_only_tomorrow_even_after_midnight(self):
        for value, expected in [('2026-11-18T23:59:59', '2026-11-19'),
                                ('2026-11-19T00:00:00', '2026-11-20'),
                                ('2026-11-19T08:30:00', '2026-11-20')]:
            with self.subTest(value=value):
                self.clock.return_value = self.ts(value)
                state = self.q.view(self.user)
                window = state['reservation_window']
                self.assertTrue(state['night_tomorrow_only'])
                self.assertEqual(window['today'], value[:10])
                self.assertEqual(window['start_dates'], [expected])
                self.assertEqual(window['night_windows'], [dict(
                    start=self.ts(expected+'T00:00:00'), end=self.ts(expected+'T09:00:00'))])

    def test_browser_and_agent_cannot_book_today_or_day_after_tomorrow(self):
        initialize_events(self.store)
        self.clock.return_value = self.ts('2026-11-18T00:30:00')
        for start in ['2026-11-18T01:00:00', '2026-11-20T00:00:00', '2026-11-19T09:00:00']:
            request = dict(machine='multi:8', cards=8, requested_seconds=3600,
                           night=True, not_before=self.ts(start))
            with self.subTest(start=start):
                with self.assertRaisesRegex(ControlError, '只能预约明天'):
                    self.q.enqueue(self.user, dict(request, notifications_ready=True))
                with self.assertRaisesRegex(ControlError, '只能预约明天'):
                    command(self.q, self.store, dict(mode='agent_execute',
                        employee=self.user['employee'], operation=secrets.token_hex(16),
                        expires=self.clock.return_value+300, request=request))
        self.assertEqual(self.rows(), {})

    def test_agent_can_book_all_eight_nodes_and_fixed_reservation_survives_date_rollover(self):
        initialize_events(self.store)
        start = self.ts('2026-11-19T00:00:00')
        result = command(self.q, self.store, dict(mode='agent_execute',
            employee=self.user['employee'], operation=secrets.token_hex(16),
            expires=self.clock.return_value+300, request=dict(machine='multi:8', cards=64,
            requested_seconds=9*3600, night=True, not_before=start)))
        row = self.rows()[result['job']]
        self.assertEqual(set(row['allocations']), set(self.nodes))
        self.assertTrue(row['night_fixed'])
        self.clock.return_value = start
        self.q.finish_night_takeovers(self.q.due_night_tasks(start), set(), {}, start)
        self.assertEqual(self.rows()[result['job']]['state'], 'active')
        self.clock.return_value = start+9*3600
        self.assertNotIn(result['job'], self.rows())

    def test_selected_three_nodes_night_is_clipped_at_nine(self):
        start = self.ts('2026-11-19T08:30:00')
        selected = self.nodes[::3]
        job = self.q.enqueue(self.user, dict(machine='selected', selected_nodes=selected,
            cards=3, requested_seconds=7200, notifications_ready=True,
            night=True, not_before=start))
        row = self.rows()[job]
        self.assertEqual(set(row['allocations']), set(selected))
        self.assertEqual(row['requested_seconds'], 1800)

    def test_nine_reset_removes_previous_period_and_keeps_new_period(self):
        old = self.add()
        self.clock.return_value = self.ts('2026-11-19T00:05:00')
        new = self.add()
        self.clock.return_value = self.ts('2026-11-19T09:00:00')
        rows = self.rows()
        self.assertNotIn(old, rows)
        self.assertEqual(rows[new]['state'], 'ready')


if __name__ == '__main__':
    unittest.main()
