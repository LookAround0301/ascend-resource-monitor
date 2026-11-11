"""Run using the candidate server's existing aiohttp installation."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from aiohttp.test_utils import TestClient, TestServer
from hub import Hub,DeliveryRejected


class FakeHub(Hub):
    async def start(self,app):
        self.sent=[]
        self.cache={'test':dict(version=1,cluster='test',label='A5',exported_at=time.time(),accounts=['u00000001'],tasks=[
            dict(state='active',employee='u00000001',cards=1),
            dict(state='active',employee='a00000001',fixed_node='PRIVATE-NODE')])}

    async def stop(self,app):
        pass

    async def send(self,employee,content):
        self.sent.append((employee,content))


class CallbackTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.hub=FakeHub(dict(database=str(Path(self.folder.name)/'state.db'),sources=[dict(id='test')],
                             routes={'/private':dict(users=['u00000001'],clusters=['test'])}))
        self.client=TestClient(TestServer(self.hub.app()))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.folder.cleanup()

    async def test_authorized_query_private_reply_and_generic_http(self):
        body=dict(sender='u00000001',receiver='u00000001',type='Text',content='/预约迁移试验 查询')
        response=await self.client.post('/private',json=body)
        self.assertEqual(response.status,200)
        self.assertNotIn('A5',await response.text())
        self.assertEqual(len(self.hub.sent),1)
        self.assertEqual(self.hub.sent[0][0],'u00000001')
        self.assertNotIn('PRIVATE-NODE',self.hub.sent[0][1])
        await self.client.post('/private',json=body)
        self.assertEqual(len(self.hub.sent),1)

    async def test_wrong_receiver_and_unlisted_sender_no_message(self):
        for body in (dict(sender='u00000001',receiver='a00000001',type='Text',content='查询'),
                     dict(sender='a00000001',receiver='a00000001',type='Text',content='查询')):
            response=await self.client.post('/private',json=body)
            self.assertEqual(response.status,200)
        self.assertEqual(self.hub.sent,[])

    async def test_stale_snapshot_hides_private_data(self):
        self.hub.cache['test']['exported_at']=time.time()-100
        await self.client.post('/private',json=dict(sender='u00000001',receiver='u00000001',type='Text',content='查询'))
        self.assertIn('暂时无法读取',self.hub.sent[0][1])
        self.assertNotIn('1 卡',self.hub.sent[0][1])

    async def test_formal_cohort_uses_live_accounts(self):
        route=self.hub.config['routes']['/private']
        route['users']=[]
        route['account_cluster']='test'
        response=await self.client.post('/private',json=dict(sender='u00000001',receiver='u00000001',type='Text',content='查询'))
        self.assertEqual(response.status,200)
        self.assertEqual(len(self.hub.sent),1)
        self.hub.cache['test']['accounts'].append('a00000001')
        await self.client.post('/private',json=dict(sender='a00000001',receiver='a00000001',type='Text',content='查询'))
        self.assertEqual(len(self.hub.sent),2)
        self.assertIn('PRIVATE-NODE',self.hub.sent[1][1])
        self.hub.cache['test']['exported_at']=time.time()-100
        await self.client.post('/private',json=dict(sender='a00000001',receiver='a00000001',type='Text',content='查询'))
        self.assertEqual(len(self.hub.sent),2)

    async def test_wrong_path_malformed_body_and_limits(self):
        self.assertEqual((await self.client.post('/other',json={})).status,404)
        self.assertEqual((await self.client.post('/private',data='not json')).status,400)
        self.assertEqual((await self.client.post('/private',data='x'*9000)).status,400)
        self.assertEqual(self.hub.sent,[])

    async def test_all_users_notices_and_couser_are_separate_and_deduplicated(self):
        now=time.time()
        self.hub.config.update(notification_sources=['test'],notification_all_users=True,
                               notification_events_enabled_at=now-5)
        snapshot=self.hub.cache['test']
        snapshot.update(notifications_version=1,notifications_generated_at=now,
            notification_recipients=['u00000001','a00000001'],notifications=[
                dict(recipient=employee,key='task:shared:ready:offer:0',kind='ready',
                     title='资源已分配',body='shared allocation',job='shared',offer_id='offer',
                     valid_until=now+60) for employee in ['u00000001','a00000001']])
        await self.hub.notices()
        self.assertEqual({who for who,_ in self.hub.sent},{'u00000001','a00000001'})
        self.assertEqual(len(self.hub.sent),2)
        await self.hub.notices()
        self.assertEqual(len(self.hub.sent),2)

    async def test_disabled_source_and_stale_feed_do_not_send(self):
        now=time.time()
        self.hub.config.update(notification_sources=['test'],notification_all_users=True)
        self.hub.cache['test'].update(notifications_version=1,notifications_generated_at=now-100,
            notification_recipients=['u00000001'],notifications=[dict(recipient='u00000001',
            key='unregistered:1',kind='unregistered',title='test',body='test',valid_until=now+60)])
        await self.hub.notices()
        self.assertEqual(self.hub.sent,[])

    async def test_rejection_diagnostics_do_not_expose_recipient_or_token(self):
        error=DeliveryRejected(200,dict(status='error',message='receiver unsubscribe',
            receiver='a00000001',auth='PRIVATE-CREDENTIAL',content='PRIVATE-MESSAGE'))
        self.assertEqual(str(error),'HTTP200/recipient-settings')
        self.assertNotIn('PRIVATE',str(error))

    async def test_verified_group_alert_goes_to_configured_group_once(self):
        now=time.time()
        self.hub.config.update(reminders_enabled=True,group_notifications=dict(enabled=True,mention_verified=True,
            cluster='test',group='1234567890123456789',repeat_seconds=600))
        snapshot=self.hub.cache['test']
        snapshot.update(notifications_version=1,notifications_generated_at=now,violations=[
            dict(id='a'*24,node='A5-118',employee='a00000001',name='User',cards=[0],sampled_at=now-15)])
        await self.hub.group_notices();self.assertEqual(self.hub.sent,[])
        snapshot['violations'][0]['sampled_at']=now
        await self.hub.group_notices();await self.hub.group_notices()
        self.assertEqual(len(self.hub.sent),1)
        self.assertEqual(self.hub.sent[0][0],'1234567890123456789')
        self.assertIn('@User',self.hub.sent[0][1]);self.assertIn('0 号卡',self.hub.sent[0][1])

    async def test_private_overtime_notice_never_becomes_group_reminder(self):
        now=time.time()
        self.hub.config.update(reminders_enabled=True,group_notifications=dict(enabled=True,mention_verified=True,
            cluster='test',group='1234567890123456789'))
        self.hub.cache['test'].update(notifications_version=1,notifications_generated_at=now,violations=[],
            notifications=[dict(kind='overdue',recipient='u00000001',body='Overtime')])
        await self.hub.group_notices();self.assertEqual(self.hub.sent,[])


if __name__=='__main__':
    unittest.main()
