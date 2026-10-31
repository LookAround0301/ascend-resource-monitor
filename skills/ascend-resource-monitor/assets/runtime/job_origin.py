"""Conservative job-origin hints; markers are declarations, not authentication."""
from pathlib import Path
import os
import posixpath
import re

ORIGIN_ENV = b'ASCEND_MONITOR_JOB_ORIGIN='
SUMMARY_LABELS = ('employee_id', 'employee_name', 'attribution', 'container',
                  'cwd', 'host_cwd', 'directory_group', 'control_ref')
# 0 unknown, 1 declared manual, 2 suspected agent, 3 declared agent, 4 mixed/conflicting.
AGENT_EXECUTABLES = {'codex', 'claude', 'claude-code', 'cursor-agent', 'aider', 'opencode'}
BOUNDARIES = {'init', 'systemd', 'sshd', 'docker', 'dockerd', 'containerd',
              'containerd-shim', 'containerd-shim-runc-v2', 'tmux', 'screen'}
AGENT_PATH = re.compile(r'/(?:\.codex|\.claude)/(?:[^/]+/)*|/\.remote-dev/jobs/[0-9]+(?:/|$)|/@(?:openai/codex|anthropic-ai/claude-code)(?:/|$)')


def environment_origin(raw):
    """Retain only the fixed enum, never arbitrary environment values."""
    values = {item[len(ORIGIN_ENV):] for item in raw.split(b'\0') if item.startswith(ORIGIN_ENV)}
    if len(values) > 1:
        return 'conflict'
    value = next(iter(values), b'')
    return value.decode('ascii') if value in (b'agent', b'manual') else ''


def agent_hint(executable='', argv=(), cwd=''):
    if posixpath.basename(executable) in AGENT_EXECUTABLES:
        return True
    # Examine executable/script paths only, not arbitrary command arguments.
    paths = [executable, cwd]
    if argv:
        paths.append(argv[0])
        if posixpath.basename(executable).startswith(('node', 'python', 'pypy')) and len(argv) > 1 and not argv[1].startswith('-'):
            paths.append(argv[1])
    return any(AGENT_PATH.search(path or '') for path in paths)


def temporary_script(executable='', argv=()):
    paths = [executable]
    if argv:
        paths.append(argv[0])
        if len(argv) > 1 and posixpath.basename(executable).startswith(('python', 'pypy', 'bash', 'sh', 'node')):
            paths.append(argv[1])
    return any(path.startswith(('/tmp/', '/var/tmp/')) and path.endswith(('.py', '.sh', '.js')) for path in paths)


class OriginClassifier:
    """Read only bounded ancestor metadata of an NPU process, once per sample."""
    def __init__(self, proc_root='/proc', max_depth=12):
        self.root = Path(proc_root)
        self.max_depth = max_depth
        self.cache = {}

    def observe(self, pid):
        if pid not in self.cache:
            try:
                path = self.root / str(pid)
                stat = (path / 'stat').read_text()
                fields = stat[stat.rfind(')') + 2:].split()
                executable = os.readlink(path / 'exe')
                argv = (path / 'cmdline').read_bytes()[:16384].decode('utf-8', 'replace').strip('\0').split('\0')
                hint = agent_hint(executable, argv, os.readlink(path / 'cwd'))
                name = posixpath.basename(executable)
                self.cache[pid] = (int(fields[1]), int(fields[19]), hint, name in BOUNDARIES,
                                   temporary_script(executable, argv), int(fields[4]), name == 'sshd')
            except (OSError, ValueError, IndexError):
                self.cache[pid] = None
        return self.cache[pid]

    def classify(self, row):
        marker = row.get('origin_marker', '')
        if marker == 'conflict':
            return 4
        if marker == 'agent':
            return 3
        hint = bool(row.get('agent_hint', False))
        temporary = bool(row.get('temporary_script', False))
        interactive = bool(row.get('has_tty', False))
        terminal_known = 'has_tty' in row
        ssh_origin = False
        pid, child_start = row['ppid'], row['start_ticks']
        seen = {row['pid']}
        for _ in range(self.max_depth):
            if hint or pid <= 1 or pid in seen:
                break
            seen.add(pid)
            parent = self.observe(pid)
            if parent is None:
                break
            parent_pid, parent_start, parent_hint, boundary, parent_temporary, tty_nr, ssh = parent
            if parent_start > child_start:
                break
            if boundary:
                ssh_origin = ssh
                break
            hint = hint or parent_hint
            temporary = temporary or parent_temporary
            interactive = interactive or tty_nr != 0
            terminal_known = True
            pid, child_start = parent_pid, parent_start
        # This combination suggests remote automation, not proof of an AI agent.
        hint = hint or (temporary and ssh_origin and terminal_known and not interactive)
        if marker == 'manual':
            return 4 if hint else 1
        return 2 if hint else 0


def summarize_origin(values):
    values = set(values)
    if 4 in values or (1 in values and values & {2, 3}):
        return 4
    if values == {3}:
        return 3
    if values == {1}:
        return 1
    if values & {2, 3}:
        return 2
    return 0
