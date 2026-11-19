import json
import secrets
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]/'assets/runtime'))
from control_common import ControlError
from queue_emergency import emergency
from queue_notifications import build_feed
from queue_store import QueueStore
from queue_unified import UnifiedQueue


class EmergencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.clock = patch('time.time', return_value=self.ts('2026-11-19T10:00:00')).start()
        self.addCleanup(patch.stopall)
        self.store = QueueStore(Path(self.tmp.name)/'queue.db')
        self.users = [self.store.user(self.store.login_without_password('a0000000'+str(i), 'User '+str(i))) for i in range(5)]
        self.admin = dict(self.users[4], admin=True)
        self.nodes = ['A5-'+str(n) for n in (179,183,187,191,220,224,228,232)]
        self.options = dict(timezone='Asia/Shanghai',strict_nights=True,overtime_control=True,
            midnight_separation=True,night_carryover_cutoff_hour=9,night_tomorrow_only=True,
            normal_max_seconds=28800,flexible_scheduling=True,admin_preemption_enabled=True)
        self.q = UnifiedQueue(self.store,self.nodes,**self.options)

    @staticmethod
    def ts(s): return datetime.fromisoformat(s).replace(tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()

    def add(self, who=0, **kw):
        data=dict(machine='multi:8',cards=64,requested_seconds=3600,notifications_ready=True,avoid_slots=[])
        data.update(kw)
        return self.q.enqueue(self.users[who],data)

    def rows(self): return {r['id']:r for r in self.q.view(self.admin)['tasks']}

    def active(self, who=0, **kw):
        job=self.add(who,**kw)
        self.q.action(self.users[who],job,'claim',self.rows()[job]['offer_id'])
        return job

    def preempt(self, job):
        preview=emergency(self.q,self.admin,job)
        request=(secrets.token_hex(16),preview['revision'])
        return emergency(self.q,self.admin,job,*request), request

    def test_selected_four_nodes_displace_only_conflicting_booking_and_notify_all_users(self):
        victim=self.active(machine='selected',selected_nodes=self.nodes[:4],cards=32)
        self.q.allowed_user(self.users[0],victim,'z00000099','add')
        untouched=self.active(1,machine='selected',selected_nodes=self.nodes[4:],cards=32)
        waiting=self.add(2)
        target=self.add(3,machine='selected',selected_nodes=self.nodes[:4],cards=32)
        original=self.rows()[victim]
        result,_=self.preempt(target)
        rows=self.rows()
        self.assertEqual(result['preempted'],[victim])
        self.assertEqual(rows[target]['state'],'ready')
        self.assertEqual(set(rows[target]['allocations']),set(self.nodes[:4]))
        self.assertEqual(rows[untouched]['state'],'active')
        self.assertEqual(rows[victim]['state'],'queued')
        self.assertEqual(rows[victim]['allocations'],{})
        self.assertIsNone(rows[victim]['activated'])
        for key in ('created','requested_seconds','cards','node_count','selected_nodes','allowed_employees'):
            self.assertEqual(rows[victim][key],original[key])
        self.assertLess(rows[victim]['queue_order'],rows[waiting]['queue_order'])
        feed=build_feed(self.q,self.store,None)
        events=[e for e in feed['events'] if e['kind']=='preempted']
        self.assertEqual({e['recipient'] for e in events},{self.users[0]['employee'],'z00000099'})
        self.assertTrue(all('不会停止进程或容器' in e['body'] for e in events))
        self.assertTrue(any(a['kind']=='preempted' for a in self.q.view(self.users[0])['alerts']))

    def test_eight_node_preemption_preserves_victim_order_ahead_of_existing_waiters(self):
        first=self.active(machine='selected',selected_nodes=self.nodes[:4],cards=32)
        second=self.active(1,machine='selected',selected_nodes=self.nodes[4:],cards=32)
        older=self.add(2);target=self.add(3)
        result,_=self.preempt(target)
        self.assertEqual(result['preempted'],[first,second])
        rows=self.rows()
        self.assertEqual([r['id'] for r in rows.values() if r['state']=='queued'],[first,second,older])
        self.assertEqual(sum(map(len,rows[target]['allocations'].values())),64)
        self.q=UnifiedQueue(self.store,self.nodes,**self.options)
        self.assertEqual([r['id'] for r in self.rows().values() if r['state']=='queued'],[first,second,older])

    def test_free_nodes_are_preferred_and_admin_can_override_avoid_start_slots(self):
        active=self.active(machine='selected',selected_nodes=self.nodes[:4],cards=32)
        target=self.add(1,machine='multi:4',cards=32,avoid_slots=[20,21])
        self.assertEqual(self.rows()[target]['state'],'queued')
        result,_=self.preempt(target)
        self.assertEqual(result['preempted'],[])
        self.assertEqual(set(result['allocations']),set(self.nodes[4:]))
        self.assertEqual(self.rows()[active]['state'],'active')
        self.q.action(self.users[1],target,'claim',self.rows()[target]['offer_id'])

    def test_three_selected_nodes_and_partial_cards_release_whole_displaced_request(self):
        old=self.active();target=self.add(1,machine='selected',selected_nodes=self.nodes[::3],cards=3)
        result,_=self.preempt(target)
        self.assertEqual(result['preempted'],[old])
        self.assertEqual(set(result['allocations']),set(self.nodes[::3]))
        self.assertEqual(list(map(len,result['allocations'].values())),[1,1,1])
        self.assertEqual(self.rows()[old]['total_cards'],64)

    def test_stale_preview_rolls_back_without_notifying_or_displacing(self):
        old=self.active();target=self.add(1)
        preview=emergency(self.q,self.admin,target)
        self.q.allowed_user(self.users[0],old,'z00000099','add')
        with self.assertRaisesRegex(ControlError,'状态已变化'):
            emergency(self.q,self.admin,target,secrets.token_hex(16),preview['revision'])
        self.assertEqual(self.rows()[old]['state'],'active')
        self.assertEqual(self.rows()[target]['state'],'queued')
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM unified_emergency_operations').fetchone()[0],0)

    def test_retry_is_idempotent_even_after_target_is_claimed(self):
        self.active();target=self.add(1)
        result,request=self.preempt(target)
        self.q.action(self.users[1],target,'claim',self.rows()[target]['offer_id'])
        self.assertEqual(emergency(self.q,self.admin,target,*request),result)
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM unified_notice_events').fetchone()[0],2)
        other=self.add(2)
        with self.assertRaisesRegex(ControlError,'操作编号'):
            emergency(self.q,self.admin,other,*request)

    def test_old_ready_offer_is_invalid_after_preemption(self):
        victim=self.add();offer=self.rows()[victim]['offer_id'];target=self.add(1)
        self.preempt(target)
        with self.assertRaises(ControlError): self.q.action(self.users[0],victim,'claim',offer)
        self.q.action(self.users[1],target,'cancel')
        self.assertEqual(self.rows()[victim]['state'],'ready')
        with self.assertRaises(ControlError): self.q.action(self.users[0],victim,'claim',offer)

    def test_persistent_notices_survive_removed_reservation_without_leaking(self):
        victim=self.active();target=self.add(1);self.preempt(target)
        self.q.action(self.users[0],victim,'cancel')
        self.q=UnifiedQueue(self.store,self.nodes,**self.options)
        self.assertTrue(any(e['kind']=='preempted' for e in build_feed(self.q,self.store,None)['events']))
        self.assertFalse(any(a['kind']=='preempted' for a in self.q.view(self.users[2])['alerts']))

    def test_requeued_job_stays_ahead_of_new_personal_urgent_job(self):
        victim=self.active();target=self.add(1);self.preempt(target)
        self.q.set_urgent_user(self.users[2]['employee'],self.users[2]['name'],True)
        newer=self.add(2,urgent=True)
        self.assertLess(self.rows()[victim]['queue_order'],self.rows()[newer]['queue_order'])

    def test_relay_accepts_preemption_notices_once_for_owner_and_collaborator(self):
        sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'assets/welink-hub/src'))
        from notification_store import init_notices, reserve_notices, queued, begin_attempt, accepted
        victim=self.active()
        self.q.allowed_user(self.users[0],victim,'z00000099','add')
        target=self.add(1);self.preempt(target)
        feed=build_feed(self.q,self.store,None)
        snapshot=dict(cluster='k31',notifications_version=1,notifications_generated_at=feed['generated_at'],
                      notifications=[e for e in feed['events'] if e['kind']=='preempted'])
        path=str(Path(self.tmp.name)/'relay.db');init_notices(path)
        reserve_notices(path,snapshot,set(feed['recipients']),self.clock.return_value-1,self.clock.return_value)
        outbox=queued(path,self.clock.return_value)
        self.assertEqual({e['recipient'] for e in outbox},{self.users[0]['employee'],'z00000099'})
        for item in outbox:
            self.assertTrue(begin_attempt(path,item['event'],self.clock.return_value));accepted(path,item['event'])
        reserve_notices(path,snapshot,set(feed['recipients']),self.clock.return_value-1,self.clock.return_value)
        self.assertEqual(queued(path,self.clock.return_value),[])

    def test_permissions_and_feature_flag(self):
        target=self.add()
        for user in (None,self.users[0]):
            with self.assertRaises(ControlError): emergency(self.q,user,target)
        self.q.admin_preemption_enabled=False
        with self.assertRaisesRegex(ControlError,'未启用'): emergency(self.q,self.admin,target)

    def test_night_boundaries_and_midnight_truncation(self):
        midnight=self.ts('2026-11-20T00:00:00')
        night=self.add(night=True,not_before=midnight)
        with self.assertRaisesRegex(ControlError,'普通预约'): emergency(self.q,self.admin,night)
        self.clock.return_value=self.ts('2026-11-19T23:55:00')+0.25
        target=self.add(1)
        self.preempt(target)
        row=self.rows()[target]
        self.assertEqual(row['effective_requested_seconds'],299)
        self.assertEqual(self.rows()[night]['not_before'],midnight)
        another=self.add(2)
        self.clock.return_value=midnight-30
        with self.assertRaisesRegex(ControlError,'不足 1 分钟'): emergency(self.q,self.admin,another)
        self.clock.return_value=midnight
        with self.assertRaisesRegex(ControlError,'09:00'): emergency(self.q,self.admin,another)

    def test_notice_write_failure_rolls_back_entire_preemption(self):
        old=self.active();target=self.add(1);preview=emergency(self.q,self.admin,target)
        with self.store.db() as db:
            db.execute("CREATE TRIGGER fail_notice BEFORE INSERT ON unified_notice_events BEGIN SELECT RAISE(ABORT,'fixture'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            emergency(self.q,self.admin,target,secrets.token_hex(16),preview['revision'])
        self.assertEqual(self.rows()[old]['state'],'active')
        self.assertEqual(self.rows()[target]['state'],'queued')


if __name__=='__main__': unittest.main()
