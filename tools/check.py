"""Validate this repository with only standard-library Python dependencies."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / 'skills/ascend-resource-monitor'

def main():
    for path in SKILL.rglob('*.py'):
        compile(path.read_text(encoding='utf-8-sig'), str(path), 'exec')
    for path in ROOT.rglob('*.json'):
        if not any(part in {'.git','dist','.venv'} for part in path.parts):
            json.loads(path.read_text(encoding='utf-8-sig'))
    subprocess.run([sys.executable,'-m','unittest','discover','-s',str(SKILL/'scripts/tests'),'-v'],check=True,cwd=ROOT)
    with tempfile.TemporaryDirectory(prefix='ascend-monitor-check-') as temporary:
        builder = str(SKILL/'scripts/build_windows_package.py')
        subprocess.run([sys.executable,builder,'--mode','wizard','--output',temporary],check=True)
        subprocess.run([sys.executable,builder,'--mode','client','--config',str(ROOT/'examples/deployment.example.json'),'--output',temporary],check=True)
        if os.name == 'nt' and shutil.which('powershell.exe'):
            wizard = Path(temporary)/'ascend-monitor-setup/wizard.ps1'
            subprocess.run(['powershell.exe','-NoProfile','-STA','-ExecutionPolicy','Bypass','-File',str(wizard),'-SelfTest'],check=True)
            # Paths are parameters rather than interpolated PowerShell source.
            subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(ROOT/'tools/check-powershell.ps1'),'-Directory',str(wizard.parent)],check=True)
            subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(ROOT/'tools/test-credentials.ps1'),'-WindowsAssets',str(wizard.parent)],check=True)
    print('Repository checks passed')

if __name__ == '__main__':
    main()
