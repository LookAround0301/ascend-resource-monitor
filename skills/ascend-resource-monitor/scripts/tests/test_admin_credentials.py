import io
import json
from pathlib import Path
import sqlite3
import sys
import tarfile
import tempfile
import types
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'assets/runtime'))
import admin_credentials as credentials
from deploy_node import atomic


def database(base):
    path = base / 'data/grafana/grafana.db'
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE user (login TEXT, is_admin INTEGER, password TEXT)')
        db.execute('INSERT INTO user VALUES (?,?,?)', ('admin', 1, 'hashed-value'))
    db.close()


class CredentialTests(unittest.TestCase):
    def test_bootstrap_uses_stdin_and_never_persists_login_password(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / 'scripts').mkdir()
            credentials.prepare_security(base, atomic)
            password = 'disposable-test-password'
            def runner(command, **options):
                self.assertNotIn(password, ' '.join(command))
                self.assertEqual(options['input'], password + '\n')
                self.assertNotIn(password, options['env'].values())
                self.assertEqual(options['env'][credentials.DISABLE_KEY], 'false')
                database(base)
                return types.SimpleNamespace(returncode=0)
            account = types.SimpleNamespace(pw_uid=123, pw_gid=123)
            self.assertTrue(credentials.initialize_administrator(base, base / 'grafana', account, password, atomic, runner))
            for path in base.rglob('*'):
                if path.is_file():
                    self.assertNotIn(password.encode(), path.read_bytes())
            env = credentials.environment_values((base / 'config/admin.env').read_text())
            self.assertNotIn(credentials.PASSWORD_KEY, env)
            self.assertEqual(env[credentials.DISABLE_KEY], 'true')

    def test_existing_database_is_never_reset_by_reinstall(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            database(base)
            def forbidden(*args, **kwargs):
                self.fail('A reinstall must not reset an existing administrator')
            self.assertFalse(credentials.initialize_administrator(base, base / 'grafana', None, None, atomic, forbidden))
            (base / 'data/grafana/grafana.db').unlink()
            with self.assertRaisesRegex(RuntimeError, 'missing'):
                credentials.administrator_exists(base)

    def test_legacy_templates_and_archives_are_sanitized_without_changing_key(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            raw = 'GF_SECURITY_ADMIN_PASSWORD=old-secret\nGF_SECURITY_SECRET_KEY=unchanged\n'
            atomic(base / 'config/admin.env', raw, 0o600)
            atomic(base / 'backups/template.env', raw, 0o600)
            archive = base / 'packages/stack.tar.gz'
            archive.parent.mkdir()
            with tarfile.open(archive, 'w:gz') as out:
                data = raw.encode()
                info = tarfile.TarInfo('stack/admin.env'); info.size = len(data)
                out.addfile(info, io.BytesIO(data))
                data = b'keep other template content'
                info = tarfile.TarInfo('stack/config.ini'); info.size = len(data)
                out.addfile(info, io.BytesIO(data))
            credentials.prepare_security(base, atomic)
            changed = credentials.harden_owned_files(base, atomic)
            self.assertIn('packages/stack.tar.gz', changed)
            for path in base.rglob('*.env'):
                self.assertNotIn('old-secret', path.read_text())
                self.assertIn('GF_SECURITY_SECRET_KEY=unchanged', path.read_text())
            with tarfile.open(archive) as source:
                self.assertNotIn(b'old-secret', source.extractfile('stack/admin.env').read())
                self.assertEqual(source.extractfile('stack/config.ini').read(), b'keep other template content')
            self.assertEqual(credentials.harden_owned_files(base, atomic), [])

    def test_failed_cli_does_not_echo_sensitive_output(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            credentials.prepare_security(base, atomic)
            account = types.SimpleNamespace(pw_uid=123, pw_gid=123)
            def runner(*args, **kwargs):
                return types.SimpleNamespace(returncode=1, stdout='secret-value', stderr='secret-value')
            with self.assertRaises(RuntimeError) as error:
                credentials.initialize_administrator(base, base, account, 'secret-value', atomic, runner)
            self.assertNotIn('secret-value', str(error.exception))
            self.assertFalse((base / 'config/admin-initialized.json').exists())

    def test_existing_database_does_not_get_a_replacement_encryption_key(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            database(base)
            with self.assertRaisesRegex(RuntimeError, 'encryption key is missing'):
                credentials.prepare_security(base, atomic)

    def test_stdin_requires_one_nonempty_line(self):
        self.assertEqual(credentials.read_password(io.StringIO('keep spaces  \n')), 'keep spaces  ')
        for text in ['', '\n', 'x' * 4097]:
            with self.assertRaises(ValueError):
                credentials.read_password(io.StringIO(text))


if __name__ == '__main__':
    unittest.main()
