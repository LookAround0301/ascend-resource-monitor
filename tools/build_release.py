"""Build credential-free generic release archives and SHA256SUMS.txt."""
import argparse
import hashlib
from pathlib import Path
import subprocess
import sys
import zipfile

ROOT=Path(__file__).resolve().parent.parent
SKILL=ROOT/'skills/ascend-resource-monitor'

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',default='dist')
    a=p.parse_args()
    out=Path(a.output).resolve()
    out.mkdir(parents=True,exist_ok=True)
    subprocess.run([sys.executable,str(SKILL/'scripts/build_windows_package.py'),'--mode','wizard','--output',str(out)],check=True)
    skill_zip=out/'ascend-resource-monitor-skill.zip'
    with zipfile.ZipFile(skill_zip,'w',zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(SKILL.rglob('*')):
            if path.is_file() and '__pycache__' not in path.parts and path.suffix!='.pyc':
                archive.write(path,'ascend-resource-monitor/'+path.relative_to(SKILL).as_posix())
    records=[]
    for path in (out/'ascend-monitor-setup.zip',skill_zip):
        with zipfile.ZipFile(path) as archive:
            if archive.testzip() is not None:
                raise RuntimeError('Invalid archive: '+path.name)
        sha=hashlib.sha256(path.read_bytes()).hexdigest()
        records.append(sha+'  '+path.name)
        print(f'{path.name}: {path.stat().st_size} bytes ({path.stat().st_size/1024:.1f} KiB)')
    (out/'SHA256SUMS.txt').write_text('\n'.join(records)+'\n',encoding='ascii')

if __name__=='__main__':
    main()
