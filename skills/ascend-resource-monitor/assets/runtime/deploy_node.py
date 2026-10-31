#!/usr/bin/env python3
"""Linux installer. Run probe on every node, then install, then verify centrally."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tarfile
import time
import urllib.parse
import urllib.request

from configuration import load, base_path
from dashboard import dashboard
from admin_credentials import (administrator_exists, prepare_security,
                               initialize_administrator, harden_owned_files, read_password)

HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
FIXED = Path('/user/mind-cluster/npu-exporter-config')
SYSTEMD = Path('/etc/systemd/system')

def run(*args, timeout=30):
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT, timeout=timeout).strip()

def get(url, headers=None):
    with HTTP.open(urllib.request.Request(url, headers=headers or {}), timeout=8) as r:
        return r.read(16 * 1024 * 1024).decode('utf-8')

def atomic(path, text, mode=0o644):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.new')
    if tmp.is_symlink() or path.is_symlink():
        raise RuntimeError('Refusing symlink: ' + str(path))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(text)
    tmp.chmod(mode)
    tmp.replace(path)

def marker(c):
    return '# Managed by ascend-resource-monitor: ' + c['id']

def unit_name(c, role):
    return 'ascend-' + c['id'] + '-' + role + '.service'

def mount_info(path):
    return json.loads(run('findmnt', '-J', '-T', str(path), '-o', 'TARGET,SOURCE,FSTYPE,MAJ:MIN'))['filesystems'][0]

def has_node(ip, port):
    try:
        return 'node_cpu_seconds_total{' in get(f'http://{ip}:{port}/metrics')
    except Exception:
        return False

def free_port(ip, port):
    with socket.socket() as s:
        try:
            s.bind((ip, port))
        except OSError as e:
            raise RuntimeError(f'Address unavailable or occupied: {ip}:{port}') from e

def probe(c, n, manifest):
    if sys.version_info < (3, 9):
        raise RuntimeError('Remote Python 3.9+ required')
    if os.geteuid() != 0:
        raise RuntimeError('Use root SSH or a sudo -n capable account')
    if platform.machine() != manifest['arch']:
        raise RuntimeError('Package architecture differs from host: ' + platform.machine())
    required = ['systemctl','systemd-analyze','findmnt','npu-smi','useradd']
    for name in required:
        if not shutil.which(name):
            raise RuntimeError('Missing command: ' + name)
    if not Path('/run/systemd/system').exists():
        raise RuntimeError('Host is not booted with systemd')
    root = Path(c['storage_root'])
    if not root.is_dir() or root.is_symlink():
        raise RuntimeError('Select an existing non-symlink data directory')
    data_mount, os_mount = mount_info(root), mount_info('/')
    if data_mount['source'] == os_mount['source'] or data_mount.get('maj:min') == os_mount.get('maj:min'):
        raise RuntimeError('Data directory is on the root filesystem')
    central = n['name'] == c['central_node']
    if central and data_mount['fstype'].startswith(('nfs','cifs','fuse')):
        raise RuntimeError('Prometheus TSDB needs a local data filesystem')
    available = shutil.disk_usage(root).free
    required_gb = c['retention_gb'] + 6 if central else 1
    if available < required_gb * 1024**3:
        raise RuntimeError(f'Need at least {required_gb} GiB available on data volume')
    base = Path(base_path(c))
    # Reject symlink components, including an executor directory controlled elsewhere.
    for p in [base, *base.parents]:
        if p.is_symlink():
            raise RuntimeError('Symlink in deployment path: ' + str(p))
    owned = base / '.deployment-id'
    if base.exists() and any(base.iterdir()) and (not owned.exists() or owned.read_text().strip() != c['id']):
        raise RuntimeError('Deployment directory is not owned by this deployment')
    if FIXED.exists() or FIXED.is_symlink():
        if not FIXED.is_symlink() or FIXED.resolve() != (base/'config/npu-exporter').resolve():
            raise RuntimeError('Existing NPU Exporter configuration belongs to another installation: ' + str(FIXED))
    roles = ['npu','process'] + (['prometheus','grafana'] if central else [])
    healthy_node = has_node(n['monitor_ip'], n['node_port'])
    if n['node_exporter'] == 'reuse' and not healthy_node:
        raise RuntimeError('Requested node_exporter reuse but endpoint is not healthy')
    own_node = SYSTEMD / unit_name(c, 'node')
    install_node = n['node_exporter'] == 'install' or not healthy_node or own_node.exists()
    if install_node:
        roles.append('node')
    ports = {'npu':(n['monitor_ip'],n['npu_port']), 'process':(n['monitor_ip'],n['process_port']),
             'node':(n['monitor_ip'],n['node_port']), 'grafana':('127.0.0.1',c['grafana_port']),
             'prometheus':('127.0.0.1',c['prometheus_port'])}
    for role in roles:
        path = SYSTEMD / unit_name(c, role)
        if path.exists():
            if path.is_symlink() or marker(c) not in path.read_text():
                raise RuntimeError('Unowned unit: ' + str(path))
        else:
            free_port(*ports[role])
    enforcing = shutil.which('getenforce') and run('getenforce') == 'Enforcing'
    if enforcing and (not shutil.which('semanage') or not shutil.which('restorecon')):
        raise RuntimeError('SELinux is enforcing; install semanage/restorecon before deployment')
    device_info = run('npu-smi', 'info', timeout=20)
    return {'node':n['name'], 'arch':platform.machine(), 'mount':data_mount,
            'available_gib':round(available/1024**3,1), 'base':str(base),
            'roles':roles, 'node_exporter':'install' if install_node else 'reuse',
            'selinux_enforcing':bool(enforcing), 'npu_info':device_info[:5000],
            'administrator_initialized':administrator_exists(base) if central else False}

def safe_extract(archive, destination, selected=None):
    """Only ordinary files/directories; validate every path before any extraction."""
    destination = Path(destination)
    with tarfile.open(archive) as tf:
        members = []
        for m in tf.getmembers():
            p = PurePosixPath(m.name)
            if p.is_absolute() or '..' in p.parts:
                raise RuntimeError('Unsafe archive path: ' + m.name)
            if selected is not None and m.name not in selected:
                continue
            if not m.isfile() and not m.isdir():
                raise RuntimeError('Unsupported archive link or special file: ' + m.name)
            parts = p.parts if selected is not None else p.parts[1:]
            if not parts:
                continue
            dest = destination.joinpath(*parts)
            if destination.resolve() not in dest.resolve().parents:
                raise RuntimeError('Archive path escapes destination')
            members.append((m, dest))
        for m, dest in members:
            if m.isdir():
                dest.mkdir(parents=True, exist_ok=True)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                with tf.extractfile(m) as src, dest.open('wb') as out:
                    shutil.copyfileobj(src, out)
                dest.chmod(0o755 if m.mode & 0o111 else 0o644)

def package_paths(manifest, package_dir, roles):
    found = {}
    for role in roles:
        if role == 'process':
            continue
        items = [p for p in manifest['packages'] if p['role'] == role]
        if len(items) != 1:
            raise RuntimeError('Exactly one package per role required: ' + role)
        item = items[0]
        if not re.fullmatch(r'[A-Za-z0-9._-]+',item['name']) or not re.fullmatch(r'[0-9a-f]{64}',item['sha256']):
            raise RuntimeError('Invalid package manifest')
        path = Path(package_dir) / item['name']
        sha = hashlib.sha256()
        with path.open('rb') as f:
            for block in iter(lambda:f.read(4*1024*1024), b''):
                sha.update(block)
        if sha.hexdigest() != item['sha256']:
            raise RuntimeError('Package checksum mismatch: ' + path.name)
        found[role] = (path,item)
    return found

def rendered_config(c):
    q = lambda s: json.dumps(s, ensure_ascii=False)
    lines = ['global:','  scrape_interval: 5s','  scrape_timeout: 4s','scrape_configs:']
    for job, key in (('node','node_port'),('npu','npu_port'),('processes','process_port')):
        lines += ['  - job_name: '+job]
        if job == 'processes':
            lines += ['    scrape_interval: 10s','    scrape_timeout: 8s']
        if job == 'npu':
            lines += ['    honor_timestamps: false']
        lines += ['    static_configs:']
        for n in c['nodes']:
            lines += ['      - targets: ['+q(n['monitor_ip']+':'+str(n[key]))+']',
                      '        labels: {node: '+q(n['name'])+', host: '+q(n['host'])+'}']
    base = str(base_path(c))
    files = {'prometheus.yml':'\n'.join(lines)+'\n',
             'dashboards/overview.json':json.dumps(dashboard(c),ensure_ascii=False,indent=2)+'\n'}
    files['provisioning/datasources/monitor.yml'] = f'''apiVersion: 1
datasources:
  - name: {q(c['title'])}
    uid: {c['id']}-prometheus
    type: prometheus
    access: proxy
    url: http://127.0.0.1:{c['prometheus_port']}
    isDefault: true
    editable: false
    jsonData: {{timeInterval: 5s}}
'''
    files['provisioning/dashboards/monitor.yml'] = f'''apiVersion: 1
providers:
  - name: {c['id']}
    type: file
    disableDeletion: true
    allowUiUpdates: false
    options:
      path: {base}/config/dashboards
'''
    files['grafana.ini'] = f'''[paths]
data = {base}/data/grafana
logs = {base}/logs/grafana
plugins = {base}/data/grafana/plugins
provisioning = {base}/config/provisioning
[server]
http_addr = 127.0.0.1
http_port = {c['grafana_port']}
root_url = http://127.0.0.1:{c['local_port']}/
[analytics]
enabled = false
reporting_enabled = false
check_for_updates = false
check_for_plugin_updates = false
[security]
admin_user = admin
disable_gravatar = true
[users]
allow_sign_up = false
viewers_can_edit = false
default_theme = dark
[auth.anonymous]
enabled = {str(c['anonymous_viewer']).lower()}
org_role = Viewer
hide_version = true
[dashboards]
min_refresh_interval = 5s
default_home_dashboard_path = {base}/config/dashboards/overview.json
[log]
mode = file
level = warn
[unified_alerting]
enabled = false
'''
    return files

def service(c, role, command, user=None, extra=''):
    base = str(base_path(c))
    restricted = f'''User={user}
Group={user}
ProtectSystem=strict
ProtectHome=false
ReadWritePaths={base}/data {base}/logs
PrivateTmp=true
''' if user else ''
    return f'''{marker(c)}
[Unit]
Description=Ascend monitor {c['id']} {role}
After=network-online.target
Wants=network-online.target
RequiresMountsFor={base}
[Service]
Type=simple
WorkingDirectory={base}/scripts
ExecStart={command}
Restart=on-failure
RestartSec=5
UMask=0027
NoNewPrivileges=true
CPUQuota={'200%' if user else '100%'}
MemoryMax={'2G' if user else '512M'}
TasksMax=256
{restricted}{extra}
[Install]
WantedBy=multi-user.target
'''

def install(c, n, manifest, packages, admin_password=None):
    import pwd
    plan = probe(c,n,manifest)
    files = package_paths(manifest,packages,plan['roles'])  # All hashes checked before mutation.
    base = Path(base_path(c))
    if n['name'] == c['central_node'] and not administrator_exists(base) and not admin_password:
        raise ValueError('New deployments require --admin-password-stdin; save the credential on the client first')
    base.mkdir(parents=True,exist_ok=True)
    base.chmod(0o755)
    atomic(base/'.deployment-id',c['id']+'\n')
    receipt = {'id':c['id'],'node':n['name'],'started_at':time.time(),'phase':'staging','services':[]}
    def record(phase):
        receipt['phase'] = phase
        atomic(base/'install-receipt.json',json.dumps(receipt,indent=2)+'\n',0o600)
    record('staging')
    for d in ('scripts','bin','config/npu-exporter','data','logs'):
        (base/d).mkdir(parents=True,exist_ok=True)
    for p in Path(__file__).parent.glob('*.py'):
        atomic(base/'scripts'/p.name,p.read_text(encoding='utf-8'),0o755)
    atomic(base/'config/deployment.json',json.dumps(c,indent=2)+'\n',0o600)
    for role,(path,item) in files.items():
        target = base/'opt'/(role+'-'+item['sha256'][:12])
        if not (target/'.complete').exists():
            if target.exists():
                raise RuntimeError('Incomplete package directory; inspect before retry: '+str(target))
            target.mkdir(parents=True)
            safe_extract(path,target,{'usr/local/bin/npu-exporter','usr/local/agreement.txt'} if role=='npu' else None)
            atomic(target/'.complete',item['sha256']+'\n')
        files[role] = (target,item)
    npu = files['npu'][0]/'usr/local/bin/npu-exporter'
    npu.chmod(0o755)
    groups = ['version','utilization','npu','ddr','sio','hbm','hccs','pcie','vnpu','nodeBase','roce','optical','network_bandwidth','network_link','ub']
    enabled = {'version','utilization','npu','ddr','hbm','nodeBase'}
    cfg = [{'metricsGroup':g,'state':'ON' if g in enabled else 'OFF','intervalSeconds':-1 if g=='version' else 86400 if g=='nodeBase' else 5} for g in groups]
    atomic(base/'config/npu-exporter/metricConfiguration.json',json.dumps(cfg,indent=2)+'\n')
    atomic(base/'config/npu-exporter/pluginConfiguration.json','[{"metricsGroup":"text","state":"OFF","intervalSeconds":60},{"metricsGroup":"myPlugin","state":"OFF","intervalSeconds":60}]\n')
    if not FIXED.exists():
        FIXED.parent.mkdir(parents=True,exist_ok=True)
        FIXED.symlink_to(base/'config/npu-exporter',target_is_directory=True)
    owners = base/'config/process-owners.json'
    if not owners.exists():
        atomic(owners,'{"containers":{},"services":{}}\n',0o600)
    units = {'npu':service(c,'npu',f'{npu} -ip {n["monitor_ip"]} -port {n["npu_port"]} -logFile {base}/logs/npu-exporter.log -maxBackups 3 -maxAge 7 -deviceResetTimeout 10',extra='Environment=LD_LIBRARY_PATH=/usr/local/Ascend/driver/lib64/driver:/usr/local/Ascend/driver/lib64/common:/usr/local/Ascend/add-ons:/usr/local/Ascend/driver/lib64:/usr/local/dcmi\n'),
             'process':service(c,'process',f'{sys.executable} {base}/scripts/process_exporter.py --listen-ip {n["monitor_ip"]} --port {n["process_port"]} --npu-url http://{n["monitor_ip"]}:{n["npu_port"]}/metrics --owners {owners}')}
    binaries = [npu,base/'scripts/process_exporter.py']
    if 'node' in plan['roles']:
        binary = files['node'][0]/'node_exporter'
        binaries.append(binary)
        units['node'] = service(c,'node',f'{binary} --web.listen-address={n["monitor_ip"]}:{n["node_port"]}')
    if n['name'] == c['central_node']:
        account_name = 'am-'+hashlib.sha256(str(base).encode()).hexdigest()[:10]
        try:
            account = pwd.getpwnam(account_name)
            if account.pw_dir != str(base/'data'):
                raise RuntimeError('Service account belongs to another installation')
        except KeyError:
            run('useradd','--system','--user-group','--no-create-home','--home-dir',str(base/'data'),'--shell','/usr/sbin/nologin',account_name)
            account = pwd.getpwnam(account_name)
        for d in ('data/prometheus','data/grafana','logs/grafana'):
            p = base/d; p.mkdir(parents=True,exist_ok=True)
            os.chown(p,account.pw_uid,account.pw_gid)
        for rel,text in rendered_config(c).items():
            dest = base/'config'/rel
            if dest.exists():
                backup = base/'backups'/time.strftime('%Y%m%d-%H%M%S')/rel
                backup.parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(dest,backup)
            atomic(dest,text)
        env = prepare_security(base, atomic)
        prom,graf = files['prometheus'][0], files['grafana'][0]
        initialize_administrator(base,graf,account,admin_password,atomic)
        harden_owned_files(base,atomic)
        run(str(prom/'promtool'),'check','config',str(base/'config/prometheus.yml'))
        binaries += [prom/'prometheus',graf/'bin/grafana']
        units['prometheus'] = service(c,'prometheus',f'{prom}/prometheus --config.file={base}/config/prometheus.yml --storage.tsdb.path={base}/data/prometheus --storage.tsdb.retention.time={c["retention_days"]}d --storage.tsdb.retention.size={c["retention_gb"]}GB --web.listen-address=127.0.0.1:{c["prometheus_port"]}',account_name)
        units['grafana'] = service(c,'grafana',f'{graf}/bin/grafana server --homepath={graf} --config={base}/config/grafana.ini',account_name,extra=f'EnvironmentFile={env}\n')
    if plan['selinux_enforcing']:
        rules = run('semanage','fcontext','-l','-C')
        for binary in binaries:
            pattern = re.escape(str(binary))
            if pattern not in rules:
                run('semanage','fcontext','-a','-t','bin_t',pattern)
            run('restorecon',str(binary))
    record('configuring-services')
    # Validate every candidate before replacing any existing unit.
    staging = base/'unit-staging'; staging.mkdir(exist_ok=True)
    for role,text in units.items():
        path = staging/unit_name(c,role)
        atomic(path,text)
        run('systemd-analyze','verify',str(path))
    for role,text in units.items():
        atomic(SYSTEMD/unit_name(c,role),text)
    run('systemctl','daemon-reload')
    for role in units:
        name = unit_name(c,role)
        run('systemctl','enable',name)
        run('systemctl','restart',name)
        receipt['services'].append(name)
        record('starting-'+role)
    for role in units:
        run('systemctl','is-active','--quiet',unit_name(c,role))
    record('services-active-awaiting-central-verification')
    return receipt

def verify(c, admin_password=None):
    base = Path(base_path(c))
    if not admin_password and not c['anonymous_viewer']:
        raise ValueError('Private dashboard verification requires --admin-password-stdin')
    headers = {'Authorization':'Basic '+base64.b64encode(('admin:'+admin_password).encode()).decode()} if admin_password else {}
    graf = f'http://127.0.0.1:{c["grafana_port"]}'
    prom = f'http://127.0.0.1:{c["prometheus_port"]}'
    def query(expr):
        data = json.loads(get(prom+'/api/v1/query?'+urllib.parse.urlencode({'query':expr})))
        if data.get('status') != 'success':
            raise RuntimeError('Prometheus query failed')
        return data['data']['result']
    last = None
    for attempt in range(30):
        try:
            health = json.loads(get(graf+'/api/health'))
            if health.get('database') != 'ok':
                raise RuntimeError('Grafana database unhealthy')
            dash = json.loads(get(graf+'/api/dashboards/uid/'+c['id'],headers))
            if len(dash['dashboard']['panels']) != 20:
                raise RuntimeError('Expected 20 provisioned panels')
            result = query('up')
            for n in c['nodes']:
                for job in ('node','npu','processes'):
                    rows = [r for r in result if r['metric'].get('node') == n['name'] and r['metric'].get('job') == job]
                    if len(rows) != 1 or float(rows[0]['value'][1]) != 1:
                        raise RuntimeError('Target not ready: '+n['name']+'/'+job)
                npus = query('npu_chip_info_utilization{node='+json.dumps(n['name'])+'}')
                if not npus or ('expected_npus' in n and len(npus) != n['expected_npus']):
                    raise RuntimeError('NPU metric count mismatch: '+n['name'])
                fresh = query('ascend_process_inventory_timestamp_seconds{node='+json.dumps(n['name'])+'}')
                if len(fresh) != 1 or time.time()-float(fresh[0]['value'][1]) > 45:
                    raise RuntimeError('Process inventory stale: '+n['name'])
            process_rows = query('ascend_process_cpu_percent')
            if any(r['metric'].get('npu_ids','-') in ('','-') for r in process_rows):
                raise RuntimeError('Non-NPU process found')
            # Exercise the same Grafana datasource proxy used by the dashboard.
            proxy = graf+'/api/datasources/proxy/uid/'+c['id']+'-prometheus/api/v1/query?query=up'
            data = json.loads(get(proxy,headers))
            if data.get('status') != 'success':
                raise RuntimeError('Grafana datasource query failed')
            out = {'verified_at':time.time(),'nodes':len(c['nodes']),'targets':len(c['nodes'])*3,'panels':20,'npu_processes':len(process_rows)}
            atomic(base/'verification.json',json.dumps(out,indent=2)+'\n',0o600)
            return out
        except Exception as e:
            last = e
            time.sleep(3)
    raise RuntimeError('Verification failed: '+str(last))

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',required=True)
    p.add_argument('--manifest',required=True)
    p.add_argument('--node')
    p.add_argument('--packages',default='packages')
    p.add_argument('--admin-password-stdin',action='store_true',help='Read admin password from stdin; never a command argument or server file')
    action = p.add_mutually_exclusive_group(required=True)
    action.add_argument('--probe',action='store_true')
    action.add_argument('--install',action='store_true')
    action.add_argument('--verify',action='store_true')
    action.add_argument('--render-only')
    args = p.parse_args()
    admin_password = read_password(sys.stdin) if args.admin_password_stdin else None
    c = load(args.config)
    manifest = json.loads(Path(args.manifest).read_text(encoding='utf-8-sig'))
    if args.render_only:
        for rel,text in rendered_config(c).items():
            atomic(Path(args.render_only)/rel,text)
        print(json.dumps({'panels':20,'rendered':args.render_only}))
        return
    if args.verify:
        out = verify(c,admin_password)
    else:
        n = next(n for n in c['nodes'] if n['name'] == args.node)
        out = probe(c,n,manifest) if args.probe else install(c,n,manifest,args.packages,admin_password)
    print(json.dumps(out,ensure_ascii=False))

if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print('ERROR: '+str(e),file=sys.stderr)
        sys.exit(1)
