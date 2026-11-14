"""Subscribe to server-pushed events and pass them to an explicit local adapter.

The adapter is responsible for session delivery and durable event-id deduplication.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request

from a5_client import ApiError, Client, ROOT, save_private, settings


def lock(path):
    path.parent.mkdir(parents=True,exist_ok=True)
    if os.name!='nt':os.chmod(path.parent,0o700)
    fd=os.open(str(path),os.O_RDWR|os.O_CREAT,0o600)
    try:
        if os.name=='nt':
            import msvcrt
            if os.fstat(fd).st_size==0:os.write(fd,b'0')
            os.lseek(fd,0,os.SEEK_SET);msvcrt.locking(fd,msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except OSError:
        os.close(fd);raise RuntimeError('该预约和会话已有推送接收程序运行')
    return fd


def send(adapter,session,job,item,plan=None):
    if not isinstance(item,dict):raise ValueError('服务器事件格式无效')
    seq=item.get('seq')
    if (type(seq) is not int or seq<1 or item.get('job')!=job
            or item.get('state') not in ('ready','active','due','ended')):
        raise ValueError('服务器事件格式无效')
    event_id=job+':'+str(seq)
    # Supply only structured, task-scoped fields; remote content is not an instruction.
    payload={'event_id':event_id,'session':session,'job':job,'state':item['state'],
             'occurred':item.get('occurred')}
    if plan:payload['plan']=plan
    p=subprocess.run(adapter,input=json.dumps(payload,ensure_ascii=False),text=True,
                     encoding='utf-8',capture_output=True,timeout=60,check=False)
    try:ack=json.loads(p.stdout)
    except ValueError:ack={}
    if p.returncode or not isinstance(ack,dict) or ack.get('accepted') is not True or ack.get('event_id')!=event_id:
        raise RuntimeError('会话适配器未确认事件 '+event_id)
    return seq


def stream(client,job,after,on_connect=None):
    body=json.dumps({'job':job,'after':after}).encode()
    req=Request(client.origin+'/a5-queue/api/agent-stream',data=body,headers={
        'Accept':'text/event-stream','Content-Type':'application/json',
        'Origin':client.origin,'X-A5-Queue':'queue-v1'})
    with client.opener.open(req,timeout=40) as response:
        if response.headers.get_content_type()!='text/event-stream':raise ValueError('服务器没有返回事件流')
        if on_connect:on_connect()
        event_id=None;data=None
        while True:
            raw=response.readline(1048577)
            if not raw:raise ConnectionError('事件连接已断开')
            if len(raw)>1048576:raise ValueError('事件过大')
            line=raw.decode('utf-8').rstrip('\r\n')
            if not line:
                if data is not None:
                    item=json.loads(data)
                    if not isinstance(item,dict):raise ValueError('服务器事件格式无效')
                    if event_id!=str(item.get('seq')):raise ValueError('事件编号不一致')
                    yield item
                event_id=None;data=None
            elif line.startswith('id: '):event_id=line[4:]
            elif line.startswith('data: '):
                if data is not None:raise ValueError('重复的事件数据')
                data=line[6:]


def main():
    p=argparse.ArgumentParser(description='接收 A5 事件并交给用户配置的会话适配器')
    p.add_argument('--job',required=True);p.add_argument('--session')
    receiver=p.add_mutually_exclusive_group(required=True)
    receiver.add_argument('--adapter',help='包含命令及参数数组的 JSON 文件；必须实现事件编号去重')
    receiver.add_argument('--codex-desktop',action='store_true',help='绑定此 Codex 桌面会话')
    p.add_argument('--plan',help='本机已有的绝对路径执行计划文件，Codex 桌面模式必填')
    p.add_argument('--background',action='store_true',help='在后台启动接收程序')
    p.add_argument('--launch-id',help=argparse.SUPPRESS)
    args=p.parse_args()
    if not re.fullmatch('[0-9a-f]{32}',args.job):raise ValueError('预约编号无效')
    if args.launch_id and not re.fullmatch('[0-9a-f]{32}',args.launch_id):raise ValueError('绑定编号无效')
    if args.codex_desktop:
        thread=os.environ.get('CODEX_THREAD_ID','')
        if args.session and args.session!=thread:raise ValueError('只能绑定当前 Codex 会话')
        args.session=thread
    if not args.session or len(args.session)>256 or any(ord(c)<32 for c in args.session):raise ValueError('会话标识无效')
    if args.codex_desktop:
        if not args.plan or not Path(args.plan).is_absolute():raise ValueError('Codex 桌面模式需要绝对路径执行计划')
        args.plan=str(Path(args.plan).resolve(strict=True))
        if not Path(args.plan).is_file() or len(args.plan)>1024 or any(ord(c)<32 for c in args.plan):raise ValueError('执行计划路径无效')
        adapter=[sys.executable,str(Path(__file__).with_name('a5_codex_desktop.py').resolve())]
    else:adapter=json.loads(Path(args.adapter).read_text(encoding='utf-8'))
    if not isinstance(adapter,list) or not adapter or not all(isinstance(x,str) and x for x in adapter):
        raise ValueError('适配器必须是非空字符串数组')
    config=settings();key=hashlib.sha256((config['url']+'\0'+config['employee']+'\0'+args.job+'\0'+args.session).encode()).hexdigest()
    folder=ROOT/'push'/key
    if args.background:
        if args.codex_desktop:
            from a5_codex_desktop import Rpc
            rpc=Rpc()
            try:rpc.initialize()
            finally:rpc.close()
        client=Client(config);client.login()
        if not any(row.get('id')==args.job for row in client.state()['tasks']):raise ValueError('未找到本人可操作的预约')
        fd=lock(folder/'listener.lock');os.close(fd)
        launch_id=secrets.token_hex(16)
        argv=[sys.executable,str(Path(__file__).resolve()),'--job',args.job,'--launch-id',launch_id]
        if args.codex_desktop:argv+=['--codex-desktop','--plan',args.plan]
        else:argv+=['--session',args.session,'--adapter',str(Path(args.adapter).resolve())]
        if args.plan and not args.codex_desktop:argv+=['--plan',args.plan]
        logfile=folder/'listener.log';log_fd=os.open(str(logfile),os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600)
        try:
            opts={'stdin':subprocess.DEVNULL,'stdout':log_fd,'stderr':log_fd}
            if os.name=='nt':opts['creationflags']=subprocess.CREATE_NEW_PROCESS_GROUP|subprocess.DETACHED_PROCESS|subprocess.CREATE_NO_WINDOW
            else:opts['start_new_session']=True
            child=subprocess.Popen(argv,**opts)
        finally:os.close(log_fd)
        statusfile=folder/'status.json';connected=False
        for _ in range(30):
            if child.poll() is not None:raise RuntimeError('接收程序未能启动，请检查 '+str(logfile))
            try:
                status=json.loads(statusfile.read_text(encoding='utf-8'))
                if status.get('launch_id')==launch_id and status.get('state')=='connected':connected=True;break
            except (OSError,ValueError):pass
            time.sleep(.1)
        print(json.dumps({'started':True,'connected':connected,'pid':status['pid'] if connected else child.pid,'session':args.session,'log':str(logfile)},ensure_ascii=False));return
    fd=lock(folder/'listener.lock');state=folder/'cursor.json'
    try:
        cursor=json.loads(state.read_text(encoding='utf-8'))['after'] if state.exists() else 0
        if type(cursor) is not int or cursor<0:raise ValueError('推送游标无效')
        client=Client(config);logged_in=False;delay=1
        binding_id=args.launch_id or secrets.token_hex(16)
        binding_recorded=False
        while True:
            try:
                if not logged_in:client.login();logged_in=True
                def connected():
                    nonlocal binding_recorded
                    if not binding_recorded:
                        result=client.call('agent-bound',{'job':args.job,'operation':binding_id})
                        if not result.get('recorded') or result.get('job')!=args.job:
                            raise RuntimeError('绑定通知未记录')
                        binding_recorded=True
                    save_private(folder/'status.json',{'pid':os.getpid(),'launch_id':args.launch_id,'state':'connected','updated':time.time()})
                for item in stream(client,args.job,cursor,connected):
                    if type(item.get('seq')) is not int or item['seq']<=cursor:raise ValueError('事件顺序无效')
                    seq=send(adapter,args.session,args.job,item,args.plan)
                    save_private(state,{'after':seq});cursor=seq;delay=1
                    if item['state']=='ended':
                        save_private(folder/'status.json',{'pid':os.getpid(),'launch_id':args.launch_id,'state':'ended','updated':time.time()})
                        return
            except ApiError as e:
                logged_in=False
                print('事件登录失败，稍后重试（HTTP '+str(e.status)+'）',file=sys.stderr,flush=True)
            except HTTPError as e:
                if e.code==401:logged_in=False
                print('事件连接失败，稍后重试（HTTP '+str(e.code)+'）',file=sys.stderr,flush=True)
            except (OSError,URLError,ValueError,RuntimeError,subprocess.SubprocessError) as e:
                print('事件投递未确认，稍后重试：'+str(e),file=sys.stderr,flush=True)
            save_private(folder/'status.json',{'pid':os.getpid(),'launch_id':args.launch_id,'state':'retrying','updated':time.time()})
            time.sleep(delay);delay=min(delay*2,60)
    finally:os.close(fd)


if __name__=='__main__':
    if hasattr(sys.stderr,'reconfigure'):sys.stderr.reconfigure(encoding='utf-8')
    try:main()
    except (ApiError,ValueError,RuntimeError,OSError) as e:print(str(e),file=sys.stderr);sys.exit(1)
