from contextlib import closing
import copy
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'runtime'))
from hub_core import init_db
from notification_store import init_notices,reserve_notices,queued,begin_attempt,accepted
from queue_notifications import authorize_export,task_events


class ConditionTests(unittest.TestCase):
    def test_private_feed_auth(self):
        token='a'*64
        self.assertTrue(authorize_export('127.0.0.1','Bearer '+token,token))
        self.assertTrue(authorize_export('::1','Bearer '+token,token))
        self.assertFalse(authorize_export('192.0.2.10','Bearer '+token,token))
        self.assertFalse(authorize_export('127.0.0.1','Bearer bad',token))
        self.assertFalse(authorize_export(None,'Bearer '+token,token))

    def ready(self):
        return dict(id='job',state='ready',can_claim=True,claim_before=1600,ready_since=1000,
                    allocations={'A5-118':[0]},offer_id='offer',requested_seconds=3600)

    def test_ready_every_minute_and_last_minute(self):
        row=self.ready()
        first=task_events(row,1000,dict(overtime_control=True))[0]
        follow=task_events(row,1060,{})[0]
        last=task_events(row,1541,{})[0]
        self.assertEqual((first['kind'],first['phase']),('ready','0'))
        self.assertEqual((follow['kind'],follow['phase']),('ready-reminder','1'))
        self.assertEqual(last['phase'],'last-minute')
        self.assertEqual(task_events(row,1600,{}),[])
        self.assertIn('不会停止实际进程',first['body'])

    def test_deferral_started_backfill_and_bottleneck_windows(self):
        row=self.ready();row['missed_claims']=2
        self.assertEqual(task_events(row,1100,{})[0]['kind'],'deferred')
        row=dict(row,state='active',activated=1000,bottleneck=True)
        self.assertEqual(task_events(row,1005,{})[-1]['kind'],'started')
        self.assertTrue(task_events(row,4010,{})[-1]['key'].endswith(':10'))
        self.assertTrue(task_events(row,4310,{})[-1]['key'].endswith(':5'))
        row['backfill_planned']=True
        self.assertEqual(task_events(row,4310,{})[-1]['kind'],'backfill-ending')
        row['backfill_planned']=False;row['not_before']=900
        self.assertEqual([e['kind'] for e in task_events(row,4310,{})],['deferred'])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.path=str(Path(self.folder.name)/'state.db')
        init_db(self.path);init_notices(self.path)

    def tearDown(self):
        self.folder.cleanup()

    def snapshot(self,now=1000,**event_changes):
        event=dict(recipient='a00000001',key='alert:one',kind='unregistered',title='test',body='message',valid_until=now+90)
        event.update(event_changes)
        return dict(cluster='a5',notifications_version=1,notifications_generated_at=now,notifications=[event])

    def reserve(self,snapshot,now=1000):
        reserve_notices(self.path,snapshot,{'a00000001'},900,now)

    def attempt_all(self,now=1000):
        for item in queued(self.path,now):
            self.assertTrue(begin_attempt(self.path,item['event'],now))
            accepted(self.path,item['event'])

    def test_persists_single_send_across_restart(self):
        snapshot=self.snapshot();self.reserve(snapshot);self.attempt_all()
        init_notices(self.path);self.reserve(snapshot,1005)
        self.assertEqual(queued(self.path,1005),[])

    def test_reappearance_new_episode_and_no_resend_stale(self):
        snapshot=self.snapshot();self.reserve(snapshot);self.attempt_all()
        stale=self.snapshot(1000);self.reserve(stale,1100)
        self.assertEqual(queued(self.path,1100),[])
        empty=self.snapshot(1105);empty['notifications']=[];self.reserve(empty,1105)
        self.reserve(self.snapshot(1110),1110)
        self.assertEqual(len(queued(self.path,1110)),1)

    def test_repeat_exact_interval_and_immediate_escalation(self):
        self.reserve(self.snapshot(kind='overdue-nonblocking',scope='overdue:job',repeat_seconds=60));self.attempt_all()
        self.reserve(self.snapshot(1020,kind='overdue-nonblocking',scope='overdue:job',repeat_seconds=60),1020)
        self.assertEqual(queued(self.path,1020),[])
        self.reserve(self.snapshot(1025,key='blocking',kind='overdue',scope='overdue:job',repeat_seconds=60),1025)
        self.assertEqual(queued(self.path,1025)[0]['payload']['title'],'超时任务已转为瓶颈');self.attempt_all(1025)
        self.reserve(self.snapshot(1084,key='blocking',kind='overdue',scope='overdue:job',repeat_seconds=60),1084)
        self.assertEqual(queued(self.path,1084),[])
        self.reserve(self.snapshot(1085,key='blocking',kind='overdue',scope='overdue:job',repeat_seconds=60),1085)
        self.assertEqual(len(queued(self.path,1085)),1)

    def test_old_release_and_initial_missed_claims_suppressed(self):
        snapshot=self.snapshot(occurred_at=800,kind='overtime-release');self.reserve(snapshot)
        self.assertEqual(queued(self.path,1000),[])
        # Historical deferral baselines are established on a source's first feed.
        with closing(sqlite3.connect(self.path)) as db,db:
            db.execute('DELETE FROM notice_sources')
        self.reserve(self.snapshot(key='missed:3',kind='deferred',historical=True))
        self.assertEqual(queued(self.path,1000),[])
        self.reserve(self.snapshot(1010,key='missed:4',kind='deferred',historical=True),1010)
        self.assertEqual(len(queued(self.path,1010)),1)

    def test_recipient_isolation_unknown_outcome_and_expiration(self):
        snapshot=self.snapshot();snapshot['notifications'].append(dict(snapshot['notifications'][0],recipient='b00000002'))
        self.reserve(snapshot)
        items=queued(self.path,1000)
        self.assertEqual([item['recipient'] for item in items],['a00000001'])
        self.assertTrue(begin_attempt(self.path,items[0]['event'],1000))
        self.assertFalse(begin_attempt(self.path,items[0]['event'],1001))
        self.reserve(snapshot,1005)
        self.assertEqual(queued(self.path,1005),[])
        self.reserve(self.snapshot(1005,key='expires'),1005)
        self.assertEqual(queued(self.path,1096),[])

    def test_cancel_pending_when_condition_clears(self):
        self.reserve(self.snapshot());item=queued(self.path,1000)[0]
        empty=self.snapshot(1005);empty['notifications']=[];self.reserve(empty,1005)
        self.assertFalse(begin_attempt(self.path,item['event'],1006))

    def test_imported_ready_compatibility(self):
        with closing(sqlite3.connect(self.path)) as db,db:
            db.execute('INSERT INTO deliveries VALUES (?,?,?)',('ready:a5:job:offer:a00000001',500,'accepted'))
        self.reserve(self.snapshot(kind='ready',job='job',offer_id='offer'))
        self.assertEqual(queued(self.path,1000),[])


if __name__=='__main__':
    unittest.main()
