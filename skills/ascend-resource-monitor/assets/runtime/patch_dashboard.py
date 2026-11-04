"""Add an authenticated Stop action to an existing process summary table."""
import argparse
import copy
import json
import re
from pathlib import Path


def add_queue_home_panel(dashboard, nodes):
    """Replace the per-node queue tables with one interactive homepage region."""
    result = copy.deepcopy(dashboard)
    result['links'] = [link for link in result.get('links', [])
                       if link.get('url') != '/a5-queue' and link.get('title') != '任务排队 / 我的任务']
    title = '排队与资源分配'
    content = '<div style="height:100%;overflow:auto"><a href="/a5-queue/home">正在加载预约功能…</a></div>'
    if 'elements' in result:
        elements=result['elements']
        if any(p.get('spec',{}).get('title')==title for p in elements.values()):
            return result
        if result.get('layout',{}).get('kind')!='GridLayout':
            raise ValueError('Queue homepage requires a grid dashboard layout')
        old_titles={node+' 排队情况' for node in nodes}
        keys={key for key,p in elements.items() if p.get('spec',{}).get('title') in old_titles}
        items=result['layout']['spec']['items']
        old_items=[item for item in items if item['spec'].get('element',{}).get('name') in keys]
        anchor=min((p['spec']['y'] for p in old_items),default=10)
        bottom=max((p['spec']['y']+p['spec']['height'] for p in old_items),default=anchor)
        remaining=[item for item in items if item not in old_items]
        for item in remaining:
            if item['spec']['y']>=bottom:item['spec']['y']+=20-(bottom-anchor)
        panel_id=min((elements[key]['spec']['id'] for key in keys),default=max((p['spec']['id'] for p in elements.values()),default=0)+1)
        for key in keys:del elements[key]
        key='panel-'+str(panel_id)
        elements[key]={'kind':'Panel','spec':{'id':panel_id,'title':title,'links':[],
                       'data':{'kind':'QueryGroup','spec':{'queries':[],'transformations':[],'queryOptions':{}}},
                       'vizConfig':{'kind':'VizConfig','group':'text','version':'','spec':{
                           'options':{'mode':'html','content':content},'fieldConfig':{'defaults':{},'overrides':[]}}}}}
        remaining.append({'kind':'GridLayoutItem','spec':{'x':0,'y':anchor,'width':24,'height':20,
                          'element':{'kind':'ElementReference','name':key}}})
        result['layout']['spec']['items']=remaining
        return result
    if any(panel.get('title') == title for panel in result.get('panels', [])):
        return result
    old_titles = {node + ' 排队情况' for node in nodes}
    old = [panel for panel in result.get('panels', []) if panel.get('title') in old_titles]
    panels = [panel for panel in result.get('panels', []) if panel.get('title') not in old_titles]
    anchor = min((p['gridPos']['y'] for p in old), default=10)
    bottom = max((p['gridPos']['y']+p['gridPos']['h'] for p in old), default=anchor)
    height = 20
    shift = height - (bottom-anchor)
    for panel in panels:
        if panel.get('gridPos', {}).get('y', 0) >= bottom:
            panel['gridPos']['y'] += shift
    panel_id = min((p['id'] for p in old), default=max((p['id'] for p in panels), default=0)+1)
    panels.append({'id': panel_id, 'title': title, 'type': 'text',
                   'gridPos': {'x': 0, 'y': anchor, 'w': 24, 'h': height},
                   'options': {'mode': 'html', 'content': content}})
    result['panels'] = panels
    return result


def add_queue_link(dashboard):
    """Only call after the explicitly requested queue backend is installed."""
    result=copy.deepcopy(dashboard)
    result['links']=[x for x in result.get('links',[]) if x.get('title')!='任务排队 / 我的任务']+[
        {'type':'link','title':'任务排队 / 我的任务','url':'/a5-queue','targetBlank':True,
         'includeVars':False,'keepTime':False,'icon':'external link'}]
    for panel in result['panels']:
        if panel.get('title')=='按工号与目录汇总 · 仅 NPU 进程':
            panel['description']='Stop 可选择停止 NPU 进程或整个容器，并需再次确认。个人账号只能操作自己登记使用且归属匹配的任务，管理员可操作全部。顶部“任务排队 / 我的任务”可直接注册，在每台服务器的独立队列登记开始和结束，无需管理员审批。'
    result['version']=result.get('version',1)+1
    return result


def add_queue_panels(dashboard,nodes,prefix='ascend'):
    """One public queue table per server, preserving all other panels."""
    result=add_queue_link(dashboard)
    titles={node:node+' 排队情况' for node in nodes}
    existing={p['title']:p for p in result['panels'] if p.get('title') in titles.values()}
    missing=[node for node in nodes if titles[node] not in existing]
    anchor=next((p['gridPos']['y']+p['gridPos']['h'] for p in result['panels'] if p.get('title')=='容器管控 · 持续拦截与白名单'),4)
    for panel in result['panels']:
        if panel['gridPos']['y']>=anchor:panel['gridPos']['y']+=len(missing)*7
    next_id=max((p['id'] for p in result['panels']),default=0)+1
    order=['queue_position','queue_status','employee_id','employee_name','requested_duration','Value','started_at','requested_at','task_title']
    names={'queue_position':'顺序','queue_status':'状态','employee_id':'工号','employee_name':'姓名','task_title':'任务',
           'requested_at':'申请时间','requested_duration':'预计占用','started_at':'开始时间','Value':'已使用时长','container':'关联容器','task_id':'任务操作'}
    for node in nodes:
        previous=existing.get(titles[node])
        position=previous['gridPos'] if previous else {'x':0,'y':anchor+missing.index(node)*7,'w':24,'h':7}
        panel={'id':previous['id'] if previous else next_id,'title':titles[node],'type':'table','gridPos':position,
               'description':'本服务器独立队列。已使用时长从“开始使用”计时，排队期间为零；预计占用仅作展示，不会超时自动停止。顶部“任务排队 / 我的任务”可登记。',
               'datasource':{'type':'prometheus','uid':'a5-prometheus'},
               'targets':[{'refId':'Queue','expr':prefix+'_queue_task_usage_seconds{node='+json.dumps(node)+'}',
                           'format':'table','instant':True,'range':False}],
               'transformations':[
                   {'id':'convertFieldType','options':{'conversions':[{'targetField':'queue_position','destinationType':'number'}]}},
                   {'id':'organize','options':{
                       'excludeByName':{k:True for k in ['Time','__name__','job','instance','node','container','task_id']},
                       'indexByName':{name:i for i,name in enumerate(order)},'renameByName':names}}],
               'options':{'showHeader':True,'cellHeight':'sm','enablePagination':True,'sortBy':[{'displayName':'顺序','desc':False}]},
               'fieldConfig':{'defaults':{'custom':{'filterable':True,'minWidth':50}},'overrides':[
                   {'matcher':{'id':'byName','options':'已使用时长'},'properties':[{'id':'unit','value':'dthms'},{'id':'decimals','value':0}]},
                   {'matcher':{'id':'byName','options':'顺序'},'properties':[{'id':'mappings','value':[{'type':'value','options':{'0':{'text':'—'}}}]}]}]}}
        for field,width in [('顺序',60),('状态',80),('工号',110),('姓名',80),('预计占用',95),('已使用时长',130),('开始时间',155),('申请时间',155),('任务',150)]:
            panel['fieldConfig']['overrides'].append({'matcher':{'id':'byName','options':field},'properties':[{'id':'custom.width','value':width}]})
        if previous:result['panels'][result['panels'].index(previous)]=panel
        else:result['panels'].append(panel);next_id+=1
    return result


def add_guard_panel(dashboard, prefix='ascend'):
    """Only call for an explicitly provisioned persistent container guard."""
    result = copy.deepcopy(dashboard)
    result['links'] = [x for x in result.get('links', []) if x.get('title') != '容器管控 / 白名单'] + [
        {'type': 'link', 'title': '容器管控 / 白名单', 'url': '/a5-control/guard',
         'targetBlank': True, 'includeVars': False, 'keepTime': False, 'icon': 'external link'}]
    title = '容器管控 · 持续拦截与白名单'
    if any(p.get('title') == title for p in result['panels']): return result
    panel_id = max(p['id'] for p in result['panels']) + 1
    for panel in result['panels']:
        if panel['gridPos']['y'] >= 4: panel['gridPos']['y'] += 6
    order = ['node', 'employee_ids', 'container', 'guard_state', 'guard_result', 'container_id']
    result['panels'].append({'id': panel_id, 'title': title, 'type': 'table',
        'gridPos': {'x': 0, 'y': 4, 'w': 24, 'h': 6},
        'description': '此表保留已停止容器的标记。持续拦截绑定完整容器 ID；只有管理员加入白名单后才解除。顶部“容器管控 / 白名单”可查看实时状态和管理白名单。',
        'datasource': {'type': 'prometheus', 'uid': 'a5-prometheus'},
        'targets': [{'refId': 'Guard', 'expr': prefix+'_container_guard_info{node=~"$node"}', 'format': 'table', 'instant': True, 'range': False}],
        'transformations': [{'id': 'organize', 'options': {
            'excludeByName': {k: True for k in ['Time', 'Value', '__name__', 'job', 'instance', 'host']},
            'indexByName': {name: i for i, name in enumerate(order)},
            'renameByName': {'node': '主机', 'employee_ids': '工号', 'container': '容器', 'guard_state': '管控标记',
                             'guard_result': '最近结果', 'container_id': '白名单管理'}}}],
        'options': {'showHeader': True, 'cellHeight': 'sm', 'enablePagination': True},
        'fieldConfig': {'defaults': {'custom': {'filterable': True}}, 'overrides': [
            {'matcher': {'id': 'byName', 'options': '白名单管理'}, 'properties': [
                {'id': 'custom.cellOptions', 'value': {'type': 'data-links'}}, {'id': 'custom.width', 'value': 135},
                {'id': 'links', 'value': [{'title': '管理白名单', 'targetBlank': True,
                    'url': '/a5-control/guard?node=${__data.fields["主机"]}&container_id=${__value.raw}'}]}]},
            {'matcher': {'id': 'byName', 'options': '管控标记'}, 'properties': [
                {'id': 'custom.cellOptions', 'value': {'type': 'color-text'}},
                {'id': 'mappings', 'value': [{'type': 'value', 'options': {'持续拦截': {'text': '持续拦截', 'color': 'red'}, '白名单': {'text': '白名单', 'color': 'green'}}}]}]}]}})
    result['version'] = result.get('version', 1) + 1
    return result


def add_stop_button(dashboard):
    result=copy.deepcopy(dashboard)
    # Data links remain visible in a read-only dashboard; the destination
    # performs administrator authentication and presents the confirmation.
    result['editable']=False
    for panel in result['panels']:
        if panel.get('title')=='NPU 进程归属与工作目录':
            organize=next(t for t in panel['transformations'] if t['id']=='organize')['options']
            organize['excludeByName'].update({'control_ref':True,'container_id':True,'container_started_at':True})
        if panel.get('title')!='按工号与目录汇总 · 仅 NPU 进程': continue
        for target in panel['targets']:
            query=target['expr']
            match=re.search(r'\b(?:sum|min|max)\s+by\s*\(([^)]*)\)',query)
            if match:
                labels=[key.strip() for key in match[1].split(',')]
                if 'control_ref' not in labels:
                    labels.append('control_ref')
                    target['expr']=query[:match.start(1)]+','.join(labels)+query[match.end(1):]
        organize=next(t for t in panel['transformations'] if t['id']=='organize')['options']
        organize['renameByName']['control_ref']='操作'
        indexes=organize.setdefault('indexByName',{})
        ordered=sorted((name for name in indexes if name!='control_ref'),key=indexes.get)
        for index,name in enumerate(ordered,1):indexes[name]=index
        indexes['control_ref']=0
        panel['description']='仅展示 NPU 进程。Stop 需先登录 Grafana 管理员，并二次确认；会停止该行对应的整个容器，影响其中全部任务。'
        overrides=[o for o in panel['fieldConfig']['overrides'] if o.get('matcher',{}).get('options')!='操作']
        overrides.append({'matcher':{'id':'byName','options':'操作'},'properties':[
            {'id':'custom.width','value':85},
            {'id':'custom.cellOptions','value':{'type':'data-links'}},
            # A new tab forces an HTTP request to the proxy endpoint instead
            # of letting Grafana's client router consume the relative URL.
            {'id':'links','value':[{'title':'Stop','targetBlank':True,
                'url':'/a5-control/confirm?node=${__data.fields["主机"]}&target=${__value.raw}'}]}
        ]})
        panel['fieldConfig']['overrides']=overrides
    login={'type':'link','title':'管理员登录','url':'/login?redirectTo=%2Fd%2F'+result['uid'],
           'targetBlank':False,'includeVars':False,'keepTime':False,'icon':'signin'}
    result['links']=[x for x in result.get('links',[]) if x.get('title')!='管理员登录']+[login]
    result['version']=result.get('version',1)+1
    if any(x.get('url')=='/a5-queue' for x in result['links']):return add_queue_link(result)
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('input');p.add_argument('output');a=p.parse_args()
    d=json.loads(Path(a.input).read_text(encoding='utf-8'))
    Path(a.output).write_text(json.dumps(add_stop_button(d),ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


if __name__=='__main__': main()
