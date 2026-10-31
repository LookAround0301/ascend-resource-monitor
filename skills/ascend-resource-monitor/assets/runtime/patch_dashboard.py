"""Add an authenticated Stop action to an existing process summary table."""
import argparse
import copy
import json
from pathlib import Path


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
        query=panel['targets'][0]['expr']
        old='sum by(node,employee_id,attribution,container,cwd,host_cwd,directory_group)'
        new='sum by(node,employee_id,attribution,container,cwd,host_cwd,directory_group,control_ref)'
        panel['targets'][0]['expr']=query.replace(old,new)
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
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('input');p.add_argument('output');a=p.parse_args()
    d=json.loads(Path(a.input).read_text(encoding='utf-8'))
    Path(a.output).write_text(json.dumps(add_stop_button(d),ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


if __name__=='__main__': main()
