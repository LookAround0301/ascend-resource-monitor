from pathlib import Path
import sys
import threading
import unittest
from types import SimpleNamespace

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'assets/runtime'))
from queue_night_enforcer import NightEnforcer
from control_common import ControlError


class NightEnforcerTests(unittest.TestCase):
    def setUp(self):
        self.cid='a'*64;self.started='2026-11-06T20:00:00Z';self.kills=[];self.events=[]
        self.rows=[{'pid':42,'container_id':self.cid,'started_at':self.started,'employee':'u12345678',
                    'npu_ids':'0','process_key':'boot:42:987'}]
        self.snapshot={'container_changed':False,'inventory_complete':True,'container_started_at':self.started,
                       'container_id':self.cid,'processes':[{'pid':42,'start_ticks':987}]}
        docker=SimpleNamespace(kill=lambda cid:self.kills.append(cid),inspect=lambda cid:{'State':{'Running':False}})
        engine=SimpleNamespace(config={'node':'a'},lock=threading.Lock(),docker=docker,write_audit=self.events.append)
        ownership=SimpleNamespace(reader=lambda:self.rows,identity_check=lambda row:True)
        self.host=SimpleNamespace(engine=engine,ownership=ownership,snapshot=lambda cid,started:self.snapshot)
        self.request={'node':'a','container_id':self.cid,'container_started_at':self.started,'confirmed':True,
                      'allowed':{'0':['u23456789']},'expected':['boot:42:987']}
        self.actor={'id':'night-scheduler','login':'night-scheduler'}
    def test_verified_unregistered_container_is_killed_and_audited(self):
        self.assertTrue(NightEnforcer(self.host).stop(self.request,self.actor)['stopped'])
        self.assertEqual(self.kills,[self.cid]);self.assertEqual([e['result'] for e in self.events],['requested','stopped'])
    def test_authorized_unknown_changed_or_stale_process_is_never_killed(self):
        for case in ('authorized','unknown','changed','stale','cards'):
            with self.subTest(case=case):
                original=dict(self.rows[0]);snapshot=dict(self.snapshot)
                if case=='authorized':self.rows[0]['employee']='u23456789'
                if case=='unknown':self.rows[0]['employee']='未知'
                if case=='changed':self.snapshot['container_changed']=True
                if case=='stale':self.rows[0]['process_key']='boot:42:988'
                if case=='cards':self.rows[0]['npu_ids']='unknown'
                with self.assertRaises(ControlError):NightEnforcer(self.host).stop(self.request,self.actor)
                self.rows[0]=original;self.snapshot=snapshot;self.host.snapshot=lambda cid,started:self.snapshot
        self.assertEqual(self.kills,[])
    def test_mixed_authorized_and_unauthorized_container_is_not_killed(self):
        self.rows.append({**self.rows[0],'pid':43,'process_key':'boot:43:111','employee':'u23456789','npu_ids':'0'})
        self.snapshot['processes'].append({'pid':43,'start_ticks':111})
        self.request['expected'].append('boot:43:111')
        with self.assertRaises(ControlError):NightEnforcer(self.host).stop(self.request,self.actor)
        self.assertEqual(self.kills,[])

    def test_container_using_cards_outside_night_scope_is_not_killed(self):
        self.rows[0]['npu_ids']='0,1'
        with self.assertRaises(ControlError):NightEnforcer(self.host).stop(self.request,self.actor)
        self.assertEqual(self.kills,[])


if __name__=='__main__':unittest.main()
