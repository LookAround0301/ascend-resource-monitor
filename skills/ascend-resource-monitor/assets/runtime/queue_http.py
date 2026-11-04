"""Same-origin reservation UI/API with separate personal account sessions."""
import asyncio
from collections import defaultdict, deque
import secrets
import time

from control_common import ControlError, audit, parse_browser_payload
from queue_store import QueueStore
from queue_processes import QueueProcesses
from queue_ui import QUEUE_HTML, QUEUE_JS
from queue_metrics import render_queue_metrics
from queue_auto import AutoQueue
from queue_unified import UnifiedQueue
from queue_unified_ui import HOME_JS

COOKIE='a5_queue_session'


def install_queue(app,config,read_identity,admin_identity,agent_override=None):
    from aiohttp import web
    settings=config['task_queue'];store=QueueStore(settings['database'])
    limits=defaultdict(deque)

    async def agent(node,path,data=None):
        if node not in config['nodes']: raise ControlError(400,'未知主机')
        if agent_override: return await agent_override(node,path,data)
        async with app['http'].request('GET' if data is None else 'POST',config['nodes'][node]['url']+path,
                                       json=data,ssl=app['tls'],allow_redirects=False,auto_decompress=True) as response:
            result=await response.json()
            if response.status!=200: raise ControlError(response.status,result.get('message','主机状态暂不可用'))
            return result

    unified=UnifiedQueue(store, config['nodes'], settings.get('reservation_buffer_seconds',600), settings.get('timezone','UTC'), settings.get('daily_reset',False)) if settings.get('unified_queue') is True else None
    auto=AutoQueue(store,agent) if not unified and settings.get('auto_match_employee') else None
    monitor=None
    if unified and settings.get('monitor_processes'):
        from queue_monitor import QueueMonitor
        async def fetch_inventory(url,query):
            from aiohttp import ClientTimeout
            async with app['http'].get(url+'/api/v1/query',params={'query':query},auto_decompress=True,timeout=ClientTimeout(total=5)) as response:
                if response.status!=200:raise ValueError('Process inventory unavailable')
                return await response.json()
        monitor=QueueMonitor(config['nodes'],settings,fetch_inventory)

    async def actor(request,required=True):
        person=store.user(request.cookies.get(COOKIE))
        try: identity=await read_identity(request)
        except ControlError: identity={'admin':False}
        if identity.get('admin'):
            if person: return {**person,'admin':True,'registered':True}
            return {'id':'grafana:'+str(identity['user']['id']),'employee':'','name':identity['user']['login'],'admin':True,'registered':False}
        if person: return {**person,'admin':False,'registered':True}
        if required: raise ControlError(401,'请先使用工号和个人密码登录')
        return None

    async def snapshot(row):
        from urllib.parse import urlencode
        if not row['container_id']:
            return {'container_id':'','container_started_at':None,'processes':[],'unbound':True}
        return await agent(row['node'],'/queue/snapshot?'+urlencode({'container_id':row['container_id'],'expected':row.get('container_started_at') or ''}))

    async def selected_container(node,cid):
        from control_common import CONTAINER_ID
        if not isinstance(cid,str) or not CONTAINER_ID.fullmatch(cid):raise ControlError(400,'请选择有效容器')
        items=await agent(node,'/queue/containers')
        info=next((item for item in items if item['id']==cid),None)
        if not info:raise ControlError(409,'该容器已不存在或不允许操作，请刷新列表')
        return info

    async def reconcile():
        for row in store.cancelling():
            try:
                if row['cancel_batch']!='[]':
                    await AutoQueue(store,agent).reconcile(row)
                    continue
                result=await agent(row['node'],'/queue/operation?operation_id='+row['cancel_operation'])
                if result['state']!='pending':store.resolve_cancel(row['id'],result['state']=='succeeded',result['message'])
            except ControlError as e:
                if e.status==404 and time.time()-(row.get('cancel_since') or time.time())>120:
                    store.resolve_cancel(row['id'],False,'主机未登记本次取消请求；预约已保留，请重新查看进程并确认')
            except Exception: pass

    async def lifecycle(app):
        async def poll():
            while True:
                try:
                    if unified: await asyncio.to_thread(unified.tick)
                    else: await reconcile()
                    if monitor: await monitor.poll()
                except Exception: pass
                if auto:
                    try:await auto.refresh()
                    except Exception:pass
                await asyncio.sleep(3)
        task=asyncio.create_task(poll())
        yield
        task.cancel()
        await asyncio.gather(task,return_exceptions=True)
    app.cleanup_ctx.append(lifecycle)

    def csrf(request):
        if (request.headers.get('Origin')!=request.scheme+'://'+request.host
                or request.headers.get('X-A5-Queue')!='queue-v1'
                or request.content_type!='application/json'):
            raise ControlError(403,'无效来源或请求格式')
        if request.content_length is None or request.content_length>16384:raise ControlError(400,'请求过大')

    def event(user,action,**fields):
        audit(config['audit_log'],{'action':'queue.'+action,'actor':{k:user[k] for k in ('id','employee','name','admin') if k in user},**fields})

    async def page(request):
        if unified:
            raise web.HTTPFound('/d/'+config.get('dashboard_uid','a5-dual-node'), headers={'Cache-Control':'no-store'})
        return web.Response(text=QUEUE_HTML.replace('__BACK__','/d/'+config.get('dashboard_uid','a5-dual-node')),
                            content_type='text/html',headers={'Cache-Control':'no-store','X-Frame-Options':'DENY',
            'Content-Security-Policy':"default-src 'none'; style-src 'unsafe-inline'; script-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'"})

    async def javascript(request):return web.Response(text=HOME_JS if unified else QUEUE_JS,content_type='application/javascript',headers={'Cache-Control':'no-store'})

    async def metrics(request):
        rows=store.view(None)['tasks'] if not unified else unified.view(None)['tasks']
        process_metrics=monitor.metrics(rows,continuing=unified.continuing()) if monitor else ''
        if unified:
            rows=[{**row,'node':node,'state':'queued' if row['state']=='ready' else row['state'], 'container':'',
                   'title':('双机共 '+str(row['total_cards'])+' 卡（各 '+str(row['cards'])+' 卡）') if row['dual'] else '单机 '+str(row['cards'])+' 卡'}
                  for row in rows for node in (list(row['allocations']) or ([row['fixed_node']] if row['fixed_node'] else []))]
        return web.Response(text=render_queue_metrics(rows,list(config['nodes']),time.time(),
                            settings.get('metric_prefix','ascend'),settings.get('timezone','UTC'))+process_metrics,
                            content_type='text/plain',headers={'Cache-Control':'no-store'})

    async def api(request):
        try:
            operation=request.match_info['operation']
            if request.method=='GET':
                person=await actor(request,required=False)
                if operation=='state':
                    state=unified.view(person) if unified else {**store.view(person),'nodes':list(config['nodes']),'server_time':time.time(),'auto_match_employee':auto is not None}
                    if monitor:state=monitor.decorate(state,unified.continuing())
                    return web.json_response(state,headers={'Cache-Control':'no-store','Vary':'Cookie, Authorization'})
                if unified and operation in ('containers','operation','control-context'):
                    raise ControlError(404,'统一队列不提供网页停止或容器关联')
                if operation=='containers':
                    if not person:raise ControlError(401,'请先登录个人账号')
                    return web.json_response(await agent(request.query['node'],'/queue/containers'),headers={'Cache-Control':'no-store'})
                if operation=='operation':
                    await admin_identity(request)
                    return web.json_response(await agent(request.query['node'],'/queue/operation?operation_id='+request.query['id']))
                if operation=='control-context':
                    if not person:raise ControlError(401,'请登录个人账号或管理员账号')
                    data=parse_browser_payload({'node':request.query['node'],'target':request.query['target'],'confirmed':True})
                    if data['node'] not in config['nodes']:raise ControlError(400,'未知主机')
                    if auto:
                        if person['admin']:return web.json_response({'direct':True,'node':data['node'],'target':request.query['target']})
                        row=store.active_on_node(person,data['node'])
                        matched=await auto.fetch(row)
                        if not any(t['container_id']==data['container_id'] and t['container_started_at']==data['container_started_at'] for t in matched['targets']):
                            raise ControlError(403,'此容器未匹配到当前账号的工号')
                        return web.json_response({'job':row['id'],'direct':False})
                    try:row=store.active_for(person,data['node'],data['container_id'])
                    except ControlError:
                        if not person['admin']:raise
                        return web.json_response({'direct':True,'node':data['node'],'target':request.query['target']})
                    if person['admin'] and row['container_started_at']!=data['container_started_at']:
                        return web.json_response({'direct':True,'node':data['node'],'target':request.query['target']})
                    if row['container_started_at']!=data['container_started_at']:raise ControlError(409,'任务关联的是旧容器实例，请刷新')
                    return web.json_response({'job':row['id'],'direct':False})
                raise ControlError(404,'未知接口')
            csrf(request);data=await request.json()
            if not isinstance(data,dict):raise ControlError(400,'请求必须为对象')
            if operation in ('register','login'):
                now=time.monotonic()
                for key in list(limits):
                    if not limits[key] or limits[key][-1]<now-300:del limits[key]
                if len(limits)>4096:raise ControlError(429,'登录请求过多，请稍后重试')
                keys=[(('ip',request.remote or ''),60),(('account',str(data.get('employee','')).strip().lower()[:64]),10)]
                for key,maximum in keys:
                    history=limits[key]
                    while history and history[0]<now-300:history.popleft()
                    if len(history)>=maximum:raise ControlError(429,'尝试次数过多，请五分钟后重试')
                for key,_ in keys:limits[key].append(now)
                if operation=='register':token=await asyncio.to_thread(store.register,data.get('employee'),data.get('name'),data.get('password'))
                else:token=await asyncio.to_thread(store.login,data.get('employee'),data.get('password'))
                response=web.json_response({'message':'登录成功'})
                response.set_cookie(COOKIE,token,path='/a5-queue',httponly=True,secure=request.secure,samesite='Strict',max_age=7*86400)
                response.headers['Cache-Control']='no-store';return response
            person=await actor(request)
            if operation=='logout':
                store.logout(request.cookies.get(COOKIE,''));response=web.json_response({'message':'个人账号已退出'})
                response.del_cookie(COOKIE,path='/a5-queue');return response
            if unified:
                if operation=='enqueue':
                    if not person['registered']:raise ControlError(400,'提交预约前请先登录个人账号')
                    event(person,'enqueue_unified')
                    job=unified.enqueue(person,data)
                    return web.json_response({'message':'已加入统一队列','job':job})
                if operation=='unified-action':
                    if data.get('confirmed') is not True:raise ControlError(400,'请先确认操作')
                    action=data.get('action');job=data.get('job')
                    if action=='claim' and (not isinstance(data.get('offer_id'),str) or not data['offer_id']):
                        raise ControlError(409,'请刷新页面后直接点击“开始使用”')
                    event(person,'unified_'+str(action),job=job)
                    unified.action(person,job,action,data.get('offer_id'))
                    return web.json_response({'message':{'claim':'已登记开始，请自行启动任务','finish':'已结束登记，预约资源已释放','cancel':'已取消预约','priority-up':'已调高一位优先级','priority-down':'已调低一位优先级'}[action]})
                raise ControlError(404,'统一队列不提供网页停止或容器关联')
            if operation=='enqueue':
                if not person['registered']:raise ControlError(400,'提交自己的预约前，请先注册个人账号')
                node=data.get('node');cid=data.get('container_id','')
                if not isinstance(node,str) or node not in config['nodes']:raise ControlError(400,'未知主机')
                if auto and cid:raise ControlError(400,'当前按工号自动匹配，无需指定容器')
                info=await selected_container(node,cid) if cid else None
                event(person,'enqueue',node=node,container_id=cid)
                job=store.enqueue(person,node,cid,data.get('title'),info['name'] if info else '',data.get('requested_seconds',3600))
                return web.json_response({'message':'已加入队列','job':job})
            if operation in ('preview','confirm'):
                action=data.get('action');mode=data.get('mode','process')
                if action not in ('claim','cancel','finish','direct','bind') or mode not in ('process','container'):raise ControlError(400,'无效操作')
                if 'stop' in data and (action!='finish' or type(data['stop']) is not bool):raise ControlError(400,'无效停止选项')
                if action=='direct':
                    await admin_identity(request)
                    parsed=parse_browser_payload({'node':data['node'],'target':data['target'],'confirmed':True})
                    row={'node':parsed['node'],'container_id':parsed['container_id'],'container_started_at':parsed['container_started_at']}
                    job='direct:'+parsed['node']+':'+parsed['container_id']
                else:
                    job=data.get('job');row=store.own(person,job)
                if action=='bind':
                    if auto:raise ControlError(400,'当前按工号自动匹配，无需手动关联')
                    if row['state']!='active' or row['container_id']:raise ControlError(409,'该任务不能重新关联容器')
                    if data.get('ownership_confirmed') is not True:raise ControlError(400,'请确认选择的是本任务自己的容器')
                    await selected_container(row['node'],data.get('container_id'))
                    row={**row,'container_id':data['container_id']}
                if action=='claim' and row['state']!='queued':raise ControlError(409,'预约状态已变化')
                queued=action=='cancel' and row['state']=='queued'
                if auto and action in ('cancel','finish') and row['state']=='active':current=await auto.fetch(row)
                else:current={'processes':[],'node':row['node'],'container_id':row['container_id']} if queued else await snapshot(row)
                current.update(node=row['node'],mode=mode)
                if action=='finish':current['finish_stop']=data.get('stop',False)
                if action in ('claim','bind') and current.get('policy')=='持续拦截':raise ControlError(409,'此容器处于持续拦截，需管理员先加入白名单')
                if operation=='preview':
                    token=store.confirmation(person,job,action,current)
                    return web.json_response({'token':token,'snapshot':current,'queued':queued})
                if data.get('confirmed') is not True:raise ControlError(400,'请先确认操作')
                saved=store.confirm_payload(person,job,action,data.get('token'))
                if (saved.get('mode')!=mode or saved.get('finish_stop',False)!=current.get('finish_stop',False)
                        or not QueueProcesses.same(saved,current)):raise ControlError(409,'停止选项、进程或容器状态已变化，请重新查看并确认')
                event(person,action,job=job,node=row['node'],mode=mode,finish_stop=current.get('finish_stop',False),pids=[p['pid'] for p in current['processes']])
                if action=='direct':
                    store.consume_confirmation(data['token']);payload=saved;payload['operation_id']=secrets.token_hex(16)
                else:row,payload=store.transition(person,job,action,data.get('token'))
                if action in ('claim','bind') or payload is None:
                    return web.json_response({'message':{'claim':'已登记开始，请自行启动任务；结束后回到本队列登记','finish':'已登记结束，下一位可以开始','bind':'容器已关联，可使用 Stop','cancel':'任务记录已取消；未向服务器发送停止指令'}[action]})
                if 'operations' in payload:
                    await AutoQueue(store,agent).submit(row,payload,person,mode)
                    return web.json_response({'message':'已提交 '+str(len(payload['operations']))+' 个匹配目标，停止成功后才会释放队列','state':'pending'})
                try:
                    result=await agent(row['node'],'/queue/kill',{'request':{'operation_id':payload['operation_id'],'snapshot':saved,'mode':mode},'actor':person})
                except ControlError as e:
                    if action!='direct':store.resolve_cancel(job,False,e.message)
                    raise
                return web.json_response({'message':'停止请求已提交，正在核实结果；任务停止成功后才会释放队列','operation_id':payload['operation_id'],'node':row['node'],'state':result['state']})
            raise ControlError(404,'未知接口')
        except ControlError as e:return web.json_response({'message':e.message},status=e.status,headers={'Cache-Control':'no-store'})
        except (ValueError,TypeError,KeyError):return web.json_response({'message':'参数无效，请刷新后重试'},status=400)
        except Exception:return web.json_response({'message':'操作结果暂不能确认，请刷新队列核实，不要连续提交'},status=503)

    app.router.add_get('/a5-queue',page)
    app.router.add_get('/a5-queue/control',page)
    app.router.add_get('/a5-queue/home',page)
    app.router.add_get('/a5-queue/queue.js',javascript)
    app.router.add_get('/a5-queue/home.js',javascript)
    app.router.add_get('/a5-queue/metrics',metrics)
    app.router.add_route('GET','/a5-queue/api/{operation}',api)
    app.router.add_route('POST','/a5-queue/api/{operation}',api)
    return store
