from pathlib import Path
import tempfile,unittest
from unittest.mock import patch
from datetime import datetime
from zoneinfo import ZoneInfo
from queue_unified import UnifiedQueue
from queue_store import QueueStore
from control_common import ControlError

class Boundary(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
  self.store=QueueStore(Path(self.tmp.name)/"queue.db"); self.u=self.store.user(self.store.register("a12345678","Test","fixture-password"))
  self.q=UnifiedQueue(self.store,["node-101","node-103"],timezone="Asia/Shanghai",daily_reset=True,cards_per_node=16,strict_nights=True,overtime_control=True,normal_priority=True,normal_max_seconds=7200,night_carryover_cutoff_hour=9,midnight_separation=True,night_start_hour=2)
  self.clock=patch("queue_unified.time.time").start();self.addCleanup(patch.stopall);self.clock.return_value=self.ts("2026-11-14T23:30:00")
 def ts(self,s):return datetime.fromisoformat(s).replace(tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
 def add(self,**k):
  d=dict(machine="node-101",cards=1,cards_per_node=16,requested_seconds=7200,notifications_ready=True,avoid_slots=[]);d.update(k);return self.q.enqueue(self.u,d)
 def row(self,id):return next(r for r in self.q.view(self.u)["tasks"] if r["id"]==id)
 def test_night_boundary_and_duration(self):
  with self.assertRaises(ControlError):self.add(night=True,not_before=self.ts("2026-11-15T01:55:00"))
  n=self.add(night=True,not_before=self.ts("2026-11-15T02:00:00"),requested_seconds=7200)
  self.assertEqual(self.row(n)["requested_seconds"],7200)
  n2=self.add(night=True,not_before=self.ts("2026-11-15T08:00:00"),requested_seconds=7200)
  self.assertEqual(self.row(n2)["requested_seconds"],3600)
 def test_normal_cutoff(self):
  j=self.add();r=self.row(j);self.q.action(self.u,j,"claim",r["offer_id"])
  self.assertEqual(self.row(j)["requested_seconds"],7200)
  self.assertEqual(self.q.normal_night_end(self.clock.return_value),self.ts("2026-11-15T02:00:00"))
  self.clock.return_value=self.ts("2026-11-15T02:00:00");self.q.tick();self.assertFalse(any(r["id"]==j for r in self.q.view(self.u)["tasks"]))
 def test_normal_overnight(self):
  self.clock.return_value=self.ts("2026-11-15T02:00:00");j=self.add();self.assertEqual(self.row(j)["state"],"queued")
  self.clock.return_value=self.ts("2026-11-15T09:00:00");self.q.tick();self.assertEqual(self.row(j)["state"],"ready")
 def test_normal_early_morning_can_start_before_two(self):
  self.clock.return_value=self.ts("2026-11-15T01:30:00");j=self.add();r=self.row(j);self.q.action(self.u,j,"claim",r["offer_id"]);self.assertEqual(self.row(j)["requested_seconds"],1800)
 def test_night_not_displaced_by_late_normal(self):
  night=self.add(night=True,not_before=self.ts("2026-11-15T02:00:00"),requested_seconds=7200)
  self.clock.return_value=self.ts("2026-11-15T01:30:00")
  normal=self.add();r=self.row(normal);self.q.action(self.u,normal,"claim",r["offer_id"])
  self.assertEqual(self.row(normal)["activated"]+self.row(normal)["requested_seconds"],self.ts("2026-11-15T02:00:00"))
  self.assertEqual(self.row(night)["not_before"],self.ts("2026-11-15T02:00:00"))
 def test_maximum_and_cards(self):
  with self.assertRaises(ControlError):self.add(requested_seconds=7260)
  j=self.add(cards=16);self.assertEqual(self.row(j)["cards"],16)
