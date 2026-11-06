"""The default A5 contract stays at eight cards; A3 is an explicit opt-in."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'assets/runtime'))
from control_common import ControlError
from queue_store import QueueStore
from queue_unified import UnifiedQueue
from queue_monitor import QueueMonitor
from queue_a3_migration import migrate
from queue_unified_ui import HOME_JS


class CapacityCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'queue.db';self.store=QueueStore(self.path)
        self.user=self.store.user(self.store.register('u12345678','Fixture','fixture-password'))
        self.clock=patch('queue_unified.time.time',return_value=1793858400)
        self.clock.start();self.addCleanup(self.clock.stop)

    def enqueue(self,queue,**data):
        return queue.enqueue(self.user,{'cards':8,'requested_seconds':600,'notifications_ready':True,**data})

    def test_a5_old_single_and_dual_payloads_keep_original_meaning(self):
        queue=UnifiedQueue(self.store,['a','b'])
        single=self.enqueue(queue)
        row=queue.view(self.user)['tasks'][0]
        self.assertEqual(row['cards'],8);self.assertEqual(row['allocations'],{'a':list(range(8))})
        queue.action(self.user,single,'cancel')
        self.enqueue(queue,dual=True)
        row=queue.view(self.user)['tasks'][0]
        self.assertEqual(row['cards'],8)
        self.assertEqual(row['allocations'],{'a':list(range(8)),'b':list(range(8))})
        self.assertEqual(queue.view(self.user)['cards_per_node'],8)

    def test_a5_machine_payload_and_restart_preserve_active_reservation(self):
        queue=UnifiedQueue(self.store,['a','b'])
        job=self.enqueue(queue,machine='dual',cards=16)
        queue.action(self.user,job,'claim')
        with self.store.db() as db:
            before=dict(db.execute('SELECT * FROM unified_tasks').fetchone())
            # Simulate a pre-upgrade A5 database without capacity metadata.
            db.execute("DELETE FROM unified_meta WHERE key='cards_per_node'")
        reopened=UnifiedQueue(self.store,['a','b'],cards_per_node=8)
        with self.store.db() as db:self.assertEqual(dict(db.execute('SELECT * FROM unified_tasks').fetchone()),before)
        self.assertEqual(reopened.view(self.user)['tasks'][0]['state'],'active')
        for data in ({'cards':9},{'cards':16},{'machine':'dual','cards':32}):
            with self.assertRaises(ControlError):self.enqueue(reopened,**data)

    def test_default_monitor_preserves_strict_a5_card_ids(self):
        monitor=QueueMonitor(['a','b'],{},None)
        self.assertEqual(monitor.cards({'npu_ids':'0,1,7'}),{0,1,7})
        for raw in ('8','15','00','01','-1','7,8',''):
            self.assertIsNone(monitor.cards({'npu_ids':raw}))

    def test_a5_is_not_automatically_expanded_by_migration(self):
        queue=UnifiedQueue(self.store,['a','b']);self.enqueue(queue)
        with self.assertRaises(ValueError):migrate(self.path,['a','b'])
        with self.assertRaises(ValueError):UnifiedQueue(self.store,['a','b'],cards_per_node=16)
        self.assertEqual(queue.view(self.user)['tasks'][0]['cards'],8)

    def test_old_shared_grants_alone_require_explicit_migration(self):
        UnifiedQueue(self.store,['a','b'])
        with self.store.db() as db:
            db.execute("DELETE FROM unified_meta WHERE key='cards_per_node'")
            db.execute('INSERT INTO unified_shared_continuing VALUES (?,?,?,?)',('a','u12345678',1,'[7]'))
        with self.assertRaises(ValueError):UnifiedQueue(self.store,['a','b'],cards_per_node=16)
        UnifiedQueue(self.store,['a','b'])

    @unittest.skipUnless(shutil.which('node'),'Node.js required for the DOM fixture')
    def test_frontend_default8_and_opt_in16_options_and_occupancy(self):
        fields='function resourceFields(){'+HOME_JS.split('  function resourceFields(){',1)[1].split('  function duration(',1)[0]
        render='function renderCards(){'+HOME_JS.split('  function renderCards(){',1)[1].split('  function render(){',1)[0]
        script=r'''
const assert=require('assert/strict'),vm=require('vm'),source=JSON.parse(require('fs').readFileSync(0,'utf8'));
class Element {
 constructor(text=''){this.textContent=text;this.children=[];this.className='';this.classList={add:c=>this.className+=' '+c};this._value='';}
 append(...items){this.children.push(...items);}
 replaceChildren(){this.children=[];this._value='';}
 get options(){return this.children;}
 get value(){return this._value||this.children[0]?.value||'';}
 set value(value){this._value=String(value);}
}
for(const capacity of [undefined,8,16]){
 const count=capacity||8,elements=Object.fromEntries(['machine','cards','resource-hint','card-grid','card-capacity-hint'].map(id=>[id,new Element()]));
 const state={nodes:['a','b'],server_time:1000,tasks:[{state:'active',allocations:{a:[count-1]}}]};
 state.card_usage={a:Array(count).fill('unreserved_empty'),b:Array(count).fill('unreserved_unknown')};state.card_usage.a[count-1]='reserved_user';
 if(capacity!==undefined)state.cards_per_node=capacity;
 const c={state,machineNodes:'',$:id=>elements[id],element:(type,text)=>new Element(text),overtimeDuration:()=>''};
 vm.createContext(c);vm.runInContext(source.fields+source.render,c);
 c.resourceFields();assert.equal(elements.cards.options.length,count);
 assert.deepEqual(elements.cards.options.map(x=>Number(x.value)),Array.from({length:count},(_,i)=>i+1));
 elements.machine.value='dual';c.resourceFields();assert.deepEqual(elements.cards.options.map(x=>Number(x.value)),count===8?[2,4,8,16]:[2,4,8,16,32]);
 c.renderCards();const lines=elements['card-grid'].children;assert.equal(lines.length,2);
 for(const line of lines){const tiles=line.children[1].children;assert.equal(tiles.length,count);assert.deepEqual(tiles.map(t=>t.children[0].textContent),Array.from({length:count},(_,i)=>i+' 号卡'));}
 const occupied=lines.flatMap(l=>l.children[1].children).filter(t=>t.className.includes('reserved-user'));
 assert.equal(occupied.length,1);assert.equal(occupied[0].children[0].textContent,(count-1)+' 号卡');assert.equal(occupied[0].children[2].textContent,'预约人使用');
 assert.ok(lines[1].children[1].children.every(t=>t.children[2].textContent==='使用情况待核实'));
}
'''
        subprocess.run(['node','-e',script],input=json.dumps({'fields':fields,'render':render}),text=True,check=True)


if __name__=='__main__':unittest.main()
