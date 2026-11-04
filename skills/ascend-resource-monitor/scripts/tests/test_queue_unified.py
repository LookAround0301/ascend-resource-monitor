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
            for start, seconds in [(stamp('2026-11-04T19:59:00'), 120), (self.deadline+60, 60),
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

    def test_fixed_single_node_is_persisted_and_never_falls_back(self):
        with patch('queue_unified.time.time', return_value=self.now):
            full = self.add(machine='a', cards=8)
            self.queue.action(self.user, full, 'claim')
            waiting = self.add(machine='a', cards=1)
            other = self.add(machine='b', cards=3)
            rows = {r['id']: r for r in self.queue.view(self.user)['tasks']}
        self.assertEqual(rows[waiting]['state'], 'queued')
        self.assertEqual(rows[waiting]['fixed_node'], 'a')
        self.assertEqual(set(rows[waiting]['planned_allocations']), {'a'})
        self.assertEqual(set(rows[other]['allocations']), {'b'})
        reopened = UnifiedQueue(self.store, ['a', 'b'], timezone='Asia/Shanghai')
        with patch('queue_unified.time.time', return_value=self.now):
            self.assertEqual(next(r for r in reopened.view(self.user)['tasks'] if r['id'] == waiting)['machine'], 'a')

    def test_auto_single_uses_available_other_node(self):
        with patch('queue_unified.time.time', return_value=self.now):
            full = self.add(machine='a', cards=8)
            self.queue.action(self.user, full, 'claim')
            job = self.add(machine='auto', cards=3)
            row = next(r for r in self.queue.view(self.user)['tasks'] if r['id'] == job)
        self.assertEqual(row['machine'], 'auto')
        self.assertEqual(row['fixed_node'], '')
        self.assertEqual(set(row['allocations']), {'b'})
        self.assertEqual(len(row['allocations']['b']), 3)

    def test_ten_minute_timeout_moves_only_one_position(self):
        with patch('queue_unified.time.time', return_value=self.now):
            jobs = [self.add(machine='a', cards=8) for _ in range(3)]
            rows = self.queue.view(self.user)['tasks']
            old_offer = rows[0]['offer_id']
            created = {r['id']:r['created'] for r in rows}
            self.assertEqual(rows[0]['claim_before'], self.now+600)
        with patch('queue_unified.time.time', return_value=self.now+599):
            self.assertEqual(self.queue.view(self.user)['tasks'][0]['id'], jobs[0])
        with patch('queue_unified.time.time', return_value=self.now+600):
            self.queue.tick()
            rows = self.queue.view(self.user)['tasks']
            self.assertEqual([r['id'] for r in rows], [jobs[1],jobs[0],jobs[2]])
            self.assertEqual([r['state'] for r in rows], ['ready','queued','queued'])
            self.assertEqual(rows[1]['missed_claims'], 1)
            self.assertEqual({r['id']:r['created'] for r in rows}, created)
            with self.assertRaises(ControlError):self.queue.action(self.user,jobs[0],'claim',old_offer)

    def test_offer_survives_restart_and_stale_offer_cannot_claim_reallocation(self):
        with patch('queue_unified.time.time', return_value=self.now):
            job=self.add(machine='a',cards=8)
            offer=self.queue.view(self.user)['tasks'][0]
        with patch('queue_unified.time.time', return_value=self.now+300):
            reopened=UnifiedQueue(self.store,['a','b'],timezone='Asia/Shanghai')
            row=reopened.view(self.user)['tasks'][0]
            self.assertEqual(row['ready_since'],offer['ready_since'])
            self.assertEqual(row['claim_before'],offer['claim_before'])
            self.assertEqual(row['offer_id'],offer['offer_id'])
        with patch('queue_unified.time.time', return_value=self.now+600):
            reopened.tick()
            new=reopened.view(self.user)['tasks'][0]
            self.assertEqual(new['missed_claims'],1)
            self.assertNotEqual(new['offer_id'],offer['offer_id'])
            with self.assertRaises(ControlError):reopened.action(self.user,job,'claim',offer['offer_id'])
            reopened.action(self.user,job,'claim',new['offer_id'])
            self.assertEqual(reopened.view(self.user)['tasks'][0]['state'],'active')

    def test_simultaneous_timeouts_each_move_back_one_place(self):
        with patch('queue_unified.time.time', return_value=self.now):
            a=self.add(machine='a',cards=8);b=self.add(machine='b',cards=8)
            c=self.add(machine='a',cards=8);d=self.add(machine='b',cards=8)
        with patch('queue_unified.time.time', return_value=self.now+600):
            rows=self.queue.view(self.user)['tasks']
            self.assertEqual([r['id'] for r in rows],[c,a,b,d])
            self.assertEqual([r['missed_claims'] for r in rows],[0,1,1,0])

    def test_claimed_work_is_not_postponed_after_ten_minutes(self):
        with patch('queue_unified.time.time', return_value=self.now):
            job=self.add(machine='a',cards=8)
            offer=self.queue.view(self.user)['tasks'][0]['offer_id']
        with patch('queue_unified.time.time', return_value=self.now+599):
            self.queue.action(self.user,job,'claim',offer)
        with patch('queue_unified.time.time', return_value=self.now+1200):
            row=self.queue.view(self.user)['tasks'][0]
            self.assertEqual(row['state'],'active')
            self.assertEqual(row['missed_claims'],0)

    def test_all_dual_totals_split_evenly_and_release_both_nodes(self):
        with patch('queue_unified.time.time', return_value=self.now):
            for total in (2, 4, 8, 16):
                with self.subTest(total=total):
                    job = self.add(machine='dual', cards=total)
                    row = self.queue.view(self.user)['tasks'][0]
                    self.assertEqual(row['total_cards'], total)
                    self.assertEqual(row['cards'], total // 2)
                    self.assertEqual(set(row['allocations']), {'a', 'b'})
                    self.assertTrue(all(len(cards) == total // 2 for cards in row['allocations'].values()))
                    self.queue.action(self.user, job, 'claim')
                    self.queue.action(self.user, job, 'finish')
                    self.assertEqual(self.queue.view(self.user)['tasks'], [])

    def test_rejects_unknown_machine_and_invalid_sizes(self):
        with patch('queue_unified.time.time', return_value=self.now):
            for data in ({'machine':'unknown'}, {'machine':[]}, {'machine':'a','cards':9},
                         {'machine':'dual','cards':3}, {'machine':'dual','cards':1},
                         {'machine':'dual','cards':True}, {'machine':'dual','cards':32}):
                with self.subTest(data=data), self.assertRaises(ControlError): self.add(**data)

    def test_old_dual_payload_and_rows_keep_sixteen_card_meaning(self):
        with patch('queue_unified.time.time', return_value=self.now):
            job = self.add(dual=True, cards=8)
            row = self.queue.view(self.user)['tasks'][0]
            self.assertEqual(row['id'], job)
            self.assertEqual(row['total_cards'], 16)
            self.assertEqual([len(v) for v in row['allocations'].values()], [8, 8])

    def test_dual_planning_avoids_busy_cards_and_keeps_four_card_groups(self):
        nodes = {'a': list(range(8)), 'b': list(range(8))}
        busy = [{'node':'a','cards':[0,1,2,3],'start':0,'end':None},
                {'node':'b','cards':[4,5,6,7],'start':0,'end':None}]
        jobs = [{'id':'dual','cards':2,'dual':True,'seconds':300}]
        row = plan(nodes, busy, jobs, 1000)[0]
        self.assertEqual(row['start'], 1000)
        self.assertTrue(set(row['allocations']['a']) <= {4,5,6,7})
        self.assertTrue(set(row['allocations']['b']) <= {0,1,2,3})
        jobs[0]['cards'] = 8
        self.assertIsNone(plan(nodes, busy, jobs, 1000)[0]['start'])

    def test_two_small_dual_jobs_do_not_overlap(self):
        nodes = {'a': list(range(8)), 'b': list(range(8))}
        jobs = [{'id':str(i),'cards':2,'dual':True,'seconds':300} for i in range(2)]
        first, second = plan(nodes, [], jobs, 1000)
        self.assertEqual([first['start'], second['start']], [1000, 1000])
        for node in nodes:
            self.assertFalse(set(first['allocations'][node]) & set(second['allocations'][node]))


if __name__ == '__main__': unittest.main()
