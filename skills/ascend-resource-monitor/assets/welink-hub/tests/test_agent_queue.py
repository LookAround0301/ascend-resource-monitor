import copy
import os
import secrets
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(Path(os.environ.get('QUEUE_TEST_RUNTIME',ROOT.parent/'runtime'))))
from control_common import ControlError
from queue_store import QueueStore
from queue_unified import UnifiedQueue
from queue_agent import command,receipt

class AgentQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.now=1794394800.0
        self.clock=patch('time.time',return_value=self.now);self.clock.start()
        self.store=QueueStore(Path(self.temp.name)/'queue.db')
        self.store.login_without_password('a00000001','User One')
        self.queue=UnifiedQueue(self.store,['A5-118','A5-124'],strict_nights=True,
                                overtime_control=True,normal_priority=True,timezone='Asia/Shanghai',night_carryover_cutoff_hour=9.5)
        self.request=dict(machine='auto',cards=1,night=False,not_before=0,requested_seconds=1800,
                          avoid_slots=[],backfill_min_seconds=0)
    def tearDown(self):self.clock.stop();self.temp.cleanup()
    def payload(self,mode='agent_preview'):
        data=dict(mode=mode,employee='a00000001',request=copy.deepcopy(self.request))
        if mode=='agent_execute':data.update(operation=secrets.token_hex(16),expires=self.now+300)
        return data
    def count(self):
        with self.store.db() as db:return db.execute('SELECT count(*) FROM unified_tasks').fetchone()[0]
    def test_preview_rolls_back_and_duplicate_execution_is_idempotent(self):
        preview=command(self.queue,self.store,self.payload());self.assertEqual(preview['effective_seconds'],1800)
        self.assertEqual(self.count(),0)
        payload=self.payload('agent_execute');first=command(self.queue,self.store,payload)
        self.assertEqual(command(self.queue,self.store,payload),first);self.assertEqual(self.count(),1)
        self.assertEqual(receipt(self.store,'a00000001',payload['operation']),first)
        with self.assertRaises(ControlError):receipt(self.store,'a00000002',payload['operation'])
        changed=copy.deepcopy(payload);changed['request']['cards']=2
        with self.assertRaises(ControlError):command(self.queue,self.store,changed)
        self.assertEqual(self.count(),1)
    def test_concurrent_same_operation_creates_one(self):
        payload=self.payload('agent_execute')
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:command(self.queue,self.store,payload),range(2)))
        self.assertEqual(results[0],results[1]);self.assertEqual(self.count(),1)
    def test_rejects_invalid_identity_unconfirmed_expired_and_urgent(self):
        invalid=[]
        p=self.payload();p['employee']='a00000002';invalid.append(p)
        p=self.payload();p['request']['notifications_ready']=True;invalid.append(p)
        p=self.payload();p['request']['urgent']=True;invalid.append(p)
        p=self.payload('agent_execute');p['expires']=self.now-1;invalid.append(p)
        p=self.payload();p['request']['admin']=True;invalid.append(p)
        for p in invalid:
            with self.subTest(p=p),self.assertRaises(ControlError):command(self.queue,self.store,p)
        self.assertEqual(self.count(),0)

if __name__=='__main__':unittest.main()
