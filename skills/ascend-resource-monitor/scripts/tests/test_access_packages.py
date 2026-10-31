import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

SKILL = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SKILL / 'scripts'))
from build_windows_package import build


def example():
    return {'id': 'lab', 'title': 'Lab monitor', 'executor_id': 'operator',
            'storage_root': '/data', 'central_node': 'a',
            'nodes': [{'name': 'a', 'host': '192.0.2.10', 'monitor_ip': '10.0.0.10'}]}


class AccessPackageTests(unittest.TestCase):
    def test_default_client_requires_no_shared_url(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / 'deployment.json'
            config.write_text(json.dumps(example()))
            archive = build('client', root / 'out', config)
            with zipfile.ZipFile(archive) as package:
                self.assertIn('client-lab/Open-Monitor.cmd', package.namelist())
                self.assertNotIn('client-lab/Open-Web.cmd', package.namelist())
                client = json.loads(package.read('client-lab/client.json'))
                self.assertNotIn('shared_url', client)
                self.assertNotIn('executor_id', client)

    def test_optional_web_entry_keeps_ssh_and_rejects_unsafe_urls(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / 'deployment.json'
            config.write_text(json.dumps(example()))
            url = 'https://192.0.2.10:8443/d/lab?refresh=5s'
            archive = build('client', root / 'out', config, shared_url=url)
            with zipfile.ZipFile(archive) as package:
                for name in ('Open-Monitor.cmd', 'Open-Web.cmd', 'open-web.ps1'):
                    self.assertIn('client-lab/' + name, package.namelist())
                self.assertEqual(json.loads(package.read('client-lab/client.json'))['shared_url'], url)
            for invalid in ('http://192.0.2.10/', 'javascript:alert(1)', 'https://user:password@192.0.2.10/',
                            'https://192.0.2.10/\n', 'https://192.0.2.10:99999/', 'https://192.0.2.10/"',
                            'https://192.0.2.10\\bad'):
                with self.subTest(url=invalid), self.assertRaises(ValueError):
                    build('client', root / 'bad', config, shared_url=invalid)
            self.assertFalse((root / 'bad').exists())
            with self.assertRaises(ValueError):
                build('wizard', root / 'wizard', shared_url=url)

    @unittest.skipUnless(os.name == 'nt' and shutil.which('powershell.exe'), 'Windows integration')
    def test_windows_dual_install_and_local_fallback_are_independent(self):
        def run(*args):
            return subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                                   *map(str, args)], capture_output=True, encoding='utf-8', errors='replace')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / 'deployment.json'
            config.write_text(json.dumps(example()))
            build('client', root / 'out', config, shared_url='https://192.0.2.10:8443/d/lab')
            folder = root / 'out/client-lab'
            self.assertEqual(run(folder / 'open-web.ps1', '-ValidateOnly').returncode, 0)
            installed = run(folder / 'install-client.ps1', '-NoLaunch', '-NoShortcut', '-DestinationRoot', root / 'installed')
            self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
            for name in ('Open-Monitor.cmd', 'Open-Web.cmd', 'open-web.ps1', 'client.json'):
                self.assertTrue((root / 'installed/lab' / name).is_file())
            client = json.loads((folder / 'client.json').read_text())
            client['shared_url'] = 'javascript:alert(1)'
            (folder / 'client.json').write_text(json.dumps(client))
            self.assertEqual(run(folder / 'open-web.ps1', '-ValidateOnly').returncode, 1)
            self.assertEqual(run(folder / 'open-monitor.ps1', '-ValidateOnly').returncode, 0)

    @unittest.skipUnless(os.name == 'nt' and shutil.which('powershell.exe'), 'Windows integration')
    def test_windows_package_function_supports_both_modes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / 'deployment.json'
            # Use normalized configuration for the standalone PowerShell helper.
            from configuration import validate
            config.write_text(json.dumps(validate(example())))
            build('wizard', root / 'templates')
            wrapper = root / 'build.ps1'
            wrapper.write_text("""param($Templates,$Config,$Destination,$SharedUrl)
. (Join-Path $Templates 'common.ps1')
New-ClientPackage (Read-JsonFile $Config) $Destination $Templates -SharedUrl $SharedUrl
""")
            for mode, url in [('local', ''), ('dual', 'https://192.0.2.10:8443/d/lab')]:
                output = root / mode
                args = ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(wrapper),
                        '-Templates', str(root / 'templates/ascend-monitor-setup'), '-Config', str(config),
                        '-Destination', str(output)]
                if url:
                    args += ['-SharedUrl', url]
                result = subprocess.run(args, capture_output=True, encoding='utf-8', errors='replace')
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertTrue((output / 'client-lab/Open-Monitor.cmd').exists())
                self.assertEqual((output / 'client-lab/Open-Web.cmd').exists(), bool(url))


if __name__ == '__main__':
    unittest.main()
