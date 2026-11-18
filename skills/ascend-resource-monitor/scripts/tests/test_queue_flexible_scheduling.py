"""Scheduling preferences and daily periods work without enabling enforcement."""
from datetime import datetime
from pathlib import Path
import json, sys, tempfile, unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'assets/runtime'))
from queue_store import QueueStore
from queue_unified import UnifiedQueue
from control_common import ControlError


class FlexibleSchedulingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=QueueStore(Path(self.temp.name)/'queue.db')
        self.user=self.store.user(self.store.register('u12345678','Owner','fixture-password'))
        self.clock=patch('queue_unified.time.time').start();self.addCleanup(patch.stopall)
        self.at('2026-11-18T11:10:00')
        self.nodes=[f'n{i}' for i in range(8)]
        self.queue=self.reopen()

    def at(self,value):
        self.clock.return_value=datetime.fromisoformat(value).replace(tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        return self.clock.return_value

    def reopen(self):
        return UnifiedQueue(self.store,self.nodes,timezone='Asia/Shanghai',daily_reset=True,
                            flexible_scheduling=True,daily_reset_normal_cutoff_hour=2)

    def add(self,**values):
        return self.queue.enqueue(self.user,{'machine':'selected','selected_nodes':['n1','n4','n7'],
                    'cards':24,'requested_seconds':3600,'avoid_slots':[],'notifications_ready':True,**values})

    def rows(self):
        return {r['id']:r for r in self.queue.view(self.user)['tasks']}

    def test_backfill_claim_shortens_duration_and_protects_exact_selected_machines(self):
        first=self.add(avoid_slots=[22,23])
        later=self.add()
        self.assertEqual(self.rows()[later]['state'],'queued')
        self.queue.update_backfill(self.user,later,1200)
        rows=self.rows();row=rows[later]
        self.assertEqual(row['state'],'ready')
        self.assertEqual(row['effective_requested_seconds'],3000)
        self.assertEqual(set(row['allocations']),{'n1','n4','n7'})
        expected=rows[first]['estimated_start']
        self.clock.return_value+=300
        self.queue.action(self.user,later,'claim',row['offer_id'])
        self.assertEqual(self.rows()[later]['requested_seconds'],2700)
        self.assertEqual(self.rows()[first]['estimated_start'],expected)
        self.at('2026-11-18T12:00:00')
        self.assertEqual(self.rows()[later]['state'],'active')
        self.assertEqual(self.rows()[first]['state'],'queued')
        self.queue.action(self.user,later,'finish')
        self.assertEqual(self.rows()[first]['state'],'ready')
        self.assertFalse(self.queue.strict_nights)
        self.assertFalse(self.queue.overtime_control)

    def test_minimum_duration_opt_out_and_stale_offer(self):
        self.add(avoid_slots=[22,23])
        later=self.add(backfill_min_seconds=3300)
        self.assertEqual(self.rows()[later]['state'],'queued')
        self.queue.update_backfill(self.user,later,1800)
        offer=self.rows()[later]['offer_id']
        self.queue.update_backfill(self.user,later,0)
        self.assertEqual(self.rows()[later]['state'],'queued')
        with self.assertRaises(ControlError):self.queue.action(self.user,later,'claim',offer)
        self.queue.update_backfill(self.user,later,1800)
        offer=self.rows()[later]['offer_id']
        self.at('2026-11-18T11:30:02')
        with self.assertRaises(ControlError):self.queue.action(self.user,later,'claim',offer)
        self.assertEqual(self.rows()[later]['state'],'queued')

    def test_avoid_windows_retract_offers_and_can_be_edited(self):
        self.at('2026-11-18T11:59:00')
        job=self.add(avoid_slots=[24,25,26,27])
        offer=self.rows()[job]
        self.assertEqual(offer['state'],'ready')
        self.assertEqual(offer['claim_before'],self.clock.return_value+60)
        self.at('2026-11-18T12:00:00')
        self.assertEqual(self.rows()[job]['state'],'queued')
        with self.assertRaises(ControlError):self.queue.action(self.user,job,'claim',offer['offer_id'])
        self.queue.update_avoid_slots(self.user,job,[])
        self.assertEqual(self.rows()[job]['state'],'ready')
        self.queue.update_avoid_slots(self.user,job,[24])
        self.assertEqual(self.rows()[job]['state'],'queued')

    def test_existing_active_duration_and_legacy_two_am_start_limit_unchanged(self):
        self.at('2026-11-19T01:45:00')
        job=self.add(requested_seconds=7200)
        self.queue.action(self.user,job,'claim')
        before=self.rows()[job]
        self.queue=self.reopen()
        self.at('2026-11-19T02:01:00')
        after=self.rows()[job]
        for key in ('activated','requested_seconds','allocations','state'):
            self.assertEqual(after[key],before[key])
        self.assertEqual(after['requested_seconds'],7200)

    def test_daily_reset_retains_new_period_including_boundary_and_delayed_restart(self):
        self.at('2026-11-19T01:59:59');old=self.add()
        self.at('2026-11-19T02:00:00');boundary=self.add()
        self.at('2026-11-19T08:30:00');new=self.add()
        self.at('2026-11-19T09:30:00');self.queue=self.reopen()
        rows=self.rows()
        self.assertNotIn(old,rows)
        self.assertEqual(set(rows),{boundary,new})
        self.assertEqual(self.queue.view(self.user)['last_daily_reset']['count'],1)
        self.at('2026-11-19T11:00:00')
        self.assertEqual(set(self.rows()),{boundary,new})
        self.at('2026-11-20T09:00:00')
        self.assertFalse(self.rows())

    def test_new_period_active_reservation_is_preserved_by_daily_reset(self):
        self.at('2026-11-19T08:30:00');job=self.add()
        self.queue.action(self.user,job,'claim');before=self.rows()[job]
        self.at('2026-11-19T09:00:00');after=self.rows()[job]
        for key in ('activated','requested_seconds','allocations','state'):
            self.assertEqual(after[key],before[key])

    def test_invalid_preferences_and_legacy_disabled_behavior(self):
        for values in ({'avoid_slots':[48]},{'backfill_min_seconds':301},{'backfill_min_seconds':3900}):
            with self.assertRaises(ControlError):self.add(**values)
        self.queue=UnifiedQueue(self.store,self.nodes)
        with self.assertRaises(ControlError):self.add(backfill_min_seconds=300)
        for hour in (-1,10,True,2.5):
            with self.assertRaises(ValueError):UnifiedQueue(self.store,self.nodes,daily_reset_normal_cutoff_hour=hour)


if __name__=='__main__':unittest.main()
