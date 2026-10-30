import unittest
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'assets/runtime'))
import process_exporter as p

class ProcessTests(unittest.TestCase):
    def test_stat_name_can_contain_parentheses_and_spaces(self):
        tail = ['S'] + ['0'] * 22
        tail[1], tail[11], tail[12], tail[19], tail[21] = '20','30','40','500','600'
        result = p.parse_stat('99 (worker (rank 0)) ' + ' '.join(tail))
        self.assertEqual((result['ppid'], result['ticks'], result['start_ticks'], result['rss_pages']), (20,70,500,600))

    def test_mount_mapping_uses_directory_boundary_and_longest_prefix(self):
        mounts = [{'Destination':'/work','Source':'/home/a'}, {'Destination':'/work/project','Source':'/mnt/share/b'}]
        self.assertEqual(p.map_host_path('/work/project/run',mounts), '/mnt/share/b/run')
        self.assertEqual(p.map_host_path('/worker/run',mounts), '')

    def test_shared_root_is_not_a_person(self):
        row={'user':'root','login_user':'','cwd':'/tmp/inference/a','host_cwd':'','entrypoint':'vllm'}
        self.assertEqual(p.owner_for(row,{'name':'infer-api'},{}), ('未知','无可用工号线索'))

    def test_directory_clue_is_marked_as_a_clue(self):
        self.assertEqual(p.owner_for({'user':'root','cwd':'/home/w00923580/run'},{},{}), ('w00923580','目录/容器线索'))

    def test_conflicting_directory_clues_are_not_guessed(self):
        result = p.owner_for({'user':'root','cwd':'/home/w00923580','entrypoint':'python /mnt/l00599173/run.py'},{},{})
        self.assertEqual(result, ('待确认','多个工号线索'))

    def test_login_evidence_and_manual_override(self):
        row={'user':'root','login_user':'a00821909','cwd':'/home/w00923580'}
        self.assertEqual(p.owner_for(row,{'name':'api'},{}), ('a00821909','登录审计用户'))
        self.assertEqual(p.owner_for(row,{'name':'api'},{'containers':{'api':'h00848570'}}), ('h00848570','人工标注'))

    def test_command_does_not_export_secrets(self):
        self.assertEqual(p.entrypoint(['python','-m','vllm.entrypoints.openai.api_server','--api-key','SECRET'],'python'), 'python -m vllm.entrypoints.openai.api_server')
        self.assertNotIn('SECRET',p.entrypoint(['bash','-c','TOKEN=SECRET run'],'bash'))
        self.assertEqual(p.entrypoint(['python','/home/user/run.py','--password','SECRET'],'python'), 'python /home/user/run.py')

if __name__ == '__main__': unittest.main()
