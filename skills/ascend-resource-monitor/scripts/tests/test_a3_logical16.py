"""A3 logical-card scheduling, ownership checks and migration regressions."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]/'assets/runtime'))
from control_common import ControlError
from queue_store import QueueStore
from queue_unified import UnifiedQueue
from queue_planner import plan
from queue_monitor import QueueMonitor
from queue_a3_migration import migrate
from test_queue_night_autofinish import inventory, stamp


class Logical16Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'queue.db'
        self.store = QueueStore(self.path)
        self.user = self.store.user(self.store.register('u12345678', 'Fixture', 'fixture-password'))
        self.now = stamp('2026-11-05T14:00:00')
        self.clock = patch('queue_unified.time.time', return_value=self.now)
        self.clock.start(); self.addCleanup(self.clock.stop)
        self.nodes = {'a': list(range(16)), 'b': list(range(16))}

    def add(self, queue, **data):
        return queue.enqueue(self.user, {'machine':'auto', 'cards':1, 'cards_per_node':16,
            'requested_seconds':600, 'notifications_ready':True, **data})

    def test_single16_and_dual32_and_capacity_response(self):
        queue = UnifiedQueue(self.store, self.nodes, cards_per_node=16)
        single = self.add(queue, machine='a', cards=16)
        state = queue.view(self.user)
        self.assertEqual(state['cards_per_node'],16)
        self.assertEqual(state['tasks'][0]['allocations'], {'a':list(range(16))})
        queue.action(self.user,single,'cancel')
        dual = self.add(queue,machine='dual',cards=32)
        row=queue.view(self.user)['tasks'][0]
        self.assertEqual(row['id'],dual)
        self.assertEqual(row['allocations'],self.nodes)
        self.assertEqual(row['cards'],16)

    def test_new_counts_reject_out_of_range_and_stale_clients(self):
        queue = UnifiedQueue(self.store,self.nodes,cards_per_node=16)
        for data in [{'cards':17},{'machine':'dual','cards':64},{'machine':'dual','cards':30},
                     {'cards_per_node':8},{'cards_per_node':None},{'cards':True}]:
            with self.assertRaises(ControlError):self.add(queue,**data)
        with self.assertRaises(ControlError):
            queue.enqueue(self.user,{'cards':8,'requested_seconds':600,'notifications_ready':True})

    def test_full_pool_is_32_and_high_ids_are_allocatable_without_overlap(self):
        jobs=[{'id':str(i),'cards':1,'seconds':600} for i in range(33)]
        result=plan(self.nodes,[],jobs,0)
        initial=[r for r in result if r['start']==0]
        self.assertEqual(len(initial),32)
        occupied=[(node,card) for r in initial for node,cards in r['allocations'].items() for card in cards]
        self.assertEqual(len(set(occupied)),32)
        self.assertIn(('a',15),occupied);self.assertIn(('b',15),occupied)
        self.assertEqual(result[-1]['start'],1200)

    def test_upper_half_and_large_single_requests(self):
        busy=[{'node':'a','cards':list(range(8)),'start':0,'end':None}]
        result=plan(self.nodes,busy,[{'id':'upper','cards':8,'seconds':600,'allowed_nodes':['a']}],1)
        self.assertEqual(result[0]['allocations'],{'a':list(range(8,16))})
        for count in range(9,17):
            row=plan(self.nodes,[],[{'id':'x','cards':count,'seconds':60}],0)[0]
            self.assertEqual(next(iter(row['allocations'].values())),list(range(count)))

    def test_logical_ids_are_independent_and_high_card_usage_prevents_release(self):
        monitor=QueueMonitor(self.nodes,{'cards_per_node':16,'metric_prefix':'a5'},None)
        monitor.ingest(inventory(self.now,[{'node':'a','cards':'0,1,14,15'}]),self.now)
        self.assertEqual(monitor.cards(monitor.current(self.now)[0]),{0,1,14,15})
        task={'id':'night','allocations':{'a':[15]},'employee':'u12345678',
              'allowed_employees':[],'activated':self.now-100,'requested_seconds':60,'night_seen_usage':1}
        for offset in (0,10,20,30):
            now=self.now+offset
            monitor.ingest(inventory(now,[{'node':'a','cards':'15'}]),now)
            observed,finished=monitor.night_completion_decisions([task],now)
            self.assertIn('night',observed);self.assertFalse(finished)
        for offset in (40,50,60):
            now=self.now+offset
            monitor.ingest(inventory(now,[{'node':'a','cards':'14'}]),now)
            finished=monitor.night_completion_decisions([task],now)[1]
            self.assertEqual(finished,{'night'} if offset==60 else set())

    def test_invalid_ids_or_stale_source_never_release(self):
        monitor=QueueMonitor(self.nodes,{'cards_per_node':16,'metric_prefix':'a5'},None)
        task={'id':'night','allocations':{'a':[15]},'employee':'u12345678',
              'allowed_employees':[],'activated':self.now-100,'requested_seconds':60,'night_seen_usage':1}
        for raw in ('16','-1','15,16','oops',''):
            for offset in (0,10,20,30):
                now=self.now+offset
                monitor.ingest(inventory(now,[{'node':'a','cards':raw}]),now)
                self.assertIsNone(monitor.cards(monitor.current(now)[0]))
                self.assertFalse(monitor.night_completion_decisions([task],now)[1])
        for offset in (0,10,20,30):
            now=self.now+offset
            monitor.ingest(inventory(now,[],invalid_node='a'),now)
            self.assertFalse(monitor.night_completion_decisions([task],now)[1])

    def test_migration_expands_all_states_once_and_preserves_auth_and_timing(self):
        old=UnifiedQueue(self.store,self.nodes)
        first=self.add(old,machine='a',cards=8)
        old.action(self.user,first,'claim')
        self.add(old,machine='b',cards=4)
        self.add(old,machine='dual',cards=16)
        with self.store.db() as db:
            db.execute('INSERT INTO unified_shared_continuing VALUES (?,?,?,?)',('a','v12345678',self.now,'[0,7]'))
            before={r['id']:dict(r) for r in db.execute('SELECT * FROM unified_tasks')}
            auth={table:[tuple(r) for r in db.execute('SELECT * FROM '+table)] for table in ('accounts','sessions')}
        with self.assertRaises(ValueError): UnifiedQueue(self.store,self.nodes,cards_per_node=16)
        self.assertEqual(migrate(self.path,self.nodes,source_layout='a3-paired8'),{'changed':True,'tasks':3,'shared_grants':1})
        self.assertFalse(migrate(self.path,self.nodes,source_layout='a3-paired8')['changed'])
        with self.store.db() as db:
            after={r['id']:dict(r) for r in db.execute('SELECT * FROM unified_tasks')}
            for key,original in before.items():
                current=after[key]
                self.assertEqual(current['cards'],2*original['cards'])
                expected={node:[n for c in cards for n in (2*c,2*c+1)] for node,cards in json.loads(original['allocations']).items()}
                self.assertEqual(json.loads(current['allocations']),expected)
                for column in set(original)-{'cards','allocations'}:self.assertEqual(current[column],original[column])
            self.assertEqual(json.loads(db.execute('SELECT cards FROM unified_shared_continuing').fetchone()[0]),[0,1,14,15])
            for table,rows in auth.items():self.assertEqual([tuple(r) for r in db.execute('SELECT * FROM '+table)],rows)
        new=UnifiedQueue(self.store,self.nodes,cards_per_node=16)
        self.assertEqual(new.view(self.user)['cards_per_node'],16)
        with self.assertRaises(ValueError):UnifiedQueue(self.store,self.nodes)

    def test_bad_migration_is_atomic(self):
        old=UnifiedQueue(self.store,self.nodes)
        first=self.add(old,machine='a',cards=1)
        bad=self.add(old,machine='b',cards=1)
        with self.store.db() as db:
            db.execute('UPDATE unified_tasks SET allocations=? WHERE id=?',('{"b":[8]}',bad))
        with self.assertRaises(ValueError):migrate(self.path,self.nodes,source_layout='a3-paired8')
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT cards FROM unified_tasks WHERE id=?',(first,)).fetchone()[0],1)
            self.assertEqual(db.execute("SELECT value FROM unified_meta WHERE key='cards_per_node'").fetchone()[0],'8')


if __name__=='__main__':unittest.main()
