import json
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from hub_core import allow_query, fresh, init_db, parse_sender, relevant, report, reserve_delivery


class PrivacyTest(unittest.TestCase):
    def test_owner_and_couser_only(self):
        row = {'employee': 'a00000001', 'allowed_people': [{'employee': 'b00000002'}]}
        self.assertTrue(relevant(row, 'a00000001'))
        self.assertTrue(relevant(row, 'b00000002'))
        self.assertFalse(relevant(row, 'c00000003'))

    def test_report_excludes_other_users(self):
        snapshot = {'label': 'A3 101/103', 'tasks': [
            {'id': 'private', 'state': 'active', 'employee': 'a00000001', 'fixed_node': 'private-node'},
            {'id': 'mine', 'state': 'queued', 'employee': 'b00000002', 'cards': 2}]}
        text = report(snapshot, 'b00000002')
        self.assertNotIn('private', text)
        self.assertIn('2 卡', text)
        self.assertIn('A3 101/103', text)

    def test_callback_identity_constraints(self):
        body = dict(sender='u00000001', receiver='u00000001', type='Text', content='/预约迁移试验 查询')
        self.assertEqual(parse_sender(body), 'u00000001')
        for change in ({'receiver': 'a00000001'}, {'sender': '../bad'}, {'type': 'Image'}, {'content': 3}):
            self.assertIsNone(parse_sender(dict(body, **change)))

    def test_freshness_and_cluster_binding(self):
        snapshot = dict(version=1, cluster='a5', exported_at=100, accounts=[], tasks=[])
        self.assertTrue(fresh(snapshot, 'a5', 150))
        self.assertFalse(fresh(snapshot, 'other', 150))
        self.assertFalse(fresh(snapshot, 'a5', 200))
        self.assertFalse(fresh(snapshot, 'a5', 50))
        self.assertFalse(fresh({}, 'a5', 100))

    def test_rate_limit_persists(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / 'state.db')
            init_db(path)
            self.assertTrue(allow_query(path, 'a00000001', 101))
            self.assertFalse(allow_query(path, 'a00000001', 102))
            self.assertTrue(allow_query(path, 'a00000001', 131))
            for index in range(18):
                self.assertTrue(allow_query(path, 'b%08d' % index, 132))
            self.assertFalse(allow_query(path, 'c00000001', 132))

    def test_delivery_dedup_and_local_import(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / 'state.db')
            init_db(path)
            row = dict(id='one', offer_id='two')
            with closing(sqlite3.connect(path)) as database, database:
                database.execute('INSERT INTO deliveries VALUES (?,?,?)', ('ready:one:two', 0, 'accepted'))
            self.assertIsNone(reserve_delivery(path, 'a5', row, 'u00000001', legacy_employee='u00000001'))
            self.assertIsNotNone(reserve_delivery(path, 'a3-101-103', row, 'u00000001'))
            self.assertIsNone(reserve_delivery(path, 'a3-101-103', row, 'u00000001'))
            self.assertIsNotNone(reserve_delivery(path, 'a5', row, 'b00000002'))


if __name__ == '__main__':
    unittest.main()
