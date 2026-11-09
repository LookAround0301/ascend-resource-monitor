from datetime import datetime
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'assets/runtime'))
from queue_night_coordinator import NightCoordinator
from queue_store import QueueStore
from queue_unified import UnifiedQueue


class Monitor:
    def __init__(self):self.rows=[];self.valid={'a','b'};self.inventory_times={'a':1,'b':1}
    def current(self,now):return self.rows
    def fresh_nodes(self,now):return self.valid
    def cards(self,row):return set(row['card_ids']) if row['card_ids'] is not None else None


class CoordinatorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=QueueStore(Path(self.temp.name)/'queue.db')
        self.user=self.store.user(self.store.register('u12345678','Owner','fixture-password'))
        self.other=self.store.user(self.store.register('u23456789','Other','fixture-password'))
        self.queue=UnifiedQueue(self.store,['a','b'],timezone='Asia/Shanghai',strict_nights=True)
        self.clock=patch('queue_unified.time.time').start();self.addCleanup(patch.stopall)
        self.stamp=lambda value:datetime.fromisoformat(value).replace(tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        self.now=self.stamp('2026-11-04T21:00:00');self.clock.return_value=self.now
        self.monitor=Monitor();self.calls=[];self.events=[]
        async def agent(node,path,body):self.calls.append((node,path,body));return {'stopped':True}
        self.coordinator=NightCoordinator(self.queue,self.monitor,agent,self.events.append)
    def add(self,user,**values):
        return self.queue.enqueue(user,{'machine':'a','cards':8,'requested_seconds':3600,'notifications_ready':True,**values})
    async def poll_at(self,now):
        self.clock.return_value=now
        with patch('queue_night_coordinator.time.time',return_value=now):await self.coordinator.poll()
    async def test_takeover_waits_for_fresh_sample_before_reclaim(self):
        normal=self.add(self.other);row=self.queue.view(self.other)['tasks'][0]
        self.queue.action(self.other,normal,'claim',row['offer_id'])
        start=self.now+1200;night=self.add(self.user,night=True,not_before=start)
        self.monitor.rows=[{'node':'a','employee':'u23456789','card_ids':list(range(8)),
                            'control_ref':'a'*64+'@2026-11-04T20:00:00Z','process_key':'boot:42:987'}]
        self.monitor.inventory_times['a']=start
        await self.poll_at(start)
        self.assertEqual(len(self.calls),1);self.assertEqual(self.calls[0][1],'/queue/night-stop')
        rows={row['id']:row for row in self.queue.view(self.user)['tasks']}
        self.assertEqual(rows[night]['state'],'queued');self.assertEqual(rows[normal]['state'],'active')
        await self.poll_at(start+3);self.assertEqual(len(self.calls),1)
        self.monitor.rows=[];self.monitor.inventory_times['a']=start+10
        await self.poll_at(start+10)
        rows={row['id']:row for row in self.queue.view(self.user)['tasks']}
        self.assertEqual(rows[night]['state'],'active');self.assertEqual(rows[night]['activated'],start)
        self.assertNotIn(normal,rows)
        with self.store.db() as db:
            audit=db.execute('SELECT job,night_job,new_allocations FROM unified_night_reclaims').fetchone()
            self.assertEqual(tuple(audit),(normal,night,'{}'))
    async def test_missing_node_inventory_prevents_takeover(self):
        start=self.now+1200;night=self.add(self.user,night=True,not_before=start)
        self.monitor.inventory_times['a']=start
        self.monitor.valid={'b'}
        await self.poll_at(start)
        self.assertFalse(self.calls);self.assertTrue(self.coordinator.status)
        self.assertEqual(next(r for r in self.queue.view(self.user)['tasks'] if r['id']==night)['state'],'queued')
    async def test_expired_night_releases_without_stopping_even_if_inventory_unavailable(self):
        start=self.now+1200;night=self.add(self.user,night=True,not_before=start)
        self.monitor.inventory_times['a']=start
        await self.poll_at(start)
        self.assertEqual(next(r for r in self.queue.view(self.user)['tasks'] if r['id']==night)['state'],'active')
        self.monitor.rows=[{'node':'a','employee':'u12345678','card_ids':[0],
                            'control_ref':'a'*64+'@2026-11-04T20:00:00Z','process_key':'boot:42:987'}]
        self.monitor.valid={'b'}
        await self.poll_at(start+3600)
        self.assertFalse(self.calls)
        self.assertNotIn(night,[r['id'] for r in self.queue.view(self.user)['tasks']])

    async def test_active_reservation_does_not_clean_new_processes_and_finish_does_not_stop(self):
        start=self.now+1200;night=self.add(self.user,night=True,not_before=start)
        self.monitor.inventory_times['a']=start
        await self.poll_at(start)
        self.monitor.rows=[{'node':'a','employee':'u23456789','card_ids':[0],
                            'control_ref':'a'*64+'@2026-11-04T20:00:00Z','process_key':'boot:42:987'}]
        self.monitor.inventory_times['a']=start+10
        await self.poll_at(start+10)
        self.assertFalse(self.calls)
        self.queue.action(self.user,night,'finish')
        await self.poll_at(start+20)
        self.assertFalse(self.calls)
        self.assertNotIn(night,[r['id'] for r in self.queue.view(self.user)['tasks']])

    async def test_next_reservation_cleans_previous_owner_at_handoff(self):
        start=self.now+1200
        first=self.add(self.user,night=True,not_before=start)
        second=self.add(self.other,night=True,not_before=start+3600)
        self.monitor.inventory_times['a']=start
        await self.poll_at(start)
        self.monitor.rows=[{'node':'a','employee':'u12345678','card_ids':list(range(8)),
                            'control_ref':'a'*64+'@2026-11-04T20:00:00Z','process_key':'boot:42:987'}]
        self.monitor.inventory_times['a']=start+3600
        await self.poll_at(start+3600)
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.calls[0][2]['request']['allowed']['0'],['u23456789'])
        self.assertNotIn(first,[r['id'] for r in self.queue.view(self.user)['tasks']])
        self.monitor.rows=[];self.monitor.inventory_times['a']=start+3610
        await self.poll_at(start+3610)
        self.assertEqual(next(r for r in self.queue.view(self.other)['tasks'] if r['id']==second)['state'],'active')


if __name__=='__main__':unittest.main()
