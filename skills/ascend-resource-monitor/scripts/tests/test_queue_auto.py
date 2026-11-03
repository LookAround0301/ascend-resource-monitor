"""Employee matching must cover every current NPU PID in a container."""
from pathlib import Path
import copy,json,sys,unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'assets/runtime'))
from control_common import ControlError
from queue_ownership import QueueOwnership,parse_inventory
from queue_processes import QueueProcesses

CID='a'*64
class MatchingTests(unittest.TestCase):
    def setUp(self):
        self.rows=[{'container_id':CID,'started_at':'start','pid':10,'process_key':'boot:10:100','employee':'u12345678'}]
        self.snap={'container_id':CID,'container_started_at':'start','container':'fixture','processes':[{'pid':10,'start_ticks':100}], 'inventory_complete':True}
        class Host:pass
        host=Host();host.snapshot=lambda cid:copy.deepcopy(self.snap);host.same=QueueProcesses.same
        self.matcher=QueueOwnership(host,reader=lambda:self.rows,identity_check=lambda row:True)

    def test_exact_match_and_foreign_employee(self):
        result=self.matcher.owned('u12345678')
        self.assertEqual(len(result['targets']),1);self.assertEqual(result['blocked'],0)
        self.assertEqual(self.matcher.owned('u12345679')['targets'],[])
        self.matcher.verify(result['targets'][0],{'employee':'u12345678'})
        with self.assertRaises(ControlError):self.matcher.verify(result['targets'][0],{'employee':'u12345679'})

    def test_mixed_unknown_missing_pid_stale_lifetime_and_partial_proc_are_refused(self):
        second={**self.rows[0],'pid':11,'process_key':'boot:11:101','employee':'u12345679'}
        self.rows.append(second);self.snap['processes'].append({'pid':11,'start_ticks':101})
        for owner in ['u12345679','未知','待确认']:
            second['employee']=owner
            result=self.matcher.owned('u12345678');self.assertEqual(result['targets'],[]);self.assertEqual(result['blocked'],1)
        self.rows.pop();self.assertEqual(self.matcher.owned('u12345678')['targets'],[])
        self.snap['processes'].pop();self.matcher.identity_check=lambda row:False
        self.assertEqual(self.matcher.owned('u12345678')['targets'],[])
        self.matcher.identity_check=lambda row:True;self.snap['inventory_complete']=False
        self.assertEqual(self.matcher.owned('u12345678')['targets'],[])

    def test_process_and_container_start_change_cannot_reuse_match(self):
        self.rows[0]['process_key']='boot:10:99';self.assertEqual(self.matcher.owned('u12345678')['targets'],[])
        self.rows[0]['process_key']='boot:10:100';self.snap['container_started_at']='new'
        self.assertEqual(self.matcher.owned('u12345678')['targets'],[])

    def test_inventory_source_freshness_completeness_and_escaping(self):
        labels={'control_ref':CID+'@start','employee_id':'u12345678','pid':'10','process_key':'boot:10:100','cwd':'a"b'}
        raw='\n'.join(['p_inventory_success 1','p_inventory_truncated 0','p_inventory_timestamp_seconds 1000','p_container_metadata_age_seconds 1','p_inventory_processes 1',
                       'p_start_time_seconds{'+','.join(k+'='+json.dumps(v) for k,v in labels.items())+'} 10'])
        self.assertEqual(parse_inventory(raw,'p_',1001)[0]['employee'],'u12345678')
        for invalid in [raw.replace('success 1','success 0'),raw.replace('truncated 0','truncated 1'),raw.replace('processes 1','processes 2')]:
            with self.assertRaises(ControlError):parse_inventory(invalid,'p_',1001)
        with self.assertRaises(ControlError):parse_inventory(raw,'p_',1050)

if __name__=='__main__':unittest.main()
