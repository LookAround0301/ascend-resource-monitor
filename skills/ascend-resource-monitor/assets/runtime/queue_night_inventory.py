"""Append a private, per-night history of observed NPU processes."""
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo


class NightInventoryLog:
    def __init__(self, directory, timezone):
        self.directory=Path(directory)
        if not self.directory.is_absolute():
            raise ValueError('night inventory log directory must be absolute')
        self.timezone=ZoneInfo(timezone)

    def write(self, now, nodes, inventory_times, rows):
        local=datetime.fromtimestamp(now,self.timezone)
        if 9<=local.hour<20:
            return False
        day=local.date()-timedelta(days=1) if local.hour<9 else local.date()
        self.directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        record={'sampled_at':local.isoformat(),'nodes':list(nodes),
                'inventory_times':inventory_times,
                'unavailable_nodes':[node for node in nodes if node not in inventory_times],
                'processes':rows}
        payload=(json.dumps(record,ensure_ascii=False,separators=(',',':'))+'\n').encode('utf-8')
        path=self.directory/('npu-inventory-'+day.isoformat()+'.jsonl')
        flags=os.O_WRONLY|os.O_CREAT|os.O_APPEND|getattr(os,'O_NOFOLLOW',0)
        fd=os.open(path,flags,0o600)
        try:
            with os.fdopen(fd,'ab') as file:
                file.write(payload)
        except BaseException:
            raise
        return True
