from datetime import datetime
from pathlib import Path
import sys,tempfile,unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'assets/runtime'))
from queue_store import QueueStore
from queue_unified import UnifiedQueue
from control_common import ControlError

def stamp(text):return datetime.fromisoformat(text).replace(tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()

class QueueOperationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.now=stamp('2026-11-04T08:50:00');self.clock=patch('queue_unified.time.time',return_value=self.now).start();self.addCleanup(patch.stopall)
        self.store=QueueStore(Path(self.temp.name)/'queue.db')
        self.users=[self.store.user(self.store.register('u1234567'+str(i),'Person'+str(i),'fixture-password')) for i in range(3)]
        self.queue=UnifiedQueue(self.store,['a','b'],timezone='Asia/Shanghai',daily_reset=True)
    def add(self,user=None,seconds=60):return self.queue.enqueue(user or self.users[0],{'machine':'a','cards':8,'requested_seconds':seconds,'notifications_ready':True})
    def claim(self,job,user=None):
        user=user or self.users[0];row=next(r for r in self.queue.view(user)['tasks'] if r['id']==job)
        self.queue.action(user,job,'claim',row['offer_id'])
    def test_overdue_notifies_current_and_next_but_not_unrelated_account(self):
        first=self.add();self.claim(first);self.add(self.users[1]);self.clock.return_value=self.now+61
        current=self.queue.view(self.users[0]);following=self.queue.view(self.users[1])
        self.assertTrue(current['tasks'][0]['overdue'])
        self.assertEqual(current['alerts'][0]['kind'],'overdue')
        self.assertEqual(following['alerts'][0]['kind'],'overdue-next')
        self.assertIn(self.users[0]['employee'],following['alerts'][0]['message'])
        self.assertFalse(self.queue.view(self.users[2])['alerts'])
    def test_only_admin_reorders_waiting_and_preserves_original_times(self):
        first=self.add();self.claim(first)
        a=self.add(self.users[1]);b=self.add(self.users[2])
        before={r['id']:r['created'] for r in self.queue.view(None)['tasks']}
        with self.assertRaises(ControlError):self.queue.action(self.users[2],b,'priority-up')
        self.queue.action({**self.users[0],'admin':True},b,'priority-up')
        rows=self.queue.view(None)['tasks'];self.assertEqual([r['id'] for r in rows],[first,b,a])
        self.assertEqual({r['id']:r['created'] for r in rows},before)
        self.queue.action({**self.users[0],'admin':True},b,'priority-down')
        self.assertEqual([r['id'] for r in self.queue.view(None)['tasks']],[first,a,b])
    def test_nine_am_clears_active_and_waiting_once_and_preserves_accounts(self):
        active=self.add();self.claim(active);self.add(self.users[1]);self.clock.return_value=stamp('2026-11-04T08:59:59')
        self.assertEqual(len(self.queue.view(None)['tasks']),2)
        self.clock.return_value=stamp('2026-11-04T09:00:00')
        self.assertEqual(self.queue.view(None)['tasks'],[])
        self.assertEqual(self.queue.continuing()[0]['employee'],self.users[0]['employee'])
        self.assertEqual(len(self.queue.continuing()),1)
        new=self.add(self.users[1]);self.assertEqual(self.queue.view(None)['tasks'][0]['id'],new)
        reopened=UnifiedQueue(self.store,['a','b'],timezone='Asia/Shanghai',daily_reset=True)
        self.assertEqual(reopened.view(None)['tasks'][0]['id'],new)
        self.assertIsNotNone(self.store.login(self.users[0]['employee'],'fixture-password'))
        self.clock.return_value=stamp('2026-11-05T09:00:00');self.assertEqual(reopened.view(None)['tasks'],[])

if __name__=='__main__':unittest.main()
