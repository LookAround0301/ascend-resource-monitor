# Ascend Resource Monitor

**把昇腾服务器的资源、NPU 卡上进程、工号线索和工作目录集中到一个实时面板。**

[English](README.en.md) · [下载 Windows 部署向导](https://github.com/LookAround0301/ascend-resource-monitor/releases/latest/download/ascend-monitor-setup.zip) · [下载 Skill](https://github.com/LookAround0301/ascend-resource-monitor/releases/latest/download/ascend-resource-monitor-skill.zip)

面向 Linux 昇腾服务器的可复用 Skill，使用 Grafana、Prometheus、NPU Exporter 与轻量进程采集器。提供原生 Windows 部署向导，部署验证后生成当前环境的一键访问安装包。

## 能看到什么

- CPU、内存、磁盘、共享盘、网络流量，以及逐卡 NPU 利用率、HBM、温度和功耗。
- **仅 NPU 卡上的进程**：PID、运行用户、工号线索、容器、进程目录和宿主机映射目录。
- 工号依据明确标注；无法识别显示“未知”，线索冲突显示“待确认”。共享 root 用户不等于真实操作者，同目录不等于同一任务。
- 多节点筛选，资源指标默认每 5 秒刷新，进程默认每 10 秒采集。

可选的 [管理员 Stop 控制](skills/ascend-resource-monitor/references/container-control.md) 可在汇总表中添加带确认框的容器停止按钮。需要单独部署受控后台并接入 Grafana 登录；默认 Windows 安装流程仍保持只读。

## 作为 Skill 使用

可以把下面这句话交给支持 Skill 安装的 Agent：

```text
帮我安装这个 Skill：https://github.com/LookAround0301/ascend-resource-monitor
Skill 目录是 skills/ascend-resource-monitor。
```

安装后使用 `$ascend-resource-monitor`，例如：

```text
帮我给两台昇腾服务器部署资源监控。先收集必要的连接和存储信息，
只显示 NPU 上的进程，最后生成 Windows 一键访问安装包。
```

或者下载 [Skill ZIP](https://github.com/LookAround0301/ascend-resource-monitor/releases/latest/download/ascend-resource-monitor-skill.zip)，把其中的 `ascend-resource-monitor` 目录放入客户端的 Skill 目录。

## Windows 部署向导

1. 下载并解压 [ascend-monitor-setup.zip](https://github.com/LookAround0301/ascend-resource-monitor/releases/latest/download/ascend-monitor-setup.zip)，双击 `Setup.cmd`。
2. 填写服务器地址、SSH 用户/端口、监控内网 IP，并选择运行 Grafana/Prometheus 的中心节点。
3. 填写执行者标识、已有数据卷目录和访问端口；可以选择本机 SSH 私钥或离线软件包目录。
4. 点击“检查环境”，确认输出与硬件匹配后点击“开始部署”。
5. 验证成功后，向导生成 `client-<部署名称>.zip`。接收者解压并双击 `Install.cmd`，即可安装桌面访问快捷方式。

Windows 需要 PowerShell 5.1 和 OpenSSH Client，**不需要安装 Python**。服务器需要 Python 3.9+、systemd、可用的 NPU 驱动及 root 或 `sudo -n` 权限。需要先连接相应 VPN/网络。

密码在 OpenSSH 终端中输入，不保存到配置或安装包。SSH 私钥留在当前 PC。访问工具使用 SSH 隧道，Grafana 和 Prometheus 默认仅监听服务器回环地址。打开面板时需保持隧道窗口运行。

## 支持范围与体积

默认软件配置为 **Linux ARM64**，沿用 Ascend 950DT / openEuler 24.03 上验证过的组件组合：NPU Exporter 26.2.0、Prometheus 3.15.1、Grafana 13.2.4；需要安装系统采集器时使用 node_exporter 1.12.1。其他硬件、系统和架构需先核对兼容性并替换 `software.json`。

Skill 压缩包约 **47 KiB**，Windows 向导约 **32 KiB**；具体以 Release 文件为准。仓库和 ZIP **不包含监控软件二进制、模型权重、密码、私钥或实际服务器配置**。软件包在部署时下载并校验 SHA-256，典型全量下载约 550 MB，也支持提前准备离线文件。

现有监控验证过 20 个面板和双机 6 个采集目标。可复用工具有 19 项本地测试，覆盖 NPU 进程筛选、归属、配置、解压边界及安装/重复安装行为；Windows 语法和向导构建已检查。**没有完成对全新服务器的完整安装验收，也没有在受限代理下完成全量在线下载验收。**

## 开发与打包

```powershell
python tools/check.py
python tools/build_release.py --output dist
```

校验仅依赖 Python 标准库；Windows 下会额外检查 PowerShell 语法和向导构建。生成的 `dist/` 包含通用部署向导、Skill ZIP 和 `SHA256SUMS.txt`，不写入 Git。

```text
skills/ascend-resource-monitor/
  SKILL.md             Skill 工作流
  assets/runtime/      Linux 采集、部署、校验与面板
  assets/windows/      Windows 向导、安装与访问脚本
  assets/software.json 固定版本及校验值
  references/          部署、配置、归属与故障说明
  scripts/             打包工具与测试
examples/              文档示例配置，使用示例地址
tools/                 仓库检查和 Release 打包
```

详细操作见 [部署说明](skills/ascend-resource-monitor/references/deployment.md) 和 [Windows 工具说明](skills/ascend-resource-monitor/references/windows.md)。已有监控安装会被检查；不会覆盖其他部署的服务、公钥、驱动或业务容器。
