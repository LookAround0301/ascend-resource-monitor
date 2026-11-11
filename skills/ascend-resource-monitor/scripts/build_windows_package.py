#!/usr/bin/env python3
"""Build a self-contained Windows deployment wizard or verified-deployment client."""
import argparse
import json
from pathlib import Path
import shutil
import sys
from urllib.parse import urlsplit
import zipfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT/'assets/runtime'))
from configuration import load, client_config

def validate_shared_url(value):
    if not isinstance(value, str) or any(c.isspace() or ord(c) < 32 or c in '\\"<>' for c in value):
        raise ValueError('Shared URL must be a plain HTTPS address without credentials')
    parsed = urlsplit(value)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise ValueError('Shared URL must use HTTPS without embedded credentials')
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError('Invalid shared HTTPS port')
    return value


def build(mode, output, config=None, software=None, shared_url=None):
    c = load(config) if config else None
    if mode == 'client' and c is None:
        raise ValueError('Client package requires --config from a verified deployment')
    if shared_url is not None:
        if mode != 'client':
            raise ValueError('--shared-url is for a verified client deployment only')
        validate_shared_url(shared_url)
    out = Path(output).resolve()
    out.mkdir(parents=True,exist_ok=True)
    name = 'ascend-monitor-setup' if mode == 'wizard' else 'client-'+c['id']
    folder = out/name
    if folder.exists():
        raise FileExistsError('Choose a fresh output directory: '+str(folder))
    folder.mkdir()
    names = ['common.ps1','open-monitor.ps1','install-client.ps1','Install.cmd','Open-Monitor.cmd']
    if mode == 'wizard':
        names += ['Setup.cmd','wizard.ps1','run-deployment.ps1','admin-credentials.ps1']
    if mode == 'wizard' or shared_url:
        names += ['Open-Web.cmd', 'open-web.ps1']
    for n in names:
        # Windows PowerShell 5.1 needs a BOM for Chinese strings.
        src = ROOT/'assets/windows'/n
        # Path.write_text gained the newline argument in Python 3.10; keep the
        # package builder usable on hosts running Python 3.9 as well.
        with (folder/n).open('w',encoding='utf-8-sig' if n.endswith('.ps1') else 'ascii',newline='\r\n') as output:
            output.write(src.read_text(encoding='utf-8-sig'))
    if mode == 'wizard':
        (folder/'runtime').mkdir()
        for src in (ROOT/'assets/runtime').glob('*.py'):
            shutil.copyfile(src,folder/'runtime'/src.name)
        manifest = Path(software) if software else ROOT/'assets/software.json'
        data = json.loads(manifest.read_text(encoding='utf-8-sig'))
        (folder/'software.json').write_text(json.dumps(data,indent=2)+'\n',encoding='utf-8')
        if c:
            (folder/'deployment.json').write_text(json.dumps(c,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        text = '解压后双击 Setup.cmd，填写服务器信息，先检查环境再开始部署。\n需要 Windows OpenSSH Client、VPN、远端 Python 3.9+ / systemd。无需在 Windows 安装 Python。\n默认软件包约 550 MB，可填写离线软件包目录。SSH 密码在终端输入，不会保存。\nGrafana 管理员密码仅保存在本机 LOCALAPPDATA/AscendMonitor/credentials 受限目录，经 SSH 标准输入初始化，服务器不保存明文密码。已有账号不会被重置。\n默认仅支持 Linux ARM64 软件配置；其他架构请更换经验证的 software.json。\n部署成功的最后一步会生成独立的 client-<部署名称>.zip，不包含管理员密码。\n'
    else:
        client = client_config(c)
        if shared_url:
            client['shared_url'] = shared_url
        (folder/'client.json').write_text(json.dumps(client,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        text = '解压后双击 Install.cmd，安装到当前用户目录并创建桌面快捷方式。\n连接 VPN 后运行桌面图标，保持 SSH 终端打开。关闭终端即断开隧道。\n直接双击 Open-Monitor.cmd 也可以使用，无需安装。\n使用本机默认 SSH 密钥，或在 SSH 终端输入密码；包内不包含私钥或密码。\n服务器监控服务开机自启，本机隧道需要重新打开。\n'
        text = '推荐方式：Open-Monitor.cmd，本地一键脚本通过 SSH 隧道打开监控。\n需要此 PC 能连接服务器 SSH，并具有可用的 SSH 登录权限。\n' + text
        if shared_url:
            text += '\n可选方式：Open-Web.cmd，直接打开已部署的 HTTPS 网页，不建立 SSH 隧道。\n纯网页可能受公司代理、端口权限和证书策略限制；若打不开，请使用推荐的 Open-Monitor.cmd。\n网页按钮不会修改代理设置或安装证书，两种入口访问同一套监控服务。\n'
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
    p.add_argument('--shared-url',help='Optional verified HTTPS dashboard URL; adds Open-Web.cmd while retaining the recommended SSH launcher')
    p.add_argument('--output',required=True)
    a=p.parse_args()
    print(build(a.mode,a.output,a.config,a.software,a.shared_url))

if __name__=='__main__':
    main()
