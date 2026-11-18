import json
import secrets
import tempfile
import unittest
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'assets/runtime'))
from queue_store import QueueStore
from queue_unified import UnifiedQueue
from queue_agent import command,initialize_events
from queue_notifications import agent_notice,build_feed
import time

class EightNodeAgentTests(unittest.TestCase):
    def test_selected_nodes_and_eight_node_notices_preserve_resource_request(self):
        with tempfile.TemporaryDirectory() as folder:
            store=QueueStore(Path(folder)/'queue.db');store.login_without_password('a00000001','Test User')
            nodes=['A5-'+str(n) for n in (179,183,187,191,220,224,228,232)]
            queue=UnifiedQueue(store,nodes,normal_max_seconds=28800,flexible_scheduling=True,timezone='Asia/Shanghai')
            initialize_events(store)
            for machine,selected,cards in [('multi:8',[],8),('selected',nodes[:3],3)]:
                with self.subTest(machine=machine):
                    data=dict(machine=machine,cards=cards,requested_seconds=28800,avoid_slots=list(range(48)))
                    if selected:data['selected_nodes']=selected
                    payload=dict(mode='agent_execute',employee='a00000001',operation=secrets.token_hex(16),expires=time.time()+300,request=data)
                    first=command(queue,store,payload);self.assertEqual(first,command(queue,store,payload))
                    with store.db() as db:
                        row=db.execute('SELECT * FROM unified_tasks WHERE id=?',(first['job'],)).fetchone()
                        event=db.execute("SELECT * FROM unified_notice_events WHERE job=? AND kind='agent-created'",(first['job'],)).fetchone()
                    self.assertEqual(row['node_count'],3 if selected else 8);self.assertEqual(row['requested_seconds'],28800)
                    self.assertEqual(json.loads(row['selected_nodes']),selected)
                    notice=agent_notice(event,queue)
                    self.assertNotIn('multi:',notice['body']);self.assertNotIn('selected',notice['body'])
                    for node in selected:self.assertIn(node,notice['body'])
                    if not selected:self.assertIn('8 台机器',notice['body'])

if __name__=='__main__':unittest.main()
