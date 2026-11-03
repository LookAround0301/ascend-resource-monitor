#!/usr/bin/env python3
"""mTLS-only, stop-only host agent. It never exposes the Docker API itself."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import ssl
import threading
import urllib.parse
from control_common import ControlError, StopEngine, parse_browser_payload


def make_handler(engine, guard=None, queue=None):
    class Handler(BaseHTTPRequestHandler):
        def reply(self, status, body):
            encoded = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self):
            if self.path.startswith('/queue/') and queue:
                try:
                    path=urllib.parse.urlsplit(self.path);q=urllib.parse.parse_qs(path.query,keep_blank_values=True)
                    if any(len(v)!=1 for v in q.values()):raise ControlError(400,'重复查询参数')
                    if path.path=='/queue/containers' and not q:result=queue.containers()
                    elif path.path=='/queue/snapshot' and set(q)=={'container_id','expected'}:
                        result=queue.snapshot(q['container_id'][0],q['expected'][0] or None)
                    elif path.path=='/queue/operation' and set(q)=={'operation_id'}:result=queue.status(q['operation_id'][0])
                    else:raise ControlError(404,'未知队列接口')
                    self.reply(200,result)
                except ControlError as e:self.reply(e.status,{'message':e.message})
                except Exception:self.reply(503,{'message':'队列进程状态暂不可用'})
                return
            if self.path == '/healthz':
                self.reply(200, {'status':'ok'})
            elif self.path == '/guard' and guard:
                self.reply(200, guard.snapshot())
            elif self.path.startswith('/info?'):
                try:
                    q=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query,strict_parsing=True)
                    if set(q)!={'node','target'} or any(len(v)!=1 for v in q.values()):
                        raise ControlError(400,'无效查询')
                    request=parse_browser_payload({'node':q['node'][0],'target':q['target'][0],'confirmed':True})
                    self.reply(200,engine.describe(request))
                except ControlError as e:self.reply(e.status,{'message':e.message})
                except (ValueError,TypeError,KeyError):self.reply(400,{'message':'无效查询'})
                except Exception:self.reply(503,{'message':'容器状态暂时无法核实'})
            else:
                self.reply(404, {'message':'Not found'})

        def do_POST(self):
            if self.path not in ('/stop', '/whitelist', '/queue/kill') or (self.path == '/whitelist' and not guard) or (self.path=='/queue/kill' and not queue):
                self.reply(404, {'message':'Not found'}); return
            try:
                length = int(self.headers.get('Content-Length','0'))
                if not 0 < length <= 65536:
                    raise ControlError(400, '无效请求长度')
                if self.headers.get('Content-Type','').split(';')[0] != 'application/json':
                    raise ControlError(415,'JSON required')
                body = json.loads(self.rfile.read(length))
                if set(body) != {'request','actor'}:
                    raise ControlError(400, '无效请求')
                if self.path=='/queue/kill':result=queue.submit(body['request'],body['actor'])
                elif self.path=='/whitelist':result=guard.whitelist(body['request'],body['actor'])
                else:result=engine.stop(body['request'],body['actor'])
                self.reply(200, result)
            except ControlError as e:
                self.reply(e.status, {'message':e.message})
            except (ValueError, TypeError, KeyError):
                self.reply(400, {'message':'无效请求'})
            except Exception:
                self.reply(503, {'message':'容器状态核验失败，操作未确认；请刷新后核实'})

        def log_message(self, *args):
            pass
    return Handler


def main():
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('--config',required=True); a=p.parse_args()
    with open(a.config,encoding='utf-8') as f: config=json.load(f)
    engine = StopEngine(config)
    guard = None
    if config.get('guard'):
        from container_guard import ContainerGuard
        guard = ContainerGuard(engine)
    queue=None
    if config.get('task_queue'):
        from queue_processes import QueueProcesses
        queue=QueueProcesses(engine,guard)
    server=ThreadingHTTPServer((config['listen_ip'],config.get('port',18084)),make_handler(engine, guard, queue))
    server.daemon_threads=True
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); context.minimum_version=ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(config['cert'],config['key']); context.load_verify_locations(config['ca'])
    context.verify_mode=ssl.CERT_REQUIRED
    server.socket=context.wrap_socket(server.socket,server_side=True)
    if guard:
        threading.Thread(target=guard.run, daemon=True, name='container-guard').start()
    server.serve_forever()


if __name__=='__main__': main()
