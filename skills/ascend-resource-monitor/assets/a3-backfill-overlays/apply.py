"""Apply a site overlay to an exact runtime copy; never changes site configuration."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('cluster', choices=('103','140'))
    parser.add_argument('runtime', type=Path, help='copy of the matching site runtime directory')
    args=parser.parse_args()
    root=Path(__file__).resolve().parent
    runtime=args.runtime.resolve()
    manifest=json.loads((root/('a3-'+args.cluster+'-sha256.json')).read_text(encoding='utf-8'))
    patch=root/('a3-'+args.cluster+'-backfill.patch')
    originals={name:(runtime/name).read_bytes() for name in manifest}
    sha=lambda data:hashlib.sha256(data).hexdigest()
    for name,entry in manifest.items():
        if sha(originals[name])!=entry['old']:
            raise SystemExit('Baseline differs: '+name)
    with tempfile.TemporaryDirectory() as temp:
        normalized=Path(temp)/'site.patch'
        normalized.write_bytes(patch.read_bytes().replace(b'\r\n',b'\n'))
        command=['git','apply','--unidiff-zero',str(normalized)]
        subprocess.run(command[:2]+['--check']+command[2:],cwd=runtime,check=True)
        try:
            subprocess.run(command,cwd=runtime,check=True)
            for name,entry in manifest.items():
                data=(runtime/name).read_bytes().replace(b'\r\n',b'\n')
                if entry['line_endings']=='crlf':data=data.replace(b'\n',b'\r\n')
                if sha(data)!=entry['new']:raise RuntimeError('Unexpected candidate hash: '+name)
                (runtime/name).write_bytes(data)
        except BaseException:
            for name,data in originals.items():(runtime/name).write_bytes(data)
            raise
    print('Overlay applied to runtime copy; hashes match the deployed candidate.')


if __name__=='__main__':main()
