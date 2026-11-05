"""Exercise the actual browser notification function against a simulated clock."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'assets/runtime'))
from queue_unified_ui import HOME_JS


class MinuteReminderTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node.js is required for the browser notification fixture')
    def test_normal_offer_reminds_each_minute_without_repeats_within_minute(self):
        body = HOME_JS.split('  function notify(){\n', 1)[1].split('\n  function render(){', 1)[0]
        source = 'function notify(){\n' + body
        script = r'''
const vm = require('vm');
const source = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const calls=[];
const row={id:'job',mine:true,state:'ready',can_claim:true,claim_before:1600,ready_since:1000,offer_id:'offer1',allocations:{}};
const context={state:{tasks:[row],user:{id:'owner'},server_time:1000,alerts:[]},notifyAlerts:()=>{},ready:()=>true,
  delivered:new Map(),bottleneckStates:new Map(),openNotices:new Set(),resource:()=>'',invalidate:()=>{},failure:'',
  window:{focus:()=>{}},host:{scrollIntoView:()=>{}},
  Notification:function(title,options){calls.push({title,tag:options.tag});this.close=()=>{};}};
vm.createContext(context);vm.runInContext(source,context);
const times=[1000,1059,1060,1119,1120,1180,1240,1300,1360,1420,1480,1540,1559];
for(const t of times){context.state.server_time=t;context.notify();}
if(calls.length!==10)throw Error('Expected 10 notifications at allocation and minutes 1 through 9, got '+calls.length);
if(new Set(calls.map(x=>x.tag)).size!==10)throw Error('Reminder phases were not distinct');
if(calls[0].title!=='A5 资源已分配')throw Error('Missing initial allocation notice');
row.offer_id='offer2';row.ready_since=1600;row.claim_before=2200;context.state.server_time=1600;context.notify();
if(calls.length!==11)throw Error('New allocation did not get a new initial notice');
'''
        subprocess.run([shutil.which('node'), '-e', script], input=json.dumps(source), text=True,
                       capture_output=True, check=True)

    @unittest.skipUnless(shutil.which('node'), 'Node.js is required for the browser notification fixture')
    def test_bottleneck_start_pre_end_and_overdue_cadence(self):
        body=HOME_JS.split('  function notifyAlerts(){\n',1)[1].split('\n  function render(){',1)[0]
        source='function notifyAlerts(){\n'+body
        script=r'''
const vm=require('vm'),source=JSON.parse(require('fs').readFileSync(0,'utf8')),calls=[];
const row={id:'job',mine:true,state:'active',activated:1000,requested_seconds:3600,allocations:{},bottleneck:false};
const context={state:{tasks:[row],user:{id:'owner'},server_time:1000,alerts:[]},registered:()=>true,permission:()=>'granted',ready:()=>true,
 delivered:new Map(),bottleneckStates:new Map(),openNotices:new Set(),alertSeen:new Set(),overdueNotices:new Map(),resource:()=>'',invalidate:()=>{},failure:'',
 window:{focus:()=>{}},host:{scrollIntoView:()=>{}},Notification:function(title,options){calls.push({title,body:options.body,at:context.state.server_time});this.close=()=>{};}};
vm.createContext(context);vm.runInContext(source,context);
function poll(t){context.state.server_time=t;context.notify();}
poll(1000);if(calls.length!==1||calls[0].title!=='A5 已开始使用')throw Error('start notification');
row.bottleneck=true;poll(1300);if(calls.length!==1)throw Error('bottleneck transition must not alert');
poll(3999);if(calls.length!==1)throw Error('too early');poll(4000);poll(4001);poll(4300);poll(4301);
if(calls.length!==3||calls[1].at!==4000||calls[2].at!==4300)throw Error('pre-end cadence');
context.state.alerts=[{id:'blocking',job:'job',kind:'overdue',repeat_seconds:300,message:'blocking'}];
poll(4600);poll(4899);if(calls.length!==4)throw Error('bottleneck repeat too early');poll(4900);
if(calls.length!==5)throw Error('missing five-minute repeat');
context.state.alerts=[{id:'nonblocking',job:'job',kind:'overdue-nonblocking',repeat_seconds:900,message:'not blocking'}];
poll(4901);poll(5799);if(calls.length!==5)throw Error('nonblocking repeat too early');poll(5800);
if(calls.length!==6||calls[5].body!=='not blocking')throw Error('missing fifteen-minute repeat');
context.state.alerts=[{id:'blocking',job:'job',kind:'overdue',repeat_seconds:300,message:'blocking'}];poll(5801);
if(calls.length!==7||!calls[6].title.includes('转为瓶颈')||!calls[6].body.includes('非瓶颈'))throw Error('bottleneck escalation');poll(6100);if(calls.length!==7)throw Error('repeat must use actual elapsed time');poll(6101);if(calls.length!==8)throw Error('missing actual five-minute repeat');
const second={...row,id:'second',activated:8000,bottleneck:true};context.state.tasks=[second];context.state.alerts=[];poll(8000);
if(!calls[calls.length-1].body.includes('瓶颈'))throw Error('start notification omitted bottleneck');
const third={...row,id:'third',activated:10000,bottleneck:false};context.state.tasks=[third];poll(10000);const before=calls.length;
poll(12500);third.bottleneck=true;poll(13102);if(calls.length!==before)throw Error('becoming a bottleneck in the ten-minute window generated an extra popup');
poll(13300);if(calls.length!==before+1)throw Error('missing five-minute reminder after transition');
const ordinary={...row,id:'ordinary',activated:15000,bottleneck:false,not_before:0};context.state.tasks=[ordinary];poll(15000);const afterOrdinaryStart=calls.length;poll(18000);poll(18300);if(calls.length!==afterOrdinaryStart)throw Error('non-bottleneck advance reminder');
const night={...row,id:'night',activated:20000,bottleneck:true,not_before:20000};context.state.tasks=[night];poll(20000);const afterNightStart=calls.length;poll(23000);poll(23300);if(calls.length!==afterNightStart)throw Error('night advance reminder');
'''
        subprocess.run([shutil.which('node'),'-e',script],input=json.dumps(source),text=True,capture_output=True,check=True)


if __name__ == '__main__':
    unittest.main()
