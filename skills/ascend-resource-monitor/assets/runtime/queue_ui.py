"""Self-service per-node reservations and optional container association."""
QUEUE_HTML = '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>A5 任务队列</title><style>
:root{color-scheme:dark}*{box-sizing:border-box}body{margin:0;background:#101217;color:#e8edf5;font:15px system-ui,sans-serif}main{max-width:1360px;margin:32px auto;padding:0 24px}header,.row{display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap}h1{font-size:28px;margin:0}h2{font-size:21px;margin:0 0 16px}p{line-height:1.7;color:#adb9cb}a{color:#89b8ff}section{border:1px solid #303948;background:#191e27;border-radius:12px;padding:24px;margin:20px 0}.row{justify-content:flex-start;align-items:end}label{display:grid;gap:7px;color:#bfcbdc}input,select{background:#10151d;color:#edf2fa;border:1px solid #46536a;border-radius:6px;padding:10px;font:inherit;min-width:180px;max-width:100%}button,a.button{font:inherit;border:0;border-radius:6px;padding:10px 16px;cursor:pointer;background:#316bd6;color:white;text-decoration:none;display:inline-block}button:disabled{opacity:.45;cursor:not-allowed}.secondary{background:#343f51}.danger{background:#b43245}.good{color:#7ed9a9}.error{color:#ffabb7}small{color:#a4b1c4}table{width:100%;border-collapse:collapse;margin-top:20px}th,td{text-align:left;border-bottom:1px solid #303948;padding:12px 8px;vertical-align:top}th{color:#a4b1c4;font-weight:500}td button{padding:7px 10px;margin:0 6px 6px 0;white-space:nowrap}.scroll{overflow:auto}.summary{color:#89b8ff}#message{white-space:pre-wrap;position:sticky;bottom:8px;background:#101217;padding:12px;border-radius:6px}#message:empty{display:none}#account-status{color:#80d4ab;margin-bottom:14px}dialog{background:#191e27;color:#e8edf5;border:1px solid #586985;border-radius:12px;padding:28px;max-width:720px;width:94vw}dialog::backdrop{background:#000b}pre{background:#10151d;border:1px solid #303948;padding:14px;white-space:pre-wrap;overflow-wrap:anywhere;max-height:260px;overflow:auto}fieldset{border:1px solid #46536a;border-radius:6px;margin:16px 0}fieldset label,.check{display:flex;align-items:center;padding:10px;gap:10px}input[type=radio],input[type=checkbox]{min-width:0}.hidden{display:none!important}#bind-container{width:100%;margin:12px 0}.queue-form{margin-top:14px}.queue-form input{width:330px}
</style></head><body><main>
<header><div><h1>A5 任务队列</h1><p>选服务器加入队列 → 登记开始 → 登记结束</p></div><div><a class="button secondary" href="__BACK__">返回监控</a> <a id="admin-login" href="/login?redirectTo=%2Fa5-queue">管理员登录</a></div></header>
<section><h2>个人账号</h2><div id="account-status"></div>
<form id="account-form" class="row"><label>工号<input id="employee" name="username" autocomplete="username" pattern="[A-Za-z][0-9]{8}" required></label><label>姓名<input id="name" autocomplete="name"></label><label>个人密码<input id="password" type="password" name="password" autocomplete="current-password" minlength="10" maxlength="128" required></label><button name="login">登录</button><button name="register" class="secondary">首次注册</button></form>
<p id="account-help">首次注册填写工号、姓名和个人密码，注册后即可加入任一服务器队列，无需管理员审批。</p><button id="logout" class="secondary hidden">退出个人账号</button><p id="admin-tools" class="hidden">管理员可操作全部队列任务。<a href="/a5-control/guard" target="_blank">容器管控 / 白名单</a></p></section>
<section id="direct" class="hidden"><h2>当前容器操作</h2><p id="direct-info"></p><button id="direct-stop" class="danger">Stop：选择停止方式</button></section>
<div id="queues"></div>
<p>两台服务器分别排队，互不占用对方名额。网页只登记使用顺序，服务由用户自行启动。需要网页 Stop 时，在使用中的任务上关联自己的容器；无需关联也可以登记开始和结束。</p>
<p><small>个人密码保护自己的任务记录。容器由用户自行选择，请勿关联他人或共用容器；关联后才能停止该容器实例的 NPU 进程或整个容器。既有自动拦截和白名单规则仍然有效。</small></p><p id="message" role="status"></p>
</main>
<dialog id="bind-dialog"><h2>关联本任务的容器（可选）</h2><p id="bind-node"></p><p>只在需要网页 Stop 时关联。请确认容器内只运行本任务，关联后不能改绑到其他容器。</p><label>搜索容器<input id="bind-search" placeholder="容器名或 ID"></label><select id="bind-container" aria-label="选择容器"></select><label class="check"><input id="bind-owner" type="checkbox">我确认这是本任务自己的容器</label><p id="bind-error" class="error"></p><div class="row"><button id="bind-next">查看关联信息</button><button id="bind-dismiss" class="secondary">返回</button></div></dialog>
<dialog id="confirm-dialog"><h2 id="confirm-title"></h2><p id="confirm-description"></p><fieldset id="stop-modes"><legend>停止方式</legend><label><input type="radio" name="mode" value="process" checked>仅停止 NPU 进程，保留容器</label><label><input type="radio" name="mode" value="container">停止整个容器，影响其中全部任务</label></fieldset><pre id="confirm-target"></pre><p id="confirm-warning" class="error"></p><div class="row"><button id="confirm-button" class="danger">确认</button><button id="dismiss" class="secondary">返回</button></div></dialog>
<script src="/a5-queue/queue.js" defer></script></body></html>'''

QUEUE_JS = '''"use strict";
const $=id=>document.getElementById(id), message=$('message');
let state=null,direct=null,operation=null,token=null,busy=false,polling=false,previewVersion=0,bindRow=null,containers=[];
const queueViews=new Map();
function element(tag,value){const el=document.createElement(tag);el.textContent=value;return el;}
function report(text,error=false){message.textContent=text;message.className=error?'error':'good';}
async function api(name,data){
 const response=await fetch('/a5-queue/api/'+name,{method:data===undefined?'GET':'POST',credentials:'same-origin',cache:'no-store',headers:data===undefined?{}:{'Content-Type':'application/json','X-A5-Queue':'queue-v1'},body:data===undefined?undefined:JSON.stringify(data)});
 const body=await response.json();if(!response.ok)throw new Error(body.message||'操作失败');return body;
}
function statusText(row){return ({queued:'排队第 '+row.position+' 位',active:'使用中',cancelling:'正在结束',cancelled:'已取消',completed:'已结束'})[row.state]||row.state;}
function date(value){return value?new Date(value*1000).toLocaleString():'—';}
function duration(value){const s=Math.max(0,Math.floor(value));return Math.floor(s/3600)+'小时 '+Math.floor(s%3600/60)+'分 '+s%60+'秒';}
function queueView(node){
 if(queueViews.has(node))return queueViews.get(node);
 const section=document.createElement('section');section.dataset.node=node;
 const heading=element('h2',node+' 队列'),summary=element('p','');summary.className='summary';section.append(heading,summary);
 const form=document.createElement('form');form.className='row queue-form';
 const label=element('label','任务说明'),input=document.createElement('input');input.required=true;input.maxLength=120;input.placeholder='例如：模型推理测试';input.setAttribute('aria-label',node+' 任务说明');label.append(input);
 const hoursLabel=element('label','预计占用（小时）'),hours=document.createElement('input');hours.type='number';hours.min='0.25';hours.max='720';hours.step='0.25';hours.value='1';hours.required=true;hours.style.width='160px';hours.setAttribute('aria-label',node+' 预计占用小时');hoursLabel.append(hours);
 const join=element('button','加入 '+node+' 队列');form.append(label,hoursLabel,join);section.append(form);
 form.onsubmit=async event=>{event.preventDefault();join.disabled=true;try{report((await api('enqueue',{node,title:input.value,requested_seconds:Math.round(Number(hours.value)*3600)})).message);input.value='';await refresh();}catch(e){report(e.message,true);}finally{join.disabled=!(state&&state.user&&state.user.registered);}};
 const scroll=document.createElement('div');scroll.className='scroll';const table=document.createElement('table'),thead=document.createElement('thead'),tr=document.createElement('tr');
 for(const title of ['状态','工号 / 姓名','任务 / 关联容器','申请时间 / 预计占用','开始 / 结束时间','已使用','操作'])tr.append(element('th',title));thead.append(tr);table.append(thead);
 const body=document.createElement('tbody');table.append(body);scroll.append(table);section.append(scroll);$('queues').append(section);
 const view={section,summary,join,body};queueViews.set(node,view);return view;
}
function renderQueues(){
 const user=state.user;
 for(const node of state.nodes){
  const view=queueView(node),rows=state.tasks.filter(r=>r.node===node),waiting=rows.filter(r=>r.state==='queued').length,active=rows.find(r=>['active','cancelling'].includes(r.state));
  view.summary.textContent=(active?'当前使用：'+active.employee+' · '+active.title:'当前空闲')+'　|　等待 '+waiting+' 项';
  view.join.disabled=!(user&&user.registered);view.body.replaceChildren();
  for(const row of rows){
   const tr=document.createElement('tr');tr.dataset.job=row.id;tr.append(element('td',statusText(row)),element('td',row.employee+' / '+row.name));
   const detail=element('td',row.title);detail.append(document.createElement('br'),element('small',row.container||'未关联容器（不影响登记）'));
   if(row.message)detail.append(document.createElement('br'),element('small',row.message));
   const requested=element('td',date(row.created));requested.append(document.createElement('br'),element('small','预计：'+(row.requested_seconds?duration(row.requested_seconds):'未填写')));
   const times=element('td','开始：'+date(row.activated));times.append(document.createElement('br'),element('small','结束：'+date(row.finished)));
   const elapsed=row.activated?Math.max(0,(row.finished||state.server_time||Date.now()/1000)-row.activated):0;
   tr.append(detail,requested,times,element('td',duration(elapsed)));
   const actions=document.createElement('td');
   function button(title,callback,danger=false){const b=element('button',title);if(danger)b.className='danger';b.onclick=callback;actions.append(b);}
   if(user&&(row.mine||user.admin)){
    if(row.can_claim)button('开始使用',()=>show({job:row.id,action:'claim'},row));
    if(row.state==='queued')button('取消排队',()=>show({job:row.id,action:'cancel'},row));
    if(row.state==='active'){
     button('结束登记',()=>show({job:row.id,action:'finish'},row));
     if(row.container_id)button('Stop',()=>show({job:row.id,action:'cancel'},row),true);
     else button('关联容器（可选）',()=>openBind(row));
    }
   }
   tr.append(actions);view.body.append(tr);
  }
  if(!rows.length){const tr=document.createElement('tr'),td=element('td','暂无任务。登录后可直接加入此队列。');td.colSpan=7;tr.append(td);view.body.append(tr);}
 }
}
async function refresh(){
 if(polling)return;polling=true;
 try{
  state=await api('state');const user=state.user,registered=user&&user.registered;
  $('account-status').textContent=user?'已登录：'+(user.employee||'')+' '+user.name+(user.admin?'（管理员）':'（个人账号）'):'未登录：可以查看队列，登记需登录';
  $('admin-login').textContent=user&&user.admin?'管理员已登录':'管理员登录';
  $('account-form').classList.toggle('hidden',!!registered);$('account-help').classList.toggle('hidden',!!registered);$('logout').classList.toggle('hidden',!registered);$('admin-tools').classList.toggle('hidden',!(user&&user.admin));
  renderQueues();const params=new URLSearchParams(location.search);$('direct').classList.add('hidden');
  if(params.has('node')&&params.has('target')&&user){
   try{direct=await api('control-context?'+params.toString());$('direct').classList.remove('hidden');$('direct-info').textContent=params.get('node')+' / '+params.get('target').split('@')[0];}
   catch(e){report(e.message,true);}
  }
 }catch(e){report(e.message,true);}finally{polling=false;}
}
$('account-form').onsubmit=async e=>{e.preventDefault();try{const data=await api(e.submitter.name,{employee:$('employee').value,name:$('name').value,password:$('password').value});$('password').value='';report(data.message);await refresh();}catch(e){report(e.message,true);}};
$('logout').onclick=async()=>{try{await api('logout',{});await refresh();}catch(e){report(e.message,true);}};
function filterContainers(){
 const previous=$('bind-container').value,query=$('bind-search').value.trim().toLowerCase();$('bind-container').replaceChildren();
 for(const c of containers.filter(c=>(c.name+' '+c.id).toLowerCase().includes(query)))$('bind-container').add(new Option(c.name+' ['+c.state+'] · '+c.id.slice(0,12),c.id));
 if([...$('bind-container').options].some(o=>o.value===previous))$('bind-container').value=previous;
}
async function openBind(row){
 bindRow=row;containers=[];$('bind-error').textContent='正在读取容器…';$('bind-node').textContent=row.node+' · '+row.title;$('bind-search').value='';$('bind-owner').checked=false;filterContainers();$('bind-dialog').showModal();
 try{containers=await api('containers?node='+encodeURIComponent(row.node));filterContainers();$('bind-error').textContent=containers.length?'':'暂无可关联的容器';}catch(e){$('bind-error').textContent=e.message;}
}
$('bind-search').oninput=filterContainers;$('bind-dismiss').onclick=()=>$('bind-dialog').close();
$('bind-next').onclick=()=>{if(!$('bind-owner').checked||!$('bind-container').value){$('bind-error').textContent='请选择容器，并确认这是本任务自己的容器';return;}$('bind-dialog').close();show({job:bindRow.id,action:'bind',container_id:$('bind-container').value,ownership_confirmed:true},bindRow);};
async function loadPreview(){
 const version=++previewVersion,request={...operation};token=null;$('confirm-button').disabled=true;$('confirm-warning').textContent='正在核实…';
 try{
  const preview=await api('preview',request);if(version!==previewVersion)return;token=preview.token;const s=preview.snapshot;
  $('stop-modes').classList.toggle('hidden',!!(!['cancel','direct'].includes(operation.action)||preview.queued||s.unbound));
  $('confirm-target').textContent=s.node+'\\n'+(s.unbound?'未关联容器':(s.container||s.container_id))+(s.container_changed?'\\n原容器实例已结束，不会操作新实例。':'')+(s.unbound?'':'\\nNPU 进程：'+(s.processes.length?s.processes.map(p=>p.pid+' ('+p.name+')').join(', '):'无'));
  let warning;
  if(operation.action==='claim')warning='仅登记开始使用，服务由你自行启动；结束后请回到该队列登记。';
  else if(operation.action==='bind')warning='确认关联此容器及其中的 NPU 进程。容器归属由你自行确认；请勿关联他人或共用容器。';
  else if(operation.action==='finish')warning=s.unbound?'仅登记结束并释放排队名额，不会停止服务器上的任何进程。请确认你的任务已结束。':s.processes.length?'仍有 NPU 进程，请先自行停止或使用 Stop。':'确认登记结束并让下一位开始。';
  else warning=preview.queued||s.unbound?'只取消队列记录，不操作容器或进程。':operation.mode==='container'?'将停止整个容器，容器内所有进程都会中断。':'将结束上面列出的 NPU 进程，必要时强制终止；容器会保留。';
  $('confirm-warning').textContent=warning;$('confirm-button').disabled=operation.action==='finish'&&s.processes.length>0;
 }catch(e){if(version===previewVersion)$('confirm-warning').textContent=e.message;}
}
async function show(request,row){
 if(busy)return;operation={...request,mode:'process'};document.querySelector('input[name=mode][value=process]').checked=true;
 $('confirm-title').textContent=({claim:'确认开始使用？',finish:'确认结束登记？',bind:'确认关联此容器？'})[request.action]||'确认 Stop / 取消任务？';
 $('confirm-description').textContent=row?row.node+' · '+row.title:'请核对主机、容器和进程';$('confirm-dialog').showModal();await loadPreview();
}
for(const radio of document.querySelectorAll('input[name=mode]'))radio.onchange=async()=>{operation.mode=radio.value;await loadPreview();};
$('dismiss').onclick=()=>{if(!busy)$('confirm-dialog').close();};
$('confirm-button').onclick=async()=>{
 if(busy||!token)return;busy=true;$('confirm-button').disabled=true;
 try{const result=await api('confirm',{...operation,token,confirmed:true});$('confirm-dialog').close();report(result.message);await refresh();
  if(operation.action==='direct'&&result.operation_id){const interval=setInterval(async()=>{try{const r=await api('operation?node='+encodeURIComponent(result.node)+'&id='+result.operation_id);if(r.state!=='pending'){clearInterval(interval);report(r.message,r.state!=='succeeded');await refresh();}}catch(e){clearInterval(interval);report(e.message,true);}},2000);}
 }catch(e){$('confirm-warning').textContent=e.message;token=null;}finally{busy=false;}
};
$('direct-stop').onclick=()=>show(direct.direct?{action:'direct',node:direct.node,target:direct.target}:{action:'cancel',job:direct.job});
refresh();setInterval(()=>{if(!busy)refresh();},3000);
'''
