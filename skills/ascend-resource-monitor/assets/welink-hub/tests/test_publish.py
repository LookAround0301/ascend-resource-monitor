import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from publisher import publish, synchronize


class PublishTest(unittest.TestCase):
    def test_success_and_ambiguous_retry_guard(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/'token').write_text('u00000001_FAKE_TEST_ONLY')
            config=dict(owner='u00000001',token_file=str(root/'token'),published_config=str(root/'published.json'))
            plugin=dict(owner='u00000001',plugin_name='A5预约',whitelist=dict(individual=['u00000001'],group=[],department=[]))
            publish(config,plugin,send=lambda payload:'发布插件成功')
            self.assertEqual(json.loads((root/'published.json').read_text(encoding='utf-8')),plugin)
            self.assertFalse(synchronize(config,['u00000001'],send=lambda payload:self.fail('unnecessary publish')))
            self.assertTrue(synchronize(config,['u00000001','a00000001'],send=lambda payload:'发布插件成功'))
            with self.assertRaises(ValueError):
                publish(config,plugin,send=lambda payload:'unknown')
            with self.assertRaises(RuntimeError):
                publish(config,plugin,send=lambda payload:self.fail('ambiguous retry'))


if __name__=='__main__':
    unittest.main()
