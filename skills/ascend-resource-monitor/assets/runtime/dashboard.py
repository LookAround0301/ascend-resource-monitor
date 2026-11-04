"""Render portable Grafana dashboard from validated configuration."""
import json
import re
from process_panels import add_process_panels

def dashboard(c):
    q = lambda text: json.dumps(text, ensure_ascii=False)
    mounts = '|'.join(re.escape(p) for p in c['shared_mounts']) or 'a^'
    panels=[]
    def panel(title, expression, x, y, w=12, h=8, kind='timeseries', unit='percent', legend='{{node}}', thresholds=None):
        item={'id':len(panels)+1,'title':title,'type':kind,'gridPos':{'x':x,'y':y,'w':w,'h':h},
              'datasource':{'type':'prometheus','uid':c['id'] + '-prometheus'},
              'targets':[{'refId':'A','expr':expression,'legendFormat':legend,'range':kind=='timeseries','instant':kind!='timeseries'}],
              'fieldConfig':{'defaults':{'unit':unit,'decimals':1,'color':{'mode':'palette-classic'},'thresholds':{'mode':'absolute','steps':thresholds or [{'color':'green','value':None},{'color':'orange','value':80},{'color':'red','value':95}]}},'overrides':[]},
              'options':{'tooltip':{'mode':'multi','sort':'desc'},'legend':{'displayMode':'table','placement':'bottom','calcs':['lastNotNull']}}}
        if kind=='stat':
            item['options']={'reduceOptions':{'calcs':['lastNotNull'],'fields':'','values':False},'orientation':'auto','textMode':'auto','colorMode':'value','graphMode':'area','justifyMode':'auto'}
        if unit=='percent': item['fieldConfig']['defaults'].update(min=0,max=100)
        panels.append(item)
    
    node='node=~"$node"'
    panel('在线主机',f'sum(up{{job="node",{node}}})',0,0,4,4,'stat','none',thresholds=[{'color':'red','value':None},{'color':'green','value':len(c['nodes'])}])
    panel('健康 NPU',f'sum(machine_healthy_npu_nums{{{node}}})',4,0,4,4,'stat','none',thresholds=[{'color':'orange','value':None},{'color':'green','value':1}])
    panel('NPU 平均利用率',f'avg(npu_chip_info_utilization{{{node}}})',8,0,4,4,'stat')
    panel('HBM 总使用量',f'sum(npu_chip_info_hbm_used_memory{{{node}}}) * 1024 * 1024',12,0,4,4,'stat','bytes')
    panel('共享盘剩余容量',f'max(node_filesystem_avail_bytes{{{node},mountpoint=~{q(mounts)}}})',16,0,4,4,'stat','bytes')
    panel('共享盘使用率',f'100 * (1 - max(node_filesystem_avail_bytes{{{node},mountpoint=~{q(mounts)}}}) / max(node_filesystem_size_bytes{{{node},mountpoint=~{q(mounts)}}}))',20,0,4,4,'stat')
    panel('NPU 利用率 · 每张卡',f'npu_chip_info_utilization{{{node}}}',0,4,12,9,legend='{{node}} · NPU {{id}}')
    panel('HBM 使用率 · 每张卡',f'100 * npu_chip_info_hbm_used_memory{{{node}}} / npu_chip_info_hbm_total_memory{{{node}}}',12,4,12,9,legend='{{node}} · NPU {{id}}')
    panel('NPU 温度',f'npu_chip_info_temperature{{{node}}}',0,13,12,8,unit='celsius',legend='{{node}} · NPU {{id}}')
    panel('NPU 功耗',f'npu_chip_info_power{{{node}}}',12,13,12,8,unit='watt',legend='{{node}} · NPU {{id}}')
    panel('CPU 使用率',f'100 * (1 - avg by(node) (rate(node_cpu_seconds_total{{mode="idle",{node}}}[$__rate_interval])))',0,21,12,8)
    panel('主机内存使用率',f'100 * (1 - node_memory_MemAvailable_bytes{{{node}}} / node_memory_MemTotal_bytes{{{node}}})',12,21,12,8)
    panel('业务与数据网卡 · 接收带宽',f'sum by(node,device) (rate(node_network_receive_bytes_total{{{node},device!="lo",device=~{q(c["network_regex"])}}}[$__rate_interval]))',0,29,12,8,unit='Bps',legend='{{node}} · {{device}}')
    panel('业务与数据网卡 · 发送带宽',f'sum by(node,device) (rate(node_network_transmit_bytes_total{{{node},device!="lo",device=~{q(c["network_regex"])}}}[$__rate_interval]))',12,29,12,8,unit='Bps',legend='{{node}} · {{device}}')
    panel('本地磁盘使用率',f'100 * (1 - node_filesystem_avail_bytes{{{node},mountpoint=~{q("/|"+re.escape(c["storage_root"]))},fstype!~"tmpfs|overlay"}} / node_filesystem_size_bytes{{{node},mountpoint=~{q("/|"+re.escape(c["storage_root"]))},fstype!~"tmpfs|overlay"}})',0,37,12,8,legend='{{node}} · {{mountpoint}}')
    panel('共享存储可用容量', f'max by(mountpoint) (node_filesystem_avail_bytes{{{node},mountpoint=~{q(mounts)}}})',12,37,12,8,unit='bytes',legend='{{mountpoint}}')
    panel('指标采集状态',f'up{{{node}}}',0,45,12,6,'stat','none',legend='{{node}} · {{job}}',thresholds=[{'color':'red','value':None},{'color':'green','value':1}])
    panel('NPU 进程数',f'npu_chip_info_process_info_num{{{node}}}',12,45,12,6,'stat','none',legend='{{node}} · NPU {{id}}')
    add_process_panels(panels, node)
    for p in panels:
        p['datasource']['uid'] = c['id'] + '-prometheus'
    labels = [n['name'] for n in c['nodes']]
    dashboard={'uid':c['id'],'title':c['title'],'tags':['Ascend','resource-monitor'],'timezone':'browser','schemaVersion':39,'version':1,'refresh':'1s','editable':False,
               'time':{'from':'now-30m','to':'now'},'timepicker':{'refresh_intervals':['1s','5s','10s','30s']},'panels':panels,
               'templating':{'list':[{'name':'node','label':'主机','type':'custom','query':','.join(labels),'multi':True,'includeAll':True,'allValue':'.*','current':{'text':'All','value':'$__all'}}]}}
    return dashboard
