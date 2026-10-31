#!/usr/bin/env python3
"""mTLS-only, stop-only host agent. It never exposes the Docker API itself."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import ssl
from control_common import ControlError, StopEngine


def make_handler(engine):
    class Handler(BaseHTTPRequestHandler):
        def reply(self, status, body):
            encoded = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self):
            if self.path == '/healthz':
                self.reply(200, {'status':'ok'})
            else:
                self.reply(404, {'message':'Not found'})

        def do_POST(self):
            if self.path != '/stop':
                self.reply(404, {'message':'Not found'}); return
            try:
                length = int(self.headers.get('Content-Length','0'))
                if not 0 < length <= 8192:
                    raise ControlError(400, '无效请求长度')
                if self.headers.get('Content-Type','').split(';')[0] != 'application/json':
                    raise ControlError(415,'JSON required')
                body = json.loads(self.rfile.read(length))
                if set(body) != {'request','actor'}:
                    raise ControlError(400, '无效请求')
                self.reply(200, engine.stop(body['request'],body['actor']))
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
    server=ThreadingHTTPServer((config['listen_ip'],config.get('port',18084)),make_handler(StopEngine(config)))
    server.daemon_threads=True
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); context.minimum_version=ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(config['cert'],config['key']); context.load_verify_locations(config['ca'])
    context.verify_mode=ssl.CERT_REQUIRED
    server.socket=context.wrap_socket(server.socket,server_side=True)
    server.serve_forever()


if __name__=='__main__': main()
