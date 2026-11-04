"""Personnel allowlist view, with authenticated administrator additions."""
import html
from control_ui import STYLE


def guard_page(user, dashboard_uid):
    esc = lambda value: html.escape(str(value), quote=True)
    role = '已登录：' + esc(user['login']) + '（管理员）' if user else '只读查看'
    form = '''<section id="add-panel" hidden><h2>添加人员白名单</h2>
<form id="add-form"><label>工号<input id="employee" pattern="[A-Za-z][0-9]{8}" maxlength="9" placeholder="例如 u12345678" required></label>
<label>姓名<input id="name" maxlength="120" placeholder="填写姓名" required></label>
<button id="add-button" type="submit">添加到白名单</button></form>
<p class="help">保存后同步到两台服务器。此前已拦截的容器仍需单独处理。</p></section>''' if user else ''
    return '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>人员白名单</title><style>''' + STYLE + '''
*{box-sizing:border-box}main{width:min(900px,94vw)}button{background:#2869bd}h1{margin-bottom:8px}h2{font-size:19px}
.toolbar,form{display:flex;gap:14px;flex-wrap:wrap;align-items:end;margin:20px 0}
label{display:grid;gap:7px}input{padding:11px 12px;background:#101216;color:#eee;border:1px solid #596273;border-radius:5px;font:inherit}
#search{width:min(420px,75vw)}#employee{width:210px}#name{width:200px}
.scroll{overflow:auto;max-height:62vh;border:1px solid #39414f;border-radius:6px}
table{width:100%;border-collapse:collapse}td,th{text-align:left;border-bottom:1px solid #39414f;padding:12px 18px}
th{position:sticky;top:0;background:#202630;color:#bbc8da}td:first-child{width:42%;font-family:monospace}
.help,#count{color:#aebacc;font-size:14px}.error{color:#ffabb7}.good{color:#7ed9a9}
button.remove{background:#8c3543;padding:7px 13px}td:last-child{width:95px}
#add-panel{border-top:1px solid #39414f;margin-top:24px;padding-top:10px}
[hidden]{display:none!important}button:disabled{opacity:.5;cursor:wait}
</style></head><body><main><div class="badge">''' + role + '''</div><h1>人员白名单</h1>
<p>已登记工号和姓名的人员。仅管理员可查看、添加和删除。</p>
<div class="toolbar"><label>搜索工号或姓名<input id="search" type="search" placeholder="输入工号或姓名"></label><span id="count"></span></div>
<p id="health" role="status">正在读取人员名单…</p>
<div class="scroll"><table><thead><tr><th>工号</th><th>姓名</th><th>操作</th></tr></thead><tbody id="people"></tbody></table></div>
''' + form + '''<p id="result" role="status"></p><p><a class="button" href="/d/''' + esc(dashboard_uid) + '''">返回监控面板</a> <a href="/login?redirectTo=%2Fa5-control%2Fguard">管理员登录</a></p>
</main><script src="/a5-control/guard.js" defer></script></body></html>'''


GUARD_JS = '''"use strict";
const $=id=>document.getElementById(id);
let people=[],busy=false,polling=false;
function render(){
 const query=$('search').value.trim().toLowerCase(),rows=people.filter(p=>(p.employee+' '+p.name).toLowerCase().includes(query));
 $('people').replaceChildren();
 for(const person of rows){const tr=document.createElement('tr');for(const key of ['employee','name']){const td=document.createElement('td');td.textContent=person[key];tr.append(td);}const td=document.createElement('td'),button=document.createElement('button');button.textContent='删除';button.className='remove';button.disabled=busy;button.onclick=()=>change('remove',person);td.append(button);tr.append(td);$('people').append(tr);}
 if(!rows.length){const tr=document.createElement('tr'),td=document.createElement('td');td.colSpan=3;td.textContent=query?'没有找到匹配的人员':'暂无人员记录';tr.append(td);$('people').append(tr);}
 $('count').textContent='显示 '+rows.length+' / 共 '+people.length+' 人';
}
async function refresh(){
 if(polling)return;polling=true;
 try{
  const response=await fetch('/a5-control/roster',{credentials:'same-origin',cache:'no-store'}),data=await response.json();
  if(!response.ok)throw Error(data.message||'读取失败');
  people=data.people;render();
  $('health').textContent=data.issues.length?data.issues.join('；'):'两台服务器的人员名单已同步';
  $('health').className=data.issues.length?'error':'good';
  if($('add-panel'))$('add-panel').hidden=!data.can_edit;
 }catch(e){$('health').textContent='读取失败，请稍后刷新：'+e.message;$('health').className='error';}
 finally{polling=false;}
}
$('search').addEventListener('input',render);
async function change(action,person){
 if(busy)return;
 const {employee,name}=person;
 if(!name){$('result').textContent='请填写姓名';return;}
 const question=action==='remove'?'确认从人员白名单删除？删除后，该工号的作业可能被自动拦截。':'确认添加到人员白名单？';
 if(!window.confirm(question+'\\n工号：'+employee+'\\n姓名：'+name))return;
 busy=true;$('add-button').disabled=true;$('result').textContent='正在同步…';
 try{
  const response=await fetch('/a5-control/roster/'+action,{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-A5-Control':'stop-v1'},body:JSON.stringify({employee,name,confirmed:true})});
  const data=await response.json();$('result').textContent=data.message;$('result').className=response.ok?'good':'error';
  if(response.ok&&action==='add')$('add-form').reset();await refresh();
 }catch(e){$('result').textContent='连接中断，请刷新名单核实后再重试。';$('result').className='error';}
 finally{busy=false;$('add-button').disabled=false;render();}
}
if($('add-form'))$('add-form').addEventListener('submit',event=>{event.preventDefault();change('add',{employee:$('employee').value.trim().toLowerCase(),name:$('name').value.trim()});});
refresh();setInterval(()=>{if(!busy)refresh();},10000);
'''
