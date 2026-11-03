"""Narrow host capability: snapshot and cancel exact NPU PIDs, never launch work."""
import json
import os
from pathlib import Path
import re
import select
import signal
import threading
import time

from control_common import CONTAINER_ID, ControlError
from process_exporter import parse_stat, CONTAINER


class QueueProcesses:
    def __init__(self,engine,guard=None):
        self.engine=engine; self.guard=guard
        self.root=Path(engine.config['task_queue']['operations_dir'])
        self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.live=set(); self.lock=threading.Lock()

    def container(self,cid):
        if not isinstance(cid,str) or not CONTAINER_ID.fullmatch(cid): raise ControlError(400,'无效容器 ID')
        info=self.engine.docker.inspect(cid)
        if info.get('Id')!=cid: raise ControlError(409,'容器身份不匹配')
        labels=(info.get('Config') or {}).get('Labels') or {}
        if info.get('Name','').lstrip('/') in self.engine.config.get('protected_containers',[]) or any(k.startswith('io.kubernetes.') for k in labels):
            raise ControlError(403,'该容器不开放个人任务进程控制')
        return info

    def containers(self):
        data=self.engine.docker.call('GET','/containers/json?all=1')
        result=[]
        for item in data[:256]:
            labels=item.get('Labels') or {}; names=item.get('Names') or ['']
            name=names[0].lstrip('/')
            if name in self.engine.config.get('protected_containers',[]) or any(k.startswith('io.kubernetes.') for k in labels): continue
            result.append({'id':item['Id'],'name':name,'state':item.get('State','')})
        return result

    def read_process(self,pid,cid):
        root=Path('/proc')/str(pid)
        try:
            stat=parse_stat((root/'stat').read_text())
            match=CONTAINER.search((root/'cgroup').read_text())
            if stat['state']=='Z' or match is None or match.group(1)!=cid: return None
            return {'pid':pid,'start_ticks':stat['start_ticks'],'name':(root/'comm').read_text().strip()[:80]}
        except (OSError,ValueError,IndexError): return None

    def snapshot(self,cid,expected=None):
        try: info=self.container(cid)
        except ControlError as e:
            if e.status==404 and expected: return {'container_id':cid,'container_started_at':expected,'container_changed':True,'processes':[]}
            raise
        state=info.get('State',{})
        started=state.get('StartedAt','')
        changed=bool(expected and (started!=expected or not state.get('Running')))
        if not state.get('Running') and not expected: raise ControlError(409,'容器尚未启动，请由用户自行启动后再登记使用')
        if changed: return {'container_id':cid,'container_started_at':expected,'container_changed':True,'processes':[]}
        pids=self.engine.get_npu() & self.engine.docker.pids(cid)
        rows=[row for pid in sorted(pids) if (row:=self.read_process(pid,cid)) is not None]
        policy=self.guard.policy(cid) if self.guard else '未标记'
        return {'container_id':cid,'container_started_at':started,'container_changed':False,
                'container':info.get('Name','').lstrip('/'),'policy':policy,'processes':rows}

    @staticmethod
    def same(a,b):
        return (a.get('container_id'),a.get('container_started_at'),bool(a.get('container_changed')),
                [(x['pid'],x['start_ticks']) for x in a['processes']]) == (
                b.get('container_id'),b.get('container_started_at'),bool(b.get('container_changed')),
                [(x['pid'],x['start_ticks']) for x in b['processes']])

    def path(self,op):
        if not isinstance(op,str) or not re.fullmatch('[0-9a-f]{32}',op): raise ControlError(400,'无效操作 ID')
        return self.root/(op+'.json')

    def save(self,op,data):
        path=self.path(op); temporary=path.with_suffix('.next')
        fd=os.open(temporary,os.O_CREAT|os.O_TRUNC|os.O_WRONLY|getattr(os,'O_NOFOLLOW',0),0o600)
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            json.dump(data,f,ensure_ascii=False);f.flush();os.fsync(f.fileno())
        os.replace(temporary,path)
        if os.name!='nt':
            directory=os.open(str(path.parent),os.O_DIRECTORY)
            try:os.fsync(directory)
            finally:os.close(directory)

    def submit(self,data,actor):
        if not isinstance(data,dict) or set(data)!={'operation_id','snapshot','mode'} or data.get('mode') not in ('process','container'): raise ControlError(400,'无效取消操作')
        if not isinstance(actor,dict) or not actor.get('id'): raise ControlError(403,'缺少已认证操作人')
        if not hasattr(os,'pidfd_open') or not hasattr(signal,'pidfd_send_signal'): raise ControlError(503,'当前内核不支持安全 PID 句柄操作')
        op=data['operation_id']; path=self.path(op)
        with self.lock:
            if path.exists():
                existing=json.loads(path.read_text())
                if existing['request']!=data or existing['actor']!=actor: raise ControlError(409,'操作 ID 已被其他请求使用')
                return {'operation_id':op,'state':existing['state']}
            snapshot=data['snapshot']
            current=self.snapshot(snapshot['container_id'],snapshot['container_started_at'])
            if not self.same(snapshot,current): raise ControlError(409,'NPU 进程已变化，请重新查看并确认')
            self.engine.write_audit({'action':'queue.stop','mode':data['mode'],'operation_id':op,'actor':actor,
                                     'container_id':snapshot['container_id'],'pids':[x['pid'] for x in snapshot['processes']],'result':'requested'})
            record={'request':data,'actor':actor,'state':'pending','created':time.time(),'message':''}
            self.save(op,record);self.live.add(op)
            threading.Thread(target=self.kill,args=(op,record),daemon=True,name='queue-cancel').start()
        return {'operation_id':op,'state':'pending'}

    def kill(self,op,record):
        snapshot=record['request']['snapshot']; handles=[]
        try:
            with self.engine.lock:
                current=self.snapshot(snapshot['container_id'],snapshot['container_started_at'])
                if not self.same(snapshot,current): raise ControlError(409,'NPU 进程已变化，取消操作未执行')
                if record['request']['mode']=='container':
                    if not current.get('container_changed'):
                        self.engine.docker.stop(snapshot['container_id'])
                    try: info=self.engine.docker.inspect(snapshot['container_id'])
                    except ControlError as e:
                        if e.status!=404: raise
                        info={'State':{'Running':False}}
                    state=info.get('State',{})
                    if state.get('Running') and state.get('StartedAt')==snapshot['container_started_at']:
                        raise ControlError(409,'容器仍在运行，请核实状态')
                    record.update(state='succeeded',message='目标容器实例已停止，预约已取消')
                    return
                for row in snapshot['processes']:
                    fd=os.pidfd_open(row['pid']);handles.append(fd)
                    actual=self.read_process(row['pid'],snapshot['container_id'])
                    if actual is None or actual['start_ticks']!=row['start_ticks']: raise ControlError(409,'进程已经退出或 PID 被复用，操作未执行')
                for fd in handles:
                    try: signal.pidfd_send_signal(fd,signal.SIGTERM)
                    except ProcessLookupError: pass
                deadline=time.monotonic()+5
                def alive(fd):
                    poll=select.poll();poll.register(fd,select.POLLIN);return not poll.poll(0)
                while any(alive(fd) for fd in handles) and time.monotonic()<deadline: time.sleep(.1)
                for fd in handles:
                    if alive(fd):
                        try: signal.pidfd_send_signal(fd,signal.SIGKILL)
                        except ProcessLookupError: pass
                # Allow exporter refresh before checking for respawned NPU workers.
                time.sleep(6)
                remaining=self.snapshot(snapshot['container_id'],snapshot['container_started_at'])
                if remaining['processes']: raise ControlError(409,'仍有 NPU 进程或进程被重新拉起；预约保留，请先检查启动器')
                record.update(state='succeeded',message='关联的 NPU 进程已结束，预约已取消')
        except ControlError as e: record.update(state='failed',message=e.message)
        except Exception: record.update(state='failed',message='取消结果需要核实；预约保留，请刷新后检查进程')
        finally:
            for fd in handles: os.close(fd)
            try:
                self.save(op,record)
                self.engine.write_audit({'action':'queue.stop','mode':record['request']['mode'],'operation_id':op,'actor':record['actor'],'result':record['state']})
            finally:
                with self.lock:self.live.discard(op)

    def status(self,op):
        path=self.path(op)
        with self.lock:
            if not path.exists(): raise ControlError(404,'取消操作尚未登记')
            record=json.loads(path.read_text())
            if record['state']=='pending' and op not in self.live:
                # Service restarted: reconcile without sending any more signals.
                snap=record['request']['snapshot']
                current=self.snapshot(snap['container_id'],snap['container_started_at'])
                remains = not current.get('container_changed') if record['request']['mode']=='container' else bool(current['processes'])
                record.update(state='failed' if remains else 'succeeded',
                              message='服务恢复后已核实；若仍有进程请重新确认取消')
                self.save(op,record)
        return {k:record[k] for k in ('state','message')}
