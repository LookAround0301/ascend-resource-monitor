"""Notification verification survives reload, but remains bound to the account and permission."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'assets/runtime'))
from queue_unified_ui import HOME_JS


class NotificationPersistenceTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'),'Node.js is required for the notification fixture')
    def test_verification_persists_per_account_and_is_invalidated(self):
        source=HOME_JS.split('  const registered=',1)[1].split('  function date(value)',1)[0]
        source='const registered='+source
        script=r'''
const vm=require('vm'),source=JSON.parse(require('fs').readFileSync(0,'utf8')),storage=new Map();
const localStorage={getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)};
function make(user){
 const elements={join:{},'enable-notifications':{},'notification-status':{}};
 function Notice(){this.close=()=>{};queueMicrotask(()=>this.onshow&&this.onshow());}
 Notice.permission='granted';
 const c={state:{user:{id:user,registered:true}},verifiedUser:null,verifying:false,version:0,failure:'',submitting:false,stopped:false,
  localStorage,window:{isSecureContext:true,Notification:Notice},Notification:Notice,openNotices:new Set(),$:(id)=>elements[id],
  notify:()=>{},setTimeout:()=>0,clearTimeout:()=>{},fetch:()=>{throw Error('unexpected fetch')}};
 vm.createContext(c);vm.runInContext(source,c);return {c,elements,ready:()=>vm.runInContext('ready()',c)};
}
(async()=>{
 const a=make('owner');await a.elements['enable-notifications'].onclick();if(!a.ready())throw Error('initial test failed');
 const reload=make('owner');reload.c.gate();if(!reload.ready())throw Error('reload did not restore verification');
 const other=make('other');other.c.gate();if(other.ready())throw Error('verification leaked across accounts');
 reload.c.Notification.permission='denied';reload.c.gate();if(reload.ready()||storage.has('a5-notification-verified-v1:owner'))throw Error('revoked permission retained verification');
 await other.elements['enable-notifications'].onclick();other.c.invalidate('failed');if(other.ready()||storage.has('a5-notification-verified-v1:other'))throw Error('failure retained verification');
 console.log('ok');
})().catch(e=>{console.error(e);process.exit(1)});
'''
        subprocess.run([shutil.which('node'),'-e',script],input=json.dumps(source),text=True,capture_output=True,check=True)


if __name__=='__main__':unittest.main()
