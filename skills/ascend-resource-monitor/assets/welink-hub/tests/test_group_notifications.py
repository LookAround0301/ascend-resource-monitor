from contextlib import closing
from pathlib import Path
import os
import sqlite3
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(Path(os.environ.get('QUEUE_TEST_RUNTIME',ROOT.parent/'runtime'))))
sys.path.insert(0,str(ROOT.parent/'runtime'));sys.path.insert(0,str(ROOT/'src'))
from queue_monitor import QueueMonitor
from queue_group_alerts import violations
from hub_core import init_db
from group_notifications import initialize,reserve


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.now=1000
        self.monitor=QueueMonitor(['A5-118','A5-124'],{},None)
        self.monitor.inventory_times={'A5-118':1000}
        self.monitor.fresh_nodes=lambda now:{'A5-118'}
        self.process=dict(node='A5-118',employee='a00000001',name='Test User',npu_ids='0,1',process_key='private',start_time_seconds=900)
        self.monitor.current=lambda now:[self.process]

    def task(self,owner='b00000002',state='active',cards=(0,),allowed=()):
        return dict(id=owner,state=state,employee=owner,allocations={'A5-118':list(cards)},allowed_employees=list(allowed))

    def events(self,tasks):return violations(self.monitor,tasks,[],[],self.now)

    def test_unreserved_cards_and_ordinary_overdue_ownership_do_not_alert(self):
        self.assertEqual(self.events([]),[])
        self.assertEqual(self.events([self.task(owner='a00000001'),self.task(state='queued')]),[])

    def test_unregistered_usage_and_registered_excess_have_exact_unauthorized_cards(self):
        result=self.events([self.task()]);self.assertEqual(result[0]['cards'],[0])
        result=self.events([self.task(),self.task(owner='a00000001',cards=(1,))])
        self.assertEqual(result[0]['cards'],[0]);self.assertEqual(result[0]['employee'],'a00000001')
        self.assertNotIn('private',str(result))

    def test_couser_permissions_and_stale_or_unknown_observations_are_excluded(self):
        self.assertEqual(self.events([self.task(allowed=['a00000001'])]),[])
        self.monitor.inventory_times={'A5-118':900}
        self.assertEqual(self.events([self.task()]),[])
        self.monitor.inventory_times={'A5-118':1000};self.process['npu_ids']='unknown'
        self.assertEqual(self.events([self.task()]),[])

    def test_only_started_reservations_trigger_group_candidates(self):
        self.assertEqual(self.events([self.task(state='ready')]),[])
        self.assertEqual(self.events([self.task(state='queued')]),[])
        result=self.events([self.task(state='ready'),self.task(owner='c00000003',cards=(1,))])
        self.assertEqual(len(result),1);self.assertEqual(result[0]['cards'],[1])
        result=self.events([self.task(state='active'),self.task(owner='c00000003',cards=(1,))])
        self.assertEqual(len(result),1);self.assertEqual(result[0]['cards'],[0,1])
        self.process['name']='所有人';self.assertEqual(self.events([self.task()]),[])


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.path=str(Path(self.folder.name)/'state.db')
        init_db(self.path);initialize(self.path)
        self.config=dict(enabled=True,mention_verified=True,cluster='a5',group='1234567890123456789',repeat_seconds=600)

    def tearDown(self):self.folder.cleanup()

    def snapshot(self,now,**event):
        item=dict(id='a'*24,node='A5-118',employee='a00000001',name='User',cards=[0],sampled_at=now)
        item.update(event)
        return dict(cluster='a5',notifications_version=1,notifications_generated_at=now,violations=[item],fresh_nodes=['A5-118'])

    def test_two_distinct_samples_required_and_cooldown_survives_restart(self):
        self.assertEqual(reserve(self.path,self.snapshot(1000),self.config,1000),[])
        self.assertEqual(reserve(self.path,self.snapshot(1000),self.config,1005),[])
        self.assertEqual(len(reserve(self.path,self.snapshot(1015),self.config,1015)),1)
        initialize(self.path)
        self.assertEqual(reserve(self.path,self.snapshot(1030),self.config,1030),[])
        self.assertEqual(reserve(self.path,self.snapshot(1610),self.config,1610),[])
        self.assertEqual(len(reserve(self.path,self.snapshot(1625),self.config,1625)),1)

    def test_extensions_before_channel_activation_are_not_sent(self):
        snap=self.snapshot(1000);snap['violations']=[];snap['extensions']=[dict(id='extension:abc:2',at=990)]
        self.assertEqual(reserve(self.path,snap,dict(self.config,enabled_at=995),1000),[])
        snap['extensions'][0]['at']=996
        self.assertEqual(len(reserve(self.path,snap,dict(self.config,enabled_at=995),1000)),1)

    def test_disabled_or_unverified_and_wrong_cluster_never_send(self):
        for config in [dict(self.config,enabled=False),dict(self.config,mention_verified=False),dict(self.config,cluster='a3')]:
            self.assertEqual(reserve(self.path,self.snapshot(1000),config,1000),[])
            self.assertEqual(reserve(self.path,self.snapshot(1015),config,1015),[])

    def test_resolved_or_changed_event_requires_new_observations_and_retains_cooldown(self):
        reserve(self.path,self.snapshot(1000),self.config,1000)
        snapshot=self.snapshot(1010);snapshot['violations']=[]
        reserve(self.path,snapshot,self.config,1010)
        self.assertEqual(reserve(self.path,self.snapshot(1015),self.config,1015),[])
        self.assertEqual(reserve(self.path,self.snapshot(1030,id='b'*24),self.config,1030),[])
        self.assertEqual(len(reserve(self.path,self.snapshot(1045,id='b'*24),self.config,1045)),1)

    def test_stale_feed_never_counts_and_other_group_cannot_reset_observation(self):
        reserve(self.path,self.snapshot(1000),self.config,1000)
        self.assertEqual(reserve(self.path,self.snapshot(1000),self.config,1100),[])
        reserve(self.path,self.snapshot(1000),dict(self.config,group='1234567890123456'),1000)
        self.assertEqual(len(reserve(self.path,self.snapshot(1015),self.config,1015)),1)

    def test_unavailable_inventory_does_not_erase_first_observation(self):
        reserve(self.path,self.snapshot(1000),self.config,1000)
        gap=self.snapshot(1015);gap['fresh_nodes']=[];gap['violations']=[]
        self.assertEqual(reserve(self.path,gap,self.config,1015),[])
        self.assertEqual(len(reserve(self.path,self.snapshot(1030),self.config,1030)),1)

    def test_sample_freshness_uses_source_clock_and_rejects_stale_source_sample(self):
        first=self.snapshot(1000);second=self.snapshot(1015)
        self.assertEqual(reserve(self.path,first,self.config,1040),[])
        self.assertEqual(len(reserve(self.path,second,self.config,1055)),1)
        other=self.snapshot(2000);other['violations'][0]['sampled_at']=1960
        self.assertEqual(reserve(self.path,other,self.config,2000),[])

    def test_extension_announcements_deliver_once_across_restart(self):
        snapshot=self.snapshot(1000);snapshot['violations']=[]
        snapshot['extensions']=[dict(id='extension:abc:2',at=1000,employee='a00000001',name='Owner',
            count=2,minutes=10,total=20,allocations={'A5-118':[0]})]
        self.assertEqual(len(reserve(self.path,snapshot,self.config,1000)),1)
        initialize(self.path)
        self.assertEqual(reserve(self.path,snapshot,self.config,1015),[])


if __name__=='__main__':unittest.main()
