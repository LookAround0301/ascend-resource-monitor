const {chromium}=require(process.env.A5_TEST_PLAYWRIGHT || 'playwright');
const path=require('node:path');
const {execFileSync}=require('node:child_process');
const assets=path.resolve(__dirname,'../../assets');
const sources={a5:'runtime/queue_unified_ui.py',a3_103:'a3-reservation-overlays/103/queue_unified_ui.py',a3_140:'a3-reservation-overlays/140/queue_unified_ui.py'};
(async()=>{
 const browser=await chromium.launch({headless:true,...(process.env.A5_TEST_BROWSER?{executablePath:process.env.A5_TEST_BROWSER}:{})});
 try{
  for(const [name,width] of [['a5',1200],['a5',390],['a3_103',1200],['a3_103',390],['a3_140',1200],['a3_140',390]]){
   const page=await browser.newPage({viewport:{width,height:900}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
   await page.addInitScript(()=>{
    const now=Date.now()/1000;
    window.__testMode='queued';window.__posts=[];window.__refreshes=0;
    window.fetch=async(url,opts={})=>{
     const key=String(url).split('/').pop();
     if(key==='unified-backfill'){window.__posts.push(JSON.parse(opts.body));return {ok:true,json:async()=>({message:'saved'})};}
     if(key==='unified-allowed-names')return {ok:true,json:async()=>({names:{}})};
     if(key==='state'){
      window.__refreshes++;
      const task={id:'fixture-job',state:window.__testMode,employee:'a00000001',name:'Example',mine:true,collaborator:false,
       created:now-120,activated:window.__testMode==='active'?now:0,requested_seconds:3600,cards:1,total_cards:1,
       allocations:window.__testMode==='active'?{'node-a':[0]}:{},planned_allocations:{},allowed_employees:[],allowed_people:[],
       not_before:0,backfill_min_seconds:0,avoid_slots:[],fixed_node:'node-a'};
      return {ok:true,json:async()=>({user:{id:'u1',employee:'a00000001',name:'Example',registered:true,admin:false},tasks:[task],
       nodes:['node-a','node-b'],cards_per_node:8,server_time:now+window.__refreshes,unified:true,strict_nights:true,
       midnight_separation:true,night_start_hour:0,normal_priority:false,overtime_control:true,normal_max_seconds:3600,
       night_carryover_cutoff_hour:9,alerts:[],process_monitor_errors:[],card_usage:{},
       reservation_window:{timezone:'Asia/Shanghai',start:now,end:now+10000,start_dates:[],night_windows:[]}})};
     }
     return {ok:true,json:async()=>({})};
    };
   });
   await page.route('http://example.test/',r=>r.fulfill({contentType:'text/html',body:'<div id="a5-queue-home"></div>'}));
   await page.goto('http://example.test/');const script=execFileSync(process.env.A5_TEST_PYTHON||'python',['-c',"import runpy,sys;print(runpy.run_path(sys.argv[1])['HOME_JS'])",path.join(assets,sources[name])],{encoding:'utf8',env:{...process.env,PYTHONUTF8:'1'}});await page.addScriptTag({content:script});
   const h=page.locator('#a5-queue-home');await h.getByRole('button',{name:'修改提前使用设置'}).click();
   let select=h.locator('select[data-backfill-for="fixture-job"]');await select.focus();
   await select.evaluate(e=>window.__selectedElement=e);const n=await page.evaluate(()=>window.__refreshes);
   await page.waitForFunction(n=>window.__refreshes>=n+2,n,{timeout:6000});
   if(!(await select.evaluate(e=>e===window.__selectedElement&&e.getRootNode().activeElement===e)))throw Error(name+': dropdown replaced while focused');
   await select.selectOption('1800');await page.waitForTimeout(1200);
   if((await select.inputValue())!=='1800')throw Error(name+': selection lost');
   await h.getByRole('button',{name:'保存提前使用设置'}).click();
   await page.waitForFunction(()=>window.__posts.length===1);
   const body=await page.evaluate(()=>window.__posts[0]);if(body.backfill_min_seconds!==1800)throw Error(name+': wrong save');
   await h.getByRole('button',{name:'修改提前使用设置'}).click();select=h.locator('select[data-backfill-for="fixture-job"]');await select.focus();
   await page.evaluate(()=>{window.__testMode='active';});
   await page.waitForFunction(()=>!document.querySelector('#a5-queue-home').shadowRoot.querySelector('select[data-backfill-for="fixture-job"]'),{timeout:6000});
   if(errors.length)throw Error(name+': '+errors.join(';'));
   console.log('PASS',name,width,'stable focus, retained selection, saved once, live state transition');await page.close();
  }
 }finally{await browser.close();}
 process.exit(0);
})().catch(e=>{console.error(e);process.exit(1)});
