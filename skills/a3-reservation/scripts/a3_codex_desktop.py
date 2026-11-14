"""Deliver a reservation event to the originating Codex desktop chat.

Uses the installed Codex app-tools MCP and the app pipe inherited from the
authorizing chat. A delivery with an unknown outcome is never sent twice.
"""
import json
import os
from pathlib import Path
import queue
import re
import sqlite3
import subprocess
import sys
import threading
import time

from a3_client import ROOT


def plugin_path():
    root=Path.home()/'.codex/plugins/cache/openai-bundled/codex-app-tools'
    options=[]
    for path in root.glob('*/server.mjs'):
        try:version=tuple(int(x) for x in path.parent.name.split('.'))
        except ValueError:continue
        options.append((version,path))
    if not options:raise RuntimeError('未找到 Codex 桌面工具插件')
    return max(options)[1]


class Rpc:
    def __init__(self):
        node=os.environ.get('CODEX_MCP_NODE_PATH')
        if not node or not Path(node).is_file() or not os.environ.get('CODEX_APP_TOOLS_PIPE_PATH'):
            raise RuntimeError('缺少 Codex 桌面会话连接；请在原 Codex 桌面会话中重新绑定')
        self.process=subprocess.Popen([node,str(plugin_path())],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL,text=True,encoding='utf-8')
        self.messages=queue.Queue();self.next_id=0
        def read():
            try:
                for line in self.process.stdout:
                    try:self.messages.put(json.loads(line))
                    except ValueError:continue
            finally:self.messages.put(None)
        self.reader=threading.Thread(target=read,daemon=True);self.reader.start()

    def emit(self,message):
        self.process.stdin.write(json.dumps(message,ensure_ascii=False)+'\n');self.process.stdin.flush()

    def call(self,method,params):
        self.next_id+=1;number=self.next_id
        self.emit({'jsonrpc':'2.0','id':number,'method':method,'params':params})
        deadline=time.monotonic()+20
        while True:
            remaining=deadline-time.monotonic()
            if remaining<=0:raise TimeoutError('Codex 桌面工具响应超时')
            try:result=self.messages.get(timeout=remaining)
            except queue.Empty:raise TimeoutError('Codex 桌面工具响应超时')
            if result is None:raise RuntimeError('Codex 桌面工具连接已关闭')
            if result.get('id')!=number:continue
            if 'error' in result:raise RuntimeError('Codex 桌面工具拒绝请求')
            return result.get('result',{})

    def initialize(self):
        self.call('initialize',{'protocolVersion':'2025-03-26','capabilities':{},
                                'clientInfo':{'name':'a3-reservation-push','version':'1.0'}})
        self.emit({'jsonrpc':'2.0','method':'notifications/initialized'})
        tools=self.call('tools/list',{}).get('tools',[])
        if not any(t.get('name')=='send_message_to_thread' for t in tools):
            raise RuntimeError('当前 Codex 桌面工具不支持会话消息')

    def close(self):
        self.process.terminate()
        try:self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:self.process.kill();self.process.wait(timeout=3)


def event_input():
    event=json.load(sys.stdin)
    if not isinstance(event,dict):raise ValueError('无效事件')
    job,session,event_id,state=(event.get(k) for k in ('job','session','event_id','state'))
    if not isinstance(job,str) or not re.fullmatch('[0-9a-f]{32}',job):raise ValueError('无效预约编号')
    if not isinstance(session,str) or session!=os.environ.get('CODEX_THREAD_ID'):
        raise ValueError('接收目标与授权此接收程序的 Codex 会话不一致')
    if not isinstance(event_id,str) or not re.fullmatch(re.escape(job)+r':[1-9][0-9]*',event_id):
        raise ValueError('无效事件编号')
    if state not in ('ready','active','due','ended'):raise ValueError('无效事件状态')
    plan=event.get('plan')
    if sys.argv[1:]!=['--test']:
        if not isinstance(plan,str) or not Path(plan).is_absolute() or not Path(plan).is_file() or len(plan)>1024 or any(ord(c)<32 for c in plan):
            raise ValueError('执行计划不可用')
    return job,session,event_id,state,plan


def db_open():
    folder=ROOT/'push';folder.mkdir(parents=True,exist_ok=True)
    if os.name!='nt':os.chmod(folder,0o700)
    path=folder/'codex-delivery.sqlite3'
    fd=os.open(str(path),os.O_CREAT|os.O_RDWR,0o600);os.close(fd)
    db=sqlite3.connect(str(path),timeout=10)
    db.execute('PRAGMA synchronous=FULL')
    db.execute('''CREATE TABLE IF NOT EXISTS deliveries (
        session TEXT NOT NULL,event_id TEXT NOT NULL,status TEXT NOT NULL,updated REAL NOT NULL,
        PRIMARY KEY(session,event_id))''');db.commit()
    return db


def main():
    if sys.argv[1:] not in ([],['--test']):raise ValueError('无效参数')
    job,session,event_id,state,plan=event_input()
    db=db_open()
    try:
        row=db.execute('SELECT status FROM deliveries WHERE session=? AND event_id=?',(session,event_id)).fetchone()
        if row and row[0]=='delivered':
            print(json.dumps({'accepted':True,'event_id':event_id}));return
        if row:raise RuntimeError('上次投递结果不明，已暂停重复发送；请检查原会话：'+event_id)
        rpc=Rpc()
        try:
            rpc.initialize()
            db.execute('BEGIN IMMEDIATE')
            try:
                db.execute('INSERT INTO deliveries VALUES (?,?,?,?)',(session,event_id,'sending',time.time()))
                db.commit()
            except BaseException:db.rollback();raise
            if sys.argv[1:]==['--test']:
                message='A3 会话推送测试：这是一条模拟事件。请简短报告当前会话是否被推送唤醒，不要操作真实预约或业务任务。'
            else:
                message=('A3 预约推送：预约 '+job+'，事件 '+event_id+'，状态 '+state+'。'
                         '此前授权的执行计划文件为 '+json.dumps(plan,ensure_ascii=False)+'。'
                         '请读取执行计划并查询预约的当前状态，再按计划处理；'
                         '检查已有操作和任务进程，避免重复启动或重复操作。')
            result=rpc.call('tools/call',{'name':'send_message_to_thread',
                'arguments':{'threadId':session,'prompt':message},'_meta':{'openai/threadId':session}})
            if result.get('isError') or not any(
                item.get('type')=='text' and _matches_thread(item.get('text'),session)
                for item in result.get('content',[]) if isinstance(item,dict)):
                raise RuntimeError('会话消息投递结果未确认：'+event_id)
            db.execute("UPDATE deliveries SET status='delivered',updated=? WHERE session=? AND event_id=?",
                       (time.time(),session,event_id));db.commit()
            print(json.dumps({'accepted':True,'event_id':event_id}))
        finally:rpc.close()
    finally:db.close()


def _matches_thread(text,session):
    try:result=json.loads(text)
    except (TypeError,ValueError):return False
    return isinstance(result,dict) and result.get('threadId')==session


if __name__=='__main__':
    if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8')
    if hasattr(sys.stderr,'reconfigure'):sys.stderr.reconfigure(encoding='utf-8')
    try:main()
    except (ValueError,RuntimeError,OSError,TimeoutError,sqlite3.Error) as e:
        print(str(e),file=sys.stderr);sys.exit(1)
