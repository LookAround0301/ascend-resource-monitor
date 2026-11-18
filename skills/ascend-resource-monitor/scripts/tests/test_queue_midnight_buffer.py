import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'assets/runtime'))
from queue_planner import plan


class MidnightBufferTests(unittest.TestCase):
    def setUp(self):
        self.nodes = {'A5-' + str(n): list(range(8))
                      for n in (179, 183, 187, 191, 220, 224, 228, 232)}
        self.midnight = 24 * 3600
        self.release = 21 * 3600 + 47 * 60 + 42.33133
        self.busy = []
        for node, cards in self.nodes.items():
            self.busy.extend([
                dict(node=node, cards=cards, start=19*3600, end=self.release),
                dict(node=node, cards=cards, start=self.midnight,
                     end=self.midnight+9*3600, fixed_night=True)])

    def forecast(self, third_seconds):
        normal = dict(cards=8, node_count=4, start_before=self.midnight,
                      clip_finish_before=self.midnight, midnight_separation=True)
        jobs = [dict(normal, id='first', seconds=3600),
                dict(normal, id='second', seconds=14400,
                     allowed_nodes=list(self.nodes)[4:]),
                dict(normal, id='third', seconds=third_seconds)]
        return plan(self.nodes, self.busy, jobs, 20*3600, buffer_seconds=600)

    def test_third_four_node_job_fits_when_only_buffer_crosses_midnight(self):
        first, second, third = self.forecast(3600)
        self.assertEqual(third['start'], self.release+3600+600)
        self.assertEqual(third['seconds'], 3600)
        self.assertEqual(third['allocations'], first['allocations'])
        self.assertTrue(set(third['allocations']).isdisjoint(second['allocations']))

    def test_third_four_node_job_is_truncated_when_duration_crosses_midnight(self):
        first, second, third = self.forecast(7200)
        self.assertEqual(third['start'], self.release+3600+600)
        self.assertEqual(third['seconds'], math.floor(self.midnight-third['start']))
        self.assertEqual(third['allocations'], first['allocations'])
        self.assertLessEqual(third['start']+third['seconds'], self.midnight)
        self.assertLess(self.midnight-third['start']-third['seconds'], 1)

    def test_midday_handoff_still_has_ten_minute_buffer(self):
        normal = dict(cards=8, allowed_nodes=list(self.nodes)[:1],
                      clip_finish_before=self.midnight, midnight_separation=True)
        result = plan(self.nodes, [], [dict(normal, id='one', seconds=3600),
                                     dict(normal, id='two', seconds=3600)], 16*3600)
        self.assertEqual(result[1]['start'], 17*3600+600)

    def test_other_pools_keep_full_buffer(self):
        job = dict(id='ordinary', cards=8, node_count=8, seconds=300,
                   start_before=self.midnight)
        nights = [b for b in self.busy if b.get('fixed_night')]
        self.assertIsNone(plan(self.nodes, nights, [job], self.midnight-600)[0]['start'])

    def test_less_than_one_minute_before_midnight_is_not_offered(self):
        job = dict(id='ordinary', cards=8, seconds=3600,
                   start_before=self.midnight, clip_finish_before=self.midnight,
                   midnight_separation=True)
        self.assertIsNone(plan(self.nodes, [], [job], self.midnight-59.5)[0]['start'])


if __name__ == '__main__':
    unittest.main()
