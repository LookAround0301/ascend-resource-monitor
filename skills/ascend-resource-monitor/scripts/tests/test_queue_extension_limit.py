"""One extension per reservation, including Agent retries and shared users."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json,os,secrets,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0,os.environ.get('QUEUE_TEST_RUNTIME',str(Path(__file__).resolve().parents[2]/'assets/runtime')))
from control_common import ControlError
from queue_store import QueueStore
from queue_unified import UnifiedQueue
from queue_agent import action as agent_action


class ExtensionLimitTests(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        self.store=QueueStore(Path(folder.name)/'queue.db')
        self.user=self.store.user(self.store.register('u12345678','Owner','fixture-password'))
        self.shared=self.store.user(self.store.register('u12345679','Shared','fixture-password'))
        self.now=datetime(2026,11,20,10,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        timer=patch('time.time',return_value=self.now);self.clock=timer.start();self.addCleanup(timer.stop)
        self.queue=self.reopen()

    def reopen(self):
        return UnifiedQueue(self.store,['a','b'],timezone='Asia/Shanghai',strict_nights=True,
                            overtime_control=True,midnight_separation=True,night_carryover_cutoff_hour=9)

    def active(self,urgent=False):
        if urgent:self.queue.set_urgent_user(self.user['employee'],self.user['name'],True)
        job=self.queue.enqueue(self.user,dict(purpose='测试预约',machine='a',cards=1,cards_per_node=self.queue.capacity,requested_seconds=600,
                                             notifications_ready=True,avoid_slots=[],urgent=urgent))
        row=next(r for r in self.queue.view(self.user)['tasks'] if r['id']==job)
        self.queue.action(self.user,job,'claim',row['offer_id'])
        self.clock.return_value=self.now+601
        return job

    def snapshot(self,job):
        with self.store.db() as db:return dict(db.execute('SELECT * FROM unified_tasks WHERE id=?',(job,)).fetchone())

    def denied(self,job,user=None):
        before=self.snapshot(job)
        with self.assertRaises(ControlError) as failure:
            self.queue.action(user or self.user,job,'extend',extension_minutes=30)
        self.assertEqual(failure.exception.status,409)
        self.assertIn('唯一一次',str(failure.exception))
        self.assertEqual(self.snapshot(job),before)

    def test_each_duration_gets_one_extension_and_second_due_is_denied(self):
        for minutes in (10,20,30):
            with self.subTest(minutes=minutes):
                self.clock.return_value=self.now;job=self.active()
                self.queue.action(self.user,job,'extend',extension_minutes=minutes)
                first=self.snapshot(job)
                self.assertEqual(first['extension_count'],1)
                self.assertEqual(first['activated']+first['requested_seconds'],self.clock.return_value+minutes*60)
                self.clock.return_value+=minutes*60+1
                self.denied(job)
                notices=self.queue.view(self.user)['alerts']
                self.assertTrue(any('已使用唯一一次延长机会，不能再次延长' in r['message'] for r in notices))
                self.queue.action(self.user,job,'finish')
        self.assertEqual(self.queue.view(self.user)['extension_limit'],1)

    def test_failed_request_does_not_consume_chance(self):
        job=self.active();self.clock.return_value=self.now+599
        with self.assertRaises(ControlError):self.queue.action(self.user,job,'extend',extension_minutes=30)
        self.assertEqual(self.snapshot(job)['extension_count'],0)
        self.clock.return_value=self.now+601
        self.queue.action(self.user,job,'extend',extension_minutes=30)
        self.assertEqual(self.snapshot(job)['extension_count'],1)

    def test_night_boundary_failure_preserves_the_only_chance(self):
        boundary=self.queue.normal_night_end(self.now)
        self.now=boundary-1200;self.clock.return_value=self.now
        job=self.active();self.clock.return_value=boundary-600
        with self.assertRaises(ControlError):self.queue.action(self.user,job,'extend',extension_minutes=30)
        self.assertEqual(self.snapshot(job)['extension_count'],0)
        self.queue.action(self.user,job,'extend',extension_minutes=10)
        row=self.snapshot(job)
        self.assertEqual(row['extension_count'],1)
        self.assertEqual(row['activated']+row['requested_seconds'],boundary)

    def test_shared_user_and_admin_cannot_get_extra_chances(self):
        job=self.active()
        with self.store.db() as db:db.execute('UPDATE unified_tasks SET allowed_employees=? WHERE id=?',(json.dumps([self.shared['employee']]),job))
        self.queue.action(self.shared,job,'extend',extension_minutes=10)
        self.clock.return_value+=601
        self.denied(job,self.user);self.denied(job,self.shared)
        self.denied(job,dict(self.shared,admin=True))

    def test_existing_and_incomplete_legacy_history_is_preserved(self):
        job=self.active();self.queue.action(self.user,job,'extend',extension_minutes=10)
        with self.store.db() as db:
            db.execute('UPDATE unified_tasks SET extension_count=3,extension_total_minutes=60 WHERE id=?',(job,))
        self.queue=self.reopen();self.clock.return_value+=601
        self.denied(job);self.assertEqual(self.snapshot(job)['extension_count'],3)
        with self.store.db() as db:
            db.execute('UPDATE unified_tasks SET extension_count=0 WHERE id=?',(job,))
            db.execute('ALTER TABLE unified_tasks DROP COLUMN extension_total_minutes')
            db.execute('ALTER TABLE unified_tasks DROP COLUMN extension_total_complete')
        self.queue=self.reopen();self.denied(job)
        self.assertEqual(self.snapshot(job)['extension_count'],1)
        self.assertEqual(self.snapshot(job)['extension_total_complete'],0)

    def test_simultaneous_first_requests_allow_exactly_one(self):
        job=self.active()
        def extend(_):
            try:self.queue.action(self.user,job,'extend',extension_minutes=10);return 'ok'
            except ControlError as error:return error.status
        with ThreadPoolExecutor(max_workers=2) as executor:results=list(executor.map(extend,range(2)))
        self.assertCountEqual(results,['ok',409]);self.assertEqual(self.snapshot(job)['extension_count'],1)

    def test_agent_same_operation_replays_but_new_operation_is_denied(self):
        job=self.active();operation=secrets.token_hex(16)
        first=agent_action(self.queue,self.store,self.user['employee'],operation,job,'extend',minutes=20)
        self.clock.return_value+=1201
        self.assertEqual(agent_action(self.queue,self.store,self.user['employee'],operation,job,'extend',minutes=20),first)
        with self.assertRaises(ControlError) as failure:
            agent_action(self.queue,self.store,self.user['employee'],secrets.token_hex(16),job,'extend',minutes=30)
        self.assertEqual(failure.exception.status,409)
        self.assertEqual(self.snapshot(job)['extension_count'],1)

    def test_urgent_can_extend_repeatedly_and_keeps_group_notice_history(self):
        job=self.active(urgent=True)
        for count,minutes in enumerate((10,20,30),1):
            self.queue.action(self.user,job,'extend',extension_minutes=minutes)
            row=self.snapshot(job)
            self.assertEqual((row['extension_count'],row['extension_total_minutes']),(count,sum((10,20,30)[:count])))
            self.clock.return_value+=minutes*60+1
            self.assertTrue(any('紧急任务不受延长次数限制' in r['message'] for r in self.queue.view(self.user)['alerts']))
        with self.store.db() as db:
            events=[json.loads(r['payload']) for r in db.execute("SELECT payload FROM unified_notice_events WHERE kind='extension' AND job=? ORDER BY at",(job,))]
        self.assertEqual([r['count'] for r in events],[2,3])
        self.assertIsNone(self.queue.view(self.user)['urgent_extension_limit'])

    def test_urgent_agent_shared_user_can_extend_again_with_idempotent_retry(self):
        job=self.active(urgent=True)
        with self.store.db() as db:db.execute('UPDATE unified_tasks SET allowed_employees=? WHERE id=?',(json.dumps([self.shared['employee']]),job))
        self.queue.action(self.user,job,'extend',extension_minutes=10);self.clock.return_value+=601
        operation=secrets.token_hex(16)
        first=agent_action(self.queue,self.store,self.shared['employee'],operation,job,'extend',minutes=30)
        self.assertEqual(agent_action(self.queue,self.store,self.shared['employee'],operation,job,'extend',minutes=30),first)
        self.assertEqual(self.snapshot(job)['extension_count'],2)

    def test_urgent_second_extension_still_respects_night_boundary(self):
        boundary=self.queue.normal_night_end(self.now)
        self.now=boundary-1800;self.clock.return_value=self.now
        job=self.active(urgent=True);self.clock.return_value=self.now+600
        self.queue.action(self.user,job,'extend',extension_minutes=10)
        self.clock.return_value=boundary-600
        with self.assertRaises(ControlError):self.queue.action(self.user,job,'extend',extension_minutes=30)
        self.assertEqual(self.snapshot(job)['extension_count'],1)
        self.queue.action(self.user,job,'extend',extension_minutes=10)
        row=self.snapshot(job)
        self.assertEqual(row['extension_count'],2)
        self.assertEqual(row['activated']+row['requested_seconds'],boundary)

    def test_urgent_second_extension_still_respects_backfill_boundary(self):
        job=self.active(urgent=True);self.queue.action(self.user,job,'extend',extension_minutes=10)
        self.clock.return_value+=601
        with self.store.db() as db:db.execute('UPDATE unified_tasks SET backfill_end_before=? WHERE id=?',(self.clock.return_value+600,job))
        with self.assertRaises(ControlError):self.queue.action(self.user,job,'extend',extension_minutes=30)
        self.assertEqual(self.snapshot(job)['extension_count'],1)


if __name__=='__main__':unittest.main()
