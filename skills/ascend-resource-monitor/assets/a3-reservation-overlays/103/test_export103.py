import json,sqlite3,tempfile,time,unittest
from pathlib import Path
from export_snapshot import export
class ExtendedExport(unittest.TestCase):
 def test_extended_accounts_and_group_events(self):
  with tempfile.TemporaryDirectory() as f:
   root=Path(f);db=sqlite3.connect(root/'db');db.execute('create table accounts(employee text)');db.executemany('insert into accounts values(?)', [('p_fixture',),('awx1234567',)]);db.commit();db.close()
   (root/'state').write_text(json.dumps(dict(tasks=[],nodes=['node-101'],cards_per_node=16)))
   (root/'token').write_text('fixture-token')
   event=dict(recipient='p_fixture',key='created',kind='agent-created',title='ok',body='ok',valid_until=time.time()+60)
   violation=dict(id='a'*24,node='node-101',employee='awx1234567',name='Fixture',cards=[15],sampled_at=time.time())
   feed=dict(version=1,generated_at=time.time(),recipients=['p_fixture','awx1234567'],events=[event],violations=[violation],fresh_nodes=['node-101'],extensions=[])
   (root/'feed').write_text(json.dumps(feed));export(dict(state_url=(root/'state').as_uri(),database=str(root/'db'),cluster='a3-101-103',label='A3',output=str(root/'out'),notifications_url=(root/'feed').as_uri(),notification_token_file=str(root/'token')))
   out=json.loads((root/'out').read_text());self.assertEqual(set(out['accounts']),{'p_fixture','awx1234567'});self.assertEqual(out['notifications'][0]['recipient'],'p_fixture');self.assertEqual(out['violations'][0]['employee'],'awx1234567')
