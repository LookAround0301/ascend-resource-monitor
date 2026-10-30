"""Grafana tables for process attribution and comparable working directories."""
def add_process_panels(panels, node):
    for existing in panels:
        if existing['gridPos']['y'] >= 4:
            existing['gridPos']['y'] += 20
    rename = {'node':'主机','employee_id':'工号','attribution':'归属依据','user':'运行用户',
              'container':'容器','name':'进程','pid':'PID','npu_ids':'NPU',
              'cwd':'进程目录','host_cwd':'宿主机目录','entrypoint':'入口程序',
              'service':'系统服务','directory_group':'目录组','started_at':'启动时间',
              'Value':'CPU（单核 %）','ascend_process_cpu_percent':'CPU（单核 %）'}
    hidden = ['Time','__name__','host','instance','job','process_key','ppid','entrypoint','service','started_at','directory_group']
    order = ['node','employee_id','attribution','user','name','pid','container','npu_ids','ascend_process_cpu_percent','Value','cwd','host_cwd']
    panels.append({
        'id':19,'title':'NPU 进程归属与工作目录','type':'table','gridPos':{'x':0,'y':4,'w':24,'h':12},
        'description':'每 10 秒更新，仅列出 NPU Exporter 报告的卡上进程，最多 200 条/主机；没有 NPU 进程时列表为空。工号来自登录/运行用户、人工标注或明确标出的目录与容器线索；root 本身不能识别实际操作者。',
        'datasource':{'type':'prometheus','uid':'a5-prometheus'},
        'targets':[{'refId':'A','expr':f'ascend_process_cpu_percent{{{node},npu_ids!="",npu_ids!="-"}}','format':'table','instant':True,'range':False}],
        'transformations':[{'id':'labelsToFields','options':{'mode':'columns'}},{'id':'merge','options':{}},{'id':'organize','options':{'excludeByName':{name:True for name in hidden},'indexByName':{name:i for i,name in enumerate(order)},'renameByName':rename}}],
        'options':{'showHeader':True,'cellHeight':'sm','enablePagination':True,'sortBy':[{'displayName':'CPU（单核 %）','desc':True}]},
        'fieldConfig':{'defaults':{'custom':{'align':'auto','filterable':True},'decimals':1},'overrides':[
            {'matcher':{'id':'byName','options':'CPU（单核 %）'},'properties':[{'id':'unit','value':'percent'},{'id':'custom.width','value':105}]},
            *[{'matcher':{'id':'byName','options':name},'properties':[{'id':'custom.width','value':300}]} for name in ['进程目录','宿主机目录']],
            {'matcher':{'id':'byName','options':'主机'},'properties':[{'id':'custom.width','value':90}]},
            {'matcher':{'id':'byName','options':'PID'},'properties':[{'id':'custom.width','value':85}]}
        ]}
    })
    group_rename = {'node':'主机','employee_id':'工号','attribution':'归属依据','container':'容器','host_cwd':'宿主机目录','cwd':'进程目录','directory_group':'目录组','Value':'RSS 合计'}
    panels.append({
        'id':20,'title':'按工号与目录汇总 · 仅 NPU 进程','type':'table','gridPos':{'x':0,'y':16,'w':24,'h':8},
        'description':'相同工号线索、宿主机目录和入口程序会得到相同目录组，方便对照多台主机；相同目录组不代表已确认是同一个任务。RSS 是逐进程相加，可能重复计算共享内存页。',
        'datasource':{'type':'prometheus','uid':'a5-prometheus'},
        'targets':[{'refId':'A','expr':f'sum by(node,employee_id,attribution,container,cwd,host_cwd,directory_group) (ascend_process_resident_memory_bytes{{{node},npu_ids!="",npu_ids!="-"}})','format':'table','instant':True,'range':False}],
        'transformations':[{'id':'labelsToFields','options':{'mode':'columns'}},{'id':'merge','options':{}},{'id':'organize','options':{'excludeByName':{'Time':True},'indexByName':{name:i for i,name in enumerate(['employee_id','directory_group','node','container','Value','host_cwd','cwd','attribution'])},'renameByName':group_rename}}],
        'options':{'showHeader':True,'cellHeight':'sm','enablePagination':True,'sortBy':[{'displayName':'RSS 合计','desc':True}]},
        'fieldConfig':{'defaults':{'custom':{'align':'auto','filterable':True},'decimals':1},'overrides':[
            {'matcher':{'id':'byName','options':'RSS 合计'},'properties':[{'id':'unit','value':'bytes'}]},
            *[{'matcher':{'id':'byName','options':name},'properties':[{'id':'custom.width','value':300}]} for name in ['进程目录','宿主机目录']]
        ]}
    })
