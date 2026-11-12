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
from queue_agent import command,receipt,action,initialize_events,list_events,record_due_events

class AgentQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.now=1794394800.0
        self.clock=patch('time.time',return_value=self.now);self.clock.start()
        self.store=QueueStore(Path(self.temp.name)/'queue.db')
        self.store.login_without_password('a00000001','User One')
        self.queue=UnifiedQueue(self.store,['A5-118','A5-124'],strict_nights=True,
                                overtime_control=True,normal_priority=True,timezone='Asia/Shanghai',night_carryover_cutoff_hour=9.5)
        initialize_events(self.store)
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

    def test_claim_extend_finish_are_idempotent_and_bound_to_owner(self):
        created=command(self.queue,self.store,self.payload('agent_execute'))
        with self.store.db() as db:row=db.execute('SELECT * FROM unified_tasks WHERE id=?',(created['job'],)).fetchone()
        self.assertEqual(row['state'],'ready');job=created['job'];offer=row['offer_id']
        op=secrets.token_hex(16)
        first=action(self.queue,self.store,'a00000001',op,job,'claim',offer)
        self.assertEqual(first['reservation']['state'],'active')
        self.assertEqual(action(self.queue,self.store,'a00000001',op,job,'claim',offer),first)
        with self.assertRaises(ControlError):action(self.queue,self.store,'a00000001',op,job,'claim','other')
        self.store.login_without_password('a00000002','User Two')
        with self.assertRaises(ControlError):action(self.queue,self.store,'a00000002',secrets.token_hex(16),job,'finish')
        with patch('time.time',return_value=self.now+1801):
            extid=secrets.token_hex(16)
            extended=action(self.queue,self.store,'a00000001',extid,job,'extend',minutes=10)
            self.assertEqual(action(self.queue,self.store,'a00000001',extid,job,'extend',minutes=10),extended)
            with self.store.db() as db:self.assertEqual(db.execute('SELECT extension_count FROM unified_tasks WHERE id=?',(job,)).fetchone()[0],1)
        finalid=secrets.token_hex(16);finished=action(self.queue,self.store,'a00000001',finalid,job,'finish')
        self.assertEqual(action(self.queue,self.store,'a00000001',finalid,job,'finish'),finished)
        self.assertEqual(self.count(),0)

    def test_queue_transitions_are_persistent_scoped_and_preview_is_rolled_back(self):
        command(self.queue,self.store,self.payload())
        with self.store.db() as db:account=dict(db.execute('SELECT id,employee FROM accounts WHERE employee=?',('a00000001',)).fetchone())
        self.assertEqual(list_events(self.store,account,0),[])
        created=command(self.queue,self.store,self.payload('agent_execute'));job=created['job']
        events=list_events(self.store,account,0,job)
        self.assertEqual(len(events),1);self.assertEqual(events[0]['state'],'ready')
        self.assertTrue(events[0]['allocations']);seq=events[0]['seq']
        self.assertEqual(list_events(self.store,{'id':'other','employee':'a00000002'},0),[])
        action(self.queue,self.store,'a00000001',secrets.token_hex(16),job,'claim',events[0]['offer_id'])
        events=list_events(self.store,account,seq,job)
        self.assertEqual(len(events),1);self.assertEqual(events[0]['state'],'active')
        # SQLite's clock is independent of the mocked Python clock.
        with self.store.db() as db:
            db.execute("UPDATE unified_tasks SET activated=(julianday('now')-2440587.5)*86400.0 WHERE id=?",(job,))
        action(self.queue,self.store,'a00000001',secrets.token_hex(16),job,'finish')
        self.assertEqual(len(list_events(self.store,account,0,job)),2)

    def test_due_event_once_per_deadline_and_after_extension(self):
        job=command(self.queue,self.store,self.payload('agent_execute'))['job']
        with self.store.db() as db:
            row=db.execute('SELECT offer_id FROM unified_tasks WHERE id=?',(job,)).fetchone()
            account=dict(db.execute('SELECT id,employee FROM accounts WHERE employee=?',('a00000001',)).fetchone())
        action(self.queue,self.store,'a00000001',secrets.token_hex(16),job,'claim',row['offer_id'])
        record_due_events(self.store,self.now+1799)
        self.assertEqual([e['state'] for e in list_events(self.store,account,0,job)],['ready','active'])
        record_due_events(self.store,self.now+1801);record_due_events(self.store,self.now+1802)
        self.assertEqual([e['state'] for e in list_events(self.store,account,0,job)],['ready','active','due'])
        with patch('time.time',return_value=self.now+1801):
            action(self.queue,self.store,'a00000001',secrets.token_hex(16),job,'extend',minutes=10)
        record_due_events(self.store,self.now+2400)
        self.assertEqual(len(list_events(self.store,account,0,job)),3)
        record_due_events(self.store,self.now+2402);record_due_events(self.store,self.now+2403)
        self.assertEqual([e['state'] for e in list_events(self.store,account,0,job)],['ready','active','due','due'])

    def test_expired_row_deletion_records_due_before_cleanup(self):
        job=command(self.queue,self.store,self.payload('agent_execute'))['job']
        with self.store.db() as db:
            account=dict(db.execute('SELECT id,employee FROM accounts WHERE employee=?',('a00000001',)).fetchone())
            db.execute("UPDATE unified_tasks SET state='active',activated=1,requested_seconds=60 WHERE id=?",(job,))
            db.execute('DELETE FROM unified_tasks WHERE id=?',(job,))
        self.assertEqual([e['state'] for e in list_events(self.store,account,0,job)],['ready','active','due'])

if __name__=='__main__':unittest.main()
