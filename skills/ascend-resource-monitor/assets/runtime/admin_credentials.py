"""Bootstrap Grafana through stdin; persist only its encryption key and password hash."""
import io
from contextlib import closing
import json
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import tarfile

PASSWORD_KEY = 'GF_SECURITY_ADMIN_PASSWORD'
DISABLE_KEY = 'GF_SECURITY_DISABLE_INITIAL_ADMIN_CREATION'


def environment_values(data):
    return dict(line.split('=', 1) for line in data.splitlines()
                if '=' in line and not line.lstrip().startswith('#'))


def sanitized_environment(data):
    lines = [line for line in data.splitlines()
             if line.split('=', 1)[0] not in {PASSWORD_KEY, DISABLE_KEY}]
    return '\n'.join(lines).rstrip() + '\n' + DISABLE_KEY + '=true\n'


def administrator_exists(base):
    base = Path(base)
    db = base / 'data/grafana/grafana.db'
    marked = (base / 'config/admin-initialized.json').exists()
    exists = False
    if db.exists() and db.stat().st_size:
        try:
            with closing(sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True)) as connection:
                exists = connection.execute("SELECT count(*) FROM user WHERE login='admin' AND is_admin=1").fetchone()[0] == 1
        except sqlite3.OperationalError as error:
            raise RuntimeError('Cannot inspect the existing Grafana database; restore or inspect it before retrying') from error
    if marked and not exists:
        raise RuntimeError('Initialized administrator/database is missing; restore it, do not create a default account')
    return exists


def prepare_security(base, atomic):
    """Keep the legacy filename for upgrades, but never retain a login password."""
    path = Path(base) / 'config/admin.env'
    data = path.read_text() if path.exists() else ''
    values = environment_values(data)
    if not values.get('GF_SECURITY_SECRET_KEY'):
        if administrator_exists(base):
            raise RuntimeError('Existing Grafana encryption key is missing; restore it instead of replacing it')
        data += '\nGF_SECURITY_SECRET_KEY=' + secrets.token_urlsafe(32) + '\n'
    atomic(path, sanitized_environment(data), 0o600)
    return path


def initialize_administrator(base, grafana, account, password, atomic, runner=None, reset=False):
    """CLI migration/bootstrap is short-lived; final credentials enter stdin only."""
    base = Path(base)
    initialized = administrator_exists(base)
    if initialized and not reset:
        atomic(base / 'config/admin-initialized.json', json.dumps({'initialized': True}) + '\n', 0o600)
        return False
    if not password or '\n' in password or '\r' in password:
        raise ValueError('A nonempty administrator password must be supplied through standard input')
    env = os.environ.copy()
    env.update(environment_values((base / 'config/admin.env').read_text()))
    env[DISABLE_KEY] = 'true' if initialized else 'false'
    # Grafana initializes its schema before consuming CLI stdin. Its temporary
    # bootstrap password is independent of the final password and never saved.
    if not initialized:
        env[PASSWORD_KEY] = secrets.token_urlsafe(48)
    else:
        env.pop(PASSWORD_KEY, None)
    command = [str(Path(grafana) / 'bin/grafana'), 'cli', '--homepath', str(grafana),
               '--config', str(base / 'config/grafana.ini'), 'admin',
               'reset-admin-password', '--password-from-stdin']
    result = (runner or subprocess.run)(command, input=password + '\n', text=True,
        capture_output=True, env=env, cwd=str(base / 'scripts'), timeout=90,
        user=account.pw_uid, group=account.pw_gid, extra_groups=[])
    if result.returncode:
        # CLI output can contain configuration values; do not echo it.
        raise RuntimeError('Grafana administrator initialization failed; inspect its database before retrying')
    if not administrator_exists(base):
        raise RuntimeError('Grafana CLI returned without creating the administrator')
    atomic(base / 'config/admin-initialized.json', json.dumps({'initialized': True}) + '\n', 0o600)
    return True


def harden_owned_files(base, atomic):
    """Remove legacy bootstrap entries, including owned template archives."""
    base = Path(base)
    changed = []
    for folder in ['config', 'backups', 'packages', 'scripts']:
        root = base / folder
        if not root.exists():
            continue
        for path in root.rglob('*.env'):
            if path.is_symlink():
                raise RuntimeError('Refusing credential-file symlink: ' + str(path))
            data = path.read_text()
            if PASSWORD_KEY in environment_values(data):
                atomic(path, sanitized_environment(data), 0o600)
                changed.append(path.relative_to(base).as_posix())
        for path in root.rglob('*.tar.gz'):
            # Large vendor distributions never contain generated credentials.
            if path.is_symlink() or path.stat().st_size > 32 * 1024 * 1024:
                continue
            replacements = {}
            with tarfile.open(path) as source:
                members = source.getmembers()
                for member in members:
                    if member.isfile() and member.name.endswith('.env') and member.size < 1024 * 1024:
                        data = source.extractfile(member).read().decode('utf-8')
                        if PASSWORD_KEY in environment_values(data):
                            replacements[member.name] = sanitized_environment(data).encode()
                if not replacements:
                    continue
                stage = path.with_name(path.name + '.sanitized')
                if stage.exists():
                    raise RuntimeError('Inspect previous sanitization attempt: ' + str(stage))
                import copy
                with tarfile.open(stage, 'x:gz') as target:
                    stage.chmod(0o600)
                    for member in members:
                        info = copy.copy(member)
                        if member.isfile():
                            data = replacements.get(member.name)
                            if data is None:
                                data = source.extractfile(member).read()
                            else:
                                info.size = len(data)
                                info.mode = 0o600
                            target.addfile(info, io.BytesIO(data))
                        else:
                            target.addfile(info)
            stage.replace(path)
            changed.append(path.relative_to(base).as_posix())
    data = base / 'data/grafana'
    if data.exists():
        data.chmod(0o700)
        for name in ['grafana.db', 'grafana.db-wal', 'grafana.db-shm']:
            if (data / name).exists():
                (data / name).chmod(0o600)
    return changed


def read_password(stream):
    password = stream.readline(4097).rstrip('\r\n')
    if not password or len(password) > 4096 or '\x00' in password:
        raise ValueError('Provide one nonempty password line on standard input')
    return password
