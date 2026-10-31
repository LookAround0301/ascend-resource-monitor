#!/usr/bin/env python3
"""Same-origin Grafana reverse proxy with authenticated, CSRF-protected stop route."""
import argparse
import asyncio
import json
import logging
import ssl
import time
import urllib.parse
from control_common import ControlError, audit, check_browser_request, is_admin, parse_browser_payload
from control_ui import confirmation, notice, CONFIRM_JS

HOP={'connection','keep-alive','proxy-authenticate','proxy-authorization','te','trailer','transfer-encoding','upgrade'}


def create_app(config, agent_override=None, describe_override=None):
    from aiohttp import web, ClientSession, ClientTimeout, DummyCookieJar, WSMsgType
    app=web.Application(client_max_size=8*1024*1024)

    async def lifespan(app):
        app['http']=ClientSession(cookie_jar=DummyCookieJar(),auto_decompress=False,
                                  timeout=ClientTimeout(total=65),trust_env=False)
        if not agent_override:
            tls=ssl.create_default_context(cafile=config['ca'])
            tls.load_cert_chain(config['cert'],config['key']); tls.minimum_version=ssl.TLSVersion.TLSv1_2
            app['tls']=tls
        yield
        await app['http'].close()
    app.cleanup_ctx.append(lifespan)

    async def read_identity(request):
        # No shared cookie jar or caller-supplied identity headers can grant authority.
        headers={key:request.headers[key] for key in ['Cookie','Authorization'] if key in request.headers}
        try:
            async with app['http'].get(config['grafana_url']+'/api/user',headers=headers,allow_redirects=False,auto_decompress=True) as r:
                if r.status in (401,403):
                    return {'authenticated':False,'admin':False,'user':None}
                if r.status != 200: raise ControlError(503,'登录身份暂时无法核实，停止操作已拒绝')
                user=await r.json()
            async with app['http'].get(config['grafana_url']+'/api/user/orgs',headers=headers,allow_redirects=False,auto_decompress=True) as r:
                orgs=await r.json() if r.status==200 else []
        except ControlError: raise
        except Exception as e: raise ControlError(503,'登录服务暂不可用，停止操作已拒绝') from e
        return {'authenticated':True,'admin':is_admin(user,orgs,config.get('org_id',1)),
                'user':{k:user.get(k) for k in ['id','login','orgId']}}

    async def identity(request):
        state=await read_identity(request)
        if not state['authenticated']:
            raise ControlError(401,'请先从面板的“管理员登录”入口登录 Grafana 管理员账号')
        if not state['admin']:
            raise ControlError(403,'只有 Grafana 管理员可以停止容器；当前账号仅可查看')
        return state['user']

    async def stop(request):
        event=None
        try:
            check_browser_request(request.headers,request.host,request.scheme)
            actor=await identity(request)
            data=parse_browser_payload(await request.json())
            node=config['nodes'].get(data['node'])
            if not node: raise ControlError(400,'目标主机不在此面板的管理范围')
            event={'action':'container.stop','actor':actor,'node':data['node'],
                   'container_id':data['container_id'],'remote':request.remote}
            audit(config['audit_log'],{**event,'result':'authorized'})
            if agent_override:
                result=await agent_override(data,actor)
            else:
                # Node URLs are exclusively server configuration, never request input.
                async with app['http'].post(node['url']+'/stop',json={'request':data,'actor':actor},
                                             ssl=app['tls'],allow_redirects=False) as r:
                    result=await r.json()
                    if r.status!=200: raise ControlError(r.status,result.get('message','停止请求失败'))
            audit(config['audit_log'],{**event,'result':'completed'})
            return web.json_response(result)
        except ControlError as e:
            if event:
                audit(config['audit_log'],{**event,'result':'rejected','status':e.status,'message':e.message})
            return web.json_response({'message':e.message},status=e.status)
        except (ValueError,TypeError,KeyError):
            return web.json_response({'message':'无效的停止请求'},status=400)
        except Exception:
            logging.exception('Container control request failed')
            return web.json_response({'message':'停止请求结果暂不确定，请刷新状态后核实，勿重复提交'},status=503)

    async def session(request):
        try: return web.json_response(await read_identity(request),headers={'Cache-Control':'no-store','Vary':'Cookie, Authorization'})
        except ControlError as e: return web.json_response({'admin':False,'message':e.message},status=e.status)

    async def confirm(request):
        back='/d/'+config.get('dashboard_uid','a5-dual-node')
        login='/login?'+urllib.parse.urlencode({'redirectTo':request.path_qs})
        try:
            actor=await identity(request)
            if set(request.query)!={'node','target'}:raise ControlError(400,'无效的容器操作链接')
            data=parse_browser_payload({'node':request.query['node'],'target':request.query['target'],'confirmed':True})
            node=config['nodes'].get(data['node'])
            if not node:raise ControlError(400,'目标主机不在管理范围')
            if describe_override:info=await describe_override(data)
            else:
                async with app['http'].get(node['url']+'/info',params={'node':data['node'],'target':request.query['target']},ssl=app['tls'],allow_redirects=False) as r:
                    info=await r.json()
                    if r.status!=200:raise ControlError(r.status,info.get('message','无法核实容器状态'))
            body=confirmation(actor,data,info,back);status=200
        except ControlError as e:body=notice(e.message,login,back);status=e.status
        except Exception:body=notice('容器状态暂时无法核实，请返回面板刷新',login,back);status=503
        return web.Response(text=body,status=status,content_type='text/html',headers={
            'Cache-Control':'no-store','Vary':'Cookie, Authorization','X-Frame-Options':'SAMEORIGIN',
            'Content-Security-Policy':"default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'self'"})

    async def confirm_js(request):
        return web.Response(text=CONFIRM_JS,content_type='application/javascript',headers={'Cache-Control':'no-store'})

    async def personalized_dashboard(data,request):
        # Grafana can load either the legacy DTO or a Kubernetes-style resource.
        model=data.get('dashboard') or data.get('spec') or {}
        links=model.get('links',[])
        if not any(x.get('title')=='管理员登录' for x in links):return data
        try:state=await read_identity(request)
        except ControlError:state={'authenticated':False,'admin':False,'user':None}
        for link in links:
            if link.get('title')!='管理员登录':continue
            if state['authenticated']:
                user=state['user'];link['title']='已登录：'+str(user.get('login') or user['id'])+('（管理员）' if state['admin'] else '（只读）')
                link['url']='/profile'
            else:link['title']='未登录 · 管理员登录'
        return data

    async def health(request):
        return web.json_response({'status':'ok'})

    async def forward(request):
        if request.path.startswith('/a5-control/'):
            return web.json_response({'message':'Not found'},status=404)
        headers={k:v for k,v in request.headers.items() if k.lower() not in HOP}
        url=config['grafana_url']+request.raw_path
        if request.headers.get('Upgrade','').lower()=='websocket':
            try:
                async with app['http'].ws_connect(url,headers=headers) as upstream:
                    downstream=web.WebSocketResponse(); await downstream.prepare(request)
                    async def relay(source,dest):
                        async for msg in source:
                            if msg.type==WSMsgType.TEXT: await dest.send_str(msg.data)
                            elif msg.type==WSMsgType.BINARY: await dest.send_bytes(msg.data)
                            else: break
                    tasks=[asyncio.create_task(relay(downstream,upstream)),asyncio.create_task(relay(upstream,downstream))]
                    done,pending=await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
                    for task in pending: task.cancel()
                    await asyncio.gather(*tasks,return_exceptions=True)
                    return downstream
            except Exception: raise web.HTTPBadGateway(text='Grafana websocket unavailable')
        try:
            is_dashboard=request.path.startswith('/api/dashboards/uid/') or (
                request.path.startswith('/apis/dashboard.grafana.app/') and '/dashboards/' in request.path)
            if request.method=='GET' and is_dashboard:
                fresh_headers={k:v for k,v in headers.items() if k.lower() not in {'if-none-match','if-modified-since'}}
                async with app['http'].get(url,headers=fresh_headers,allow_redirects=False,auto_decompress=True) as upstream:
                    if upstream.status==200:
                        data=await upstream.json()
                        return web.json_response(await personalized_dashboard(data,request),headers={'Cache-Control':'no-store','Vary':'Cookie, Authorization'})
                    return web.Response(status=upstream.status,body=await upstream.read(),content_type='application/json')
            async with app['http'].request(request.method,url,headers=headers,data=request.content,
                                           allow_redirects=False) as upstream:
                response=web.StreamResponse(status=upstream.status,reason=upstream.reason)
                for k,v in upstream.headers.items():
                    if k.lower() not in HOP: response.headers.add(k,v)
                await response.prepare(request)
                async for chunk in upstream.content.iter_chunked(64*1024): await response.write(chunk)
                await response.write_eof()
                return response
        except Exception: raise web.HTTPBadGateway(text='Grafana is temporarily unavailable')

    app.router.add_post('/a5-control/stop',stop)
    app.router.add_get('/a5-control/session',session)
    app.router.add_get('/a5-control/confirm',confirm)
    app.router.add_get('/a5-control/confirm.js',confirm_js)
    app.router.add_get('/a5-control/healthz',health)
    app.router.add_route('*','/{tail:.*}',forward)
    return app


def main():
    from aiohttp import web
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('--config',required=True); a=p.parse_args()
    with open(a.config,encoding='utf-8') as f: config=json.load(f)
    web.run_app(create_app(config),host='127.0.0.1',port=config.get('port',3000),access_log=None,print=None,shutdown_timeout=5)


if __name__=='__main__': main()
