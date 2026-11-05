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

    def test_night_end_after_nine_starts_automatically_when_due(self):
        start = stamp('2026-11-04T21:00:00')
        job = self.add(night=True, not_before=start, requested_seconds=13*3600)
        row = self.queue.view(self.user)['tasks'][0]
        self.assertEqual(row['id'], job)
        self.assertEqual(row['estimated_start'], start)
        self.assertTrue(row['night_end_overdue'])
        self.assertFalse(row['window_expired'])
        self.clock.return_value = start
        row = self.queue.view(self.user)['tasks'][0]
        self.assertEqual(row['state'], 'active')
        self.assertEqual(row['activated'], start)
        self.assertFalse(row['can_claim'])
        self.assertEqual(row['claim_before'], 0)

    def test_night_duration_limit_does_not_change_existing_reservations(self):
        start = stamp('2026-11-04T20:00:00')
        job = self.add(night=True, not_before=start, requested_seconds=13*3600)
        with self.assertRaises(ControlError):
            self.add(night=True, not_before=start, requested_seconds=13*3600+300)
        with self.store.db() as db:
            db.execute('UPDATE unified_tasks SET requested_seconds=? WHERE id=?', (14*3600,job))
        reopened = UnifiedQueue(self.store, ['a','b'], timezone='Asia/Shanghai')
        row = next(r for r in reopened.view(self.user)['tasks'] if r['id']==job)
        self.assertEqual(row['requested_seconds'],14*3600)

    def test_night_estimate_has_no_claim_window_or_handoff_buffer(self):
        start = stamp('2026-11-04T20:00:00')
        first = self.add(night=True, not_before=start)
        second = self.add(night=True, not_before=start)
        self.clock.return_value = start
        rows = {r['id']: r for r in self.queue.view(self.user)['tasks']}
        self.assertEqual(rows[first]['state'], 'active')
        self.assertEqual(rows[second]['estimated_start'], start+3600)

    def test_night_starts_via_scheduler_without_browser_or_claim(self):
        start = stamp('2026-11-04T20:00:00')
        job = self.add(night=True, not_before=start)
        self.clock.return_value = start
        self.queue.tick()
        with self.store.db() as db:
            row = db.execute('SELECT state,activated,claim_before FROM unified_tasks WHERE id=?', (job,)).fetchone()
        self.assertEqual((row['state'], row['activated'], row['claim_before']), ('active', start, 0))

    def test_queued_night_predictions_replan_and_match_unchanged_start(self):
        start=stamp('2026-11-04T20:00:00')
        first=self.add(machine='auto',night=True,not_before=start)
        second=self.add(machine='auto',night=True,not_before=start)
        rows={row['id']:row for row in self.queue.view(self.user)['tasks']}
        for row in (rows[first],rows[second]):
            self.assertEqual(row['state'],'queued');self.assertEqual(row['allocations'],{})
            self.assertEqual(row['estimated_start'],start);self.assertEqual(len(row['planned_allocations']),1)
        self.assertNotEqual(set(rows[first]['planned_allocations']),set(rows[second]['planned_allocations']))
        original=rows[second]['planned_allocations']
        self.queue.action(self.user,first,'cancel')
        revised=next(row for row in self.queue.view(self.user)['tasks'] if row['id']==second)
        self.assertNotEqual(revised['planned_allocations'],original)
        self.clock.return_value=start
        active=next(row for row in self.queue.view(self.user)['tasks'] if row['id']==second)
        self.assertEqual(active['state'],'active');self.assertEqual(active['allocations'],revised['planned_allocations'])

    def test_unknown_release_does_not_claim_a_night_prediction(self):
        active=self.add(requested_seconds=60)
        self.queue.action(self.user,active,'claim',self.queue.view(self.user)['tasks'][0]['offer_id'])
        self.clock.return_value=self.now+61
        night=self.add(night=True,not_before=stamp('2026-11-04T20:00:00'))
        row=next(row for row in self.queue.view(self.user)['tasks'] if row['id']==night)
        self.assertIsNone(row['estimated_start']);self.assertEqual(row['planned_allocations'],{})

    def test_preexisting_ready_night_preserves_allocation_and_becomes_active(self):
        start = stamp('2026-11-04T20:00:00')
        job = self.add(night=True, not_before=start)
        with self.store.db() as db:
            db.execute("UPDATE unified_tasks SET state='ready',allocations=?,ready_since=?,claim_before=?,offer_id=? WHERE id=?",
                       ('{\"a\":[0,1,2,3,4,5,6,7]}', start, start+600, 'old-offer', job))
        self.clock.return_value = start+30
        row = self.queue.view(self.user)['tasks'][0]
        self.assertEqual(row['state'], 'active')
        self.assertEqual(row['activated'], start)
        self.assertEqual(row['allocations'], {'a': list(range(8))})
        self.assertEqual(row['offer_id'], '')
        self.assertEqual(row['missed_claims'], 0)
        reopened = UnifiedQueue(self.store, ['a', 'b'], timezone='Asia/Shanghai')
        self.assertEqual(reopened.view(self.user)['tasks'][0]['activated'], start)

    def test_normal_estimated_start_at_or_after_two_is_flagged(self):
        start = stamp('2026-11-04T20:00:00')
        job = self.add(night=True, not_before=start, requested_seconds=8*3600)
        self.clock.return_value = start
        self.assertEqual(self.queue.view(self.user)['tasks'][0]['state'], 'active')
        self.clock.return_value = stamp('2026-11-04T22:00:00')
        normal = self.add()
        row = next(r for r in self.queue.view(self.user)['tasks'] if r['id'] == normal)
        self.assertIsNone(row['estimated_start'])
        self.assertTrue(row['normal_start_after_two'])

    def test_late_normal_does_not_delay_night_and_can_fill_early_release(self):
        start = stamp('2026-11-04T20:00:00')
        active = self.add(night=True, not_before=start, requested_seconds=8*3600)
        self.clock.return_value = start
        self.assertEqual(self.queue.view(self.user)['tasks'][0]['state'], 'active')
        self.clock.return_value = stamp('2026-11-04T22:00:00')
        normal = self.add()
        night = self.add(night=True, not_before=stamp('2026-11-04T23:00:00'))
        rows = {r['id']: r for r in self.queue.view(self.user)['tasks']}
        self.assertTrue(rows[normal]['normal_start_after_two'])
        self.assertIsNone(rows[normal]['estimated_start'])
        self.assertEqual(rows[night]['estimated_start'], stamp('2026-11-05T04:00:00'))
        other = self.store.user(self.store.register('u12345679', 'Other', 'fixture-password'))
        for viewer in (None, other, {**other, 'admin': True}):
            other_row = next(r for r in self.queue.view(viewer)['tasks'] if r['id'] == normal)
            self.assertFalse(other_row['normal_start_after_two'])
        self.clock.return_value = stamp('2026-11-04T22:30:00')
        self.queue.action(self.user, active, 'finish')
        rows = {r['id']: r for r in self.queue.view(self.user)['tasks']}
        self.assertEqual(rows[normal]['state'], 'ready')
        self.assertFalse(rows[normal]['normal_start_after_two'])
        self.assertEqual(rows[night]['estimated_start'], stamp('2026-11-04T23:40:00'))

    def test_offer_from_before_two_expires_at_two_and_releases_night_task(self):
        self.clock.return_value = stamp('2026-11-05T01:55:00')
        normal = self.add()
        night = self.add(night=True, not_before=stamp('2026-11-05T01:56:00'))
        rows = {r['id']: r for r in self.queue.view(self.user)['tasks']}
        self.assertEqual(rows[normal]['claim_before'], stamp('2026-11-05T02:00:00'))
        old_offer = rows[normal]['offer_id']
        self.clock.return_value = stamp('2026-11-05T02:00:00')
        rows = {r['id']: r for r in self.queue.view(self.user)['tasks']}
        self.assertEqual(rows[normal]['state'], 'queued')
        self.assertTrue(rows[normal]['normal_start_after_two'])
        self.assertEqual(rows[night]['state'], 'active')
        with self.assertRaises(ControlError):
            self.queue.action(self.user, normal, 'claim', old_offer)

    def test_later_normal_task_does_not_delay_waiting_night_task(self):
        active = self.add(requested_seconds=7200)
        offer = self.queue.view(self.user)['tasks'][0]['offer_id']
        self.queue.action(self.user, active, 'claim', offer)
        night = self.add(night=True, not_before=stamp('2026-11-04T20:00:00'), requested_seconds=3600)
        normal = self.add()
        rows = {row['id']: row for row in self.queue.view(self.user)['tasks']}
        self.assertEqual(rows[night]['estimated_start'], stamp('2026-11-04T21:00:00'))
        self.assertEqual(rows[normal]['estimated_start'], stamp('2026-11-04T22:10:00'))
        self.assertLess(rows[night]['position'], rows[normal]['position'])

    def test_later_normal_backfills_only_with_enough_start_buffer(self):
        night = self.add(night=True, not_before=stamp('2026-11-04T20:00:00'))
        self.clock.return_value = stamp('2026-11-04T19:00:00')
        normal = self.add(requested_seconds=49*60)
        rows = {row['id']: row for row in self.queue.view(self.user)['tasks']}
        self.assertEqual(rows[night]['estimated_start'], stamp('2026-11-04T20:00:00'))
        self.assertEqual(rows[normal]['state'], 'ready')
        self.assertEqual(rows[normal]['claim_before'], stamp('2026-11-04T19:01:00'))

    def test_later_normal_cannot_backfill_across_start_buffer(self):
        night = self.add(night=True, not_before=stamp('2026-11-04T20:00:00'))
        self.clock.return_value = stamp('2026-11-04T19:00:00')
        normal = self.add(requested_seconds=55*60)
        rows = {row['id']: row for row in self.queue.view(self.user)['tasks']}
        self.assertEqual(rows[night]['estimated_start'], stamp('2026-11-04T20:00:00'))
        self.assertEqual(rows[normal]['estimated_start'], stamp('2026-11-04T21:10:00'))

    def test_expired_offer_does_not_reorder_earlier_night_behind_normal(self):
        night = self.add(night=True, not_before=stamp('2026-11-04T20:00:00'))
        first = self.add(requested_seconds=1800)
        second = self.add(requested_seconds=1800)
        self.clock.return_value += 601
        self.queue.tick()
        with self.store.db() as db:
            order = [row['id'] for row in db.execute(
                "SELECT id FROM unified_tasks WHERE state IN ('ready','queued') ORDER BY queue_order,rowid")]
        self.assertEqual(order, [night, second, first])

    def test_night_follows_expected_normal_end_but_never_starts_before_actual_release(self):
        normal = self.add(requested_seconds=7200)
        offer = self.queue.view(self.user)['tasks'][0]['offer_id']
        self.queue.action(self.user, normal, 'claim', offer)
        night = self.add(night=True, not_before=stamp('2026-11-04T20:00:00'))
        row = next(r for r in self.queue.view(self.user)['tasks'] if r['id']==night)
        self.assertEqual(row['estimated_start'], self.now+7200)
        self.clock.return_value=self.now+7200
        row = next(r for r in self.queue.view(self.user)['tasks'] if r['id']==night)
        self.assertEqual(row['state'], 'queued')
        self.assertIsNone(row['estimated_start'])
        self.queue.action(self.user, normal, 'finish')
        row = next(r for r in self.queue.view(self.user)['tasks'] if r['id']==night)
        self.assertEqual(row['state'], 'active')
        self.assertEqual(row['activated'], self.now+7200)

    def test_active_night_is_not_preempted_by_normal(self):
        night = self.add(night=True, not_before=stamp('2026-11-04T20:00:00'))
        self.clock.return_value = stamp('2026-11-04T20:00:00')
        before = self.queue.view(self.user)['tasks'][0]
        normal = self.add()
        rows = {r['id']: r for r in self.queue.view(self.user)['tasks']}
        self.assertEqual(rows[night]['state'], 'active')
        self.assertEqual(rows[night]['activated'], before['activated'])
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
        for value in ('2026-11-04T19:59:00', '2026-11-05T09:00:00', '2026-11-05T09:01:00'):
            with self.assertRaises(ControlError):
                self.add(night=True, not_before=stamp(value))
        job = self.add(night=True, not_before=stamp('2026-11-05T08:55:00'))
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
        future = self.add(night=True, not_before=stamp('2026-11-05T20:00:00'), requested_seconds=3600)
        normal = self.add(machine='b', requested_seconds=60)
        self.clock.return_value = start
        row = next(r for r in self.queue.view(self.user)['tasks'] if r['id'] == active)
        self.assertEqual(row['state'], 'active')
        original_start=row['activated'];original_end=original_start+row['requested_seconds']
        self.clock.return_value = stamp('2026-11-05T09:00:00')
        rows = {r['id']: r for r in self.queue.view(self.user)['tasks']}
        self.assertNotIn(normal, rows)
        self.assertEqual(rows[active]['state'], 'active')
        self.assertNotIn(waiting,rows)
        self.assertEqual(rows[future]['state'],'queued')
        self.assertEqual(rows[active]['activated'],original_start)
        self.assertEqual(rows[active]['activated']+rows[active]['requested_seconds'],original_end)
        self.assertTrue(rows[active]['morning_carryover'])
        self.assertTrue(rows[active]['night_end_overdue'])
        reopened = UnifiedQueue(self.store, ['a', 'b'], timezone='Asia/Shanghai', daily_reset=True)
        self.assertEqual({r['id'] for r in reopened.view(self.user)['tasks']}, {active,future})
        reopened.action(self.user,active,'finish')
        self.assertEqual({r['id'] for r in reopened.view(self.user)['tasks']},{future})

    def test_night_handoff_waits_for_planned_start_without_process_monitor(self):
        start=stamp('2026-11-04T20:00:00')
        first=self.add(night=True,not_before=start,requested_seconds=3600)
        second=self.add(night=True,not_before=start+7200,requested_seconds=3600)
        self.clock.return_value=start;self.queue.tick()
        self.clock.return_value=start+3600;self.queue.tick()
        rows={r['id']:r for r in self.queue.view(self.user)['tasks']}
        self.assertEqual(rows[first]['state'],'active')
        self.assertEqual(rows[second]['estimated_start'],start+7200)
        self.clock.return_value=start+7200;self.queue.tick()
        rows={r['id']:r for r in self.queue.view(self.user)['tasks']}
        self.assertNotIn(first,rows)
        self.assertEqual((rows[second]['state'],rows[second]['activated']),('active',start+7200))

    def test_night_handoff_never_preempts_before_first_expected_end(self):
        start=stamp('2026-11-04T20:00:00')
        first=self.add(night=True,not_before=start,requested_seconds=7200)
        second=self.add(night=True,not_before=start+3600,requested_seconds=3600)
        self.clock.return_value=start;self.queue.tick()
        self.clock.return_value=start+3600;rows={r['id']:r for r in self.queue.view(self.user)['tasks']}
        self.assertEqual(rows[first]['state'],'active')
        self.assertEqual(rows[second]['estimated_start'],start+7200)
        self.clock.return_value=start+7200;self.queue.tick()
        rows={r['id']:r for r in self.queue.view(self.user)['tasks']}
        self.assertNotIn(first,rows)
        self.assertEqual(rows[second]['state'],'active')

    def test_earlier_night_handoff_is_not_delayed_by_later_normal(self):
        start=stamp('2026-11-04T20:00:00')
        first=self.add(night=True,not_before=start,requested_seconds=3600)
        second=self.add(night=True,not_before=start+7200,requested_seconds=3600)
        self.clock.return_value=start;self.queue.tick()
        self.clock.return_value=start+1800;normal=self.add(requested_seconds=3600)
        self.clock.return_value=start+3600;self.queue.tick()
        rows={r['id']:r for r in self.queue.view(self.user)['tasks']}
        self.assertEqual(rows[first]['state'],'active')
        self.assertEqual(rows[normal]['state'],'queued')
        self.clock.return_value=start+7200;self.queue.tick()
        rows={r['id']:r for r in self.queue.view(self.user)['tasks']}
        self.assertNotIn(first,rows)
        self.assertEqual(rows[normal]['state'],'queued')
        self.assertEqual(rows[second]['state'],'active')

    def test_morning_carryover_never_handed_off_to_next_night(self):
        start=stamp('2026-11-05T08:00:00')
        self.clock.return_value=start-3600
        self.queue=UnifiedQueue(self.store,['a','b'],timezone='Asia/Shanghai',daily_reset=True)
        first=self.add(night=True,not_before=start,requested_seconds=3600)
        self.clock.return_value=start;self.queue.tick()
        self.clock.return_value=stamp('2026-11-05T09:00:00');self.queue.tick()
        next_start=stamp('2026-11-05T20:00:00')
        second=self.add(night=True,not_before=next_start)
        self.clock.return_value=next_start;rows={r['id']:r for r in self.queue.view(self.user)['tasks']}
        self.assertTrue(rows[first]['morning_carryover'])
        self.assertEqual(rows[second]['state'],'queued')
        self.queue.action(self.user,first,'finish')
        self.assertEqual(self.queue.view(self.user)['tasks'][0]['state'],'active')


if __name__ == '__main__':
    unittest.main()
