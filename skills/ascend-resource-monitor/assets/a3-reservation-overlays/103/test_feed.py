"""Exercise the actual queue planner and process monitor, without network calls."""
import json
import os
from pathlib import Path
import sys
import secrets
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(Path(os.environ.get('QUEUE_TEST_RUNTIME',ROOT.parent/'runtime'))))
sys.path.insert(0,str(ROOT.parent/'runtime'))
from queue_store import QueueStore
from queue_unified import UnifiedQueue
from queue_monitor import QueueMonitor
from queue_notifications import build_feed
from queue_agent import command,bind_notice


class FeedTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.now=1794394800.0  # 11:00 UTC, outside normal task cutoff hours.
        self.clock=patch('time.time',return_value=self.now);self.time_mock=self.clock.start()
        self.store=QueueStore(Path(self.folder.name)/'queue.db')
        for employee,name in [('a00000001','Owner'),('b00000002','Other'),('c00000003','Waiter')]:
            self.store.login_without_password(employee,name)
        with self.store.db() as db:
            self.accounts={r['employee']:r['id'] for r in db.execute('SELECT * FROM accounts')}
        self.queue=UnifiedQueue(self.store,['A5-118','A5-124'],strict_nights=True,
            overtime_control=True,normal_priority=True,night_carryover_cutoff_hour=9.5,missed_claim_limit=3)

    def tearDown(self):
        self.clock.stop();self.folder.cleanup()

    def task(self,job,employee,state='active',cards=8,node='A5-118',age=1800,seconds=3600,allowed=()):
        with self.store.db() as db:
            db.execute('''INSERT INTO unified_tasks
                (id,account,title,state,created,activated,requested_seconds,cards,
                 fixed_node,allocations,queue_order,allowed_employees)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                (job,self.accounts[employee],'',state,self.now-7200,self.now-age if state=='active' else None,
                 seconds,cards,node,json.dumps({node:list(range(cards))}) if state=='active' else '{}',
                 db.execute('SELECT count(*) FROM unified_tasks').fetchone()[0]+1,json.dumps(allowed)))

    def test_agent_creation_and_binding_are_private_and_expire(self):
        req=dict(machine='A5-118',cards=1,night=False,not_before=0,requested_seconds=1800,avoid_slots=[])
        created=command(self.queue,self.store,dict(mode='agent_execute',employee='a00000001',request=req,
            operation=secrets.token_hex(16),expires=self.now+300))
        with self.store.db() as db:
            owner=dict(db.execute("SELECT id,employee,name FROM accounts WHERE employee='a00000001'").fetchone())
        bind_notice(self.store,owner,created['job'],secrets.token_hex(16))
        feed=build_feed(self.queue,self.store,None)
        notices=[e for e in feed['events'] if e['kind'] in ('agent-created','agent-bound')]
        self.assertEqual({e['kind'] for e in notices},{'agent-created','agent-bound'})
        self.assertTrue(all(e['recipient']=='a00000001' for e in notices))
        self.assertTrue(all(e['valid_until']==self.now+3600 for e in notices))
        self.assertTrue(all('A5-118' in e['body'] for e in notices))
        self.assertNotIn('session',json.dumps(notices));self.assertNotIn('plan',json.dumps(notices))
        self.time_mock.return_value=self.now+3601
        self.assertFalse(any(e['kind'] in ('agent-created','agent-bound') for e in build_feed(self.queue,self.store,None)['events']))

    def test_extended_person_receives_agent_and_group_candidates(self):
        self.store.login_without_password('p_fixture','Fixture')
        req=dict(machine='A5-118',cards=1,night=False,not_before=0,requested_seconds=1800,avoid_slots=[])
        created=command(self.queue,self.store,dict(mode='agent_execute',employee='p_fixture',request=req,
            operation=secrets.token_hex(16),expires=self.now+300))
        with self.store.db() as db:
            owner=dict(db.execute("SELECT id,employee,name FROM accounts WHERE employee='p_fixture'").fetchone())
        bind_notice(self.store,owner,created['job'],secrets.token_hex(16))
        notices=[e for e in build_feed(self.queue,self.store,None)['events'] if e['kind'] in ('agent-created','agent-bound')]
        self.assertEqual(len(notices),2);self.assertTrue(all(e['recipient']=='p_fixture' for e in notices))
        self.task('owner','a00000001',cards=1,node='A5-124')
        monitor=QueueMonitor(self.queue.nodes,{},None);monitor.current=lambda now:[dict(node='A5-124',employee='p_fixture',name='Fixture',npu_ids='0',process_key='fixture',start_time_seconds=self.now-1000)]
        monitor.fresh_nodes=lambda now:{'A5-124'};monitor.inventory_times={'A5-124':self.now}
        feed=build_feed(self.queue,self.store,monitor)
        self.assertTrue(any(v['employee']=='p_fixture' for v in feed['violations']))

    def test_real_ready_allocation_owner_and_unregistered_couser_only(self):
        self.task('ready','a00000001',state='queued',allowed=['d00000004'])
        feed=build_feed(self.queue,self.store,None)
        notices=[e for e in feed['events'] if e['kind']=='ready']
        self.assertEqual({e['recipient'] for e in notices},{'a00000001','d00000004'})
        self.assertTrue(all('A5-118' in e['body'] for e in notices))
        self.assertTrue(all('不会停止实际进程' in e['body'] for e in notices))
        self.assertNotIn('account',json.dumps(feed))

    def test_real_overdue_and_affected_waiter_cousers(self):
        self.task('active','a00000001',age=4000,allowed=['d00000004'])
        self.task('waiter','c00000003',state='queued',allowed=['e00000005'])
        feed=build_feed(self.queue,self.store,None)
        owners=[e for e in feed['events'] if e['kind'] in ('overdue','overdue-joint','overdue-dependent')]
        affected=[e for e in feed['events'] if e['kind'].endswith('-next')]
        self.assertEqual({e['recipient'] for e in owners},{'a00000001','d00000004'})
        self.assertTrue(all(e['repeat_seconds']==60 for e in owners))
        self.assertEqual({e['recipient'] for e in affected},{'c00000003','e00000005'})
        self.assertFalse(any(e['recipient']=='b00000002' for e in feed['events']))

    def test_real_monitor_unregistered_and_excess_use_reaches_affected_couser(self):
        self.task('mine','a00000001',cards=4,allowed=['d00000004'])
        self.task('other','b00000002',cards=1)
        with self.store.db() as db:
            db.execute("UPDATE unified_tasks SET allocations=? WHERE id='other'",(json.dumps({'A5-118':[4]}),))
        monitor=QueueMonitor(self.queue.nodes,{},None)
        monitor.current=lambda now:[dict(node='A5-118',employee='b00000002',name='Other',
            npu_ids='0',process_key='private-process',start_time_seconds=self.now-1000)]
        monitor.fresh_nodes=lambda now:set(self.queue.nodes)
        feed=build_feed(self.queue,self.store,monitor)
        excess=[e for e in feed['events'] if e['kind']=='over-allocation']
        self.assertEqual({e['recipient'] for e in excess},{'a00000001','d00000004'})
        monitor.current=lambda now:[dict(node='A5-118',employee='b00000002',name='Other',
            npu_ids='0',process_key='private-process',start_time_seconds=self.now-1000)]
        with self.store.db() as db:db.execute("DELETE FROM unified_tasks WHERE id='other'")
        feed=build_feed(self.queue,self.store,monitor)
        intrusion=[e for e in feed['events'] if e['kind']=='unregistered']
        self.assertEqual({e['recipient'] for e in intrusion},{'a00000001','b00000002','d00000004'})
        self.assertNotIn('private-process',json.dumps(feed))

    def test_expired_registration_release_reaches_owner_and_couser(self):
        self.task('expired','a00000001',age=4300,allowed=['d00000004'])
        feed=build_feed(self.queue,self.store,None)
        release=[e for e in feed['events'] if e['kind']=='overtime-release']
        self.assertEqual({e['recipient'] for e in release},{'a00000001','d00000004'})
        self.assertTrue(all(e['occurred_at']==self.now for e in release))
        self.assertTrue(all('10 分钟' in e['body'] for e in release))

    def test_third_missed_offer_deletes_reservation_and_notifies_people(self):
        self.task('missed','a00000001',state='queued',allowed=['d00000004'])
        for count in range(3):
            with self.store.db() as db:
                db.execute("UPDATE unified_tasks SET state='ready',ready_since=?,claim_before=?,allocations=? WHERE id='missed'",
                    (self.now-600,self.now,json.dumps({'A5-118':[0]})))
                self.queue._expire_offers(db,self.now)
                row=db.execute("SELECT missed_claims FROM unified_tasks WHERE id='missed'").fetchone()
                if count<2:self.assertEqual(row['missed_claims'],count+1)
                else:self.assertIsNone(row)
        feed=build_feed(self.queue,self.store,None)
        cancelled=[e for e in feed['events'] if e['kind']=='missed-cancel']
        self.assertEqual({e['recipient'] for e in cancelled},{'a00000001','d00000004'})
        self.assertTrue(all('3 次' in e['body'] for e in cancelled))

    def test_legacy_misses_do_not_trigger_immediate_cancel(self):
        self.task('legacy','a00000001',state='queued')
        with self.store.db() as db:db.execute("UPDATE unified_tasks SET missed_claims=28,missed_policy_baseline=28 WHERE id='legacy'")
        for attempt in range(3):
            with self.store.db() as db:
                db.execute("UPDATE unified_tasks SET state='ready',ready_since=?,claim_before=?,allocations=? WHERE id='legacy'",(self.now-600,self.now,json.dumps({'A5-118':[0]})))
                self.queue._expire_offers(db,self.now)
                row=db.execute("SELECT missed_claims,missed_policy_baseline FROM unified_tasks WHERE id='legacy'").fetchone()
                if attempt<2:self.assertEqual((row['missed_claims'],row['missed_policy_baseline']),(29+attempt,28))
                else:self.assertIsNone(row)

    def test_default_queue_keeps_reservation_after_third_miss(self):
        self.queue.missed_claim_limit=0
        self.task('retained','a00000001',state='queued')
        for count in range(3):
            with self.store.db() as db:
                db.execute("UPDATE unified_tasks SET state='ready',ready_since=?,claim_before=? WHERE id='retained'",
                    (self.now-600,self.now))
                self.queue._expire_offers(db,self.now)
                row=db.execute("SELECT missed_claims,state FROM unified_tasks WHERE id='retained'").fetchone()
                self.assertEqual((row['missed_claims'],row['state']),(count+1,'queued'))

    def test_second_extension_records_primary_owner_and_no_first_event(self):
        self.task('extended','a00000001',age=3601,allowed=['b00000002'])
        user=dict(id=self.accounts['b00000002'],employee='b00000002',admin=False)
        self.queue.action(user,'extended','extend',extension_minutes=10)
        self.assertEqual(build_feed(self.queue,self.store,None)['extensions'],[])
        self.time_mock.return_value=self.now+601
        self.queue.action(user,'extended','extend',extension_minutes=20)
        extension=build_feed(self.queue,self.store,None)['extensions'][0]
        self.assertEqual((extension['employee'],extension['count'],extension['minutes'],extension['total']),
                         ('a00000001',2,20,30))


if __name__=='__main__':unittest.main()
