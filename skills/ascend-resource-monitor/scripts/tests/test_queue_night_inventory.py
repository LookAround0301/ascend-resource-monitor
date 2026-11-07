from datetime import datetime
import json
from pathlib import Path
import sys
import tempfile
import unittest
from zoneinfo import ZoneInfo
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'assets/runtime'))
from queue_night_inventory import NightInventoryLog
from queue_monitor import QueueMonitor
from test_queue_monitor import inventory_many


class NightInventoryTests(unittest.TestCase):
    def test_records_samples_at_night_in_one_file_per_night(self):
        zone=ZoneInfo('Asia/Shanghai')
        def stamp(value):return datetime.fromisoformat(value).replace(tzinfo=zone).timestamp()
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)/'history';log=NightInventoryLog(folder,'Asia/Shanghai')
            self.assertFalse(log.write(stamp('2026-11-06T19:59:59'),['a'],{},[]))
            self.assertFalse(folder.exists())
            row={'pid':'42','employee':'u12345678','cwd':'/work','host_cwd':'/host/work','npu_ids':'1'}
            self.assertTrue(log.write(stamp('2026-11-06T20:00:00'),['a'],{'a':1},[row]))
            self.assertTrue(log.write(stamp('2026-11-07T08:59:50'),['a'],{'a':2},[row]))
            self.assertFalse(log.write(stamp('2026-11-07T09:00:00'),['a'],{},[]))
            files=list(folder.iterdir());self.assertEqual([p.name for p in files],['npu-inventory-2026-11-06.jsonl'])
            records=[json.loads(line) for line in files[0].read_text(encoding='utf-8').splitlines()]
            self.assertEqual(len(records),2);self.assertEqual(records[0]['processes'],[row])
            self.assertEqual(records[1]['inventory_times'],{'a':2})

    def test_rejects_relative_directory(self):
        with self.assertRaises(ValueError):NightInventoryLog('relative/logs','Asia/Shanghai')


class MonitorLogTests(unittest.IsolatedAsyncioTestCase):
    async def test_monitor_logs_each_fresh_ten_second_sample_with_work_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            stamp=datetime.fromisoformat('2026-11-06T20:00:00').replace(tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
            data=inventory_many(stamp,[('u12345678','Owner','0')])
            metric=next(item for item in data['data']['result'] if item['metric']['__name__'].endswith('start_time_seconds'))
            metric['metric'].update(cwd='/work',host_cwd='/mnt/work',user='root',name='python',attribution='fixture')
            async def fetch(url,query):return data
            monitor=QueueMonitor(['a'],{'metric_prefix':'a5','timezone':'Asia/Shanghai','night_inventory_log_dir':tmp},fetch)
            with patch('queue_monitor.time',SimpleNamespace(time=lambda:stamp,monotonic=lambda:100)):
                await monitor.poll()
            with patch('queue_monitor.time',SimpleNamespace(time=lambda:stamp+9,monotonic=lambda:109)):
                await monitor.poll()
            with patch('queue_monitor.time',SimpleNamespace(time=lambda:stamp+10,monotonic=lambda:110)):
                await monitor.poll()
            records=[json.loads(line) for line in next(Path(tmp).glob('*.jsonl')).read_text(encoding='utf-8').splitlines()]
            self.assertEqual(len(records),2)
            self.assertEqual(records[0]['processes'][0]['cwd'],'/work')
            self.assertEqual(records[0]['processes'][0]['host_cwd'],'/mnt/work')
            self.assertEqual(records[0]['processes'][0]['employee'],'u12345678')


if __name__=='__main__':unittest.main()
