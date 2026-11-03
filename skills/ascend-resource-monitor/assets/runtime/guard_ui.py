"""Administrator allowlist page; no browser-side Docker capability."""
import html
from control_ui import STYLE


def guard_page(user, dashboard_uid):
    esc = lambda value: html.escape(str(value), quote=True)
    return '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>容器管控 / 白名单</title><style>''' + STYLE + '''main{width:min(1100px,94vw)}table{width:100%;border-collapse:collapse;margin:20px 0}td,th{text-align:left;border-bottom:1px solid #39414f;padding:10px;overflow-wrap:anywhere}input,select{padding:9px;background:#101216;color:#eee;border:1px solid #596273}input{width: min(650px,80vw)}.mono{font:12px monospace}button.allow{background:#246648}small{color:#b0bac8}</style>
<main><div class="badge">已登录：''' + esc(user['login']) + '''（管理员）</div><h1>容器管控 / 白名单</h1>
<p>仅对有工号、姓名为空的 NPU 作业自动标记并停止整个容器。标记绑定主机与完整容器 ID；改名、重启不会解除。已标记容器即使暂时不用 NPU，启动后也会被停止。</p>
<p>加入白名单后才解除拦截，不会自动启动容器；移出白名单后，曾被标记的容器恢复拦截。删除并重建是新的容器 ID，需要重新设置白名单。</p>
<p id="health" role="status">正在读取…</p><div id="records"></div>
<h2>提前加入白名单</h2><form id="allow-form"><label>主机 <select id="node" required></select></label><p><label>完整容器 ID<br><input id="cid" pattern="[0-9a-f]{64}" maxlength="64" required></label></p><button class="allow">加入白名单</button></form>
<p id="result" role="status"></p><a class="button" href="/d/''' + esc(dashboard_uid) + '''">返回监控面板</a></main><script src="/a5-control/guard.js" defer></script></html>'''


GUARD_JS = '''"use strict";
const result=document.getElementById('result'), records=document.getElementById('records'), nodes=document.getElementById('node');
let busy=false;
function text(tag,value){const el=document.createElement(tag);el.textContent=value;return el;}
async function change(node,cid,allowed){
 if(busy)return;
 const action=allowed?'加入白名单并解除自动拦截':'移出白名单（曾被标记的容器会再次被自动停止）';
 if(!window.confirm(`${node}\n${cid}\n确认${action}？`))return;
 busy=true; result.textContent='正在保存…';
 try{const r=await fetch('/a5-control/whitelist',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-A5-Control':'stop-v1'},body:JSON.stringify({node,container_id:cid,allowed,confirmed:true})});const data=await r.json();result.textContent=data.message;if(r.ok)await refresh();}
 catch(e){result.textContent='连接中断，请刷新核实白名单状态，勿重复提交。';}finally{busy=false;}
}
async function refresh(){
 const r=await fetch('/a5-control/guard/status',{credentials:'same-origin',cache:'no-store'}), data=await r.json();
 if(!r.ok)throw new Error(data.message||'读取失败');
 records.replaceChildren();const selected=nodes.value;nodes.replaceChildren();let messages=[];
 for(const host of data.nodes){
  nodes.add(new Option(host.node,host.node));
  messages.push(host.node+'：'+(host.enabled?'自动管控已开启':'自动管控未开启')+(host.error?'；'+host.error:''));
  records.append(text('h2',host.node));const table=document.createElement('table');const header=document.createElement('tr');
  for(const h of ['容器 / 工号','完整 ID','标记','最近结果','操作'])header.append(text('th',h));table.append(header);
  for(const row of host.entries||[]){const tr=document.createElement('tr');tr.append(text('td',row.container+' / '+(row.employee_ids||[]).join(', ')));const id=text('td',row.container_id);id.className='mono';tr.append(id,text('td',row.policy),text('td',row.last_result||''));const td=document.createElement('td'),button=text('button',row.whitelisted?'移出白名单':'加入白名单');if(!row.whitelisted)button.className='allow';button.onclick=()=>change(host.node,row.container_id,!row.whitelisted);td.append(button);tr.append(td);table.append(tr);}
  records.append(table);if(!(host.entries||[]).length)records.append(text('p','暂无标记或白名单记录'));
 }
 if(selected)nodes.value=selected;
 const params=new URLSearchParams(location.search);if(params.has('node'))nodes.value=params.get('node');if(params.has('container_id'))document.getElementById('cid').value=params.get('container_id');
 document.getElementById('health').textContent=messages.join('；');
}
document.getElementById('allow-form').addEventListener('submit',e=>{e.preventDefault();change(nodes.value,document.getElementById('cid').value.trim(),true);});
refresh().catch(e=>{document.getElementById('health').textContent=e.message;});
setInterval(()=>{if(!busy)refresh().catch(()=>{});},5000);
'''
