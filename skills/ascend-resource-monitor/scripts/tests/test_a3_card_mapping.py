"""Verify chip mapping and safe empty-card decisions using isolated inventories."""
import unittest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'assets/runtime'))
from queue_monitor import QueueMonitor
from test_queue_night_autofinish import inventory

class A3CardMappingTests(unittest.TestCase):
    def setUp(self):
        self.now=1000000
        self.map={node:{str(chip):chip//2 for chip in range(16)} for node in ['a','b']}
        self.monitor=QueueMonitor(['a','b'],{'metric_prefix':'a5','npu_card_map':self.map},None)

    def task(self):
        return {'id':'night','allocations':{'a':[7]},'employee':'u12345678','allowed_employees':[],
                'activated':self.now-100,'requested_seconds':60,'night_seen_usage':1}

    def test_both_chips_map_to_the_same_physical_card(self):
        self.monitor.ingest(inventory(self.now,[{'node':'a','cards':'0,1,14,15'}]),self.now)
        self.assertEqual(self.monitor.current(self.now)[0]['npu_ids'],'0,7')

    def test_any_chip_on_the_allocated_card_prevents_auto_finish(self):
        for chip in ['14','15']:
            for offset in [0,10,20,30]:
                now=self.now+offset
                self.monitor.ingest(inventory(now,[{'node':'a','cards':chip}]),now)
                observed,finished=self.monitor.night_completion_decisions([self.task()],now)
                self.assertIn('night',observed);self.assertFalse(finished)

    def test_unknown_chip_cannot_be_treated_as_an_empty_card(self):
        for offset in [0,10,20,30]:
            now=self.now+offset
            self.monitor.ingest(inventory(now,[{'node':'a','cards':'16'}]),now)
            self.assertIsNone(self.monitor.cards(self.monitor.current(now)[0]))
            self.assertFalse(self.monitor.night_completion_decisions([self.task()],now)[1])

    def test_empty_fresh_inventories_release_after_three_samples(self):
        for offset in [0,10,20]:
            now=self.now+offset;self.monitor.ingest(inventory(now,[]),now)
            finished=self.monitor.night_completion_decisions([self.task()],now)[1]
            self.assertEqual(finished,{'night'} if offset==20 else set())

    def test_invalid_or_incomplete_mapping_is_rejected(self):
        for value in [{'a':self.map['a']},{'a':{'0':8},'b':{'0':0}},{'a':{'0':True},'b':{'0':0}}]:
            with self.assertRaises(ValueError):QueueMonitor(['a','b'],{'npu_card_map':value},None)

if __name__=='__main__':unittest.main()
