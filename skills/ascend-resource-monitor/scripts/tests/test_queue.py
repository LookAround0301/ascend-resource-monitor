"""Reservation ownership, ordering and host safeguards; never touch live Docker."""
from pathlib import Path
import copy
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'assets/runtime'))
from control_common import ControlError
from queue_store import QueueStore
from queue_processes import QueueProcesses
from patch_dashboard import add_queue_link,add_stop_button,add_queue_panels
from queue_metrics import render_queue_metrics

CID='a'*64
START='2026-11-03T00:00:00.000000000Z'


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=QueueStore(Path(self.temp.name)/'queue.db')
        self.token=self.store.register('u12345678','One','test-only-password')
        self.one=self.store.user(self.token)
        self.two=self.store.user(self.store.register('u12345679','Two','another-test-password'))
        self.admin={'id':'admin','admin':True}

    def grant_job(self,user=None,cid=CID,node='a'):
        user=user or self.one
        return self.store.enqueue(user,node,cid,'test','test-container' if cid else '')

    def transition(self,user,job,action,processes=None):
        payload={'container_started_at':START,'processes':processes or []}
        token=self.store.confirmation(user,job,action,payload)
        return self.store.transition(user,job,action,token)

    def test_password_and_session_are_hashed(self):
        self.assertEqual(self.one,self.store.user(self.store.login('U12345678','test-only-password')))
        with self.store.db() as db:
            row=dict(db.execute('SELECT * FROM accounts WHERE id=?',(self.one['id'],)).fetchone())
            tokens=[r[0] for r in db.execute('SELECT token FROM sessions')]
        self.assertNotIn('test-only-password',str(row));self.assertNotIn(self.token,tokens)
        with self.assertRaises(ControlError):self.store.login('u12345678','wrong-password')
        self.store.logout(self.token);self.assertIsNone(self.store.user(self.token))

    def test_passwordless_login_preserves_existing_account_and_reservation(self):
        job=self.grant_job()
        with self.store.db() as db:
            credentials=tuple(db.execute('SELECT salt,digest FROM accounts WHERE id=?',(self.one['id'],)).fetchone())
        logged_in=self.store.user(self.store.login_without_password(' U12345678 ', ' One '))
        self.assertEqual(logged_in,self.one)
        self.assertEqual(self.store.own(logged_in,job)['id'],job)
        self.assertEqual(self.store.user(self.token),self.one)
        with self.store.db() as db:
            self.assertEqual(tuple(db.execute('SELECT salt,digest FROM accounts WHERE id=?',(self.one['id'],)).fetchone()),credentials)
        with self.assertRaises(ControlError):self.store.login_without_password('u12345678','Wrong name')

    def test_passwordless_first_login_creates_one_account_and_validates_input(self):
        first=self.store.user(self.store.login_without_password('u12345670','New user'))
        second=self.store.user(self.store.login_without_password('U12345670','New user'))
        self.assertEqual(first,second)
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM accounts WHERE employee=?',('u12345670',)).fetchone()[0],1)
        for employee,name in [('bad','Name'),('u12345671',''),('u12345671','Name\nx')]:
            with self.subTest(employee=employee,name=name),self.assertRaises(ControlError):
                self.store.login_without_password(employee,name)

    def test_self_service_container_cannot_be_taken_from_unfinished_task(self):
        self.grant_job()
        with self.assertRaises(ControlError):self.store.enqueue(self.two,'a',CID,'test')

    def test_unbound_registration_start_finish_and_separate_queues(self):
        first=self.grant_job(cid='');second=self.grant_job(self.two,cid='');other=self.grant_job(cid='',node='b')
        self.transition(self.one,first,'claim');self.transition(self.one,other,'claim')
        with self.assertRaises(ControlError):self.transition(self.two,second,'claim')
        self.transition(self.one,first,'finish');self.transition(self.two,second,'claim')
        self.assertIsNotNone(self.store.own(self.one,first)['finished'])
        self.assertEqual(self.store.own(self.one,other)['state'],'active')

    def test_late_binding_is_immutable_and_owner_scoped(self):
        job=self.grant_job(cid='');self.transition(self.one,job,'claim')
        payload={'container_id':CID,'container':'fixture','container_started_at':START,'processes':[]}
        with self.assertRaises(ControlError):self.store.confirmation(self.two,job,'bind',payload)
        token=self.store.confirmation(self.one,job,'bind',payload)
        self.store.transition(self.one,job,'bind',token)
        self.assertEqual(self.store.own(self.one,job)['container_id'],CID)
        token=self.store.confirmation(self.one,job,'bind',payload)
        with self.assertRaises(ControlError):self.store.transition(self.one,job,'bind',token)

    def test_old_database_index_migration_preserves_existing_reservation(self):
        job=self.grant_job()
        with self.store.db() as db:
            db.execute("CREATE UNIQUE INDEX container_pending ON reservations(node,container_id) WHERE state IN ('queued','active','cancelling')")
        migrated=QueueStore(self.store.path)
        self.assertEqual(migrated.own(self.one,job)['container_id'],CID)
        migrated.enqueue(self.one,'a','','first unbound');migrated.enqueue(self.two,'a','','second unbound')

    def test_requested_time_and_display_elapsed(self):
        job=self.store.enqueue(self.one,'a','','fixture',requested_seconds=5400)
        self.assertEqual(self.store.own(self.one,job)['requested_seconds'],5400)
        with self.assertRaises(ControlError):self.store.enqueue(self.one,'a','','fixture',requested_seconds=-1)
        row={'id':job,'node':'a','state':'active','employee':'u12345678','name':'Name','title':'test"\\\nvalue',
             'created':1000,'activated':2000,'requested_seconds':5400,'position':None}
        metrics=render_queue_metrics([row],['a','b'],2065)
        self.assertIn('} 65',metrics);self.assertIn('requested_duration="1.5 小时"',metrics)
        self.assertIn('queue_status="当前空闲"',metrics);self.assertNotIn('test"\\\nvalue',metrics)
        row['state']='queued';self.assertNotIn('} 65',render_queue_metrics([row],['a'],2065))
        row['state']='completed';self.assertNotIn('task_id="'+job+'"',render_queue_metrics([row],['a'],2065))
        self.assertNotIn('password',metrics)

    def test_queue_panels_are_separate_and_idempotent(self):
        dashboard={'uid':'lab','panels':[{'id':1,'title':'existing','gridPos':{'x':0,'y':4,'w':24,'h':6}}]}
        once=add_queue_panels(dashboard,['node-a','node-b']);twice=add_queue_panels(once,['node-a','node-b'])
        self.assertEqual(once['panels'],twice['panels']);self.assertEqual(len(twice['panels']),3)
        self.assertIn('node="node-a"',twice['panels'][1]['targets'][0]['expr'])
        self.assertIn('node="node-b"',twice['panels'][2]['targets'][0]['expr'])

    def test_fifo_per_node_and_single_active(self):
        first=self.grant_job();second=self.grant_job(self.two,'b'*64);other=self.grant_job(self.two,'c'*64,'b')
        with self.assertRaises(ControlError):self.transition(self.two,second,'claim')
        self.transition(self.one,first,'claim');self.transition(self.two,other,'claim')
        with self.assertRaises(ControlError):self.transition(self.two,second,'claim')
        self.transition(self.one,first,'finish');self.transition(self.two,second,'claim')

    def test_owner_admin_and_one_use_confirmation(self):
        job=self.grant_job()
        with self.assertRaises(ControlError):self.store.confirmation(self.two,job,'cancel',{})
        self.assertEqual(self.store.own(self.admin,job)['id'],job)
        token=self.store.confirmation(self.one,job,'cancel',{'processes':[]})
        self.store.transition(self.one,job,'cancel',token)
        with self.assertRaises(ControlError):self.store.transition(self.one,job,'cancel',token)
        with self.assertRaises(ControlError):self.store.consume_confirmation(token)

    def test_active_cancel_not_released_until_host_result(self):
        job=self.grant_job();self.transition(self.one,job,'claim')
        with self.assertRaises(ControlError):self.transition(self.one,job,'finish',[{'pid':123}])
        _,payload=self.transition(self.one,job,'cancel')
        self.assertEqual(len(payload['operation_id']),32)
        self.assertEqual(self.store.own(self.one,job)['state'],'cancelling')
        self.store.resolve_cancel(job,False,'still running')
        self.assertEqual(self.store.own(self.one,job)['state'],'active')

    def test_dashboard_queue_link_survives_stop_refresh(self):
        dashboard={'uid':'test','panels':[],'templating':{'list':[{'name':'node'}]}}
        result=add_stop_button(add_queue_link(dashboard))
        result=add_queue_link(result)
        self.assertEqual(len([x for x in result['links'] if x['url']=='/a5-queue']),1)
        self.assertEqual(result['templating'],dashboard['templating'])


class FakeDocker:
    def __init__(self):
        self.info={'Id':CID,'Name':'/test','Config':{'Labels':{}},'State':{'Running':True,'StartedAt':START}}
        self.stops=[]
    def inspect(self,cid):return copy.deepcopy(self.info)
    def pids(self,cid):return {101,102}
    def stop(self,cid):self.stops.append(cid);self.info['State']['Running']=False


class HostTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.events=[];self.docker=FakeDocker()
        class Engine:pass
        self.engine=Engine();self.engine.config={'task_queue':{'operations_dir':self.temp.name}}
        self.engine.docker=self.docker;self.engine.get_npu=lambda:{101,999}
        self.engine.write_audit=self.events.append;self.engine.lock=threading.Lock()
        self.host=QueueProcesses(self.engine)
        self.host.read_process=lambda pid,cid:{'pid':pid,'start_ticks':55,'name':'fixture'}

    def record(self,mode='process'):
        return {'request':{'operation_id':'e'*32,'snapshot':self.host.snapshot(CID),'mode':mode},'actor':{'id':'test'},'state':'pending'}

    def test_intersection_and_restart(self):
        snap=self.host.snapshot(CID)
        self.assertEqual([p['pid'] for p in snap['processes']],[101])
        self.docker.info['State']['StartedAt']='new'
        current=self.host.snapshot(CID,START)
        self.assertTrue(current['container_changed']);self.assertEqual(current['processes'],[])
        self.assertFalse(self.host.same(snap,current))

    def test_kubernetes_and_protected_refused(self):
        self.docker.info['Config']['Labels']={'io.kubernetes.pod.name':'pod'}
        with self.assertRaises(ControlError):self.host.snapshot(CID)
        self.docker.info['Config']['Labels']={};self.engine.config['protected_containers']=['test']
        with self.assertRaises(ControlError):self.host.snapshot(CID)

    def test_container_mode_and_recovery_never_signal(self):
        record=self.record('container');self.host.kill('e'*32,record)
        self.assertEqual(self.docker.stops,[CID]);self.assertEqual(record['state'],'succeeded')
        self.docker.info['State']['Running']=True;record=self.record('container')
        self.host.save('e'*32,record)
        self.assertEqual(self.host.status('e'*32)['state'],'failed')
        self.assertEqual(self.docker.stops,[CID])

    def test_pid_reuse_refused_before_any_signal(self):
        record=self.record();initial=self.host.read_process
        calls=iter([initial(101,CID),{'pid':101,'start_ticks':999,'name':'reused'}])
        self.host.read_process=lambda pid,cid:next(calls)
        with patch.object(os,'pidfd_open',return_value=900,create=True),patch.object(os,'close'),patch.object(signal,'pidfd_send_signal',create=True) as send:
            self.host.kill('e'*32,record)
        send.assert_not_called();self.assertEqual(record['state'],'failed');self.assertEqual(self.docker.stops,[])

    def test_stale_and_idempotent_requests(self):
        data=self.record()['request'];actor={'id':'test'}
        with patch.object(os,'pidfd_open',create=True),patch.object(signal,'pidfd_send_signal',create=True),patch('queue_processes.threading.Thread') as thread:
            self.host.submit(data,actor);self.host.submit(data,actor)
            self.assertEqual(thread.call_count,1)
            with self.assertRaises(ControlError):self.host.submit(data,{'id':'other'})
            data=copy.deepcopy(data);data['operation_id']='f'*32;data['snapshot']['processes'][0]['start_ticks']=777
            with self.assertRaises(ControlError):self.host.submit(data,actor)


@unittest.skipUnless(sys.platform=='linux' and hasattr(os,'pidfd_open'),'Linux PID handle test')
class LinuxProcessTests(unittest.TestCase):
    def test_only_owned_fixture_child_is_stopped(self):
        children=[subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']) for _ in range(2)]
        try:
            with tempfile.TemporaryDirectory() as root:
                class Engine:pass
                engine=Engine();engine.config={'task_queue':{'operations_dir':root}}
                engine.lock=threading.Lock();engine.write_audit=lambda event:None
                host=QueueProcesses(engine)
                from process_exporter import parse_stat
                row={'pid':children[0].pid,'start_ticks':parse_stat(Path('/proc',str(children[0].pid),'stat').read_text())['start_ticks'],'name':'isolated-test-child'}
                host.read_process=lambda pid,cid:dict(row) if children[0].poll() is None else None
                host.snapshot=lambda cid,expected=None:{'container_id':CID,'container_started_at':START,'container_changed':False,
                    'processes':[dict(row)] if children[0].poll() is None else []}
                record={'request':{'snapshot':host.snapshot(CID),'mode':'process'},'actor':{'id':'fixture'},'state':'pending'}
                host.kill('e'*32,record)
                self.assertEqual(record['state'],'succeeded');self.assertIsNotNone(children[0].poll());self.assertIsNone(children[1].poll())
        finally:
            for child in children:
                if child.poll() is None:child.terminate()
                child.wait(timeout=10)


if __name__=='__main__':unittest.main()
