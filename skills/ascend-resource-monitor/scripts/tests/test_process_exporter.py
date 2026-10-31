import unittest
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'assets/runtime'))
import process_exporter as p

class ProcessTests(unittest.TestCase):
    def test_process_clock_uses_start_ticks_and_does_not_carry_over_after_exit(self):
        import copy
        import types
        from unittest.mock import patch
        obj=p.Inventory.__new__(p.Inventory)
        obj.args=types.SimpleNamespace(owners='missing.json',npu_url='http://test',limit=200)
        obj.previous={};obj.last_sample=0;obj.tick_rate=100;obj.boot_id='test';obj.boot_time=1000
        obj.containers={};obj.last_container_success=0
        row={'pid':123,'ppid':1,'start_ticks':200,'ticks':0,'rss':42,'user':'root',
             'login_user':'','sudo_user':'','name':'worker','entrypoint':'python','cwd':'/job',
             'host_cwd':'/job','container_id':'','container':'','service':'','control_ref':''}
        with patch.object(obj,'refresh_containers'),patch.object(p,'npu_processes',side_effect=[{123:{'0'}},{},{123:{'0'}}]),patch.object(obj,'read_process',side_effect=lambda pid:copy.deepcopy(row)):
            obj.sample()
            first=[line for line in obj.body.decode().splitlines() if line.startswith('ascend_process_start_time_seconds{')]
            self.assertEqual(float(first[0].rsplit(' ',1)[1]),1002)
            obj.sample()
            self.assertNotIn('ascend_process_start_time_seconds{',obj.body.decode())
            row['start_ticks']=900
            obj.sample()
            last=[line for line in obj.body.decode().splitlines() if line.startswith('ascend_process_start_time_seconds{')]
            self.assertEqual(float(last[0].rsplit(' ',1)[1]),1009)

    def test_usage_table_preserves_grouping_and_stop_through_repeated_patches(self):
        import copy
        import re
        from process_panels import add_process_panels,add_usage_time
        from patch_dashboard import add_stop_button
        panels=[];add_process_panels(panels,'node=~"$node"')
        dashboard=add_stop_button({'uid':'test','panels':panels})
        before=copy.deepcopy(dashboard)
        add_usage_time(dashboard['panels'])
        self.assertEqual(before,dashboard)
        summary=dashboard['panels'][1]
        self.assertEqual(len(summary['targets']),2)
        a,b=summary['targets']
        self.assertEqual(re.search(r'sum by\(([^)]*)\)',a['expr'])[1],re.search(r'min by\(([^)]*)\)',b['expr'])[1])
        self.assertIn('employee_name',b['expr']);self.assertIn('control_ref',b['expr'])
        self.assertIn('time() - min by(',b['expr'])
        self.assertEqual(summary['transformations'][0]['options']['valueLabel'],'display_metric')
        self.assertTrue(all(t['instant'] and not t['range'] for t in summary['targets']))

    def test_sample_reloads_names_and_keeps_unmatched_npu_processes(self):
        import copy
        import json
        import tempfile
        import types
        from unittest.mock import patch
        obj=p.Inventory.__new__(p.Inventory)
        obj.previous={};obj.last_sample=0;obj.tick_rate=100;obj.boot_id='test';obj.boot_time=0
        obj.containers={};obj.last_container_success=0
        row={'pid':123,'ppid':1,'start_ticks':100,'ticks':0,'rss':42,'user':'a00000001',
             'login_user':'','sudo_user':'','name':'worker','entrypoint':'python','cwd':'/job',
             'host_cwd':'/job','container_id':'','container':'','service':'','control_ref':''}
        with tempfile.TemporaryDirectory() as directory:
            owners=Path(directory)/'owners.json'
            owners.write_text(json.dumps({'employee_names':{'a00000001':'测试姓名'}},ensure_ascii=False),encoding='utf-8')
            obj.args=types.SimpleNamespace(npu_url='http://test',owners=str(owners),limit=200)
            with patch.object(obj,'refresh_containers'),patch.object(p,'npu_processes',return_value={123:{'0'}}),patch.object(obj,'read_process',side_effect=lambda pid:copy.deepcopy(row)) as read:
                self.assertEqual(obj.sample()['rows'],1)
                self.assertIn('employee_name="测试姓名"',obj.body.decode())
                owners.write_text('{}',encoding='utf-8')
                self.assertEqual(obj.sample()['rows'],1)
                self.assertIn('employee_name=""',obj.body.decode())
                self.assertEqual([call.args for call in read.call_args_list],[(123,),(123,)])

    def test_roster_lookup_is_exact_and_unknown_names_are_blank(self):
        mapping={'employee_names':{'a00000001':'测试姓名','b00000002':['one','two'],'c00000003':'bad\nname'}}
        self.assertEqual(p.employee_name_for(' A00000001 ',mapping),'测试姓名')
        for account in ('a00000002','00000001','未知','待确认','b00000002','c00000003'):
            self.assertEqual(p.employee_name_for(account,mapping),'')
        self.assertEqual(p.employee_name_for('a00000001',{'employee_names':None}),'')
        self.assertEqual(p.owner_for({'user':'root','cwd':'/home/a00000001'},{},mapping),('a00000001','目录/容器线索'))

    def test_name_columns_preserve_control_reference_and_are_idempotent(self):
        import copy
        from process_panels import add_process_panels, add_name_columns
        from patch_dashboard import add_stop_button
        panels=[]
        add_process_panels(panels,'node=~"$node"')
        dashboard=add_stop_button({'uid':'test','panels':panels})
        before=copy.deepcopy(dashboard)
        add_name_columns(dashboard['panels'])
        self.assertEqual(before,dashboard)
        for panel in dashboard['panels']:
            options=next(t['options'] for t in panel['transformations'] if t['id']=='organize')
            self.assertEqual(options['renameByName']['employee_name'],'姓名')
            self.assertEqual(options['indexByName']['employee_name'],options['indexByName']['employee_id']+1)
        query=dashboard['panels'][1]['targets'][0]['expr']
        self.assertIn('employee_name',query)
        self.assertIn('control_ref',query)
        action=next(o for o in dashboard['panels'][1]['fieldConfig']['overrides'] if o['matcher']['options']=='操作')
        self.assertTrue(next(v['value'] for v in action['properties'] if v['id']=='links')[0]['targetBlank'])

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
