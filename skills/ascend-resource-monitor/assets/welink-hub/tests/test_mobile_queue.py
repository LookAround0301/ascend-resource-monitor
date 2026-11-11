import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import os
import secrets
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(Path(os.environ.get('QUEUE_TEST_RUNTIME',ROOT.parent/'runtime'))))
sys.path.insert(0,str(ROOT.parent/'runtime'))
from control_common import ControlError
from queue_store import QueueStore
from queue_unified import UnifiedQueue
from queue_mobile import command


class MobileQueueTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.now=1794394800.0
        self.clock=patch('time.time',return_value=self.now);self.clock.start()
        self.store=QueueStore(Path(self.folder.name)/'queue.db')
        for i in range(1,5):self.store.login_without_password('a'+str(i).zfill(8),'User'+str(i))
        with self.store.db() as db:self.accounts={r['employee']:r['id'] for r in db.execute('SELECT * FROM accounts')}
        self.queue=UnifiedQueue(self.store,['A5-118','A5-124'],strict_nights=True,
            overtime_control=True,normal_priority=True,night_carryover_cutoff_hour=9.5)

    def tearDown(self):self.clock.stop();self.folder.cleanup()

    def task(self,employee='a00000001',state='active',node='A5-118',allowed=()):
        job=secrets.token_hex(16)
        with self.store.db() as db:
            order=db.execute('SELECT count(*) FROM unified_tasks').fetchone()[0]+1
            db.execute('''INSERT INTO unified_tasks (id,account,title,state,created,activated,
                requested_seconds,cards,fixed_node,allocations,queue_order,allowed_employees)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',(job,self.accounts[employee],'',state,self.now-7200,
                self.now-1800 if state=='active' else None,3600,8,node,
                json.dumps({node:list(range(8))}) if state=='active' else '{}',order,json.dumps(allowed)))
        return job

    def preview(self,job,action='release',employee='a00000001'):
        return command(self.queue,self.store,dict(mode='preview',employee=employee,job=job,action=action))

    def execution(self,preview,action='release',employee='a00000001'):
        return dict(mode='execute',employee=employee,job=preview['job'],action=action,
                    version=preview['version'],operation=secrets.token_hex(16),expires=self.now+300)

    def tasks(self):
        with self.store.db() as db:return [dict(r) for r in db.execute('SELECT * FROM unified_tasks ORDER BY queue_order,rowid')]

    def test_release_uses_existing_finish_and_duplicate_is_idempotent(self):
        job=self.task();p=self.preview(job)
        self.assertEqual(len(self.tasks()),1)
        execution=self.execution(p);first=command(self.queue,self.store,execution)
        self.assertEqual(self.tasks(),[])
        self.assertIn('不会因此停止',first['message'])
        self.assertEqual(command(self.queue,self.store,execution),first)

    def test_couser_can_finish_active_but_not_cancel_queued_or_defer(self):
        job=self.task(allowed=['a00000002'])
        p=self.preview(job,employee='a00000002')
        command(self.queue,self.store,self.execution(p,employee='a00000002'))
        job=self.task(state='queued',allowed=['a00000002'])
        for action in ['release','defer']:
            with self.assertRaises(ControlError) as e:self.preview(job,action,employee='a00000002')
            self.assertEqual(e.exception.status,403)

    def test_unauthorized_identity_and_removed_couser_cannot_execute(self):
        job=self.task(allowed=['a00000002'])
        with self.assertRaises(ControlError):self.preview(job,employee='a00000003')
        p=self.preview(job,employee='a00000002')
        with self.store.db() as db:db.execute("UPDATE unified_tasks SET allowed_employees='[]' WHERE id=?",(job,))
        with self.assertRaises(ControlError):command(self.queue,self.store,self.execution(p,employee='a00000002'))
        self.assertEqual(len(self.tasks()),1)

    def test_task_changes_after_preview_require_new_confirmation(self):
        job=self.task();p=self.preview(job)
        with self.store.db() as db:db.execute('UPDATE unified_tasks SET requested_seconds=4000 WHERE id=?',(job,))
        with self.assertRaises(ControlError) as e:command(self.queue,self.store,self.execution(p))
        self.assertEqual(e.exception.status,409)
        self.assertEqual(len(self.tasks()),1)

    def test_defer_exactly_one_and_replay_does_not_defer_again(self):
        self.task(employee='a00000004')
        first=self.task(state='queued');second=self.task(employee='a00000002',state='queued')
        third=self.task(employee='a00000003',state='queued')
        p=self.preview(first,'defer');execution=self.execution(p,'defer')
        command(self.queue,self.store,execution)
        self.assertEqual([r['id'] for r in self.tasks() if r['state']=='queued'],[second,first,third])
        command(self.queue,self.store,execution)
        self.assertEqual([r['id'] for r in self.tasks() if r['state']=='queued'],[second,first,third])

    def test_neighbor_change_invalidates_defer_and_urgent_and_last_rejected(self):
        self.task(employee='a00000004')
        first=self.task(state='queued');second=self.task(employee='a00000002',state='queued')
        self.task(employee='a00000003',state='queued')
        p=self.preview(first,'defer')
        with self.store.db() as db:db.execute('DELETE FROM unified_tasks WHERE id=?',(second,))
        with self.assertRaises(ControlError):command(self.queue,self.store,self.execution(p,'defer'))
        with self.store.db() as db:db.execute('UPDATE unified_tasks SET urgent=1 WHERE id=?',(first,))
        with self.assertRaises(ControlError):self.preview(first,'defer')

    def test_ready_cancel_and_defer_use_exact_current_offer(self):
        first=self.task(state='queued');second=self.task(employee='a00000002',state='queued')
        p=self.preview(first,'defer')
        self.assertEqual(p['state'],'ready')
        command(self.queue,self.store,self.execution(p,'defer'))
        states={r['id']:r['state'] for r in self.tasks()}
        self.assertEqual(states[first],'queued');self.assertEqual(states[second],'ready')
        p=self.preview(first);command(self.queue,self.store,self.execution(p))
        self.assertNotIn(first,{r['id'] for r in self.tasks()})

    def test_concurrent_same_confirmation_executes_once(self):
        p=self.preview(self.task());execution=self.execution(p)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:command(self.queue,self.store,execution),range(2)))
        self.assertEqual(results[0],results[1]);self.assertEqual(self.tasks(),[])
        with self.store.db() as db:self.assertEqual(db.execute('SELECT count(*) FROM welink_command_receipts').fetchone()[0],1)

    def test_expired_confirmation_and_extra_fields_rejected(self):
        p=self.preview(self.task());execution=self.execution(p);execution['expires']=self.now-1
        with self.assertRaises(ControlError):command(self.queue,self.store,execution)
        execution['admin']=True
        with self.assertRaises(ControlError):command(self.queue,self.store,execution)
        self.assertEqual(len(self.tasks()),1)


if __name__=='__main__':unittest.main()
