import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock

RUNTIME = Path(__file__).resolve().parents[2]/'assets/runtime'
sys.path.insert(0, str(RUNTIME))
import job_evidence as e


class EvidenceTests(unittest.TestCase):
    def test_literal_nested_shell_and_python_paths_without_execution(self):
        contexts = {}
        scripts, records = e.shell_paths('#!/bin/bash\n# ignored\ncd /job; bash -lc "python3 -u ./run.py >> ./run.jsonl"', '/', contexts=contexts)
        self.assertEqual(scripts, ['/job/run.py']); self.assertEqual(records, ['/job/run.jsonl'])
        self.assertEqual(contexts['/job/run.py'], '/job')
        self.assertEqual(e.command_paths(['echo', '/tmp/not-executed.sh'], '/')[0], [])
        self.assertEqual(e.command_paths(['bash', '-c', 'python3 $(bad).py'], '/')[0], [])
        contexts = {}
        self.assertEqual(e.python_paths('import subprocess\nsubprocess.Popen(["bash", "./start.sh"], cwd="/work")', '/', contexts)[0], ['/work/start.sh'])
        self.assertEqual(contexts['/work/start.sh'], '/work')
        self.assertEqual(e.shell_paths('cd $UNKNOWN; python3 relative.py', '/job')[0], [])
        self.assertEqual(e.python_paths('subprocess.run(["python3", "relative.py"], cwd=unknown)', '/job')[0], [])

    def test_record_must_match_process_lifetime_not_only_pid_or_container(self):
        identities = [{'pid': 42, 'namespace_pid': 7, 'start_ticks': 200, 'container_id': 'c'*64}]
        base = {'container_id': 'c'*64, 'pid': 7, 'process_start_time_seconds': 1002}
        self.assertTrue(e.matching_record(base, identities, 'abcdef123456-long', 100, 1000))
        self.assertFalse(e.matching_record(dict(base, process_start_time_seconds=901), identities, 'abcdef123456-long', 100, 1000))
        self.assertFalse(e.matching_record({'pid': 7, 'container_id': 'c'*64}, identities, 'abcdef123456-long', 100, 1000))
        self.assertFalse(e.matching_record({'boot_id': [], 'origin': ['agent']}, identities, 'abcdef123456-long', 100, 1000))
        self.assertTrue(e.matching_record({'process_key': 'abcdef123456:42:200'}, identities, 'abcdef123456-long', 100, 1000))

    def test_polling_alone_does_not_imply_agent_and_conflicts_are_visible(self):
        self.assertEqual(e.merge_evidence(0, {'polling_hint': True}), 0)
        self.assertEqual(e.merge_evidence(0, {'script_hint': True}), 2)
        self.assertEqual(e.merge_evidence(0, {'declared': 'agent'}), 3)
        self.assertEqual(e.merge_evidence(3, {'declared': 'manual'}), 4)
        self.assertEqual(e.merge_evidence(1, {'record_hint': True}), 4)

    def test_failed_helper_never_discards_existing_classification(self):
        probe = e.EvidenceProbe()
        row = {'process_key': 'key', 'pid': 42, 'start_ticks': 200, 'container_id': 'c'*64}
        with patch.object(e.subprocess, 'Popen', side_effect=OSError):
            self.assertEqual(probe.check([row]), {})
        self.assertEqual(probe.status, 'failed')
        self.assertEqual(e.merge_evidence(3, {}), 3)

    def test_cache_is_retired_when_pid_lifetime_changes(self):
        probe = e.EvidenceProbe(); now = e.time.monotonic()
        probe.cache = {'old': (now, {'declared': 'agent'})}; probe.next_run = now + 60
        row = {'process_key': 'new', 'pid': 42, 'start_ticks': 300, 'container_id': 'c'*64}
        self.assertEqual(probe.check([row]), {})
        self.assertFalse(probe.cache)

    def test_timeout_kills_only_the_inspection_helper_and_returns_no_new_evidence(self):
        probe = e.EvidenceProbe()
        child = Mock(); child.communicate.side_effect = [subprocess.TimeoutExpired('inspector', 2), (b'', b'')]
        row = {'process_key': 'key', 'pid': 42, 'start_ticks': 200, 'container_id': 'c'*64}
        with patch.object(e.subprocess, 'Popen', return_value=child):
            self.assertEqual(probe.check([row]), {})
        child.kill.assert_called_once()
        self.assertEqual(probe.status, 'timeout')

    @unittest.skipUnless(os.name == 'posix', 'Container-root reads use POSIX directory file descriptors')
    def test_container_reads_reject_links_devices_and_oversized_scripts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); (root/'job').mkdir(); (root/'job/run.sh').write_text('echo ok')
            (root/'job/link.sh').symlink_to('/etc/passwd')
            (root/'job/huge.py').write_text('x'*40000)
            os.mkfifo(root/'job/pipe.sh')
            reader = e.ContainerReader(root, {'files': 0, 'bytes': 0})
            try:
                self.assertEqual(reader.read('/job/run.sh'), 'echo ok')
                for path in ['/job/link.sh', '/job/huge.py', '/job/pipe.sh', '/etc/passwd', '/job/../etc/passwd']:
                    self.assertIsNone(reader.read(path), path)
            finally: reader.close()

    @unittest.skipUnless(os.name == 'posix', 'Container-root reads use POSIX directory file descriptors')
    def test_linked_script_and_record_inspection_returns_only_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            proc = Path(temp); root = proc/'42/root'; (root/'job').mkdir(parents=True)
            (root/'job/start.sh').write_text('# Generated by Codex\nPASSWORD=SECRET\npython3 /job/train.py >> /job/run.jsonl\nwhile true; do docker inspect c; sleep 5; done\n')
            (root/'job/train.py').write_text('print("worker")')
            record = {'process_key': 'abcdef123456:42:200', 'origin': 'agent', 'agent_name': 'codex', 'action': 'poll', 'timestamp': 1003, 'token': 'SECRET'}
            # A large log is read from its bounded tail, not discarded or loaded in full.
            (root/'job/run.jsonl').write_text('x'*20000+'\n'+json.dumps(record)+'\n'+json.dumps(dict(record, timestamp=1008))+'\n')
            (proc/'sys/kernel/random').mkdir(parents=True); (proc/'sys/kernel/random/boot_id').write_text('abcdef123456-long')
            (proc/'stat').write_text('btime 1000\n')
            identity = {'pid': 42, 'ppid': 1, 'start_ticks': 200, 'container_id': 'c'*64, 'namespace_pid': 7, 'cwd': '/job', 'exe': '/bin/bash', 'argv': ['bash', '/job/start.sh']}
            request = {'pid': 42, 'start_ticks': 200, 'container_id': 'c'*64}
            with patch.object(e, 'snapshot', return_value=identity):
                result = e.inspect_process(request, {'files': 0, 'bytes': 0}, proc)
            self.assertEqual(result['declared'], 'agent')
            self.assertEqual(result['scripts'], 2)
            self.assertTrue(result['script_hint']); self.assertTrue(result['record_hint']); self.assertTrue(result['polling_hint'])
            self.assertNotIn('SECRET', json.dumps(result)); self.assertNotIn('/job', json.dumps(result))
            with patch.object(e, 'snapshot', side_effect=[identity, dict(identity, start_ticks=999)]):
                result = e.inspect_process(request, {'files': 0, 'bytes': 0}, proc)
            self.assertFalse(result['script_hint']); self.assertEqual(result['declared'], '')


if __name__ == '__main__': unittest.main()
