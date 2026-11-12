"""Subscribe to server-pushed events and pass them to an explicit local adapter.

The adapter is responsible for session delivery and durable event-id deduplication.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
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


def send(adapter,session,job,item):
    if not isinstance(item,dict):raise ValueError('服务器事件格式无效')
    seq=item.get('seq')
    if (type(seq) is not int or seq<1 or item.get('job')!=job
            or item.get('state') not in ('ready','active','due')):
        raise ValueError('服务器事件格式无效')
    event_id=job+':'+str(seq)
    # Supply only structured, task-scoped fields; remote content is not an instruction.
    payload={'event_id':event_id,'session':session,'job':job,'state':item['state'],
             'occurred':item.get('occurred')}
    p=subprocess.run(adapter,input=json.dumps(payload,ensure_ascii=False),text=True,
                     encoding='utf-8',capture_output=True,timeout=60,check=False)
    try:ack=json.loads(p.stdout)
    except ValueError:ack={}
    if p.returncode or not isinstance(ack,dict) or ack.get('accepted') is not True or ack.get('event_id')!=event_id:
        raise RuntimeError('会话适配器未确认事件 '+event_id)
    return seq


def stream(client,job,after):
    body=json.dumps({'job':job,'after':after}).encode()
    req=Request(client.origin+'/a5-queue/api/agent-stream',data=body,headers={
        'Accept':'text/event-stream','Content-Type':'application/json',
        'Origin':client.origin,'X-A5-Queue':'queue-v1'})
    with client.opener.open(req,timeout=40) as response:
        if response.headers.get_content_type()!='text/event-stream':raise ValueError('服务器没有返回事件流')
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
    p.add_argument('--job',required=True);p.add_argument('--session',required=True)
    p.add_argument('--adapter',required=True,help='包含命令及参数数组的 JSON 文件；必须实现事件编号去重')
    args=p.parse_args()
    if not re.fullmatch('[0-9a-f]{32}',args.job):raise ValueError('预约编号无效')
    if not args.session or len(args.session)>256 or any(ord(c)<32 for c in args.session):raise ValueError('会话标识无效')
    adapter=json.loads(Path(args.adapter).read_text(encoding='utf-8'))
    if not isinstance(adapter,list) or not adapter or not all(isinstance(x,str) and x for x in adapter):
        raise ValueError('适配器必须是非空字符串数组')
    config=settings();key=hashlib.sha256((config['url']+'\0'+config['employee']+'\0'+args.job+'\0'+args.session).encode()).hexdigest()
    folder=ROOT/'push'/key;fd=lock(folder/'listener.lock');state=folder/'cursor.json'
    try:
        cursor=json.loads(state.read_text(encoding='utf-8'))['after'] if state.exists() else 0
        if type(cursor) is not int or cursor<0:raise ValueError('推送游标无效')
        client=Client(config);logged_in=False;delay=1
        while True:
            try:
                if not logged_in:client.login();logged_in=True
                for item in stream(client,args.job,cursor):
                    if type(item.get('seq')) is not int or item['seq']<=cursor:raise ValueError('事件顺序无效')
                    seq=send(adapter,args.session,args.job,item)
                    save_private(state,{'after':seq});cursor=seq;delay=1
            except ApiError as e:
                logged_in=False
                print('事件登录失败，稍后重试（HTTP '+str(e.status)+'）',file=sys.stderr,flush=True)
            except HTTPError as e:
                if e.code==401:logged_in=False
                print('事件连接失败，稍后重试（HTTP '+str(e.code)+'）',file=sys.stderr,flush=True)
            except (OSError,URLError,ValueError,RuntimeError,subprocess.SubprocessError) as e:
                print('事件投递未确认，稍后重试：'+str(e),file=sys.stderr,flush=True)
            time.sleep(delay);delay=min(delay*2,60)
    finally:os.close(fd)


if __name__=='__main__':
    if hasattr(sys.stderr,'reconfigure'):sys.stderr.reconfigure(encoding='utf-8')
    try:main()
    except (ValueError,RuntimeError,OSError) as e:print(str(e),file=sys.stderr);sys.exit(1)
