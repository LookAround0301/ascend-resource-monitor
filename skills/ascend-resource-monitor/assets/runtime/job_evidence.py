"""Bounded, read-only evidence from scripts and records tied to live NPU jobs.

The worker reads the container's filesystem through its live process root. It
never enters a shell, executes a discovered script, or returns file contents.
"""
import ast
import json
import os
from pathlib import Path
import posixpath
import re
import shlex
import stat
import subprocess
import sys
import time

from job_origin import AGENT_EXECUTABLES, BOUNDARIES, agent_hint

CONTAINER = re.compile(r'(?:docker[-/])([0-9a-f]{64})(?:[./\n]|$)')
SCRIPT_SUFFIXES = ('.sh', '.py', '.js')
AGENT_HEADER = re.compile(r'(?im)^\s*#\s*(?:generated|created|written)\s+by\s+(?:codex|claude(?: code)?|cursor(?: agent)?|aider|opencode)\b')
POLL_COMMAND = re.compile(r'\b(?:docker\s+(?:inspect|stats|logs|ps)|npu-smi|curl\b[^\n]{0,200}/health)\b')
POLL_REPEAT = re.compile(r'\b(?:while|watch|sleep|time\.sleep)\b')


def literal_path(value, cwd):
    if not isinstance(value, str) or not value or len(value) > 1024 or any(c in value for c in '$`\n\0*?'):
        return None
    if not value.startswith('/') and not cwd: return None
    path = posixpath.normpath(value if value.startswith('/') else posixpath.join(cwd, value))
    if not path.startswith('/') or path.startswith(('/proc/', '/sys/', '/dev/', '/etc/')):
        return None
    if any(part in {'.ssh', '.aws', '.azure', '.kube'} for part in path.split('/')):
        return None
    if posixpath.basename(path).lower() in {'auth.json', 'credentials.json', 'config.json', '.env'}:
        return None
    return path


def command_paths(argv, cwd, depth=0, contexts=None):
    """Extract literal executed scripts and log destinations, without evaluation."""
    if depth > 2 or len(argv) > 256:
        return [], []
    scripts, records = [], []
    tokens = list(argv)
    for i, token in enumerate(tokens[:-1]):
        if token in {'>', '>>', '2>', '2>>', '--log-file', '--logfile', '--output-log'}:
            path = literal_path(tokens[i + 1], cwd)
            if path and path.endswith(('.log', '.json', '.jsonl')):
                records.append(path)
    while tokens and (tokens[0] in {'exec', 'nohup', 'env'} or re.match(r'^[A-Za-z_][A-Za-z0-9_]*=', tokens[0])):
        tokens.pop(0)
    if not tokens:
        return scripts, records
    exe = posixpath.basename(tokens[0])
    if exe in {'bash', 'sh', 'zsh', 'dash'}:
        for index, token in enumerate(tokens[1:], 1):
            if token.startswith('-') and 'c' in token[1:] and index + 1 < len(tokens):
                a, b = shell_paths(tokens[index + 1], cwd, depth + 1, contexts)
                return scripts + a, records + b
    candidates = []
    if tokens[0].endswith(SCRIPT_SUFFIXES):
        candidates.append(tokens[0])
    elif exe.startswith(('python', 'pypy', 'node')) or exe in {'bash', 'sh', 'zsh', 'dash', '.', 'source'}:
        for token in tokens[1:]:
            if token in {'-c', '-m', '--eval', '-e'}:
                break
            if token.startswith('-'):
                continue
            if token.endswith(SCRIPT_SUFFIXES):
                candidates.append(token)
            break
    for token in candidates:
        path = literal_path(token, cwd)
        if path:
            scripts.append(path)
            if contexts is not None: contexts[path] = cwd
    return scripts, records


def shell_paths(text, cwd, depth=0, contexts=None):
    if len(text) > 32768 or depth > 2:
        return [], []
    try:
        lexer = shlex.shlex(text, posix=True, punctuation_chars=';&|<>\n')
        lexer.whitespace = ' \t\r'
        lexer.whitespace_split = True
        lexer.commenters = '#'
        tokens = list(lexer)
    except ValueError:
        return [], []
    if len(tokens) > 2048:
        return [], []
    scripts, records, command = [], [], []
    for token in tokens + [';']:
        if token in {'&&', '||', '|', '&'} or not token.strip(';\n'):
            if command[:1] == ['cd'] and len(command) == 2:
                cwd = literal_path(command[1], cwd)
            else:
                a, b = command_paths(command, cwd, depth, contexts)
                scripts.extend(a); records.extend(b)
            command = []
        else:
            command.append(token)
    return scripts, records


def python_paths(text, cwd, contexts=None):
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return [], []
    scripts, records = [], []
    for index, node in enumerate(ast.walk(tree)):
        if index > 2048: break
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute) or not isinstance(node.func.value, ast.Name):
            continue
        if node.func.value.id != 'subprocess' or node.func.attr not in {'run', 'Popen', 'call', 'check_call', 'check_output'} or not node.args:
            continue
        values = node.args[0]
        if not isinstance(values, (ast.List, ast.Tuple)) or not all(isinstance(v, ast.Constant) and isinstance(v.value, str) for v in values.elts):
            continue
        call_cwd = cwd
        for keyword in node.keywords:
            if keyword.arg == 'cwd':
                call_cwd = literal_path(keyword.value.value, cwd) if isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str) else None
        a, b = command_paths([v.value for v in values.elts], call_cwd, contexts=contexts)
        scripts.extend(a); records.extend(b)
    return scripts, records


class ContainerReader:
    """No symlink traversal below the deliberately opened /proc/PID/root."""
    def __init__(self, root, budget, namespace=''):
        self.fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        self.budget = budget
        info = os.fstat(self.fd)
        self.namespace = (namespace, info.st_dev, info.st_ino)

    def close(self):
        os.close(self.fd)

    def read(self, path, limit=32768, tail=False):
        cache = self.budget.setdefault('cache', {})
        key = (self.namespace, path, limit, tail)
        if key in cache: return cache[key]
        if literal_path(path, '/') != path or self.budget['files'] >= 24 or self.budget['bytes'] >= 524288:
            return None
        fd = os.dup(self.fd)
        try:
            parts = path.lstrip('/').split('/')
            for index, part in enumerate(parts):
                flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                if index != len(parts) - 1: flags |= os.O_DIRECTORY
                next_fd = os.open(part, flags, dir_fd=fd)
                os.close(fd); fd = next_fd
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or (info.st_size > limit and not tail):
                return None
            self.budget['files'] += 1
            if tail and info.st_size > limit:
                os.lseek(fd, info.st_size - limit, os.SEEK_SET)
            raw = os.read(fd, min(limit + 1, 524288 - self.budget['bytes']))
            self.budget['bytes'] += len(raw)
            if tail and info.st_size > limit:
                raw = raw.partition(b'\n')[2]
            cache[key] = raw.decode('utf-8', 'replace') if len(raw) <= limit else None
            return cache[key]
        except OSError:
            cache[key] = None
            return None
        finally:
            os.close(fd)


def snapshot(pid, proc=Path('/proc')):
    path = proc / str(pid)
    raw = (path/'stat').read_text()
    fields = raw[raw.rfind(')') + 2:].split()
    match = CONTAINER.search((path/'cgroup').read_text())
    namespace = next((line.split()[1:] for line in (path/'status').read_text().splitlines() if line.startswith('NSpid:')), [])
    return {'pid': pid, 'ppid': int(fields[1]), 'start_ticks': int(fields[19]),
            'container_id': match.group(1) if match else '', 'namespace_pid': int(namespace[-1]) if namespace else pid,
            'exe': os.readlink(path/'exe'), 'cwd': os.readlink(path/'cwd'),
            'argv': (path/'cmdline').read_bytes()[:16384].decode('utf-8', 'replace').strip('\0').split('\0')}


def matching_record(record, identities, boot_id, tick_rate, boot_time):
    """Require a PID plus its lifetime, never just a shared directory/container."""
    if not isinstance(record, dict): return False
    for item in identities:
        key = boot_id[:12] + ':' + str(item['pid']) + ':' + str(item['start_ticks'])
        if record.get('process_key') == key:
            return True
        if isinstance(record.get('boot_id'), str) and record['boot_id'] in {boot_id, boot_id[:12]} and record.get('host_pid') == item['pid'] and record.get('start_ticks') == item['start_ticks']:
            return True
        timestamp = record.get('process_start_time_seconds')
        if record.get('container_id') == item['container_id'] and record.get('pid') == item['namespace_pid'] and isinstance(timestamp, (int, float)):
            if abs(timestamp - (boot_time + item['start_ticks']/tick_rate)) < 1:
                return True
    return False


def inspect_process(request, budget, proc=Path('/proc')):
    result = {'script_hint': False, 'record_hint': False, 'polling_hint': False, 'declared': '', 'scripts': 0, 'records': 0}
    first = snapshot(request['pid'], proc)
    if first['start_ticks'] != request['start_ticks'] or not request['container_id'] or first['container_id'] != request['container_id']:
        return result
    identities, scripts, records, seen = [], [], [], set()
    current = first
    for _ in range(12):
        if current['pid'] in seen or current['container_id'] != first['container_id'] or posixpath.basename(current['exe']) in BOUNDARIES:
            break
        seen.add(current['pid']); identities.append(current)
        contexts = {}
        a, b = command_paths(current['argv'], current['cwd'], contexts=contexts)
        scripts.extend((path, contexts.get(path, current['cwd']), 0) for path in a); records.extend(b)
        if posixpath.basename(current['exe']) in {'bash', 'sh', 'zsh'}:
            try:
                path = literal_path(os.readlink(proc/str(current['pid'])/'fd/255'), current['cwd'])
                if path and path.endswith('.sh'): scripts.append((path, current['cwd'], 0))
            except OSError: pass
        if current['ppid'] <= 1: break
        try:
            parent = snapshot(current['ppid'], proc)
        except (OSError, ValueError, IndexError): break
        if parent['start_ticks'] > current['start_ticks']: break
        current = parent
    reader = ContainerReader(proc/str(first['pid'])/'root', budget, first['container_id'])
    script_seen = set()
    try:
        while scripts and len(script_seen) < 6:
            path, cwd, depth = scripts.pop(0)
            if path in script_seen or depth > 2: continue
            script_seen.add(path)
            text = reader.read(path)
            if text is None: continue
            result['scripts'] += 1
            result['script_hint'] |= bool(AGENT_HEADER.search(text) or re.search(r'(?m)^\s*(?:export\s+)?ASCEND_MONITOR_JOB_ORIGIN=[\"\']?agent\b', text))
            result['polling_hint'] |= bool(POLL_COMMAND.search(text) and POLL_REPEAT.search(text))
            # Only literal calls are followed; generated shell/Python code is never evaluated.
            contexts = {}
            nested, logs = python_paths(text, cwd, contexts) if path.endswith('.py') else shell_paths(text, cwd, contexts=contexts)
            scripts.extend((value, contexts.get(value, cwd), depth + 1) for value in nested)
            records.extend(logs)
            records.extend([path + '.origin.json', posixpath.join(posixpath.dirname(path), '.ascend-monitor-origin.json')])
            result['script_hint'] |= any(agent_hint(value) for value in nested)
        boot_id = (proc/'sys/kernel/random/boot_id').read_text().strip()
        boot_time = next(int(line.split()[1]) for line in (proc/'stat').read_text().splitlines() if line.startswith('btime '))
        tick_rate = os.sysconf('SC_CLK_TCK')
        declared = set(); polling_times = set()
        for path in list(dict.fromkeys(records))[:8]:
            raw = reader.read(path, 16384, tail=path.endswith(('.log', '.jsonl')))
            if raw is None: continue
            try:
                values = [json.loads(raw)]
            except ValueError:
                values = []
                for line in raw.splitlines()[-40:]:
                    try: values.append(json.loads(line))
                    except ValueError: pass
            for record in values:
                if not matching_record(record, identities, boot_id, tick_rate, boot_time): continue
                result['records'] += 1
                if isinstance(record.get('origin'), str) and record['origin'] in {'agent', 'manual'}: declared.add(record['origin'])
                if isinstance(record.get('agent_name'), str) and record['agent_name'] in AGENT_EXECUTABLES: result['record_hint'] = True
                if isinstance(record.get('action'), str) and record['action'] in {'container_status', 'health_check', 'poll'} and type(record.get('timestamp')) in (int, float):
                    polling_times.add(record['timestamp'])
        result['declared'] = next(iter(declared)) if len(declared) == 1 else ('conflict' if declared else '')
        result['polling_hint'] |= len(polling_times) >= 2
        # The same PID must still belong to the same process/container after the reads.
        last = snapshot(request['pid'], proc)
        if last['start_ticks'] != first['start_ticks'] or last['container_id'] != first['container_id']:
            return {key: '' if key == 'declared' else 0 for key in result}
        return result
    finally:
        reader.close()


def merge_evidence(code, evidence):
    declared = evidence.get('declared', '')
    hint = evidence.get('script_hint', False) or evidence.get('record_hint', False)
    if declared == 'conflict' or code == 4: return 4
    if declared == 'agent': return 4 if code == 1 else 3
    if declared == 'manual': return 4 if code in {2, 3} or hint else 1
    if hint: return 4 if code == 1 else (3 if code == 3 else 2)
    return code  # Polling alone is never evidence of an AI agent.


class EvidenceProbe:
    def __init__(self):
        self.cache = {}
        self.next_run = 0
        self.pending = None
        self.status = 'idle'

    def check(self, rows):
        now = time.monotonic()
        keys = {row['process_key'] for row in rows}
        self.cache = {key: value for key, value in self.cache.items() if key in keys and now - value[0] < 60}
        if self.pending is not None:
            if self.pending.poll() is None: return {}
            self.pending = None
        requests = [{'pid': row['pid'], 'start_ticks': row['start_ticks'], 'container_id': row['container_id'], 'key': row['process_key']} for row in rows if row['container_id']]
        if requests and now >= self.next_run:
            self.next_run = now + 30
            try:
                child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--worker'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            except OSError:
                self.status = 'failed'
                return {key: evidence for key, (_, evidence) in self.cache.items()}
            try:
                output, _ = child.communicate(json.dumps(requests).encode(), timeout=2)
                data = json.loads(output) if child.returncode == 0 and len(output) < 131072 else {}
                if not isinstance(data, dict): data = {}
                for key, evidence in data.items():
                    if key in keys and isinstance(evidence, dict): self.cache[key] = (now, evidence)
                self.status = 'ok' if child.returncode == 0 else 'failed'
            except (subprocess.TimeoutExpired, ValueError):
                child.kill()
                try: child.communicate(timeout=.2)
                except subprocess.TimeoutExpired: self.pending = child
                self.status = 'timeout'
        return {key: evidence for key, (_, evidence) in self.cache.items()}


def main():
    requests = json.loads(sys.stdin.buffer.read(131072))
    budget = {'files': 0, 'bytes': 0}; result = {}
    for request in requests[:200]:
        try: result[request['key']] = inspect_process(request, budget)
        except (OSError, ValueError, IndexError, KeyError, StopIteration): pass
    print(json.dumps(result))


if __name__ == '__main__': main()
