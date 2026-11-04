"""Personnel writes use private temporary rosters, never a server's real roster."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'assets/runtime'))
from control_common import ControlError
from personnel_roster import RosterStore, combine, personnel


class RosterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'owners.json'
        self.original = {'employee_names': {'u12345678': '已有人员'}, 'containers': {'keep': 'u12345678'}, 'services': {}}
        self.path.write_text(json.dumps(self.original), encoding='utf-8')
        self.path.chmod(0o640); self.events = []
        self.engine = SimpleNamespace(config={'node': 'node-a', 'guard': {'owners_file': str(self.path)}},
                                      lock=threading.RLock(), write_audit=self.events.append)
        self.store = RosterStore(self.engine)
        self.actor = {'id': 1, 'login': 'admin'}

    def data(self, **extra):
        return {'node': 'node-a', 'employee': 'U12345679', 'name': ' 新人员 ',
                'revision': self.store.snapshot()['revision'], 'confirmed': True, **extra}

    def test_add_preserves_other_fields_permissions_and_retries(self):
        request = self.data(); mode = self.path.stat().st_mode & 0o777
        self.assertTrue(self.store.add(request, self.actor)['changed'])
        mapping = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual(mapping['containers'], self.original['containers'])
        self.assertEqual(mapping['services'], {})
        self.assertEqual(mapping['employee_names']['u12345678'], '已有人员')
        self.assertEqual(mapping['employee_names']['u12345679'], '新人员')
        self.assertEqual(self.path.stat().st_mode & 0o777, mode)
        before = self.path.read_bytes(); stamp = self.path.stat().st_mtime_ns
        self.assertFalse(self.store.add(request, self.actor)['changed'])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.path.stat().st_mtime_ns, stamp)
        self.assertEqual([e['result'] for e in self.events], ['requested', 'saved'])
        self.assertEqual(list(self.path.parent.glob('*.roster-*')), [])

    def test_reject_conflict_stale_revision_actor_and_unconfirmed(self):
        before = self.path.read_bytes()
        for data, actor in [(self.data(employee='u12345678', name='另一个名字'), self.actor),
                            (self.data(revision='0'*64), self.actor),
                            (self.data(confirmed=False), self.actor), (self.data(), {})]:
            with self.assertRaises(ControlError): self.store.add(data, actor)
            self.assertEqual(self.path.read_bytes(), before)

    def test_validate_identity_and_name(self):
        self.assertEqual(personnel(' U12345678 ', ' 张三 '), ('u12345678', '张三'))
        for employee, name in [('12345678', '名'), ('u123', '名'), ('u12345678', ''),
                               ('u12345678', '名\n字'), ([], '名'), ('u12345678', 'x'*121)]:
            with self.assertRaises(ControlError): personnel(employee, name)

    def test_audit_failure_does_not_write(self):
        before = self.path.read_bytes()
        def fail(event): raise OSError('audit unavailable')
        self.engine.write_audit = fail
        with self.assertRaises(OSError): self.store.add(self.data(), self.actor)
        self.assertEqual(self.path.read_bytes(), before)

    def test_bad_or_empty_roster_fails_closed(self):
        for value in ['not json', '{}', '{"employee_names": {}}']:
            self.path.write_text(value, encoding='utf-8')
            with self.assertRaises(ControlError): self.store.snapshot()

    def test_union_reports_missing_or_conflicting_sources(self):
        one = {'people': [{'employee': 'u12345678', 'name': '甲'}]}
        self.assertEqual(combine({'a': one, 'b': one})['issues'], [])
        self.assertEqual(len(combine({'a': one, 'b': {'error': True}})['issues']), 1)
        different = {'people': [{'employee': 'u12345678', 'name': '乙'}]}
        self.assertTrue(combine({'a': one, 'b': different})['issues'])


if __name__ == '__main__': unittest.main()
