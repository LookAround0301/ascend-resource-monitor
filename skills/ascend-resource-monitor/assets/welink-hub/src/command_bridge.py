"""Forced SSH command; forwards only bounded A5 mobile JSON to a local API."""
import json
import os
from pathlib import Path
import sys
from urllib.error import HTTPError
from urllib.request import Request,ProxyHandler,build_opener


def main():
    if os.environ.get('SSH_ORIGINAL_COMMAND')!='a5-mobile-command':return 64
    raw=sys.stdin.buffer.read(8193)
    if len(raw)>8192:return 64
    try:
        payload=json.loads(raw)
        if not isinstance(payload,dict):return 64
        token=Path('/etc/ascend-welink-command/token').read_text().strip()
        request=Request('http://127.0.0.1:3000/a5-queue/internal/mobile-command',data=raw,
                        headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
        try:
            response=build_opener(ProxyHandler({})).open(request,timeout=10)
        except HTTPError as error:response=error
        with response:
            body=json.loads(response.read(16385))
            if not isinstance(body,dict):return 69
            print(json.dumps(dict(status=response.code,body=body),ensure_ascii=False))
        return 0
    except Exception:
        return 69


if __name__=='__main__':sys.exit(main())
