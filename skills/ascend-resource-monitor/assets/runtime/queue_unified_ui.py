"""Queue controls mounted in the Grafana homepage, inside an isolated shadow DOM."""
import json

HOME_FRAGMENT = '''<style>
:host{display:block;height:100%;overflow:auto;color:#d8d9da;font:14px system-ui,sans-serif}*{box-sizing:border-box}.row{display:flex;gap:10px;align-items:end;flex-wrap:wrap}section{padding:10px 4px;border-bottom:1px solid #34383f}h3{font-size:15px;margin:0 0 10px}label{display:grid;gap:5px}input,select{background:#111217;color:#eee;border:1px solid #555b66;border-radius:4px;padding:7px;font:inherit;min-width:110px}input[type=password]{max-width:200px}button{font:inherit;border:0;border-radius:4px;padding:8px 12px;background:#316bd6;color:#fff;cursor:pointer}.secondary{background:#343f51}button:disabled{opacity:.45;cursor:not-allowed}.hidden{display:none!important}p{margin:8px 0;line-height:1.5;color:#adb9cb}.good{color:#7ed9a9}.error{color:#ffabb7}.scroll{overflow:auto}table{width:100%;border-collapse:collapse}th,td{text-align:left;border-bottom:1px solid #34383f;padding:9px 7px;vertical-align:top;white-space:nowrap}th{color:#adb9cb;font-weight:500}small{color:#a4b1c4}td button{padding:6px 9px;margin:0 5px 4px 0}#message:empty{display:none}
</style>
<section><h3>个人账号与桌面通知</h3><p id="account-status"></p>
<form id="account-form" class="row"><label>工号<input id="employee" autocomplete="username" pattern="[A-Za-z][0-9]{8}" required></label><label>姓名<input id="name" autocomplete="name"></label><label>个人密码<input id="password" type="password" autocomplete="current-password" minlength="10" maxlength="128" required></label><button name="login">登录</button><button name="register" class="secondary">首次注册</button></form>
<div class="row"><button id="logout" class="secondary hidden" type="button">退出个人账号</button><button id="enable-notifications" type="button" disabled>开启并验证桌面通知</button></div><p id="notification-status" role="status">登录后开启并验证桌面通知，才能加入队列。</p></section>
<section><h3>提交预约</h3><form id="join-form" class="row"><label>机器<select id="machine" required></select></label><label>卡数（合计）<select id="cards" required></select></label><label>预约类型<select id="kind"><option value="normal">普通任务</option><option value="night">夜间长任务</option></select></label><label>预计时长（小时）<input id="hours" type="number" min="0" max="2" step="1" value="1" required></label><label>分钟<select id="minutes" aria-label="预计时长（分钟）"></select></label><label id="start-label" class="hidden">预约开始时间<input id="start" type="datetime-local" step="60"></label><button id="join" disabled>加入队列</button></form><p id="resource-hint"></p><p id="reservation-window"></p><p>普通任务最长 2 小时，时长按 5 分钟一档选择；每台 1–4 卡位于同一四卡组。分配后 10 分钟内点击“开始使用”，超时向后顺延一位。用完后自行停止服务，再结束登记。</p></section>
<section><div class="scroll"><table><thead><tr><th>状态</th><th>工号 / 姓名</th><th>资源需求</th><th>机器与卡号</th><th>申请时间</th><th>开始时间</th><th>预计占用</th><th>预计结束时间</th><th>操作</th></tr></thead><tbody id="tasks"></tbody></table></div></section><p id="message" role="status"></p>'''

HOME_JS = r'''"use strict";
(()=>{
 if(window.__a5QueueHomeLoaded)return;window.__a5QueueHomeLoaded=true;
 const fragment=__FRAGMENT__;
 let currentHost=null,dispose=null;
 function mount(host){
  const root=host.attachShadow({mode:'open'});root.innerHTML=fragment;
  const $=id=>root.getElementById(id);
  for(let minute=0;minute<60;minute+=5){const option=document.createElement('option');option.value=String(minute);option.textContent=minute+' 分钟';$('minutes').append(option);}
  let machineNodes='',state=null,polling=false,stopped=false,submitting=false,verifiedUser=null,verifying=false,version=0,lastRefreshAt=0;
  let failure='';const delivered=new Map(),openNotices=new Set();
  const registered=()=>!!(state&&state.user&&state.user.registered);
  const supported=()=>window.isSecureContext&&('Notification' in window);
  const permission=()=>supported()?Notification.permission:'unsupported';
  const ready=()=>registered()&&verifiedUser===state.user.id&&permission()==='granted';
  function element(tag,text){const el=document.createElement(tag);el.textContent=text;return el;}
  function report(text,error=false){$('message').textContent=text;$('message').className=error?'error':'good';}
  function gate(){
   if(permission()!=='granted')verifiedUser=null;
   $('join').disabled=!ready()||submitting;
   $('enable-notifications').disabled=!registered()||verifying||!supported();
   $('enable-notifications').textContent=verifying?'正在验证通知…':ready()?'重新验证桌面通知':'开启并验证桌面通知';
   const note=$('notification-status');note.className=ready()?'good':'error';
   note.textContent=!registered()?'登录后开启并验证桌面通知，才能加入队列。':!supported()?'当前浏览器或访问地址不支持桌面通知，请使用本机隧道或受信任的 HTTPS。':verifying?'正在发送测试桌面通知，请稍候。':ready()?'桌面通知已验证，可以预约。请保持主页打开并登录。':failure|| (permission()==='denied'?'通知已被阻止，请在浏览器站点设置中允许通知，再重新验证。':'请先点击“开启并验证桌面通知”；验证通过后才可加入队列。');
  }
  async function api(name,data){
   const response=await fetch('/a5-queue/api/'+name,{method:data===undefined?'GET':'POST',credentials:'same-origin',cache:'no-store',headers:data===undefined?{}:{'Content-Type':'application/json','X-A5-Queue':'queue-v1'},body:data===undefined?undefined:JSON.stringify(data)});
   const body=await response.json();if(!response.ok)throw new Error(body.message||'操作失败');return body;
  }
  function invalidate(text){verifiedUser=null;failure=text;gate();}
  function closeNotices(){for(const n of openNotices)n.close();openNotices.clear();}
  $('enable-notifications').onclick=async()=>{
   if(!registered()||verifying||!supported())return;
   const user=state.user.id,check=++version;verifying=true;verifiedUser=null;failure='';gate();
   try{
    if(Notification.permission!=='granted')await Notification.requestPermission();
    if(Notification.permission!=='granted')throw new Error('通知未开启，请在浏览器站点设置中允许通知。');
    await new Promise((resolve,reject)=>{
     const n=new Notification('A5 排队通知测试',{body:'桌面通知已显示，可返回主页加入队列。',tag:'a5-queue-notification-check'});openNotices.add(n);
     const timer=setTimeout(()=>{n.close();openNotices.delete(n);reject(new Error('未确认测试通知显示成功，请检查浏览器和系统通知设置后重试。'));},8000);
     n.onshow=()=>{clearTimeout(timer);setTimeout(()=>{n.close();openNotices.delete(n);},2000);resolve();};
     n.onerror=()=>{clearTimeout(timer);n.close();openNotices.delete(n);reject(new Error('测试通知发送失败，请开启浏览器和系统通知后重试。'));};
    });
    if(!stopped&&check===version&&registered()&&state.user.id===user)verifiedUser=user;
   }catch(e){if(check===version)failure=e.message;}
   finally{if(check===version){verifying=false;gate();if(state)notify();}}
  };
  function date(value){return value?new Date(value*1000).toLocaleString():'—';}
  function resourceFields(){
   const nodes=(state&&state.nodes)||[],signature=JSON.stringify(nodes),machine=$('machine'),cards=$('cards');
   if(signature!==machineNodes){const selected=machine.value;machine.replaceChildren();const auto=element('option','单机任意机器');auto.value='auto';machine.append(auto);for(const node of nodes){const option=element('option','单机 '+node.replace(/^A5-/,'') );option.value=node;machine.append(option);}const dual=element('option','双机');dual.value='dual';machine.append(dual);if([...machine.options].some(o=>o.value===selected))machine.value=selected;machineNodes=signature;}
   const dual=machine.value==='dual',sizes=dual?[2,4,8,16]:[1,2,3,4,5,6,7,8],selected=Number(cards.value);
   if([...cards.options].map(o=>Number(o.value)).join()!==sizes.join()){cards.replaceChildren();for(const count of sizes){const option=element('option',count+' 卡');option.value=String(count);cards.append(option);}if(sizes.includes(selected))cards.value=String(selected);}
   $('resource-hint').textContent=dual?'双机合计 '+cards.value+' 卡，每台 '+Number(cards.value)/2+' 卡；具体卡号按队列可用情况分配。':machine.value==='auto'?'自动选择可用单机，按队列分配机器和卡号。':'使用 '+machine.value+'，具体卡号按队列可用情况分配。';
  }
  function duration(seconds){const minutes=Math.round(seconds/60),hours=Math.floor(minutes/60),rest=minutes%60;return (hours?hours+' 小时':'')+(hours&&rest?' ':'')+(rest?rest+' 分钟':'')||'0 分钟';}
  function localInput(value){const d=new Date(value*1000);return d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0')+'T'+String(d.getHours()).padStart(2,'0')+':'+String(d.getMinutes()).padStart(2,'0');}
  function timeLimits(){
   const night=$('kind').value==='night',seconds=Number($('hours').value)*3600+Number($('minutes').value)*60,window=state&&state.reservation_window;
   $('start-label').classList.toggle('hidden',!night);$('start').required=night;$('hours').max=night?'33':'2';
   const start=night?new Date($('start').value).getTime()/1000:state&&state.server_time;
   let error=!Number.isInteger(seconds)||seconds<300?'预计时长至少 5 分钟':!night&&seconds>7200?'普通任务最长 2 小时':'';
   if(!error&&window&&Number.isFinite(start)&&start+seconds>window.end)error='预计结束时间不能超过次日上午 9 点';
   $('minutes').setCustomValidity(error);
   if(window){$('start').min=localInput(Math.ceil(window.start/60)*60);$('start').max=localInput(Math.floor((window.end-seconds)/60)*60);$('reservation-window').textContent='本次可预约：现在至 '+date(window.end)+'。开始和预计结束时间均须在此范围内。';}
  }
  function resource(allocation){return Object.entries(allocation||{}).map(([node,cards])=>node+'：'+cards.join('、')).join('；')||'待分配';}
  function notify(){
   if(!ready())return;
   const live=new Set(state.tasks.map(row=>row.id));for(const id of delivered.keys())if(!live.has(id))delivered.delete(id);
   for(const row of state.tasks){
    if(!row.mine)continue;
    let event='',title='',body='';
    const seen=delivered.get(row.id)||new Set();
    if(row.missed_claims&&!seen.has('deferred:'+row.missed_claims)){event='deferred:'+row.missed_claims;title='A5 预约已顺延一位';body='超过 10 分钟未开始，这次分配已释放，预约向后顺延一位。请查看新的排队状态。';}
    else if(row.state==='ready'&&row.can_claim){const remaining=Math.max(0,row.claim_before-state.server_time),elapsed=Math.max(0,state.server_time-(row.ready_since||state.server_time));const phase=remaining<=60?'last-minute':([0,120,240,360,480,540].filter(t=>t<=elapsed).pop()||0);event='ready:'+(row.offer_id||row.ready_since||row.claim_before)+':'+phase;title=phase===0?'A5 资源已分配':'A5 提醒：请及时开始使用';body=resource(row.allocations)+'，剩余 '+Math.ceil(remaining/60)+' 分钟。请点击“开始使用”，超时预约将向后顺延一位。';}
    else if(row.state==='active'&&row.activated&&row.requested_seconds){const remaining=row.activated+row.requested_seconds-state.server_time;const warning=Math.min(row.not_before?3600:900,Math.max(60,row.requested_seconds/4));if(remaining<=0){event='overdue';title='A5 预约已到预计结束时间';body='请自行停止服务，并在主页结束登记。';}else if(remaining<=warning){event='ending';title='A5 预约即将到期';body='预计剩余 '+Math.ceil(remaining/60)+' 分钟，请准备释放资源。';}}
    if(!event||(delivered.get(row.id)||new Set()).has(event))continue;
    try{const n=new Notification(title,{body,tag:'a5-queue:'+row.id+':'+event});openNotices.add(n);const seen=delivered.get(row.id)||new Set();seen.add(event);delivered.set(row.id,seen);n.onerror=()=>{seen.delete(event);invalidate('桌面通知发送失败，请重新验证通知后再提交预约。');};n.onclose=()=>openNotices.delete(n);n.onclick=()=>{window.focus();host.scrollIntoView({block:'center'});n.close();};}catch(e){invalidate('桌面通知发送失败，请重新开启并验证。');break;}
   }
  }
  function render(){
   $('account-status').textContent=state.user?'已登录：'+(state.user.employee||'')+' '+state.user.name:'未登录';
   $('account-form').classList.toggle('hidden',registered());$('logout').classList.toggle('hidden',!registered());gate();notify();resourceFields();timeLimits();
   const body=$('tasks');body.replaceChildren();
   for(const row of state.tasks){
    if(!['queued','ready','active'].includes(row.state))continue;
    const tr=document.createElement('tr');tr.dataset.job=row.id;
    tr.append(element('td',({queued:'等待中 · 第 '+row.position+' 位',ready:'已分配，待开始',active:'使用中'})[row.state]),element('td',row.employee+' / '+row.name),element('td',row.dual?'双机共 '+(row.total_cards||row.cards*2)+' 卡（各 '+row.cards+' 卡）':(row.fixed_node||'单机')+' · '+row.cards+' 卡'));
    const allocation=element('td',resource(row.allocations));if(row.claim_before&&row.state==='ready'){const remaining=Math.max(0,Math.ceil(row.claim_before-state.server_time));allocation.append(document.createElement('br'),element('small','请在 '+Math.floor(remaining/60)+'分'+remaining%60+'秒内开始'));}if(row.missed_claims)allocation.append(document.createElement('br'),element('small','超时顺延 '+row.missed_claims+' 次'));tr.append(allocation);
    tr.append(element('td',date(row.created)));
    const start=row.activated?date(row.activated):row.window_expired?'时段已过，请取消后重新预约':row.state==='ready'?'可立即开始':row.estimated_start?'预计 '+date(row.estimated_start):row.finish_before?'截止前暂无可用时段':row.not_before?'不早于 '+date(row.not_before):'预计时间待确认';tr.append(element('td',start),element('td',duration(row.requested_seconds)),element('td',row.activated&&row.requested_seconds?date(row.activated+row.requested_seconds):row.estimated_start?date(row.estimated_start+row.requested_seconds):'—'));
    const actions=element('td','');function button(label,action){const b=element('button',label);b.onclick=async()=>{if(action!=='claim'&&!window.confirm(action==='finish'?'确认已自行停止服务并释放这些卡，结束登记？':label+'？'))return;b.disabled=true;try{report((await api('unified-action',{job:row.id,action,offer_id:row.offer_id,confirmed:true})).message);await refresh();}catch(e){report(e.message,true);}finally{b.disabled=false;}};actions.append(b);}
    if(state.user&&(row.mine||state.user.admin)){if(row.state==='ready')button('开始使用','claim');if(row.state==='queued'||row.state==='ready')button('取消预约','cancel');if(row.state==='active')button('结束登记','finish');}tr.append(actions);body.append(tr);
   }
   if(!body.children.length){const tr=element('tr',''),td=element('td','暂无排队或使用中的预约。');td.colSpan=9;tr.append(td);body.append(tr);}
  }
  async function refresh(){
   if(polling||stopped)return;polling=true;lastRefreshAt=Date.now();
   try{const next=await api('state');if(stopped)return;if((state&&state.user&&state.user.id)!==(next.user&&next.user.id)){version++;verifiedUser=null;verifying=false;failure='';delivered.clear();closeNotices();}state=next;render();}catch(e){if(!stopped)report(e.message,true);}finally{polling=false;}
  }
  $('account-form').onsubmit=async e=>{e.preventDefault();try{const result=await api(e.submitter.name,{employee:$('employee').value,name:$('name').value,password:$('password').value});$('password').value='';report(result.message);await refresh();}catch(err){report(err.message,true);}};
  $('logout').onclick=async()=>{try{await api('logout',{});verifiedUser=null;closeNotices();await refresh();}catch(err){report(err.message,true);}};
  $('machine').onchange=resourceFields;$('cards').onchange=resourceFields;
  $('kind').onchange=timeLimits;$('hours').oninput=timeLimits;$('minutes').onchange=timeLimits;$('start').oninput=timeLimits;timeLimits();
  $('join-form').onsubmit=async e=>{e.preventDefault();timeLimits();if(!$('join-form').reportValidity())return;if(!ready()){gate();report('请先开启桌面通知并通过测试，才能加入队列。',true);return;}const night=$('kind').value==='night';submitting=true;gate();try{report((await api('enqueue',{machine:$('machine').value,cards:Number($('cards').value),night,not_before:night?new Date($('start').value).getTime()/1000:0,requested_seconds:Number($('hours').value)*3600+Number($('minutes').value)*60,notifications_ready:true})).message);await refresh();}catch(err){report(err.message,true);}finally{submitting=false;gate();}};
  function refreshDelay(){
   const control=document.querySelector('button[aria-label^="Choose refresh time interval"]');
   if(control&&control.textContent.trim()==='Off')return 0;
   return ({'1s':1000,'5s':5000,'10s':10000,'30s':30000})[new URLSearchParams(window.location.search).get('refresh')]||1000;
  }
  refresh();const interval=setInterval(()=>{const delay=refreshDelay();if(delay&&Date.now()-lastRefreshAt>=delay)refresh();},1000);
  const focus=()=>{gate();refresh();};window.addEventListener('focus',focus);
  return ()=>{stopped=true;version++;clearInterval(interval);window.removeEventListener('focus',focus);closeNotices();};
 }
 function attach(){const marker=document.querySelector('a[href="/a5-queue/home"]');const host=marker?marker.parentElement:document.getElementById('a5-queue-home');if(host===currentHost)return;if(dispose)dispose();dispose=null;currentHost=host;if(host&&!host.shadowRoot){host.id='a5-queue-home';dispose=mount(host);}}
 new MutationObserver(attach).observe(document.documentElement,{childList:true,subtree:true});attach();
})();
'''

HOME_JS = HOME_JS.replace('__FRAGMENT__', json.dumps(HOME_FRAGMENT, ensure_ascii=False))
