"""Explicit machine selections survive scheduling, contention and reloads."""
from datetime import datetime, timezone
from pathlib import Path
import sys, tempfile, unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'assets/runtime'))
from queue_planner import plan
from queue_night_policy import reserve_night
from queue_store import QueueStore
from queue_unified import UnifiedQueue
from control_common import ControlError


class SelectedNodeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store = QueueStore(Path(self.temp.name) / 'queue.db')
        self.user = self.store.user(self.store.register('u12345678', 'Fixture', 'fixture-password'))
        self.nodes = {f'n{i}': list(range(8)) for i in range(8)}
        self.now = datetime(2026, 11, 17, 12, tzinfo=timezone.utc).timestamp()
        clock = patch('queue_unified.time.time', return_value=self.now)
        clock.start(); self.addCleanup(clock.stop)
        self.queue = UnifiedQueue(self.store, self.nodes)

    def add(self, **data):
        return self.queue.enqueue(self.user, {'machine': 'selected', 'selected_nodes': ['n1','n6','n7'],
                    'cards': 24, 'requested_seconds': 600, 'notifications_ready': True, **data})

    def test_exact_three_machines_persist_without_changing_existing_task(self):
        old = self.queue.enqueue(self.user, {'machine':'n0','cards':8,'requested_seconds':7200,'notifications_ready':True})
        self.queue.action(self.user, old, 'claim')
        before = next(r for r in self.queue.view(self.user)['tasks'] if r['id']==old)
        job = self.add()
        reopened = UnifiedQueue(self.store, self.nodes)
        rows = {r['id']:r for r in reopened.view(self.user)['tasks']}
        row = rows[job]
        self.assertEqual(row['selected_nodes'], ['n1','n6','n7'])
        self.assertEqual((row['machine'],row['node_count'],row['total_cards']), ('selected',3,24))
        self.assertEqual(row['allocations'], {node:list(range(8)) for node in row['selected_nodes']})
        for key in ('state','allocations','activated','requested_seconds'):
            self.assertEqual(rows[old][key],before[key])

    def test_busy_selected_machine_waits_instead_of_substituting(self):
        old = self.queue.enqueue(self.user, {'machine':'n6','cards':8,'requested_seconds':600,'notifications_ready':True})
        self.queue.action(self.user, old, 'claim')
        job = self.add()
        row = next(r for r in self.queue.view(self.user)['tasks'] if r['id']==job)
        self.assertEqual(row['state'],'queued')
        self.assertEqual(set(row['planned_allocations']), {'n1','n6','n7'})
        self.queue.action(self.user, old, 'finish')
        row = next(r for r in self.queue.view(self.user)['tasks'] if r['id']==job)
        self.assertEqual(set(row['allocations']), {'n1','n6','n7'})

    def test_single_and_all_eight_machine_selections(self):
        for selected in (['n7'], list(self.nodes)):
            job = self.add(selected_nodes=selected, cards=8*len(selected))
            row = next(r for r in self.queue.view(self.user)['tasks'] if r['id']==job)
            self.assertEqual(set(row['allocations']), set(selected))
            self.queue.action(self.user, job, 'cancel')

    def test_invalid_machine_selections_and_card_totals_are_rejected(self):
        for changes in ({'selected_nodes':[]},{'selected_nodes':['n1','n1']},
                {'selected_nodes':['missing']},{'selected_nodes':['n1',{}]},
                {'selected_nodes':None},{'selected_nodes':'n1'}, {'machine':'auto'},
                {'cards':4},{'cards':True},{'requested_seconds':7260}):
            with self.subTest(changes=changes), self.assertRaises(ControlError): self.add(**changes)
        with self.assertRaises(ControlError):
            self.queue.enqueue(self.user, {'machine':'selected','cards':1,'requested_seconds':600,'notifications_ready':True})

    def test_both_night_policies_keep_selected_machines(self):
        start = self.now + 8*3600
        for strict in (False,True):
            self.queue = UnifiedQueue(self.store,self.nodes,strict_nights=strict)
            job = self.add(night=True, not_before=start)
            row = next(r for r in self.queue.view(self.user)['tasks'] if r['id']==job)
            self.assertEqual(set(row['allocations'] or row['planned_allocations']), {'n1','n6','n7'})
            self.queue.action(self.user,job,'cancel')

    def test_fixed_night_conflict_cannot_use_other_free_nodes(self):
        selected = ['n1','n6','n7']
        reserved = [{'start':1000,'end':2000,'allocations':{'n6':list(range(8))}}]
        self.assertIsNone(reserve_night(self.nodes,reserved,1000,600,8,node_count=3,selected_nodes=selected))
        self.assertEqual(set(reserve_night(self.nodes,reserved,2000,600,8,node_count=3,selected_nodes=selected)),set(selected))

    def test_planner_does_not_protect_unselected_nodes(self):
        busy = [{'node':'n1','cards':list(range(8)),'start':0,'end':None}]
        jobs = [{'id':'selected','cards':8,'node_count':2,'dual':True,'allowed_nodes':['n1','n6'],'seconds':60,'start_before':2000},
                {'id':'other','cards':8,'allowed_nodes':['n0'],'seconds':60}]
        result = plan(self.nodes,busy,jobs,1000)
        self.assertIsNone(result[0]['start'])
        self.assertEqual(result[1]['allocations'], {'n0':list(range(8))})
        self.assertEqual(result[1]['start'],1000)


if __name__ == '__main__': unittest.main()
