"""Browser UI for reservation-only queues and owner-scoped Stop actions."""
QUEUE_HTML = '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>A5 任务队列</title><style>
:root{color-scheme:dark}*{box-sizing:border-box}body{margin:0;background:#101217;color:#e8edf5;font:15px system-ui,sans-serif}main{max-width:1280px;margin:32px auto;padding:0 24px}header{display:flex;justify-content:space-between;align-items:center;gap:20px}h1{font-size:28px;margin:0}h2{font-size:19px;margin:0 0 18px}p{line-height:1.7;color:#adb9cb}a{color:#89b8ff}section{border:1px solid #303948;background:#191e27;border-radius:12px;padding:22px;margin:20px 0}.row{display:flex;gap:12px;align-items:end;flex-wrap:wrap}label{display:grid;gap:7px;color:#bfcbdc}input,select{background:#10151d;color:#edf2fa;border:1px solid #46536a;border-radius:6px;padding:10px;font:inherit;min-width:180px;max-width:100%}button,a.button{font:inherit;border:0;border-radius:6px;padding:10px 16px;cursor:pointer;background:#316bd6;color:white;text-decoration:none;display:inline-block}button:disabled{opacity:.45;cursor:wait}.secondary{background:#343f51}.danger{background:#b43245}.good{color:#7ed9a9}.error{color:#ffabb7}small{color:#a4b1c4}table{width:100%;border-collapse:collapse}th,td{text-align:left;border-bottom:1px solid #303948;padding:12px 8px;vertical-align:top}th{color:#a4b1c4;font-weight:500}td button{padding:6px 10px;margin:0 6px 6px 0;white-space:nowrap}.scroll{overflow:auto}.badge{color:#8dbdff;font-weight:600}#message{white-space:pre-wrap}#account-status{color:#80d4ab}dialog{background:#191e27;color:#e8edf5;border:1px solid #586985;border-radius:12px;padding:28px;max-width:680px;width:92vw}dialog::backdrop{background:#000a}pre{background:#10151d;border:1px solid #303948;padding:14px;white-space:pre-wrap;overflow-wrap:anywhere;max-height:280px;overflow:auto}fieldset{border:1px solid #46536a;border-radius:6px;margin:16px 0}fieldset label{display:flex;align-items:center;padding:10px}input[type=radio]{min-width:0}#grant-container{max-width:500px}.hidden{display:none!important}
</style></head><body><main>
<header><div><h1>A5 任务队列</h1><p>登记顺序 · 自行启动 · 安全结束任务</p></div><div><a class="button secondary" href="__BACK__">返回监控</a> <a id="admin-login" href="/login?redirectTo=%2Fa5-queue">管理员登录</a></div></header>
<section><h2>个人账号</h2><div id="account-status"></div>
<form id="account-form" class="row"><label>工号<input id="employee" name="username" autocomplete="username" pattern="[A-Za-z][0-9]{8}" required></label><label id="name-label">姓名<input id="name" autocomplete="name"></label><label>个人密码<input id="password" type="password" name="password" autocomplete="current-password" minlength="10" maxlength="128" required></label><button name="login">登录</button><button name="register" class="secondary">首次注册</button></form>
<p id="account-help">首次注册填写工号、姓名并设置个人密码。注册后由管理员授权可使用的容器。此账号与管理员账号分别管理。</p><button id="logout" class="secondary hidden">退出个人账号</button></section>
<section id="direct" class="hidden"><h2>当前容器操作</h2><p id="direct-info"></p><button id="direct-stop" class="danger">Stop：选择停止方式</button></section>
<section><h2>新建预约</h2><p>每台主机分别排队。轮到后点击“开始使用”，再自行通过 SSH 启动服务；网页不会执行任何启动命令。一次预约对应一个容器，该容器在预约期间只运行这一项任务。</p>
<form id="enqueue-form" class="row"><label>已授权的主机 / 容器<select id="grant" required></select></label><label>任务说明<input id="title" maxlength="120" placeholder="例如：推理性能测试" required></label><button id="enqueue">加入队列</button></form><p id="grant-hint"></p></section>
<section><h2>当前队列与近期任务</h2><div class="scroll"><table><thead><tr><th>主机</th><th>排队 / 状态</th><th>工号 / 姓名</th><th>任务 / 容器</th><th>登记时间</th><th>操作</th></tr></thead><tbody id="tasks"></tbody></table></div></section>
<section id="admin" class="hidden"><h2>管理员：容器授权</h2><p>一个容器同时只授权给一个个人账号。授权该账号登记使用并结束其中的 NPU 任务；选择“停整个容器”会影响容器内全部进程。请确认容器归属，避免把共用容器当作个人容器。</p>
<form id="grant-form" class="row"><label>个人账号<select id="grant-account" required></select></label><label>主机<select id="grant-node" required></select></label><label>容器<select id="grant-container" required></select></label><button>确认授权</button></form><div class="scroll"><table><thead><tr><th>主机</th><th>容器</th><th>授权账号</th><th>操作</th></tr></thead><tbody id="grants"></tbody></table></div><p><a href="/a5-control/guard" target="_blank">管理自动拦截与白名单</a>：个人注册不会解除原有自动拦截规则。</p></section>
<p id="message" role="status"></p><p><small>取消等待中的预约不会操作进程。Stop 仅对确认页列出的容器实例或 NPU 进程执行；如果进程自动重启，预约会保留并提示检查。队列无法阻止用户绕过网页自行启动服务。</small></p>
</main><dialog id="confirm-dialog"><h2 id="confirm-title"></h2><p id="confirm-description"></p><fieldset id="stop-modes"><legend>停止方式</legend><label><input type="radio" name="mode" value="process" checked>仅停止 NPU 进程，保留容器</label><label><input type="radio" name="mode" value="container">停止整个容器，影响其中全部任务</label></fieldset><pre id="confirm-target"></pre><p id="confirm-warning" class="error"></p><div class="row"><button id="confirm-button" class="danger">确认</button><button id="dismiss" class="secondary">返回</button></div></dialog>
<script src="/a5-queue/queue.js" defer></script></body></html>'''

QUEUE_JS = '''"use strict";
const $=id=>document.getElementById(id), message=$('message');
let state=null, adminData=null, direct=null, operation=null, token=null, busy=false, polling=false, previewVersion=0;
function element(tag,value){const el=document.createElement(tag);el.textContent=value;return el;}
function report(text,error=false){message.textContent=text;message.className=error?'error':'good';}
async function api(name,data){
 const response=await fetch('/a5-queue/api/'+name,{method:data===undefined?'GET':'POST',credentials:'same-origin',cache:'no-store',headers:data===undefined?{}:{'Content-Type':'application/json','X-A5-Queue':'queue-v1'},body:data===undefined?undefined:JSON.stringify(data)});
 const body=await response.json();if(!response.ok)throw new Error(body.message||'操作失败');return body;
}
function options(select,items){const previous=select.value;select.replaceChildren();for(const item of items)select.add(new Option(item.text,item.value));if(items.some(x=>x.value===previous))select.value=previous;}
function statusText(row){return ({queued:row.position===1?'排队第 1 位':'排队第 '+row.position+' 位',active:'使用中',cancelling:'正在结束',cancelled:'已取消',completed:'已完成'})[row.state]||row.state;}
async function refresh(){
 if(polling)return;polling=true;
 try{
  state=await api('state');const user=state.user, registered=user&&user.registered;
  $('account-status').textContent=user?'已登录：'+(user.employee||'')+' '+user.name+(user.admin?'（管理员）':'（个人账号）'):'未登录：可以查看队列，操作需登录';
  $('admin-login').textContent=user&&user.admin?'管理员已登录':'管理员登录';
  $('account-form').classList.toggle('hidden',!!registered);$('account-help').classList.toggle('hidden',!!registered);$('logout').classList.toggle('hidden',!registered);
  options($('grant'),state.grants.map(g=>({text:g.node+' / '+g.container,value:g.node+'|'+g.container_id})));
  $('enqueue').disabled=!registered||!state.grants.length;
  $('grant-hint').textContent=!registered?'先注册或登录个人账号。':!state.grants.length?'暂未获得容器授权，请联系管理员在本页授权。':'仅显示管理员授权给你的容器。';
  const rows=$('tasks');rows.replaceChildren();
  for(const row of state.tasks){
   const tr=document.createElement('tr');tr.append(element('td',row.node),element('td',statusText(row)),element('td',row.employee+' / '+row.name));
   const detail=element('td',row.title);detail.append(document.createElement('br'),element('small',row.container));if(row.message)detail.append(document.createElement('br'),element('small',row.message));tr.append(detail,element('td',new Date(row.created*1000).toLocaleString()));
   const actions=document.createElement('td');
   function button(title,action,danger){const b=element('button',title);if(danger)b.className='danger';b.onclick=()=>show({job:row.id,action},row);actions.append(b);}
   if(user&&(row.mine||user.admin)){if(row.can_claim)button('开始使用','claim',false);if(row.state==='queued')button('取消预约','cancel',false);if(row.state==='active'){button('完成 / 释放','finish',false);button('Stop','cancel',true);}}
   tr.append(actions);rows.append(tr);
  }
  if(!state.tasks.length){const tr=document.createElement('tr'),td=element('td','暂无预约');td.colSpan=6;tr.append(td);rows.append(tr);}
  $('admin').classList.toggle('hidden',!(user&&user.admin));
  const params=new URLSearchParams(location.search);
  if(params.has('node')&&params.has('target')&&user){
   try{direct=await api('control-context?'+params.toString());$('direct').classList.remove('hidden');$('direct-info').textContent=params.get('node')+' / '+params.get('target').split('@')[0];}
   catch(e){$('direct').classList.add('hidden');report(e.message,true);}
  }
 }catch(e){report(e.message,true);}finally{polling=false;}
}
$('account-form').onsubmit=async e=>{e.preventDefault();try{const mode=e.submitter.name;const data=await api(mode,{employee:$('employee').value,name:$('name').value,password:$('password').value});$('password').value='';report(data.message);await refresh();if(state.user&&state.user.admin)await refreshAdmin();}catch(e){report(e.message,true);}};
$('logout').onclick=async()=>{try{await api('logout',{});await refresh();}catch(e){report(e.message,true);}};
$('enqueue-form').onsubmit=async e=>{e.preventDefault();const [node,container_id]=$('grant').value.split('|');try{report((await api('enqueue',{node,container_id,title:$('title').value})).message);$('title').value='';await refresh();}catch(e){report(e.message,true);}};
async function loadPreview(){
 const version=++previewVersion, request={...operation};
 token=null;$('confirm-button').disabled=true;$('confirm-warning').textContent='正在核实目标…';
 try{
  const preview=await api('preview',request);if(version!==previewVersion)return;token=preview.token;const s=preview.snapshot;
  $('stop-modes').classList.toggle('hidden',!['cancel','direct'].includes(operation.action)||preview.queued);
  $('confirm-target').textContent=s.node+'\\n'+(s.container||s.container_id)+'\\n'+(s.container_changed?'原容器实例已结束，不会操作新实例。\\n':'')+'NPU 进程：'+(s.processes.length?s.processes.map(p=>p.pid+' ('+p.name+')').join(', '):'无');
  $('confirm-warning').textContent=operation.action==='claim'?'只登记开始使用，不会启动服务。已有 NPU 进程将关联到本次任务；使用期间请勿在此容器混跑其他任务。':operation.action==='finish'?(s.processes.length?'仍有 NPU 进程，请先停止任务，或使用 Stop。':'确认完成预约并让下一位开始。'):preview.queued?'只取消排队，不操作容器或进程。':operation.mode==='container'?'将停止整个容器，容器内所有进程都会中断。':'将结束上面列出的 NPU 进程，必要时强制终止；容器会保留。';
  $('confirm-button').disabled=operation.action==='finish'&&s.processes.length>0;
 }catch(e){if(version===previewVersion)$('confirm-warning').textContent=e.message;}
}
async function show(request,row){
 if(busy)return;operation={...request,mode:'process'};document.querySelector('input[name=mode][value=process]').checked=true;
 $('confirm-title').textContent=request.action==='claim'?'确认开始使用？':request.action==='finish'?'确认完成预约？':'确认 Stop / 取消任务？';
 $('confirm-description').textContent=row?row.title+' / '+row.container:'请核对主机、容器和进程';
 $('confirm-dialog').showModal();await loadPreview();
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
async function refreshAdmin(){
 if(!state||!state.user||!state.user.admin)return;
 adminData=await api('admin');
 options($('grant-account'),adminData.accounts.map(a=>({text:a.employee+' / '+a.name,value:a.id})));
 options($('grant-node'),adminData.hosts.map(h=>({text:h.node,value:h.node})));containerOptions();
 $('grants').replaceChildren();
 for(const g of adminData.grants){const tr=document.createElement('tr'),account=adminData.accounts.find(a=>a.id===g.account);tr.append(element('td',g.node),element('td',g.container),element('td',account?account.employee+' / '+account.name:g.account));const td=document.createElement('td'),b=element('button','撤销授权');b.className='secondary';b.onclick=async()=>{if(!confirm('确认撤销 '+g.container+' 的个人控制授权？'))return;try{report((await api('grant',{node:g.node,container_id:g.container_id,account:null,confirmed:true})).message);await refresh();await refreshAdmin();}catch(e){report(e.message,true);}};td.append(b);tr.append(td);$('grants').append(tr);}
}
function containerOptions(){const host=adminData.hosts.find(h=>h.node===$('grant-node').value);options($('grant-container'),(host?host.containers:[]).map(c=>({text:c.name+' ['+c.state+']',value:c.id})));}
$('grant-node').onchange=containerOptions;
$('grant-form').onsubmit=async e=>{e.preventDefault();if(!confirm('确认把此容器的个人任务控制权限授予选中的账号？请确认这是该账号专用的作业容器。'))return;try{report((await api('grant',{node:$('grant-node').value,container_id:$('grant-container').value,account:$('grant-account').value,confirmed:true})).message);await refresh();await refreshAdmin();}catch(e){report(e.message,true);}};
refresh().then(refreshAdmin).catch(e=>report(e.message,true));setInterval(()=>{if(!busy)refresh();},3000);setInterval(()=>{if(!busy)refreshAdmin().catch(()=>{});},15000);
'''
