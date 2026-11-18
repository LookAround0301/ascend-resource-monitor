from pathlib import Path
import sys,tempfile,unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'assets/runtime'))
from queue_planner import plan
from queue_night_policy import reserve_night
from queue_store import QueueStore
from queue_unified import UnifiedQueue
from control_common import ControlError
from personnel_roster import personnel

class MultiNodeTests(unittest.TestCase):
    def setUp(self):
        self.nodes={f'n{i}':list(range(8)) for i in range(8)}

    def test_all_64_cards_and_no_overlap(self):
        result=plan(self.nodes,[],[{'id':'a','node_count':8,'cards':8,'seconds':60},
                                  {'id':'b','node_count':4,'cards':8,'seconds':60}],1000,10)
        self.assertEqual(len(result[0]['allocations']),8)
        self.assertEqual(sum(map(len,result[0]['allocations'].values())),64)
        self.assertEqual(result[1]['start'],1070)
        self.assertEqual(len(result[1]['allocations']),4)

    def test_dual_selects_two_free_nodes_from_pool(self):
        busy=[{'node':f'n{i}','cards':list(range(8)),'start':1,'end':None} for i in range(6)]
        result=plan(self.nodes,busy,[{'id':'a','dual':True,'cards':8,'seconds':60}],1000)
        self.assertEqual(set(result[0]['allocations']),{'n6','n7'})

    def test_night_uses_four_nodes_and_half_open_interval(self):
        first=reserve_night(self.nodes,[],1000,60,8,node_count=4)
        second=reserve_night(self.nodes,[{'start':1000,'end':1060,'allocations':first}],1000,60,8,node_count=4)
        self.assertFalse(set(first)&set(second))
        self.assertIsNone(reserve_night(self.nodes,[{'start':1000,'end':1060,'allocations':first}],1000,60,8,node_count=8))
        self.assertEqual(len(reserve_night(self.nodes,[{'start':1000,'end':1060,'allocations':first}],1060,60,8,node_count=8)),8)

    def test_persistence_allowed_counts_and_two_hour_limit(self):
        with tempfile.TemporaryDirectory() as t:
            store=QueueStore(Path(t)/'queue.db');user=store.user(store.register('u12345678','Fixture','fixture-password'))
            queue=UnifiedQueue(store,self.nodes,timezone='UTC')
            request={'machine':'multi:8','cards':64,'requested_seconds':7200,'notifications_ready':True}
            with patch('queue_unified.time.time',return_value=1794910000):
                job=queue.enqueue(user,request)
                row=next(x for x in queue.view(user)['tasks'] if x['id']==job)
                self.assertEqual((row['node_count'],row['total_cards'],len(row['allocations'])),(8,64,8))
                restored=UnifiedQueue(store,self.nodes,timezone='UTC')
                row=next(x for x in restored.view(user)['tasks'] if x['id']==job)
                self.assertEqual(row['machine'],'multi:8')
                for count in (3,5,6,7,9):
                    with self.assertRaises(ControlError):queue.enqueue(user,{**request,'machine':f'multi:{count}'})
                with self.assertRaises(ControlError):queue.enqueue(user,{**request,'requested_seconds':7260})

    def test_service_account_roster(self):
        self.assertEqual(personnel('p_fixture_bot','Fixture'),('p_fixture_bot','Fixture'))
        for account in ('p_','p_../x','p_name space'):
            with self.assertRaises(ControlError):personnel(account,'Fixture')

if __name__=='__main__':unittest.main()
