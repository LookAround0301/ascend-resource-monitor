import copy
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import types
import unittest
from unittest.mock import patch

RUNTIME = Path(__file__).resolve().parents[2] / 'assets/runtime'
sys.path.insert(0, str(RUNTIME))
from job_origin import OriginClassifier, agent_hint, environment_origin, summarize_origin, temporary_script
import process_exporter as exporter
from process_panels import add_job_origin, add_name_columns, add_process_panels
from patch_dashboard import add_stop_button


class OriginTests(unittest.TestCase):
    def test_marker_is_exact_and_does_not_return_other_environment_values(self):
        raw = b'PASSWORD=SECRET\0ASCEND_MONITOR_JOB_ORIGIN=agent\0TOKEN=SECRET'
        self.assertEqual(environment_origin(raw), 'agent')
        self.assertEqual(environment_origin(b'ASCEND_MONITOR_JOB_ORIGIN=manual'), 'manual')
        self.assertEqual(environment_origin(b'ASCEND_MONITOR_JOB_ORIGIN=SECRET'), '')
        self.assertEqual(environment_origin(b'ASCEND_MONITOR_JOB_ORIGIN=agent\0ASCEND_MONITOR_JOB_ORIGIN=manual'), 'conflict')

    def test_hints_require_specific_identity_not_the_word_agent_or_workload_args(self):
        self.assertTrue(agent_hint('/usr/bin/codex'))
        self.assertTrue(agent_hint('/usr/bin/node', ['node', '/opt/@anthropic-ai/claude-code/cli.js']))
        self.assertTrue(agent_hint(cwd='/data/.remote-dev/jobs/123/run'))
        for path in ['/usr/bin/ssh', '/usr/bin/ssh-agent', '/usr/bin/python', '/work/agent-project']:
            self.assertFalse(agent_hint(path, ['python', 'worker.py', 'codex'], '/job'))

    def test_parent_hint_boundaries_pid_reuse_and_explicit_markers(self):
        row = {'pid': 99, 'ppid': 20, 'start_ticks': 100}
        classifier = OriginClassifier()
        with patch.object(classifier, 'observe', return_value=(1, 80, True, False, False, 0, False)):
            self.assertEqual(classifier.classify(row), 2)
            self.assertEqual(classifier.classify(dict(row, origin_marker='agent')), 3)
            self.assertEqual(classifier.classify(dict(row, origin_marker='manual')), 4)
        for observation in [None, (1, 101, True, False, False, 0, False), (1, 80, True, True, False, 0, False)]:
            with patch.object(classifier, 'observe', return_value=observation):
                self.assertEqual(classifier.classify(row), 0)
        with patch.object(classifier, 'observe', return_value=None):
            self.assertEqual(classifier.classify(dict(row, origin_marker='manual')), 1)
            self.assertEqual(classifier.classify(dict(row, user='root', login_user='a00000001', tty=True)), 0)

    def test_parent_walk_is_bounded_and_handles_cycles(self):
        classifier = OriginClassifier(max_depth=3)
        row = {'pid': 99, 'ppid': 20, 'start_ticks': 100}
        with patch.object(classifier, 'observe', side_effect=[(19, 99, False, False, False, 0, False), (18, 98, False, False, False, 0, False), (17, 97, False, False, False, 0, False)]) as observe:
            self.assertEqual(classifier.classify(row), 0)
            self.assertEqual(observe.call_count, 3)
        with patch.object(classifier, 'observe', return_value=(20, 80, False, False, False, 0, False)) as observe:
            self.assertEqual(classifier.classify(row), 0)
            self.assertEqual(observe.call_count, 1)

    def test_ssh_tmp_and_noninteractive_are_required_together(self):
        self.assertTrue(temporary_script('/usr/bin/python3', ['python3', '/tmp/run.py']))
        self.assertFalse(temporary_script('/usr/bin/python3', ['python3', '/job/run.py']))
        classifier = OriginClassifier()
        row = {'pid': 99, 'ppid': 20, 'start_ticks': 100, 'has_tty': False}
        shell = (10, 90, False, False, True, 0, False)
        ssh = (1, 80, False, True, False, 0, True)
        for first, last, expected in [(shell, ssh, 2),
                                      ((10, 90, False, False, False, 0, False), ssh, 0),
                                      ((10, 90, False, False, True, 123, False), ssh, 0),
                                      (shell, (1, 80, False, True, False, 0, False), 0)]:
            with patch.object(classifier, 'observe', side_effect=[first, last]):
                self.assertEqual(classifier.classify(row), expected)

    def test_group_never_upgrades_partial_evidence_to_all_agent_or_all_manual(self):
        for values, result in [([0, 0], 0), ([1, 1], 1), ([3, 3], 3), ([0, 3], 2),
                               ([0, 2], 2), ([2, 3], 2), ([0, 1], 0), ([1, 3], 4),
                               ([1, 2], 4), ([0, 4], 4)]:
            self.assertEqual(summarize_origin(values), result, values)

    @unittest.skipUnless(os.name == 'posix', 'The remote workload launcher uses POSIX exec')
    def test_real_launcher_propagates_marker_and_preserves_arguments(self):
        for origin in ('agent', 'manual'):
            result = subprocess.run([sys.executable, str(RUNTIME/'launch_job.py'), '--origin', origin, '--',
                                     sys.executable, '-c', 'import json,os,sys;print(json.dumps([os.environ.get("ASCEND_MONITOR_JOB_ORIGIN"),sys.argv[1:]]))', 'a b', 'literal$'],
                                    capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(result.stdout), [origin, ['a b', 'literal$']])

    def test_exported_group_has_one_value_and_does_not_add_non_npu_processes(self):
        obj = exporter.Inventory.__new__(exporter.Inventory)
        obj.args = types.SimpleNamespace(owners='missing.json', npu_url='http://test', limit=200)
        obj.previous = {}; obj.last_sample = 0; obj.tick_rate = 100; obj.boot_id = 'test'; obj.boot_time = 1000
        obj.containers = {}; obj.last_container_success = 0
        row = {'pid': 123, 'ppid': 1, 'start_ticks': 200, 'ticks': 0, 'rss': 42, 'user': 'root',
               'login_user': '', 'sudo_user': '', 'name': 'worker', 'entrypoint': 'python', 'cwd': '/job',
               'host_cwd': '/job', 'container_id': '', 'container': '', 'service': '', 'control_ref': '', 'origin_marker': 'agent'}
        def read(pid):
            return dict(row, pid=pid, origin_marker='agent' if pid == 123 else 'manual')
        with patch.object(obj, 'refresh_containers'), patch.object(exporter, 'npu_processes', side_effect=[{123: {'0'}, 124: {'1'}}, {}]), patch.object(obj, 'read_process', side_effect=read) as read_mock:
            obj.sample()
            values = [s for s in obj.body.decode().splitlines() if s.startswith('ascend_process_group_origin{')]
            self.assertEqual(len(values), 1)
            self.assertTrue(values[0].endswith(' 4'))
            self.assertNotIn('origin_marker', obj.body.decode())
            obj.sample()
            self.assertNotIn('ascend_process_group_origin{', obj.body.decode())
            self.assertEqual([c.args for c in read_mock.call_args_list], [(123,), (124,)])

    def test_panel_replaces_rss_preserves_identity_duration_stop_and_is_idempotent(self):
        panels = []; add_process_panels(panels, 'node=~"$node"')
        dashboard = add_stop_button({'uid': 'test', 'panels': panels})
        panels = dashboard['panels']
        before = copy.deepcopy(dashboard)
        add_job_origin(panels); add_name_columns(panels)
        self.assertEqual(before['panels'], panels)
        summary = panels[1]
        self.assertEqual([t['refId'] for t in summary['targets']], ['Origin', 'Usage'])
        first, second = summary['targets']
        self.assertNotIn('resident_memory', first['expr'])
        self.assertNotIn('npu_ids', first['expr'])
        self.assertEqual(re.search(r'max by\(([^)]*)\)', first['expr'])[1], re.search(r'min by\(([^)]*)\)', second['expr'])[1])
        self.assertIn('control_ref', first['expr']); self.assertIn('employee_name', first['expr'])
        overrides = {o['matcher']['options']: o['properties'] for o in summary['fieldConfig']['overrides']}
        self.assertIn('使用时长', overrides); self.assertIn('操作', overrides); self.assertNotIn('RSS 合计', overrides)
        mapping = next(p['value'] for p in overrides['作业来源'] if p['id'] == 'mappings')[0]['options']
        self.assertEqual({v['text'] for v in mapping.values()}, {'未知', '人工作业', '疑似 Agent', 'Agent 作业', '混合/待确认'})


if __name__ == '__main__':
    unittest.main()
