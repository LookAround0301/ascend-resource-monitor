"""Keep the formal plugin aligned with fresh A5 accounts; fail closed on ambiguity."""
import hashlib
import json
import os
from pathlib import Path
import time
from urllib.request import ProxyHandler, Request, build_opener


def atomic_json(path, value):
    temporary = path.with_suffix('.tmp')
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600),
                   'w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def publish(config, plugin, send=None):
    path = Path(config['published_config'])
    pending = path.with_name('publish-pending.json')
    fingerprint = hashlib.sha256(json.dumps(plugin, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    if pending.exists():
        # Do not repeat any previous ambiguous request, even if the cohort changes.
        raise RuntimeError('Previous publish outcome needs inspection')
    token = Path(config['token_file']).read_text(encoding='utf-8').strip()
    if not token.startswith(config['owner'] + '_'):
        raise ValueError('Credential owner mismatch')
    atomic_json(pending, dict(fingerprint=fingerprint, at=time.time()))
    payload = dict(plugin_info=json.dumps(plugin, ensure_ascii=False), auth=token)
    if send is None:
        request = Request(config['publish_url'],
                          data=json.dumps(payload, ensure_ascii=False).encode(),
                          headers={'Content-Type': 'application/json'})
        with build_opener(ProxyHandler({})).open(request, timeout=12) as response:
            result = json.loads(response.read(32768))
    else:
        result = send(payload)
    if result != '发布插件成功':
        raise ValueError('Publish response unconfirmed')
    atomic_json(path, plugin)
    pending.unlink()


def synchronize(config, people, send=None):
    plugin = json.loads(Path(config['published_config']).read_text(encoding='utf-8'))
    people = sorted(set(people))
    if (plugin['owner'] != config['owner'] or plugin['plugin_name'] != 'A5预约'
            or config['owner'] not in people):
        raise ValueError('Unexpected formal plugin or account cohort')
    if sorted(plugin['whitelist']['individual']) == people:
        return False
    plugin['whitelist'] = dict(individual=people, group=[], department=[])
    publish(config, plugin, send=send)
    return True
