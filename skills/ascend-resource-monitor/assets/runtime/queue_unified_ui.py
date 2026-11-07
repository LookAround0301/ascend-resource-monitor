"""Queue controls mounted in the Grafana homepage, inside an isolated shadow DOM."""
import json

HOME_FRAGMENT = '''<style>
:host{display:block;height:100%;overflow:auto;color:#d8d9da;font:14px system-ui,sans-serif}*{box-sizing:border-box}.row{display:flex;gap:10px;align-items:end;flex-wrap:wrap}section{padding:10px 4px;border-bottom:1px solid #34383f}h3{font-size:15px;margin:0 0 10px}label{display:grid;gap:5px}input,select{background:#111217;color:#eee;border:1px solid #555b66;border-radius:4px;padding:7px;font:inherit;min-width:110px}input[type=password]{max-width:200px}button{font:inherit;border:0;border-radius:4px;padding:8px 12px;background:#316bd6;color:#fff;cursor:pointer}.secondary{background:#343f51}button:disabled{opacity:.45;cursor:not-allowed}.hidden{display:none!important}p{margin:8px 0;line-height:1.5;color:#adb9cb}.good{color:#7ed9a9}.error{color:#ff7886}.warning{color:#ffb457}.scroll{overflow:auto}table{width:100%;border-collapse:collapse}th,td{text-align:left;border-bottom:1px solid #34383f;padding:9px 7px;vertical-align:top;white-space:nowrap}th{color:#adb9cb;font-weight:500}small{color:#a4b1c4}.allowed-users{white-space:normal;max-width:320px;overflow-wrap:anywhere;line-height:1.5;margin:6px 0;color:#c6d2e0}td button{padding:6px 9px;margin:0 5px 4px 0}#message:empty{display:none}
.allowed-editor{white-space:normal;width:210px;margin:7px 0}.allowed-chips{display:flex;flex-wrap:wrap;gap:4px;margin-bottom:5px}.allowed-chips .allowed-chip{margin:0;padding:4px 7px;background:#343f51;font-size:12px}.allowed-input{width:100%;min-width:0;padding:6px}.allowed-results{display:grid;gap:3px;margin-top:4px}.allowed-results button{margin:0;text-align:left;background:#343f51;font-size:12px}.allowed-hint{display:block;margin-top:4px;white-space:normal;font-size:12px;color:#a4b1c4}
.standby-status{white-space:normal;min-width:200px;max-width:220px;line-height:1.45;overflow-wrap:anywhere}
.bottleneck{color:#f2d66d}
.avoid-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(66px,1fr));gap:5px;margin:8px 0}.avoid-grid button{padding:6px 3px;background:#313b4a;font-size:12px}.avoid-grid button.selected{background:#a65b32;color:#fff}.avoid-panel{white-space:normal;min-width:300px;max-width:780px}.meal-start{color:#ffbb65;font-weight:600}
.queue-group td{padding:12px 8px;background:#202832;color:#d4e0ed;font-weight:600}.queue-group.night td{background:#242b42;color:#d8ddff}.queue-group small{margin-left:12px;font-weight:400}.night-task{background:rgba(95,105,176,.08)}.night-badge{display:inline-block;margin-right:7px;padding:2px 6px;border-radius:4px;background:#434e80;color:#e7eaff;font-size:12px;line-height:1.3}.queue-empty td{color:#a4b1c4}
.card-grid{display:grid;gap:8px}.card-node{display:grid;grid-template-columns:85px minmax(0,1fr);gap:10px;align-items:center}.card-list{display:grid;grid-template-columns:repeat(8,minmax(0,1fr));gap:8px}.card-tile{border:1px solid #466174;border-radius:8px;padding:9px 6px;background:#1b3038;min-width:0;text-align:center;white-space:normal;overflow-wrap:anywhere}.card-tile.occupied{background:#433332;border-color:#86594e}.card-tile.buffer{background:#423b28;border-color:#8d7949}.card-tile strong,.card-tile span{display:block}.card-tile span{font-size:12px;margin-top:4px;color:#c1cbd5}@media(max-width:700px){.card-node{grid-template-columns:1fr}.card-list{grid-template-columns:repeat(4,minmax(0,1fr))}}
.card-tile.reserved-user{background:#193b32;border-color:#3f866a}.card-tile.reserved-empty{background:#1d324b;border-color:#4d729a}.card-tile.reserved-other,.card-tile.reserved-mixed{background:#492e32;border-color:#aa5960}.card-tile.unreserved-used{background:#483923;border-color:#ac8748}.card-tile.unreserved-empty{background:#26332d;border-color:#617866}.card-tile.uncertain{background:#30323a;border-color:#757b89}.card-tile .card-reservation{color:#e0e6ef}.card-tile .card-observation{color:#f0f2f6}.card-tile .card-forecast{color:#b6c7df}
</style>
<section><h3>个人账号与桌面通知</h3><p id="account-status"></p>
<form id="account-form" class="row"><label>工号<input id="employee" autocomplete="username" pattern="[A-Za-z][0-9]{8}" required></label><label>姓名<input id="name" autocomplete="name" maxlength="60" required></label><button type="submit">登录</button></form>
<div class="row"><button id="logout" class="secondary hidden" type="button">退出个人账号</button><button id="enable-notifications" type="button" disabled>开启并验证桌面通知</button></div><p id="notification-status" role="status">登录后开启并验证桌面通知，才能加入队列。</p></section>
<section><h3>卡预约占用情况</h3><p id="card-capacity-hint">预约与 NPU 进程状态分开显示。</p><p>使用情况来自 NPU 进程采样；“未检出进程”不代表容器或机器已释放。预约人包含登记的共同使用人，采集或归属不明时显示待核实。</p><div id="card-grid" class="card-grid"></div></section>
<section><h3>提交预约</h3><form id="join-form" class="row"><label>机器<select id="machine" required></select></label><label>卡数（合计）<select id="cards" required></select></label><label>预约类型<select id="kind"><option value="normal">普通任务</option><option value="night">夜间长任务</option></select></label><label id="duration-hour-label">预计时长（小时）<select id="hours" required></select></label><label id="duration-minute-label">分钟<select id="minutes" aria-label="预计时长（分钟）"></select></label><label id="start-date-label" class="hidden">预约开始日期<select id="start-date"><option value="">选择日期</option></select></label><label id="start-hour-label" class="hidden">开始时间（小时）<select id="start-hour"><option value="">选择小时</option></select></label><label id="start-minute-label" class="hidden">开始时间（分钟）<select id="start-minute"></select></label><button id="join" disabled>加入队列</button><span id="message" role="status"></span></form><div id="avoid-area" class="hidden"><p>默认避开午休 12:00–14:00 和晚饭 17:30–19:30；饭点可工作的用户可以取消选择。</p><button id="avoid-toggle" type="button" class="secondary">选择避开时段</button><div id="avoid-form" class="avoid-panel hidden"></div></div><p id="resource-hint"></p><p id="reservation-window"></p><p id="queue-rules">普通任务最长 2 小时，分配后最多 10 分钟内点击“开始使用”，最迟须在凌晨 2 点前开始。夜间任务按排程自动开始登记，由本人设置的 agent 启动；夜间交接不检查进程，其他情况可在卡连续空闲后自动结束。每日 9 点清空普通任务登记并取消未开始的夜间预约；已开始的夜间任务保留原时间，用完后自行结束登记。实际任务不会被停止。</p></section>
<div id="queue-alerts" class="hidden" role="status"></div><section><div class="scroll"><table><thead><tr><th>状态</th><th>工号 / 姓名</th><th>资源需求</th><th>机器与卡号</th><th>申请时间</th><th>开始时间</th><th>占用时长</th><th>结束时间</th><th>操作</th></tr></thead><tbody id="tasks"></tbody></table></div></section>'''

HOME_JS = r'''"use strict";
(()=>{
 if(window.__a5QueueHomeLoaded)return;window.__a5QueueHomeLoaded=true;
 const fragment=__FRAGMENT__;
 let currentHost=null,dispose=null;
 function mount(host){
  const root=host.attachShadow({mode:'open'});root.innerHTML=fragment;
  const $=id=>root.getElementById(id);
  let machineNodes='',state=null,polling=false,stopped=false,submitting=false,verifiedUser=null,verifying=false,version=0,lastRefreshAt=0,composingAllowed=false,autoCheckUser=null;
  let failure='';const delivered=new Map(),openNotices=new Set(),alertSeen=new Set(),overdueNotices=new Map(),bottleneckStates=new Map(),allowedEditors=new Map(),avoidEditors=new Map();
  const initialAvoid=new Set([24,25,26,27,35,36,37,38]);
  function clearAllowedEditors(){for(const editor of allowedEditors.values())clearTimeout(editor.timer);allowedEditors.clear();}
  function expandPanel(){
   const item=host.closest('.react-grid-item'),grid=item&&item.parentElement,panel=host.closest('section[data-testid]');
   if(!item||!grid||!panel||!item.contains(panel))return {update:()=>{},dispose:()=>{}};
   const originals=new Map();let baseItem=parseFloat(item.style.height)||0,baseGrid=parseFloat(grid.style.height)||0,lastItem=0,lastGrid=0,frame=0;
   function set(el,prop,value){let saved=originals.get(el);if(!saved){saved=new Map();originals.set(el,saved);}if(!saved.has(prop))saved.set(prop,[el.style.getPropertyValue(prop),el.style.getPropertyPriority(prop)]);if(el.style.getPropertyValue(prop)!==value||el.style.getPropertyPriority(prop)!=='important')el.style.setProperty(prop,value,'important');}
   set(host,'height','auto');for(let el=host;el&&el!==panel;el=el.parentElement)set(el,'overflow','visible');
   function layout(){
    frame=0;if(stopped||!item.isConnected)return;
    const height=parseFloat(item.style.height)||0,gridHeight=parseFloat(grid.style.height)||0;
    if(height&&height!==lastItem)baseItem=height;if(gridHeight&&gridHeight!==lastGrid)baseGrid=gridHeight;
    const content=host.getBoundingClientRect(),container=panel.getBoundingClientRect(),needed=Math.ceil(content.height+content.top-container.top+12);
    const expanded=Math.max(baseItem,needed),extra=Math.max(0,expanded-baseItem);lastItem=expanded;lastGrid=baseGrid+extra;
    set(item,'height',expanded+'px');set(panel,'height',expanded+'px');set(grid,'height',lastGrid+'px');
    const y=el=>{const match=(el.style.transform||'').match(/translate\(\s*[-\d.]+px\s*,\s*([-\d.]+)px\s*\)/);return match?Number(match[1]):null;};
    const top=y(item);if(top!==null)for(const sibling of grid.children){if(sibling===item||!sibling.classList.contains('react-grid-item'))continue;const position=y(sibling);if(position!==null&&position>=top+baseItem-1)set(sibling,'translate','0px '+extra+'px');}
   }
   function schedule(){if(!frame)frame=requestAnimationFrame(layout);}
   const observer=typeof ResizeObserver==='function'?new ResizeObserver(schedule):null;if(observer)observer.observe(host);window.addEventListener('resize',schedule);schedule();
   return {update:layout,dispose:()=>{if(observer)observer.disconnect();window.removeEventListener('resize',schedule);if(frame)cancelAnimationFrame(frame);for(const [el,values] of originals)for(const [prop,[value,priority]] of values){if(value)el.style.setProperty(prop,value,priority);else el.style.removeProperty(prop);}}};
  }
  const expansion=expandPanel();
  const registered=()=>!!(state&&state.user&&state.user.registered);
  const supported=()=>window.isSecureContext&&('Notification' in window);
  const permission=()=>supported()?Notification.permission:'unsupported';
  const ready=()=>registered()&&verifiedUser===state.user.id&&permission()==='granted';
  function verificationSaved(user){try{return localStorage.getItem('a5-notification-verified-v1:'+user)==='1';}catch(e){return false;}}
  function saveVerification(user,valid){if(!user)return;try{const key='a5-notification-verified-v1:'+user;if(valid)localStorage.setItem(key,'1');else localStorage.removeItem(key);}catch(e){}}
  function element(tag,text){const el=document.createElement(tag);el.textContent=text;return el;}
  function report(text,error=false){$('message').textContent=text;$('message').className=error?'error':'good';}
  function gate(){
   if(permission()!=='granted'){if(registered())saveVerification(state.user.id,false);verifiedUser=null;autoCheckUser=null;}
   else if(registered()&&!verifying&&!failure&&verificationSaved(state.user.id))verifiedUser=state.user.id;
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
  function invalidate(text){if(registered())saveVerification(state.user.id,false);verifiedUser=null;failure=text;gate();}
  function closeNotices(){for(const n of openNotices)n.close();openNotices.clear();}
  $('enable-notifications').onclick=async()=>{
   if(!registered()||verifying||!supported())return;
   const user=state.user.id,check=++version;verifying=true;verifiedUser=null;saveVerification(user,false);failure='';gate();
   try{
    if(Notification.permission!=='granted')await Notification.requestPermission();
    if(Notification.permission!=='granted')throw new Error('通知未开启，请在浏览器站点设置中允许通知。');
    await new Promise((resolve,reject)=>{
     const n=new Notification('A5 排队通知测试',{body:'桌面通知已显示，可返回主页加入队列。',tag:'a5-queue-notification-check'});openNotices.add(n);
     const timer=setTimeout(()=>{n.close();openNotices.delete(n);reject(new Error('未确认测试通知显示成功，请检查浏览器和系统通知设置后重试。'));},8000);
     n.onshow=()=>{clearTimeout(timer);setTimeout(()=>{n.close();openNotices.delete(n);},2000);resolve();};
     n.onerror=()=>{clearTimeout(timer);n.close();openNotices.delete(n);reject(new Error('测试通知发送失败，请开启浏览器和系统通知后重试。'));};
    });
    if(!stopped&&check===version&&registered()&&state.user.id===user){verifiedUser=user;saveVerification(user,true);}
   }catch(e){if(check===version)failure=e.message;}
   finally{if(check===version){verifying=false;gate();if(state)notify();}}
  };
  function date(value){return value?new Date(value*1000).toLocaleString('zh-CN',{timeZone:(state&&state.reservation_window&&state.reservation_window.timezone)||'Asia/Shanghai'}):'—';}
  function dateMinute(value){return value?new Intl.DateTimeFormat('zh-CN',{timeZone:(state&&state.reservation_window&&state.reservation_window.timezone)||'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).format(new Date(value*1000)):'—';}
  function renderAvoidGrid(container,selected){
   container.replaceChildren();container.append(element('small','按北京时间每天重复；点亮表示不在该时段开始。'));
   const grid=element('div','');grid.className='avoid-grid';
   for(let i=0;i<48;i++){const label=String(Math.floor(i/2)).padStart(2,'0')+':'+(i%2?'30':'00'),b=element('button',label);b.type='button';b.className=selected.has(i)?'selected':'';b.setAttribute('aria-pressed',String(selected.has(i)));b.onclick=()=>{if(selected.has(i))selected.delete(i);else selected.add(i);b.classList.toggle('selected',selected.has(i));b.setAttribute('aria-pressed',String(selected.has(i)));};grid.append(b);}container.append(grid);
  }
  renderAvoidGrid($('avoid-form'),initialAvoid);
  $('avoid-toggle').onclick=()=>{$('avoid-form').classList.toggle('hidden');};
  function mealStart(value){if(!value)return false;const zone=(state&&state.reservation_window&&state.reservation_window.timezone)||'Asia/Shanghai';const parts=new Intl.DateTimeFormat('en-GB',{timeZone:zone,hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).formatToParts(new Date(value*1000));const h=Number(parts.find(p=>p.type==='hour').value),m=Number(parts.find(p=>p.type==='minute').value),minute=h*60+m;return (minute>=720&&minute<840)||(minute>=1050&&minute<1170);}
  function resourceFields(){
   const nodes=(state&&state.nodes)||[],signature=JSON.stringify(nodes),machine=$('machine'),cards=$('cards');
   if(signature!==machineNodes){const selected=machine.value;machine.replaceChildren();const auto=element('option','单机任意机器');auto.value='auto';machine.append(auto);for(const node of nodes){const option=element('option','单机 '+node.replace(/^A5-/,'') );option.value=node;machine.append(option);}const dual=element('option','双机');dual.value='dual';machine.append(dual);if([...machine.options].some(o=>o.value===selected))machine.value=selected;machineNodes=signature;}
   const capacity=state.cards_per_node||8,dual=machine.value==='dual',sizes=dual?[2,4,8,16,32].filter(n=>n<=2*capacity):Array.from({length:capacity},(_,i)=>i+1),selected=Number(cards.value);
   if([...cards.options].map(o=>Number(o.value)).join()!==sizes.join()){cards.replaceChildren();for(const count of sizes){const option=element('option',count+' 卡');option.value=String(count);cards.append(option);}if(sizes.includes(selected))cards.value=String(selected);}
   $('resource-hint').textContent=dual?'双机合计 '+cards.value+' 卡，每台 '+Number(cards.value)/2+' 卡；具体卡号按队列可用情况分配。':machine.value==='auto'?'自动选择可用单机，按队列分配机器和卡号。':'使用 '+machine.value+'，具体卡号按队列可用情况分配。';
  }
  function duration(seconds){const minutes=Math.round(seconds/60),hours=Math.floor(minutes/60),rest=minutes%60;return (hours?hours+' 小时':'')+(hours&&rest?' ':'')+(rest?rest+' 分钟':'')||'0 分钟';}
  function overtimeDuration(seconds){return seconds>0&&seconds<60?'不足 1 分钟':duration(Math.floor(Math.max(0,seconds)/60)*60);}
  function localDate(value){const d=new Date(value*1000);return d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0');}
  let timestampZone='',timestampFormatter=null;const timestampCache=new Map();
  function zonedTimestamp(day,hour,minute){if(!day||hour===''||minute==='')return NaN;const zone=(state&&state.reservation_window&&state.reservation_window.timezone)||'Asia/Shanghai';if(zone!==timestampZone){timestampZone=zone;timestampFormatter=new Intl.DateTimeFormat('en-CA',{timeZone:zone,year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'});timestampCache.clear();}const key=day+'T'+hour+':'+minute;if(timestampCache.has(key))return timestampCache.get(key);const utc=Date.parse(key+':00Z');let guess=utc;for(let i=0;i<2;i++){const parts=timestampFormatter.formatToParts(new Date(guess));const get=type=>parts.find(p=>p.type===type).value;guess=utc-(Date.UTC(+get('year'),+get('month')-1,+get('day'),+get('hour'),+get('minute'),+get('second'))-guess);}if(timestampCache.size>3000)timestampCache.clear();timestampCache.set(key,guess/1000);return guess/1000;}
  const pad=n=>String(n).padStart(2,'0');
  function selectedStart(){return zonedTimestamp($('start-date').value,$('start-hour').value,$('start-minute').value);}
  function setOptions(select,items,preferred,placeholder){const signature=JSON.stringify([items,placeholder]);if(select.dataset.options===signature){if(!select.value&&!placeholder&&items.length)select.value=items.some(item=>item[0]===preferred)?preferred:items[0][0];return;}const old=select.value;select.replaceChildren();if(placeholder){const option=element('option',placeholder);option.value='';select.append(option);}for(const [value,label] of items){const option=element('option',label);option.value=value;select.append(option);}const values=items.map(item=>item[0]);select.value=values.includes(old)?old:values.includes(preferred)?preferred:placeholder?'':values[0]||'';select.dataset.options=signature;}
  function updateDates(){if(!state||!state.reservation_window)return;const days=state.reservation_window.start_dates||[];setOptions($('start-date'),days.map((day,i)=>[day,(i?'明天':'今天')+'（'+day+'）']),days[0],null);}
  let lastStartChoice=null,startExpired=false;
  function updateNightOptions(){
   const w=state&&state.reservation_window;if(!w)return;const day=$('start-date').value,valid={},previousHour=$('start-hour').value,previousMinute=$('start-minute').value;
   for(let hour=0;hour<24;hour++)for(let minute=0;minute<60;minute+=5){const value=zonedTimestamp(day,pad(hour),pad(minute));if(value>state.server_time&&(w.night_windows||[]).some(range=>value>=range.start&&value<range.end))(valid[pad(hour)]||=([])).push(pad(minute));}
   const hours=Object.keys(valid);setOptions($('start-hour'),hours.map(h=>[h,h+' 点']),'','选择小时');let selectedHour=$('start-hour').value;
   const changed=!!lastStartChoice&&(day!==lastStartChoice.day||previousHour!==lastStartChoice.hour);
   if(changed){startExpired=false;$('start-minute').value='';}
   else if(lastStartChoice&&lastStartChoice.hour&&(selectedHour!==previousHour||!(valid[selectedHour]||[]).includes(previousMinute))){$('start-hour').value='';selectedHour='';startExpired=true;}
   const minutes=selectedHour?(valid[selectedHour]||[]):['00'];setOptions($('start-minute'),minutes.map(m=>[m,m+' 分']),'00',null);lastStartChoice={day,hour:selectedHour};
  }
  function timeLimits(){
   updateDates();const night=$('kind').value==='night',window=state&&state.reservation_window;
   $('avoid-area').classList.toggle('hidden',!state||!state.strict_nights||night);
   for(const part of ['date','hour','minute']){const el=$('start-'+part);$('start-'+part+'-label').classList.toggle('hidden',!night);el.required=night;el.disabled=!night;}
   const maxHours=night?13:2;setOptions($('hours'),Array.from({length:maxHours+1},(_,h)=>[String(h),h+' 小时']),'0',null);
   const hours=Number($('hours').value),minuteValues=hours===maxHours?[0]:Array.from({length:12},(_,i)=>i*5).filter(m=>hours!==0||m>0);
   setOptions($('minutes'),minuteValues.map(m=>[String(m),m+' 分钟']),'30',null);
   if(night)updateNightOptions();const start=night?selectedStart():state&&state.server_time,seconds=hours*3600+Number($('minutes').value)*60;
   let error=!Number.isInteger(seconds)||seconds<300?'预计时长至少 5 分钟':seconds>maxHours*3600?(night?'夜间任务最长 13 小时':'普通任务最长 2 小时'):'';
   if(!error&&!night&&window&&Number.isFinite(start)&&start+seconds>window.end)error='预计结束时间不能超过次日上午 9 点';
   $('minutes').setCustomValidity(error);
   const selected=night&&window&&Number.isFinite(start)?(window.night_windows||[]).find(w=>start>=w.start&&start<w.end):null;
   $('start-date').setCustomValidity(night&&Number.isFinite(start)&&!selected?'夜间任务开始时间须在晚上 20:00 至次日 09:00 前':'');
   if(window){$('reservation-window').textContent=night?(startExpired?'所选开始时间已过，请重新选择。':$('start-hour').options.length===1?'当前日期没有可选的开始时间，请选择其他日期。':'')+'夜间任务开始时间为 20:00 至次日 09:00 前，最长 13 小时；分钟按 5 分钟选择；后来的预约需避开已有排程。'+(selected&&start+seconds>selected.end?'预计结束时间超出次日 9 点，仍可预约。':''):'普通任务预计结束时间不晚于 '+date(window.end)+'。';}
   if(state&&state.strict_nights){
    if(night){$('resource-hint').textContent=state.normal_priority?'机器和卡号固定；普通任务优先，夜间开始时间可能顺延。':'提交成功后机器和卡号固定；所选时段冲突时请调整预约。';}
    else $('reservation-window').textContent=state.normal_priority?'普通任务须在午夜 0 点前开始，超过凌晨 1 点的时长会自动截短。':'普通任务须在凌晨 1 点前开始，时长超过凌晨 2 点的部分会自动截短。';
   }
  }

  function resource(allocation){return Object.entries(allocation||{}).map(([node,cards])=>node+'：'+cards.join('、')).join('；')||'待分配';}
  function notifyAlerts(){
   const alerts=state.alerts||[],live=new Set(alerts.map(a=>a.id));for(const id of alertSeen)if(!live.has(id))alertSeen.delete(id);
   const jobs=new Set(state.tasks.map(row=>row.id));for(const id of overdueNotices.keys())if(!jobs.has(id))overdueNotices.delete(id);
   if(!registered()||permission()!=='granted'||failure)return;
   for(const alert of alerts){const repeating=!!(alert.job&&alert.repeat_seconds),previous=repeating&&overdueNotices.get(alert.job),noticeUser=state.user.id,escalated=!!(previous&&previous.kind==='overdue-nonblocking'&&['overdue','overdue-joint','overdue-dependent'].includes(alert.kind));if(repeating){if(previous&&state.server_time-previous.at<alert.repeat_seconds&&!escalated)continue;}else if(alertSeen.has(alert.id))continue;try{const n=new Notification(escalated?(alert.kind==='overdue-joint'?'A5 超时任务共同阻塞':alert.kind==='overdue-dependent'?'A5 超时任务阻塞后续预约':'A5 超时任务已转为瓶颈'):alert.kind==='unregistered'?'A5 插队提醒':alert.kind==='over-allocation'?'A5 预约资源占用提醒':alert.kind==='reset'?'A5 预约清空通知':alert.kind==='night-rescheduled'?'A5 夜间预约顺延提醒':alert.kind==='extended-next'?'A5 排队延长提醒':alert.kind==='overtime-release'?'A5 预约登记已释放':'A5 使用超时提醒',{body:escalated?(alert.kind==='overdue-joint'?'你的超时预约与其他超时任务共同影响后续预约，请尽快处理。':alert.kind==='overdue-dependent'?'你的超时预约占用后续预约所需资源，请尽快处理。':'你的超时预约已由非瓶颈转为瓶颈，正在阻挡后续预约，请尽快处理。'):alert.message,tag:'a5-alert:'+alert.id});if(repeating)overdueNotices.set(alert.job,{at:state.server_time,kind:alert.kind});else alertSeen.add(alert.id);openNotices.add(n);n.onerror=()=>{if(repeating)overdueNotices.delete(alert.job);else alertSeen.delete(alert.id);if(registered()&&state.user.id===noticeUser)invalidate('通知发送失败，请重新验证桌面通知。');};n.onclose=()=>openNotices.delete(n);n.onclick=()=>{window.focus();host.scrollIntoView({block:'center'});n.close();};}catch(e){invalidate('通知发送失败，请重新验证桌面通知。');break;}}
  }
  function notify(){
   notifyAlerts();
   if(!ready())return;
   const live=new Set(state.tasks.map(row=>row.id));for(const id of delivered.keys())if(!live.has(id))delivered.delete(id);for(const id of bottleneckStates.keys())if(!live.has(id))bottleneckStates.delete(id);
   for(const row of state.tasks){
    if(!row.mine&&!row.collaborator)continue;
    let event='',title='',body='';
    const seen=delivered.get(row.id)||new Set();
    if(row.state==='active'&&row.activated&&row.requested_seconds){const remaining=row.activated+row.requested_seconds-state.server_time,previous=bottleneckStates.get(row.id);if(row.bottleneck&&previous===false){if(remaining<=600)seen.add('bottleneck-ending:10');if(remaining<=300)seen.add('bottleneck-ending:5');delivered.set(row.id,seen);}bottleneckStates.set(row.id,!!row.bottleneck);}
    if(row.missed_claims&&!seen.has('deferred:'+row.missed_claims)){event='deferred:'+row.missed_claims;title='A5 预约已顺延一位';body='超过 10 分钟未开始，这次分配已释放，预约向后顺延一位。请查看新的排队状态。';}
    else if(row.state==='ready'&&row.can_claim){const remaining=Math.max(0,row.claim_before-state.server_time),elapsed=Math.max(0,state.server_time-(row.ready_since||state.server_time));const phase=remaining<=60?'last-minute':Math.floor(elapsed/60);event='ready:'+(row.offer_id||row.ready_since||row.claim_before)+':'+phase;title=phase===0?'A5 资源已分配':'A5 提醒：请及时开始使用';body=resource(row.allocations)+'，剩余 '+Math.ceil(remaining/60)+' 分钟。请点击“开始使用”，超时预约将向后顺延一位。'+(state.overtime_control?'登记释放不会停止实际进程，请先查看卡使用情况。':'');}
    else if(row.state==='active'&&row.activated&&row.requested_seconds){const remaining=row.activated+row.requested_seconds-state.server_time,elapsed=state.server_time-row.activated,started='started:'+row.activated;if(elapsed>=0&&elapsed<30&&!seen.has(started)){event=started;title='A5 已开始使用';body=resource(row.allocations)+'。'+(row.bottleneck?'当前是瓶颈任务，预计结束时间会影响等待中的预约，请按时释放。':'请留意预计结束时间。');}else if(remaining<=0){if(!state.alerts){event='overdue';title='A5 预约已到预计结束时间';body='请自行停止服务，并在主页结束登记。';}}else if(!row.not_before&&row.bottleneck&&remaining<=300&&row.requested_seconds>300){event='bottleneck-ending:5';title='A5 瓶颈任务即将到期';body='预计剩余 '+Math.ceil(remaining/60)+' 分钟；预计结束时间会影响等待中的预约。';}else if(!row.not_before&&row.bottleneck&&remaining>300&&remaining<=600&&row.requested_seconds>600){event='bottleneck-ending:10';title='A5 瓶颈任务即将到期';body='预计剩余 '+Math.ceil(remaining/60)+' 分钟；预计结束时间会影响等待中的预约。';}}
    if(!event||(delivered.get(row.id)||new Set()).has(event))continue;
    try{const noticeUser=state.user.id,n=new Notification(title,{body,tag:'a5-queue:'+row.id+':'+event});openNotices.add(n);const seen=delivered.get(row.id)||new Set();seen.add(event);delivered.set(row.id,seen);n.onerror=()=>{seen.delete(event);if(registered()&&state.user.id===noticeUser)invalidate('桌面通知发送失败，请重新验证通知后再提交预约。');};n.onclose=()=>openNotices.delete(n);n.onclick=()=>{window.focus();host.scrollIntoView({block:'center'});n.close();};}catch(e){invalidate('桌面通知发送失败，请重新开启并验证。');break;}
   }
  }
  function renderCards(){
   const grid=$('card-grid');grid.replaceChildren();const capacity=state.cards_per_node||8;$('card-capacity-hint').textContent='每台 '+capacity+' 个卡位（0–'+(capacity-1)+'），双机合计 '+2*capacity+' 卡。预约与 NPU 进程状态分开显示。';const now=state.server_time,buffer=state.reservation_buffer_seconds??600;
   const labels={reserved_user:['reserved-user','已预约','预约人使用'],reserved_other:['reserved-other','已预约','其他人使用'],reserved_mixed:['reserved-mixed','已预约','预约人及其他人使用'],reserved_empty:['reserved-empty','已预约','未检出进程'],reserved_unverified:['uncertain','已预约','使用人待核实'],reserved_unknown:['uncertain','已预约','使用情况待核实'],unreserved_used:['unreserved-used','未预约','检测到进程'],unreserved_empty:['unreserved-empty','未预约','未检出进程'],unreserved_unknown:['uncertain','未预约','使用情况待核实'],reservation_conflict:['uncertain','预约冲突','待核实']};
   for(const node of state.nodes||[]){const line=element('div','');line.className='card-node';line.append(element('strong',node));const list=element('div','');list.className='card-list';
    for(let card=0;card<capacity;card++){const tile=element('div','');tile.className='card-tile';tile.append(element('strong',card+' 号卡'));const occupied=state.tasks.find(row=>['ready','active'].includes(row.state)&&((row.allocations||{})[node]||[]).includes(card));
     const reported=((state.card_usage||{})[node]||[])[card],status=Object.prototype.hasOwnProperty.call(labels,reported)?reported:(occupied?'reserved_unknown':'unreserved_unknown'),[color,reservation,observation]=labels[status];tile.classList.add(color);
     const reservationText=element('span',occupied&&occupied.state==='ready'&&reservation==='已预约'?'已分配':reservation),observedText=element('span',observation);reservationText.className='card-reservation';observedText.className='card-observation';tile.append(reservationText,observedText);
     if(status==='reserved_user')tile.title='检测到预约申请人或已登记共同使用人的 NPU 进程';else if(status==='reserved_other'||status==='reserved_mixed')tile.title='检测到不属于此卡预约人或共同使用人的 NPU 进程，请核对进程归属';else if(status.includes('unknown')||status==='reserved_unverified'||status==='reservation_conflict')tile.title='采集、卡号或进程归属不完整，无法确认该卡的完整使用情况';
     if(status==='unreserved_empty'){const future=state.tasks.filter(row=>row.state==='queued'&&row.estimated_start!=null&&((row.planned_allocations||{})[node]||[]).includes(card)).map(row=>row.estimated_start);const next=future.length?Math.min(...future):null;const unknown=state.tasks.some(row=>row.state==='queued'&&row.estimated_start==null);const until=next==null?null:next-buffer;let label=until!=null?(until<=now?'开始缓冲中':'预约空档 '+overtimeDuration(until-now)):unknown?'预约空档待确认':'暂无后续预约';if(unknown&&until!=null&&until>now)label='预约空档待确认';const hint=element('span',label);hint.className='card-forecast';tile.append(hint);}list.append(tile);}line.append(list);grid.append(line);}
  }
  function render(){
   if(composingAllowed)return;
   $('account-status').textContent=state.user?'已登录：'+(state.user.employee||'')+' '+state.user.name:'未登录';
   $('account-form').classList.toggle('hidden',registered());$('logout').classList.toggle('hidden',!registered());gate();notify();resourceFields();timeLimits();renderCards();
   if(state.strict_nights)$('queue-rules').textContent=state.normal_priority?'普通任务须在午夜 0 点前开始，最晚凌晨 1 点结束；超时最多宽限 10 分钟，但凌晨 1 点须释放登记。普通任务优先，未开始的夜间预约可能顺延，机器和卡位保持不变；已进入接管或运行的夜间任务不会被新普通任务中断。夜间接管会按预约卡位清理非预约容器。':'普通任务须在凌晨 1 点前开始，时长超过凌晨 2 点的部分会自动截短。'+(state.overtime_control?'普通任务超时后有 10 分钟可延长，未延长则自动释放登记，容器和任务不会被停止。':'')+'夜间预约提交后机器、卡位和时段固定；到期会强制停止占用预约卡的非预约容器，夜间任务结束后也会停止其超时容器。异常时资源接管待核实。';
   const alerts=$('queue-alerts');alerts.replaceChildren();for(const alert of state.alerts||[]){if(alert.kind==='unregistered')continue;const p=element('p',alert.message);p.className=alert.kind==='reset'?'good':alert.kind==='overdue-nonblocking'?'warning':'error';alerts.append(p);}for(const text of state.process_monitor_errors||[])alerts.append(element('p',text));alerts.classList.toggle('hidden',!alerts.children.length);
   const body=$('tasks'),active=root.activeElement,focusJob=active&&active.dataset.allowedFor,selection=focusJob?[active.selectionStart,active.selectionEnd]:null;body.replaceChildren();
   const visible=state.tasks.filter(row=>['queued','ready','active'].includes(row.state)),groupPositions=new Map(),counts=[0,0];
   for(const row of visible)if(row.state==='queued'||row.state==='ready'){const group=Number(!!row.not_before);groupPositions.set(row.id,++counts[group]);}
   for(const night of [false,true]){
    const group=element('tr',''),heading=element('td',night?'夜间任务':'普通任务');group.className='queue-group'+(night?' night':'');heading.colSpan=9;heading.append(element('small',state.strict_nights&&night?(state.normal_priority?'卡位固定；未开始的夜间任务可能顺延':'已确认的机器、卡位和时段固定'):'顺位按类型分别显示，实际分配以排程为准'));group.append(heading);body.append(group);
    const rows=visible.filter(row=>!!row.not_before===night).sort((a,b)=>Number(b.state==='active')-Number(a.state==='active'));
    if(!rows.length){const empty=element('tr',''),cell=element('td',night?'暂无夜间任务':'暂无普通任务');empty.className='queue-empty';cell.colSpan=9;empty.append(cell);body.append(empty);}
    for(const row of rows){
    const tr=document.createElement('tr');tr.dataset.job=row.id;if(night)tr.className='night-task';
    const status=element('td',row.overdue&&night&&state.strict_nights?'夜间预约已到期 · 正在清理':row.overdue?'使用中 · 已超时 '+Math.max(1,Math.ceil(row.overdue_seconds/60))+' 分钟 · '+(row.overdue_bottleneck?'阻塞后续预约':row.overdue_joint_blocker?'与其他超时任务共同阻塞':row.overdue_pending_blocker?'阻塞后续预约（尚有其他占用）':'当前非瓶颈'):row.state==='active'&&row.bottleneck?'使用中 · 瓶颈任务':({queued:row.night_unconfirmed?'旧预约冲突 · 待处理':row.night_schedule_blocked?'资源释放待确认':row.night_fixed?(state.server_time>=row.not_before?'已预约 · 接管中':'已预约'):'等待中 · 组内第 '+groupPositions.get(row.id)+' 位',ready:'已分配，待开始',active:'使用中'})[row.state]);if(night){const badge=element('span','夜间');badge.className='night-badge';status.prepend(badge);}if(row.overdue)status.className=row.overdue_bottleneck||row.overdue_joint_blocker||row.overdue_pending_blocker?'error':'warning';else if(row.state==='active'&&row.bottleneck)status.className='bottleneck';if(row.mine&&row.normal_start_after_two){status.classList.add('standby-status');status.append(document.createElement('br'),element('strong',row.normal_start_after_midnight?'预计无法在午夜 0 点前开始，候补中':row.normal_start_after_one?'预计无法在凌晨 1 点前开始，候补中':'预计无法在凌晨 2 点前开始，候补中'));status.append(document.createElement('br'),element('span','资源提前释放时可替补；可考虑申请夜间任务，并自行设置自己的 agent 定时拉起任务。'));}
    if(state.overtime_control&&row.state==='ready'){const warnings=[];for(const [node,cards] of Object.entries(row.allocations||{}))for(const card of cards){const value=((state.card_usage||{})[node]||[])[card];if(['reserved_other','reserved_mixed'].includes(value))warnings.push(card);}if(warnings.length){status.append(document.createElement('br'),element('small','⚠ 预约卡仍检测到其他人的进程，请先核实实际使用情况'));}}
    if(row.night_end_requested)status.append(document.createElement('br'),element('small','正在结束：等待容器清理和资源确认'));
    if(row.night_reclaimed)status.append(document.createElement('br'),element('small','夜间接管已回收部分卡位'));
    if(state.overtime_control&&row.extension_minutes)status.append(document.createElement('br'),element('small','已延长 '+row.extension_minutes+' 分钟'));
    if(state.overtime_control&&!night&&row.overdue&&row.overdue_seconds<600)status.append(document.createElement('br'),element('small','超时处理剩余 '+Math.ceil(Math.max(0,(row.overtime_release_at||state.server_time+600-row.overdue_seconds)-state.server_time)/60)+' 分钟'));
    if(row.state==='active'&&!row.bottleneck&&!row.overdue_joint_blocker&&!row.overdue_pending_blocker){const remaining=row.overtime_remaining_seconds,info=element('small',typeof remaining==='number'&&Number.isFinite(remaining)?'预计还可超时 '+overtimeDuration(remaining):'可超时余量待确认');info.title='按争用同一批等待资源的任务预计结束时间计算，不含确认缓冲；实际结束时间可能变化。';status.append(document.createElement('br'),info);}
    const people=element('td',row.employee+' / '+row.name);for(const person of row.allowed_people||[]){people.append(document.createElement('br'),element('small','共同：'+person.employee+(person.name?' / '+person.name:'')));}tr.append(status,people,element('td',row.dual?'双机共 '+(row.total_cards||row.cards*2)+' 卡（各 '+row.cards+' 卡）':(row.fixed_node||'单机')+' · '+row.cards+' 卡'));
    const allocation=element('td','');const allocations=Object.entries(row.allocations||{}),predicted=row.state==='queued'&&row.not_before&&row.estimated_start!=null?Object.entries(row.planned_allocations||{}):[];if(allocations.length||predicted.length){const display=allocations.length?allocations:predicted;for(const [node,cards] of display){if(allocation.childNodes.length)allocation.append(document.createElement('br'));allocation.append(document.createTextNode((!allocations.length?'预计 ':'')+node+'：'+cards.join('、')));}if(!allocations.length){allocation.append(document.createElement('br'),element('small','预计可能变化，以实际分配为准'));}}else if(row.state==='queued'&&row.not_before&&(row.dual||row.fixed_node)){allocation.textContent=row.dual?'指定双机（待分配）':'指定 '+row.fixed_node+'（待分配）';}else allocation.textContent='待分配';if(row.claim_before&&row.state==='ready'){const remaining=Math.max(0,Math.ceil(row.claim_before-state.server_time));allocation.append(document.createElement('br'),element('small','请在 '+Math.floor(remaining/60)+'分'+remaining%60+'秒内开始'));}if(row.missed_claims)allocation.append(document.createElement('br'),element('small','超时顺延 '+row.missed_claims+' 次'));tr.append(allocation);
    tr.append(element('td',dateMinute(row.created)));
    const start=row.activated?dateMinute(row.activated):row.window_expired?'时段已过，请取消后重新预约':row.state==='ready'?'可立即开始':row.estimated_start?(row.night_fixed&&!state.normal_priority?'预约 ':'预计 ')+dateMinute(row.estimated_start):row.mine&&row.normal_start_after_two?(row.normal_start_after_midnight?'候补：午夜 0 点前未排到':row.normal_start_after_one?'候补：凌晨 1 点前未排到':'候补：凌晨 2 点前未排到'):'预计时间待确认';const end=element('td',row.activated&&row.requested_seconds?dateMinute(row.activated+row.requested_seconds):row.state==='ready'?dateMinute(state.server_time+(row.effective_requested_seconds??row.requested_seconds)):row.estimated_start?dateMinute(row.estimated_start+(row.effective_requested_seconds??row.requested_seconds)):'—');if(row.night_end_overdue)end.append(document.createElement('br'),element('strong','结束时间超出次日 9 点'));const length=element('td',duration(row.effective_requested_seconds??row.requested_seconds));if(row.effective_requested_seconds<row.requested_seconds)length.append(document.createElement('br'),element('small',state.normal_priority?'已按凌晨 1 点截止时间截短':'已按夜间预约和截止时间截短'));const startCell=element('td',start),startValue=row.activated||(row.state==='ready'?state.server_time:row.estimated_start);if(night&&state.normal_priority&&row.night_requested_start&&row.not_before>row.night_requested_start){startCell.append(document.createElement('br'),element('small','原预约 '+dateMinute(row.night_requested_start)));}if(!night&&mealStart(startValue)){const mark=element('small','🍴 饭点开始');mark.className='meal-start';startCell.append(document.createElement('br'),mark);}tr.append(startCell,length,end);
    const actions=element('td','');function button(label,action){const b=element('button',label);b.onclick=async()=>{if((action==='finish'||action==='cancel')&&!window.confirm(action==='finish'?(state.strict_nights&&row.not_before?'结束夜间预约将停止该时段内仍占用预约卡的容器，确认结束？':'确认已自行停止服务并释放这些卡，结束登记？'):label+'？'))return;b.disabled=true;try{report((await api('unified-action',{job:row.id,action,offer_id:row.offer_id,confirmed:true})).message);await refresh();}catch(e){report(e.message,true);}finally{b.disabled=false;}};actions.append(b);}
    if(state.user&&(row.mine||row.collaborator||state.user.admin)){if(row.state==='ready')button('开始使用','claim');if((row.mine||state.user.admin)&&(row.state==='queued'||row.state==='ready')){button('取消预约','cancel');if(state.normal_priority&&night&&row.night_takeover){actions.lastElementChild.disabled=true;actions.lastElementChild.title='夜间资源接管中，请待开始后结束登记';}}if(row.state==='active'&&!row.night_end_requested&&(!row.not_before||row.morning_carryover||!state.night_auto_finish_enabled))button('结束登记','finish');}tr.append(actions);body.append(tr);
    if(state.overtime_control&&!night&&row.state==='active'&&row.overdue&&row.overdue_seconds<600&&state.user&&(row.mine||row.collaborator||state.user.admin)){
     const select=element('select','');select.setAttribute('aria-label','延长时长');for(const minutes of [10,20,30]){const option=element('option',minutes+' 分钟');option.value=String(minutes);select.append(option);}const extend=element('button','延长使用');extend.type='button';extend.onclick=async()=>{extend.disabled=true;try{const result=await api('unified-action',{job:row.id,action:'extend',extension_minutes:Number(select.value),confirmed:true});report(result.message);await refresh();}catch(e){report(e.message,true);}finally{extend.disabled=false;}};actions.append(select,extend);
    }
    if(state.user&&state.user.admin&&row.state==='queued'){button('置顶','priority-top');actions.lastElementChild.disabled=!row.can_raise_priority;button('↑ 调高','priority-up');actions.lastElementChild.disabled=!row.can_raise_priority;}
    if(state.user&&(row.mine||state.user.admin)&&['queued','ready'].includes(row.state)){button('↓ 调低','priority-down');actions.lastElementChild.disabled=!row.can_lower_priority;}
    if(state.strict_nights&&!night&&state.user&&(row.mine||state.user.admin)&&['queued','ready'].includes(row.state)){
     const edit=element('button',avoidEditors.has(row.id)?'收起避开时段':'修改避开时段');edit.type='button';edit.className='secondary';edit.onclick=()=>{if(avoidEditors.has(row.id))avoidEditors.delete(row.id);else avoidEditors.set(row.id,new Set(row.avoid_slots||[]));render();};actions.append(edit);
     if(avoidEditors.has(row.id)){const panel=element('div','');panel.className='avoid-panel';const selected=avoidEditors.get(row.id);renderAvoidGrid(panel,selected);const save=element('button','保存避开时段');save.type='button';save.onclick=async()=>{save.disabled=true;try{const result=await api('unified-avoid',{job:row.id,avoid_slots:[...selected],confirmed:true});avoidEditors.delete(row.id);report(result.message);await refresh();}catch(e){report(e.message,true);}finally{save.disabled=false;}};panel.append(save);actions.append(panel);}
    }
    if(state.user&&row.mine){
     const allowed=row.allowed_employees||[];let editor=allowedEditors.get(row.id);if(!editor){editor={query:'',matches:[],loading:false,saving:false,error:'',seq:0,timer:null,open:false,names:{},namesKey:'',namesLoading:false,namesNext:0};allowedEditors.set(row.id,editor);}
     const namesKey=allowed.join(',');if(!allowed.length)editor.namesKey='';if(allowed.length&&!editor.namesLoading&&(editor.namesKey!==namesKey||Date.now()>=editor.namesNext)){
      editor.namesKey=namesKey;editor.namesNext=Date.now()+60000;editor.namesLoading=true;
      api('unified-allowed-names',{job:row.id}).then(result=>{if(!stopped&&allowedEditors.get(row.id)===editor&&editor.namesKey===namesKey)editor.names=result.names||{};}).catch(()=>{}).finally(()=>{editor.namesLoading=false;if(!stopped&&allowedEditors.get(row.id)===editor)render();});
     }
     const box=element('div','');box.className='allowed-editor';const chips=element('div','');chips.className='allowed-chips';
     async function changeAllowed(employee,change,name){if(editor.saving)return;editor.saving=true;editor.seq++;clearTimeout(editor.timer);editor.loading=false;editor.matches=[];editor.error='';render();try{const result=await api('unified-allowed',{job:row.id,employee,change,confirmed:true,confirmed_name:name});if(change==='add'&&name)editor.names[employee]=name;editor.query='';report(result.message);await refresh();}catch(e){editor.error=e.message;}finally{editor.saving=false;if(!stopped&&allowedEditors.get(row.id)===editor)render();}}
     for(const employee of allowed){const label=editor.names[employee]?editor.names[employee]+' · '+employee:employee;const chip=element('button',label+' ×');chip.type='button';chip.className='allowed-chip';chip.title='点击移除 '+label;chip.setAttribute('aria-label','移除允许用户 '+label);chip.disabled=editor.saving;chip.onclick=()=>changeAllowed(employee,'remove');chips.append(chip);}if(allowed.length){box.append(element('small','允许用户（点击移除）'),chips);}
     const toggle=element('button',editor.open?'收起共同使用人':'登记共同使用人');toggle.type='button';toggle.className='secondary';toggle.onclick=()=>{editor.open=!editor.open;editor.seq++;clearTimeout(editor.timer);editor.query='';editor.matches=[];editor.loading=false;editor.error='';render();};box.append(toggle);if(editor.open){
     const input=document.createElement('input');input.className='allowed-input';input.dataset.allowedFor=row.id;input.placeholder='搜索工号或姓名';input.setAttribute('aria-label','搜索允许用户工号或姓名');input.autocomplete='off';input.maxLength=120;input.value=editor.query;input.disabled=editor.saving;
     function search(value){editor.query=value;editor.seq++;clearTimeout(editor.timer);editor.matches=[];editor.error='';editor.loading=false;const query=editor.query.trim();if(query&&/^[\x00-\x7f]+$/.test(query)&&query.length<(/[0-9]/.test(query)?3:2))editor.error='请输入姓名或至少 3 位工号';else if(query&&!/^[\p{L}\p{N} ·.'-]+$/u.test(query))editor.error='请输入有效的工号或姓名';else if(query){const seq=editor.seq;editor.loading=true;editor.timer=setTimeout(async()=>{try{const result=await api('unified-allowed-search',{job:row.id,query});if(stopped||allowedEditors.get(row.id)!==editor||editor.seq!==seq)return;editor.matches=result.people;editor.error=result.people.length?'':'未找到匹配用户';}catch(err){if(!stopped&&allowedEditors.get(row.id)===editor&&editor.seq===seq)editor.error=err.message;}finally{if(!stopped&&allowedEditors.get(row.id)===editor&&editor.seq===seq){editor.loading=false;render();}}},300);}render();}
     input.addEventListener('compositionstart',()=>{composingAllowed=true;editor.seq++;clearTimeout(editor.timer);editor.loading=false;editor.matches=[];});input.addEventListener('compositionend',()=>{composingAllowed=false;search(input.value);});input.oninput=e=>{if(!e.isComposing&&!composingAllowed)search(input.value);};
     box.append(input);const results=element('div','');results.className='allowed-results';for(const person of editor.matches){if(allowed.includes(person.employee))continue;const item=element('button',person.name+' · '+person.employee);item.type='button';item.disabled=editor.saving;item.onclick=()=>changeAllowed(person.employee,'add',person.name);results.append(item);}box.append(results);if(editor.loading||editor.error){const hint=element('small',editor.loading?'正在查询…':editor.error);hint.className='allowed-hint';box.append(hint);}}actions.append(box);
    }
   }
   }
   if(focusJob){const input=[...body.querySelectorAll('input[data-allowed-for]')].find(el=>el.dataset.allowedFor===focusJob);if(input&&!input.disabled){input.focus({preventScroll:true});input.setSelectionRange(...selection);}}
   expansion.update();
  }
  async function refresh(){
   if(polling||stopped)return;polling=true;lastRefreshAt=Date.now();
   try{const next=await api('state');if(stopped)return;if((state&&state.user&&state.user.id)!==(next.user&&next.user.id)){version++;verifiedUser=null;verifying=false;autoCheckUser=null;composingAllowed=false;failure='';delivered.clear();alertSeen.clear();overdueNotices.clear();bottleneckStates.clear();clearAllowedEditors();closeNotices();}state=next;const live=new Set(state.tasks.filter(row=>row.mine).map(row=>row.id));for(const [id,editor] of allowedEditors)if(!live.has(id)){clearTimeout(editor.timer);allowedEditors.delete(id);if(root.activeElement&&root.activeElement.dataset.allowedFor===id)composingAllowed=false;}render();if(registered()&&permission()==='granted'&&!ready()&&!verifying&&!failure&&autoCheckUser!==state.user.id){autoCheckUser=state.user.id;$('enable-notifications').click();}}catch(e){if(!stopped)report(e.message,true);}finally{polling=false;}
  }
  $('account-form').onsubmit=async e=>{e.preventDefault();try{const result=await api('login',{employee:$('employee').value,name:$('name').value});report(result.message);await refresh();}catch(err){report(err.message,true);}};
  $('logout').onclick=async()=>{try{await api('logout',{});verifiedUser=null;closeNotices();await refresh();}catch(err){report(err.message,true);}};
  $('machine').onchange=resourceFields;$('cards').onchange=resourceFields;
  $('kind').onchange=timeLimits;$('hours').onchange=timeLimits;$('minutes').onchange=timeLimits;for(const part of ['date','hour','minute'])$('start-'+part).onchange=timeLimits;timeLimits();
  $('join-form').onsubmit=async e=>{e.preventDefault();timeLimits();if(!$('join-form').reportValidity())return;if(!ready()){gate();report('请先开启桌面通知并通过测试，才能加入队列。',true);return;}const night=$('kind').value==='night';submitting=true;gate();try{report((await api('enqueue',{cards_per_node:state.cards_per_node||8,machine:$('machine').value,cards:Number($('cards').value),night,not_before:night?selectedStart():0,requested_seconds:Number($('hours').value)*3600+Number($('minutes').value)*60,avoid_slots:state.strict_nights&&!night?[...initialAvoid]:[],notifications_ready:true})).message);await refresh();}catch(err){report(err.message,true);}finally{submitting=false;gate();}};
  function refreshDelay(){
   const control=document.querySelector('button[aria-label^="Choose refresh time interval"]');
   if(control&&control.textContent.trim()==='Off')return 0;
   return ({'1s':1000,'5s':5000,'10s':10000,'30s':30000})[new URLSearchParams(window.location.search).get('refresh')]||1000;
  }
  refresh();const interval=setInterval(()=>{const delay=refreshDelay();if(delay&&Date.now()-lastRefreshAt>=delay)refresh();},1000);
  const focus=()=>{gate();refresh();};window.addEventListener('focus',focus);
  return ()=>{stopped=true;version++;clearInterval(interval);clearAllowedEditors();expansion.dispose();window.removeEventListener('focus',focus);closeNotices();};
 }
 function attach(){const marker=document.querySelector('a[href="/a5-queue/home"]');const host=marker?marker.parentElement:document.getElementById('a5-queue-home');if(host===currentHost)return;if(dispose)dispose();dispose=null;currentHost=host;if(host&&!host.shadowRoot){host.id='a5-queue-home';dispose=mount(host);}}
 new MutationObserver(attach).observe(document.documentElement,{childList:true,subtree:true});attach();
})();
'''

HOME_JS = HOME_JS.replace('__FRAGMENT__', json.dumps(HOME_FRAGMENT, ensure_ascii=False))
