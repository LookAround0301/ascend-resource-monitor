"""Urgent priority, authorization and non-preemption through real queue storage."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timezone

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'assets/runtime'))
from control_common import ControlError
from queue_store import QueueStore
from queue_unified import UnifiedQueue


class UrgentQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=QueueStore(Path(self.temp.name)/'queue.db')
        self.clock=patch('queue_unified.time.time',return_value=datetime(2026,11,7,10,tzinfo=timezone.utc).timestamp())
        self.mock_clock=self.clock.start();self.addCleanup(self.clock.stop)
        self.queue=UnifiedQueue(self.store,{'a':{},'b':{}})
        self.users=[]
        for i in range(4):
            token=self.store.register('u1234567'+str(i),'User '+str(i),'password-for-fixture')
            self.users.append(self.store.user(token))
        self.queue.set_urgent_user(self.users[1]['employee'],self.users[1]['name'],True)
        self.queue.set_urgent_user(self.users[2]['employee'],self.users[2]['name'],True)
        # The existing active reservation consumes every card on both nodes.
        active=self.add(self.users[0],machine='dual',cards=16)
        row=self.rows()[active]
        self.queue.action(self.users[0],active,'claim',offer_id=row['offer_id'])

    def add(self,user,**overrides):
        return self.queue.enqueue(user,{'machine':'a','cards':8,'requested_seconds':3600,
                                        'notifications_ready':True,**overrides})

    def rows(self):return {row['id']:row for row in self.queue.view(self.users[0])['tasks']}
    def waiting(self):return [row['id'] for row in self.queue.view(self.users[0])['tasks'] if row['state']=='queued']

    def test_urgent_submission_and_later_mark_are_fifo_and_reversible(self):
        ordinary=self.add(self.users[0]);later=self.add(self.users[2])
        first=self.add(self.users[1],urgent=True)
        self.queue.set_urgent(self.users[2],later,True)
        self.assertEqual(self.waiting(),[first,later,ordinary])
        self.assertEqual([self.rows()[job]['urgent'] for job in (first,later,ordinary)],[1,1,0])
        self.queue.set_urgent(self.users[1],first,False)
        self.assertEqual(self.waiting(),[later,ordinary,first])
        self.queue.set_urgent(self.users[1],first,True)
        self.assertEqual(self.waiting(),[later,first,ordinary])

    def test_no_unauthorized_or_night_priority_and_manual_moves_cannot_jump_urgent(self):
        normal=self.add(self.users[3]);urgent=self.add(self.users[1],urgent=True)
        with self.assertRaises(ControlError):self.add(self.users[3],urgent=True)
        with self.assertRaises(ControlError):self.queue.set_urgent(self.users[3],normal,True)
        with self.assertRaises(ControlError):self.queue.set_urgent(self.users[1],normal,True)
        with self.assertRaises(ControlError):self.add(self.users[1],urgent=True,night=True,not_before=self.mock_clock.return_value+36000)
        with self.assertRaises(ControlError):self.queue.action({**self.users[0],'admin':True},urgent,'priority-top')
        with self.assertRaises(ControlError):self.queue.action({**self.users[0],'admin':True},normal,'priority-top')
        self.assertEqual(self.waiting(),[urgent,normal])

    def test_revocation_blocks_new_marks_but_owner_can_cancel_existing_mark(self):
        urgent=self.add(self.users[1],urgent=True)
        self.queue.set_urgent_user(self.users[1]['employee'],self.users[1]['name'],False)
        self.assertFalse(self.queue.view(self.users[1])['can_urgent'])
        self.assertTrue(self.rows()[urgent]['urgent'])
        self.queue.set_urgent(self.users[1],urgent,False)
        with self.assertRaises(ControlError):self.queue.set_urgent(self.users[1],urgent,True)

    def test_existing_ready_offer_is_not_taken_back(self):
        # A separately offered reservation keeps its allocation and offer id.
        self.queue.action(self.users[0],next(row['id'] for row in self.rows().values() if row['state']=='active'),'finish')
        offered=self.add(self.users[0]);before=self.rows()[offered]
        self.assertEqual(before['state'],'ready')
        self.add(self.users[1],urgent=True)
        after=self.rows()[offered]
        self.assertEqual((after['state'],after['offer_id'],after['allocations']),('ready',before['offer_id'],before['allocations']))


if __name__=='__main__':unittest.main()
