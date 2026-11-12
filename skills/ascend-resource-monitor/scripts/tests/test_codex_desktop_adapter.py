import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[3]/'a5-reservation/scripts'))
import a5_codex_desktop as adapter


class FakeRpc:
    sent=[]
    fail=False
    def initialize(self):pass
    def close(self):pass
    def call(self,method,params):
        self.sent.append(params)
        if self.fail:raise TimeoutError('delivery outcome unknown')
        return {'content':[{'type':'text','text':json.dumps({'threadId':params['arguments']['threadId']})}]}


class DesktopAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.plan=self.root/'plan.txt';self.plan.write_text('test plan',encoding='utf-8')
        self.session='test-session';self.job='a'*32
        self.event={'job':self.job,'session':self.session,'state':'ready',
                    'event_id':self.job+':1','plan':str(self.plan)}
        FakeRpc.sent=[];FakeRpc.fail=False
        self.env=patch.dict(os.environ,{'CODEX_THREAD_ID':self.session});self.env.start()
        self.root_patch=patch.object(adapter,'ROOT',self.root);self.root_patch.start()
        self.rpc_patch=patch.object(adapter,'Rpc',FakeRpc);self.rpc_patch.start()
    def tearDown(self):
        self.rpc_patch.stop();self.root_patch.stop();self.env.stop();self.temp.cleanup()
    def run_adapter(self,event=None):
        output=io.StringIO()
        with patch.object(sys,'argv',['a5_codex_desktop.py']),patch.object(sys,'stdin',io.StringIO(json.dumps(event or self.event))),patch.object(sys,'stdout',output):
            adapter.main()
        return json.loads(output.getvalue())
    def test_success_is_deduplicated_and_scoped_to_current_thread(self):
        self.assertEqual(self.run_adapter()['event_id'],self.event['event_id'])
        self.assertEqual(self.run_adapter()['event_id'],self.event['event_id'])
        self.assertEqual(len(FakeRpc.sent),1)
        call=FakeRpc.sent[0]
        self.assertEqual(call['arguments']['threadId'],self.session)
        self.assertIn(json.dumps(str(self.plan),ensure_ascii=False),call['arguments']['prompt'])
        self.assertEqual(call['_meta']['openai/threadId'],self.session)
        with self.assertRaises(ValueError):self.run_adapter({**self.event,'session':'different-session'})
    def test_unknown_result_is_never_resent(self):
        FakeRpc.fail=True
        with self.assertRaises(TimeoutError):self.run_adapter()
        FakeRpc.fail=False
        with self.assertRaisesRegex(RuntimeError,'结果不明'):self.run_adapter()
        self.assertEqual(len(FakeRpc.sent),1)


if __name__=='__main__':unittest.main()
