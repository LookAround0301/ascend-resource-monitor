#!/usr/bin/env python3
"""Build a self-contained Windows deployment wizard or verified-deployment client."""
import argparse
import json
from pathlib import Path
import shutil
import sys
import zipfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT/'assets/runtime'))
from configuration import load, client_config

def build(mode, output, config=None, software=None):
    c = load(config) if config else None
    if mode == 'client' and c is None:
        raise ValueError('Client package requires --config from a verified deployment')
    out = Path(output).resolve()
    out.mkdir(parents=True,exist_ok=True)
    name = 'ascend-monitor-setup' if mode == 'wizard' else 'client-'+c['id']
    folder = out/name
    if folder.exists():
        raise FileExistsError('Choose a fresh output directory: '+str(folder))
    folder.mkdir()
    names = ['common.ps1','open-monitor.ps1','install-client.ps1','Install.cmd','Open-Monitor.cmd']
    if mode == 'wizard':
        names += ['Setup.cmd','wizard.ps1','run-deployment.ps1']
    for n in names:
        # Windows PowerShell 5.1 needs a BOM for Chinese strings.
        src = ROOT/'assets/windows'/n
        (folder/n).write_text(src.read_text(encoding='utf-8-sig'),encoding='utf-8-sig' if n.endswith('.ps1') else 'ascii',newline='\r\n')
    if mode == 'wizard':
        (folder/'runtime').mkdir()
        for src in (ROOT/'assets/runtime').glob('*.py'):
            shutil.copyfile(src,folder/'runtime'/src.name)
        manifest = Path(software) if software else ROOT/'assets/software.json'
        data = json.loads(manifest.read_text(encoding='utf-8-sig'))
        (folder/'software.json').write_text(json.dumps(data,indent=2)+'\n',encoding='utf-8')
        if c:
            (folder/'deployment.json').write_text(json.dumps(c,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        text = '解压后双击 Setup.cmd，填写服务器信息，先检查环境再开始部署。\n需要 Windows OpenSSH Client、VPN、远端 Python 3.9+ / systemd。无需在 Windows 安装 Python。\n默认软件包约 550 MB，可填写离线软件包目录。密码在 SSH 终端输入，不会保存。\n默认仅支持 Linux ARM64 软件配置；其他架构请更换经验证的 software.json。\n部署成功的最后一步会生成独立的 client-<部署名称>.zip。\n'
    else:
        (folder/'client.json').write_text(json.dumps(client_config(c),ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        text = '解压后双击 Install.cmd，安装到当前用户目录并创建桌面快捷方式。\n连接 VPN 后运行桌面图标，保持 SSH 终端打开。关闭终端即断开隧道。\n直接双击 Open-Monitor.cmd 也可以使用，无需安装。\n使用本机默认 SSH 密钥，或在 SSH 终端输入密码；包内不包含私钥或密码。\n服务器监控服务开机自启，本机隧道需要重新打开。\n'
    (folder/'使用说明.txt').write_text(text,encoding='utf-8-sig')
    archive=out/(name+'.zip')
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
        for p in sorted(folder.rglob('*')):
            if p.is_file():
                z.write(p,p.relative_to(out).as_posix())
    return archive

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--mode',choices=['wizard','client'],default='wizard')
    p.add_argument('--config',help='Validated deployment JSON; no passwords/private keys')
    p.add_argument('--software',help='Override verified software manifest for the wizard')
    p.add_argument('--output',required=True)
    a=p.parse_args()
    print(build(a.mode,a.output,a.config,a.software))

if __name__=='__main__':
    main()
