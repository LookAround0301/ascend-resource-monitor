"""Forced SSH command. No command arguments, forwarding, or database access."""
import json
import os
from pathlib import Path
import sys
import time

if os.environ.get('SSH_ORIGINAL_COMMAND') != 'snapshot':
    sys.exit(64)
try:
    raw = Path('/var/lib/ascend-welink-export/snapshot.json').read_bytes()
    snapshot = json.loads(raw)
    age = time.time() - float(snapshot['exported_at'])
    if len(raw) > 2_000_000 or not -30 <= age <= 90:
        raise ValueError('Unavailable snapshot')
except Exception:
    sys.exit(69)
sys.stdout.buffer.write(raw)
