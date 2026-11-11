from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from export_snapshot import export


class ExportTest(unittest.TestCase):
    def test_minimal_export_reads_committed_wal_without_queue_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            database=root/'queue.db'
            with closing(sqlite3.connect(database)) as db:
                db.execute('PRAGMA journal_mode=WAL')
                db.execute('CREATE TABLE accounts (employee TEXT, name TEXT, digest TEXT)')
                db.execute('INSERT INTO accounts VALUES (?,?,?)',('u00000001','name','private-password-hash'))
                db.commit()
                state=root/'state.json'
                state.write_text(json.dumps({'tasks':[
                    dict(id='task',state='queued',employee='u00000001',salt='SECRET',
                         allowed_people=[dict(employee='a00000001',name='a',secret='hidden')]),
                    dict(id='finished',state='finished')], 'card_usage': {'sensitive_workdir':'hidden'}}))
                target=root/'snapshot.json'
                export(dict(state_url=state.as_uri(),database=str(database),cluster='test',label='Test',output=str(target)))
                result=json.loads(target.read_text(encoding='utf-8'))
                self.assertEqual(result['accounts'],['u00000001'])
                self.assertEqual(len(result['tasks']),1)
                self.assertEqual(result['tasks'][0]['allowed_people'],[dict(employee='a00000001',name='a')])
                self.assertNotIn('private-password-hash',target.read_text())
                self.assertNotIn('SECRET',target.read_text())
                self.assertNotIn('sensitive_workdir',target.read_text())
                self.assertEqual(db.execute('SELECT count(*) FROM accounts').fetchone()[0],1)

    def test_forced_reader_rejects_commands(self):
        import os
        reader=Path(__file__).resolve().parents[1]/'src'/'read_snapshot.py'
        for command in ('cat /etc/passwd','snapshot; id','sh',''):
            result=subprocess.run([sys.executable,str(reader)],env=dict(os.environ,SSH_ORIGINAL_COMMAND=command),capture_output=True)
            self.assertEqual(result.returncode,64)
            self.assertEqual(result.stdout,b'')

    def test_group_candidate_export_contains_no_raw_process_details(self):
        import time
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);database=root/'queue.db'
            with closing(sqlite3.connect(database)) as db,db:db.execute('CREATE TABLE accounts (employee TEXT)')
            (root/'state.json').write_text(json.dumps(dict(tasks=[],nodes=['A5-118'],cards_per_node=8)))
            (root/'token').write_text('test-only-not-a-real-token')
            event=dict(id='a'*24,node='A5-118',employee='a00000001',name='User',cards=[0],sampled_at=time.time(),private_cwd='PRIVATE-CWD')
            (root/'feed.json').write_text(json.dumps(dict(version=1,generated_at=time.time(),recipients=[],events=[],violations=[event])))
            export(dict(state_url=(root/'state.json').as_uri(),database=str(database),cluster='a5',label='A5',
                output=str(root/'snapshot.json'),notifications_url=(root/'feed.json').as_uri(),notification_token_file=str(root/'token')))
            snapshot=json.loads((root/'snapshot.json').read_text())
            self.assertEqual(snapshot['violations'][0]['cards'],[0])
            self.assertNotIn('PRIVATE-CWD',str(snapshot))
            self.assertNotIn('test-only-not-a-real-token',str(snapshot))

    def test_extension_feed_is_validated_and_reduced_before_export(self):
        import time
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);database=root/'queue.db'
            with closing(sqlite3.connect(database)) as db,db:db.execute('CREATE TABLE accounts (employee TEXT)')
            (root/'state.json').write_text(json.dumps(dict(tasks=[],nodes=['A5-118'],cards_per_node=8)))
            (root/'token').write_text('test-only-token')
            event=dict(id='extension:abc:2',employee='a00000001',name='Owner',at=time.time(),count=2,
                       minutes=10,total=20,allocations={'A5-118':[0]},private='secret')
            feed=dict(version=1,generated_at=time.time(),recipients=[],events=[],violations=[],
                      fresh_nodes=['A5-118'],extensions=[event])
            (root/'feed.json').write_text(json.dumps(feed))
            cfg=dict(state_url=(root/'state.json').as_uri(),database=str(database),cluster='a5',label='A5',
                output=str(root/'snapshot.json'),notifications_url=(root/'feed.json').as_uri(),notification_token_file=str(root/'token'))
            export(cfg)
            snapshot=json.loads((root/'snapshot.json').read_text())
            self.assertEqual(snapshot['extensions'][0]['count'],2)
            self.assertNotIn('secret',str(snapshot))
            event['allocations']={'A5-118':[8]};(root/'feed.json').write_text(json.dumps(feed))
            with self.assertRaises(ValueError):export(cfg)


if __name__=='__main__':
    unittest.main()
