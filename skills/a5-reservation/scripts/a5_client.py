"""A5 reservation client for local agents; standard library only."""
import argparse
from datetime import datetime, timedelta, timezone
import http.cookiejar
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import ssl
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPCookieProcessor, HTTPSHandler, ProxyHandler, Request, build_opener

ROOT=Path(os.environ.get('A5_RESERVATION_HOME',Path.home()/'.a5-reservation'))
CONFIG=ROOT/'config.json'

class ApiError(Exception):
    def __init__(self,status,message):super().__init__(message);self.status=status

def save_private(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    if os.name!='nt':os.chmod(path.parent,0o700)
    temporary=path.with_name(path.name+'.'+secrets.token_hex(4)+'.tmp')
    fd=os.open(str(temporary),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            json.dump(data,f,ensure_ascii=False,indent=2);f.flush();os.fsync(f.fileno())
        os.replace(temporary,path)
    finally:
        if temporary.exists():temporary.unlink()

def base_url(url):
    p=urlsplit(url.strip().rstrip('/'))
    if p.username or p.password or p.path or p.query or p.fragment or not p.hostname or p.scheme not in ('http','https'):
        raise ValueError('接口地址必须是完整的 HTTP(S) 源地址，不含路径和凭据')
    try:is_local=ipaddress.ip_address(p.hostname).is_loopback
    except ValueError:is_local=p.hostname.lower()=='localhost'
    if p.scheme=='http' and not is_local:raise ValueError('非本机接口必须使用经过证书校验的 HTTPS；也可使用 SSH 隧道')
    return p.scheme+'://'+p.netloc

def settings():
    config=json.loads(CONFIG.read_text(encoding='utf-8')) if CONFIG.exists() else {}
    for field,var in [('employee','A5_EMPLOYEE'),('name','A5_NAME'),('url','A5_QUEUE_URL'),('ca_file','A5_CA_FILE')]:
        if os.environ.get(var):config[field]=os.environ[var]
    if not re.fullmatch(r'[a-z][0-9]{8}',str(config.get('employee',''))):raise ValueError('请先配置工号，例如 a12345678')
    if not isinstance(config.get('name'),str) or not config['name'].strip():raise ValueError('请先配置姓名')
    config['url']=base_url(config.get('url','http://127.0.0.1:13000'))
    return config

class Client:
    def __init__(self,config):
        self.config=config;self.origin=config['url'];self.jar=http.cookiejar.CookieJar()
        ctx=ssl.create_default_context(cafile=config.get('ca_file') or None)
        self.opener=build_opener(ProxyHandler({}),HTTPCookieProcessor(self.jar),HTTPSHandler(context=ctx))
    def call(self,op,data=None):
        headers={'Accept':'application/json'}
        if data is not None:headers.update({'Content-Type':'application/json','Origin':self.origin,'X-A5-Queue':'queue-v1'})
        req=Request(self.origin+'/a5-queue/api/'+op,data=None if data is None else json.dumps(data,ensure_ascii=False).encode(),headers=headers)
        try:
            with self.opener.open(req,timeout=15) as response:return json.loads(response.read(4000001))
        except HTTPError as e:
            try:message=json.loads(e.read(8192)).get('message','接口返回错误')
            except Exception:message='接口返回错误'
            raise ApiError(e.code,message)
    def login(self):self.call('login',{'employee':self.config['employee'],'name':self.config['name']})
    def state(self):
        state=self.call('state')
        keys=('id','state','employee','name','mine','collaborator','position','machine','total_cards','requested_seconds','effective_requested_seconds','estimated_start','not_before','allocations','claim_before','offer_id','activated')
        return {'server_time':state.get('server_time'),'nodes':state.get('nodes'),
                'tasks':[{k:r.get(k) for k in keys if k in r} for r in state.get('tasks',[]) if r.get('mine') or r.get('collaborator')]}

def main():
    p=argparse.ArgumentParser(description='A5 预约 Agent 客户端')
    sub=p.add_subparsers(dest='action',required=True)
    cfg=sub.add_parser('configure');cfg.add_argument('--employee',required=True);cfg.add_argument('--name',required=True);cfg.add_argument('--url',default='http://127.0.0.1:13000');cfg.add_argument('--ca-file')
    sub.add_parser('query')
    create=sub.add_parser('create');create.add_argument('--machine',default='auto');create.add_argument('--cards',type=int,required=True);create.add_argument('--minutes',type=int,required=True);create.add_argument('--night-start');create.add_argument('--backfill-minutes',type=int,default=0)
    recover=sub.add_parser('recover');recover.add_argument('operation')
    retry=sub.add_parser('retry');retry.add_argument('operation')
    args=p.parse_args()
    if args.action=='configure':
        employee=args.employee.strip().lower();name=args.name.strip()
        if not re.fullmatch(r'[a-z][0-9]{8}',employee) or not name:raise ValueError('工号或姓名格式无效')
        data={'employee':employee,'name':name,'url':base_url(args.url)}
        if args.ca_file:data['ca_file']=args.ca_file
        save_private(CONFIG,data);print(json.dumps({'configured':True,'url':data['url']},ensure_ascii=False));return
    config=settings();client=Client(config);client.login()
    if args.action=='query':print(json.dumps(client.state(),ensure_ascii=False));return
    if args.action=='create':
        if args.cards<1 or args.minutes<1 or args.backfill_minutes<0:raise ValueError('卡数、时长和插空时长无效')
        night=bool(args.night_start)
        if night:
            start=datetime.fromisoformat(args.night_start)
            if start.tzinfo is None:start=start.replace(tzinfo=timezone(timedelta(hours=8)))
            start=start.timestamp()
        else:start=0
        payload={'machine':args.machine,'cards':args.cards,'night':night,'not_before':start,
                 'requested_seconds':args.minutes*60,'backfill_min_seconds':args.backfill_minutes*60}
        operation=secrets.token_hex(16)
        journal={'operation':operation,'employee':config['employee'],'url':config['url'],'request':payload}
        save_private(ROOT/'requests'/(operation+'.json'),journal)
        print('预约请求编号：'+operation,file=sys.stderr,flush=True)
    else:
        operation=args.operation
        if not re.fullmatch('[0-9a-f]{32}',operation):raise ValueError('请求编号无效')
        journal=json.loads((ROOT/'requests'/(operation+'.json')).read_text(encoding='utf-8'))
        if journal['employee']!=config['employee'] or journal['url']!=config['url']:raise ValueError('该请求属于其他工号或接口地址')
    try:
        if args.action=='recover':result=client.call('agent-result',{'operation':operation})
        else:result=client.call('agent-enqueue',{'operation':operation,'request':journal['request']})
    except ApiError as e:
        if e.status>=500:
            raise RuntimeError('预约结果未确认，请用 recover 或 retry 加原请求编号 '+operation+' 核对；不要新建请求') from e
        raise
    except (URLError,TimeoutError,OSError,ValueError) as e:
        raise RuntimeError('预约结果未确认，请用 recover 或 retry 加原请求编号 '+operation+' 核对；不要新建请求') from e
    print(json.dumps({'operation':operation,**result},ensure_ascii=False))
    try:print(json.dumps({'current':client.state()},ensure_ascii=False))
    except Exception:print('预约已创建，但当前队列状态读取失败；可稍后查询。',file=sys.stderr)

if __name__=='__main__':
    if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8')
    if hasattr(sys.stderr,'reconfigure'):sys.stderr.reconfigure(encoding='utf-8')
    try:main()
    except (ApiError,ValueError,RuntimeError,FileNotFoundError,URLError) as e:
        print(str(e),file=sys.stderr);sys.exit(1)
