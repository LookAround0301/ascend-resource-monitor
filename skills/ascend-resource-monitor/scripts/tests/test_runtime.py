import copy
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
import hashlib
import types
import sqlite3
from unittest.mock import patch

SKILL=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(SKILL/'assets/runtime'))
import configuration as config
import deploy_node as deploy
import process_exporter as proc
from dashboard import dashboard

def example():
    return config.validate({'id':'lab','title':'实验室','executor_id':'operator','storage_root':'/data','central_node':'a','nodes':[{'name':'a','host':'192.0.2.1','monitor_ip':'10.0.0.1'},{'name':'b','host':'192.0.2.2','monitor_ip':'10.0.0.2'}]})

class RuntimeTests(unittest.TestCase):
    def test_paths_and_defaults(self):
        c=example()
        self.assertEqual(str(config.base_path(c)),'/data/operator/ascend-monitor/lab')
        self.assertEqual(config.client_config(c)['host'],'192.0.2.1')
        self.assertNotIn('executor_id',config.client_config(c))

    def test_rejects_injection_secrets_duplicates_and_bad_endpoints(self):
        for change in ({'id':'a;reboot'},{'executor_id':'../root'},{'storage_root':'/data/../etc'},{'password':'x'},{'central_node':'missing'},{'grafana_port':9090}):
            with self.subTest(change=change),self.assertRaises(ValueError):
                config.validate({**example(),**change})
        c=example(); c['nodes'][1]['name']='a'
        with self.assertRaises(ValueError): config.validate(c)
        for ip in ('0.0.0.0','127.0.0.1'):
            c=example(); c['nodes'][0]['monitor_ip']=ip
            with self.assertRaises(ValueError): config.validate(c)

    def test_dashboard_is_dynamic_and_process_tables_are_npu_only(self):
        c=example(); d=dashboard(c)
        self.assertEqual(d['refresh'],'1s')
        self.assertEqual(d['timepicker']['refresh_intervals'],['1s','5s','10s','30s'])
        utilization=next(p for p in d['panels'] if p['title']=='NPU 平均利用率')['targets'][0]['expr']
        self.assertIn('npu_chip_info_overall_utilization',utilization)
        self.assertIn('or on(node,id)',utilization)
        self.assertEqual(len(d['panels']),20)
        self.assertEqual(d['templating']['list'][0]['query'],'a,b')
        for p in d['panels']:
            self.assertEqual(p['datasource']['uid'],'lab-prometheus')
        for p in d['panels'][-2:]:
            for target in p['targets']:
                if target.get('refId') == 'Origin':
                    # This metric is already grouped from NPU-only rows by the exporter.
                    self.assertIn('ascend_process_group_origin{node=~"$node"}', target['expr'])
                else:
                    self.assertIn('npu_ids!=""', target['expr'])
            self.assertEqual([t['id'] for t in p['transformations']],['labelsToFields','merge','organize'])
        files=deploy.rendered_config(c)
        self.assertIn('10.0.0.2:18083',files['prometheus.yml'])
        self.assertIn('http_addr = 127.0.0.1',files['grafana.ini'])
        self.assertIn('min_refresh_interval = 1s',files['grafana.ini'])
        self.assertNotIn('PASSWORD',''.join(files.values()))
        self.assertNotIn('141.61.',json.dumps(d))

    def test_archive_path_escape_and_links_rejected_before_any_write(self):
        for kind in ('escape','symlink'):
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as t:
                root=Path(t); archive=root/'bad.tar'
                with tarfile.open(archive,'w') as tf:
                    ok=tarfile.TarInfo('top/ok'); ok.size=1; tf.addfile(ok,io.BytesIO(b'x'))
                    bad=tarfile.TarInfo('top/../../outside' if kind=='escape' else 'top/link')
                    if kind=='symlink': bad.type=tarfile.SYMTYPE; bad.linkname='/etc/passwd'
                    tf.addfile(bad)
                with self.assertRaises(RuntimeError): deploy.safe_extract(archive,root/'out')
                self.assertFalse((root/'out/ok').exists())

    def test_npu_layer_allowlist(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t); archive=root/'ok.tar'
            with tarfile.open(archive,'w') as tf:
                for name in ('usr/local/bin/npu-exporter','etc/profile'):
                    m=tarfile.TarInfo(name); m.size=4; tf.addfile(m,io.BytesIO(b'data'))
            deploy.safe_extract(archive,root/'out',{'usr/local/bin/npu-exporter'})
            self.assertTrue((root/'out/usr/local/bin/npu-exporter').exists())
            self.assertFalse((root/'out/etc').exists())

    def test_package_hash_is_checked_before_install(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'package.tar'; p.write_bytes(b'bad')
            manifest={'packages':[{'role':'npu','name':p.name,'sha256':'0'*64}]}
            with self.assertRaisesRegex(RuntimeError,'checksum'):
                deploy.package_paths(manifest,t,['npu'])

    def test_npu_source_failure_does_not_become_successful_empty_snapshot(self):
        with patch.object(proc.HTTP,'open',side_effect=OSError('down')):
            with self.assertRaises(RuntimeError): proc.npu_processes('http://invalid')

    def test_npu_source_deduplicates_and_ignores_unrelated_metrics(self):
        body=b'npu_chip_info_process_info_num{id="0"} 1\nnpu_chip_info_process_info{process_id="123",id="0"} 1\nnpu_chip_info_process_info{process_id="123",id="1"} 1\nother{process_id="456",id="2"} 1\n'
        with patch.object(proc.HTTP,'open',return_value=io.BytesIO(body)):
            self.assertEqual(dict(proc.npu_processes('http://test')),{123:{'0','1'}})

    def test_empty_device_process_list_is_valid_with_count_metrics(self):
        with patch.object(proc.HTTP,'open',return_value=io.BytesIO(b'npu_chip_info_process_info_num{id="0"} 0\n')):
            self.assertEqual(dict(proc.npu_processes('http://test')), {})

    def test_missing_process_metrics_is_a_compatibility_failure(self):
        with patch.object(proc.HTTP,'open',return_value=io.BytesIO(b'npu_chip_info_utilization{id="0"} 0\n')):
            with self.assertRaises(RuntimeError): proc.npu_processes('http://test')

    def test_collection_reads_only_device_reported_pids(self):
        obj=proc.Inventory.__new__(proc.Inventory)
        import types, threading
        obj.args=types.SimpleNamespace(npu_url='http://test',owners='missing.json',limit=200)
        obj.previous={}; obj.last_sample=0; obj.tick_rate=100; obj.boot_id='boot'; obj.boot_time=0
        obj.containers={}; obj.last_container_success=0
        row={'pid':123,'ppid':1,'start_ticks':100,'ticks':0,'rss':0,'user':'root','login_user':'','sudo_user':'','name':'worker','entrypoint':'python','cwd':'/job','host_cwd':'/job','container_id':'','container':'','service':'','control_ref':''}
        with patch.object(obj,'refresh_containers'),patch.object(proc,'npu_processes',return_value={123:{'0'}}),patch.object(obj,'read_process',return_value=row) as read:
            out=obj.sample()
        read.assert_called_once_with(123)
        self.assertEqual(out['rows'],1)
        self.assertIn(b'npu_ids="0"',obj.body)

    def test_install_and_reinstall_preserve_encryption_key_without_plaintext_password(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t); base=root/'data'; packages=root/'packages'; packages.mkdir()
            c=example(); n=c['nodes'][0]
            files={'npu':['usr/local/bin/npu-exporter','usr/local/agreement.txt'],
                   'prometheus':['top/prometheus','top/promtool'], 'grafana':['top/bin/grafana'], 'node':['top/node_exporter']}
            manifest={'packages':[]}
            for role,names in files.items():
                path=packages/(role+'.tar')
                with tarfile.open(path,'w') as tf:
                    for name in names:
                        m=tarfile.TarInfo(name); m.mode=0o755; m.size=4; tf.addfile(m,io.BytesIO(b'test'))
                manifest['packages'].append({'role':role,'name':path.name,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
            plan={'roles':['npu','process','prometheus','grafana','node'],'selinux_enforcing':False}
            account=types.SimpleNamespace(pw_dir=str(base/'data'),pw_uid=123,pw_gid=123)
            fake_pwd=types.SimpleNamespace(getpwnam=lambda name:account)
            def bootstrap(*args):
                path=base/'data/grafana/grafana.db'
                if not path.exists():
                    with sqlite3.connect(path) as db:
                        db.execute('CREATE TABLE user (login TEXT, is_admin INTEGER)')
                        db.execute("INSERT INTO user VALUES ('admin',1)")
                    db.close()
            with patch.dict(sys.modules,{'pwd':fake_pwd}),patch.object(deploy,'initialize_administrator',side_effect=bootstrap),patch.object(deploy,'probe',return_value=plan),patch.object(deploy,'base_path',return_value=base),patch.object(deploy,'SYSTEMD',root/'systemd'),patch.object(deploy,'FIXED',root/'npu-config'),patch.object(deploy.os,'chown',create=True),patch.object(deploy,'run',return_value='') as commands,patch.object(Path,'symlink_to',lambda p,target,**kw:p.write_text(str(target))):
                first=deploy.install(c,n,manifest,packages,admin_password='local-only-password')
                secret=(base/'config/admin.env').read_text()
                changed=copy.deepcopy(c); changed['title']='Changed'
                second=deploy.install(changed,n,manifest,packages)
            self.assertEqual(secret,(base/'config/admin.env').read_text())
            self.assertNotIn('GF_SECURITY_ADMIN_PASSWORD',secret)
            self.assertIn('GF_SECURITY_DISABLE_INITIAL_ADMIN_CREATION=true',secret)
            self.assertNotIn('local-only-password',''.join(p.read_text(errors='ignore') for p in base.rglob('*') if p.is_file()))
            self.assertEqual(len(first['services']),5)
            self.assertEqual(second['phase'],'services-active-awaiting-central-verification')
            self.assertTrue(list((base/'backups').rglob('grafana.ini')))
            unit=(root/'systemd/ascend-lab-prometheus.service').read_text()
            self.assertIn('--web.listen-address=127.0.0.1:9090',unit)
            self.assertTrue(any(call.args[:2]==('systemctl','restart') for call in commands.call_args_list))

if __name__=='__main__': unittest.main()
