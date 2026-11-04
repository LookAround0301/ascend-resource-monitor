# Ascend Resource Monitor

**把昇腾服务器的资源、NPU 卡上进程、工号线索和工作目录集中到一个实时面板。**

[English](README.en.md) · [下载 Windows 部署向导](https://github.com/LookAround0301/ascend-resource-monitor/releases/latest/download/ascend-monitor-setup.zip) · [下载 Skill](https://github.com/LookAround0301/ascend-resource-monitor/releases/latest/download/ascend-resource-monitor-skill.zip)

面向 Linux 昇腾服务器的可复用 Skill，使用 Grafana、Prometheus、NPU Exporter 与轻量进程采集器。默认推荐本地一键脚本访问：Windows 向导完成部署验证后，生成当前环境的 SSH 隧道启动脚本和安装包。纯网页访问作为可选方式。

当前完整功能、配置位置、部署步骤与本轮优化见 [监控面板使用与部署总览](docs/monitoring-guide.zh.md)。

## 能看到什么

- CPU、内存、磁盘、共享盘、网络流量，以及逐卡 NPU 利用率、HBM、温度和功耗。
- **仅 NPU 卡上的进程**：PID、运行用户、工号线索、容器、进程目录和宿主机映射目录。
- 工号依据明确标注；无法识别显示“未知”，线索冲突显示“待确认”。共享 root 用户不等于真实操作者，同目录不等于同一任务。
- 多节点筛选，页面支持 1 / 5 / 10 / 30 秒刷新，默认 1 秒；资源抓取通常为 5 秒，进程默认每 10 秒采集。
- 按工号、目录、主机和容器汇总 NPU 进程；容器内目录映射到宿主机挂载目录。
- 汇总表显示当前 NPU 进程的使用时长，从本组最早启动的进程计时；全部结束后移除，新进程重新计时，不累计上次时长。
- 汇总表用“作业来源”替代 RSS：Agent 作业、人工作业、疑似 Agent、未知、混合/待确认。明确分类依赖启动标记，进程链只作线索；没有 Agent 线索不等于人工。参见 [来源标记方法](skills/ascend-resource-monitor/references/job-origin.md)。
- 针对当前 NPU 作业读取容器内关联启动脚本和记录，支持多层启动命令；匹配 PID 与启动时间，避免把旧记录或同容器其他任务当成本次作业。定期检查状态仅作辅助，脚本正文和敏感参数不展示。
- 可从用户提供的名单按工号显示姓名，未匹配时留空；名单保存在私有部署配置中。
- 管理员密码仅保存在操作者本机的受限目录；服务器不保留明文管理员密码，重复部署不会重置已有账号。

可选的 [管理员 Stop 控制](skills/ascend-resource-monitor/references/container-control.md) 在汇总表显示 Stop，点击后打开容器确认页；只有 Grafana 管理员才能提交停止。停止的是该行对应的整个容器，最多等待 30 秒正常退出。后台校验当前 NPU 进程、完整容器 ID 和启动时间，并记录操作日志；页面显示当前登录账号与管理员/只读状态。需要单独部署受控后台并接入 Grafana 登录；默认 Windows 安装流程仍保持只读。

可选的[自动容器管控](skills/ascend-resource-monitor/references/container-guard.md)：经明确启用后，对有工号但姓名为空的 NPU 作业容器自动标记、停止，并持续拦截同一容器重启；管理员可确认加入白名单解除拦截。默认关闭，姓名表/采集异常时暂停新增标记。

可选的[统一资源队列](docs/monitoring-guide.zh.md#统一队列规则)将两台各 8 卡主机集中预约。机器可选任意空闲单机、指定单机或双机；单机 1–8 卡，双机合计 2 / 4 / 8 / 16 卡并平均分配。小时配合 5 分钟档位输入时长，普通任务最长 2 小时；新预约的开始和预计结束不得晚于次日上午 09:00。

资源分配后 10 分钟内直接点击“开始使用”，无二次弹窗。分配时及第 2、4、6、8、9 分钟提醒；超时未开始则释放本次分配，预约向后顺延一位，保留原申请时间。截止时间、排队顺序和分配编号在服务端保存，刷新或重启代理不重置。开始使用后申请时间仍可见。

用完先自行停止任务，再确认“结束登记”。统一队列只登记和分配资源，不启动或停止业务进程，不是硬件隔离系统。旧版每节点独立队列和网页 Stop 仍有[单独参考](skills/ascend-resource-monitor/references/task-queue.md)，其停止流程不适用于当前统一队列。

人员白名单入口显示已有工号和姓名，支持搜索。仅 Grafana 管理员可查看、添加和删除人员，并同步到两台主机的人员表；普通用户不可访问该名单。重复添加相同记录不会重复写入，不覆盖同工号的不同姓名。白名单人员免于自动拦截，历史标记保留；“持续拦截”表只显示仍受管控的容器。详见[人员名单管理](skills/ascend-resource-monitor/references/personnel-roster.md)。

## 访问方式：推荐本地脚本，纯网页可选

**优先使用 `Open-Monitor.cmd`**：连接公司内网 / VPN 并完整解压访问包后，双击脚本建立 SSH 隧道并自动打开浏览器，使用期间保持终端窗口运行。需要该 PC 能连接服务器 SSH 并有登录权限；不用直接访问服务器的 HTTPS 端口。`Install.cmd` 可安装桌面快捷方式。

| 访问方式 | 本地一键脚本（推荐） | 纯网页入口（可选） |
| --- | --- | --- |
| 地址示例 | `http://127.0.0.1:13000` | `https://192.0.2.10:8443` |
| 每台 PC 的操作 | 启动脚本并保持 SSH 隧道运行 | 连接公司内网 / VPN 后打开网页 |
| 账号 | SSH 连接凭据；Stop 另需管理员登录 | 查看免登录；Stop 需管理员登录 |
| 限制 | 需要 SSH 网络和登录权限 | 可能受公司代理、端口权限和证书策略限制 |

如果已部署并验证 [HTTPS 统一入口](skills/ascend-resource-monitor/references/web-entry.md)，可同时提供 `Open-Web.cmd`，让用户自行尝试直接网页访问；两种方式连接同一套监控服务。纯网页出现 `ERR_TUNNEL_CONNECTION_FAILED`、连接超时或证书策略阻止时，优先回到本地脚本，不要求每位用户修改公司代理或证书设置。

统一入口需要单独配置 HTTPS 监听地址和证书，底层 Grafana / Prometheus 仍仅监听回环地址。推荐公司信任的证书；一台 PC 验证成功不代表其他 PC 的网络和浏览器策略相同。网页入口不依赖操作者电脑运行，但仍依赖服务器、网络和浏览器允许访问。默认 Windows 向导继续交付本地脚本。

使用当前仓库的 Skill 可以请求：

```text
部署监控并生成 Windows 本地一键访问脚本，作为推荐入口。
如果已有验证过的 HTTPS 网页入口，也附上 Open-Web.cmd 供用户选择。
普通用户只读查看，启用 Stop 时需管理员登录并二次确认。
```

本文以当前源码为准；Release 包以其对应版本为准，已下载的旧访问包不会自动更新。

统一队列需在已有代理配置中设置 `task_queue.unified_queue: true`，首页显示“排队与资源分配”。Windows 基础向导不会默认开启队列、白名单写入或自动容器管控；详见[扩展部署步骤](docs/monitoring-guide.zh.md#统一队列白名单与管控扩展)。

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

SSH 密码在 OpenSSH 终端中输入，不保存到配置或安装包。Grafana 管理员密码在本机生成，保存于 `%LOCALAPPDATA%\AscendMonitor\credentials\<部署名称>.json`，仅当前用户与 SYSTEM 可读。已有账号复用本机记录，缺失时安全询问原密码；密码通过标准输入完成初始化，服务器只保留密码哈希和必要的加密密钥。SSH 私钥留在当前 PC，访问安装包不携带密码。详见[密码加固流程](skills/ascend-resource-monitor/references/credentials.md)。

访问工具使用 SSH 隧道，Grafana 和 Prometheus 默认仅监听服务器回环地址。打开面板时需保持隧道窗口运行。其他 PC 需建立自己的隧道并独立登录管理员；服务器服务开机自启，PC 或网络断开后重新运行快捷方式即可。

## 支持范围与体积

默认软件配置为 **Linux ARM64**，沿用 Ascend 950DT / openEuler 24.03 上验证过的组件组合：NPU Exporter 26.2.0、Prometheus 3.15.1、Grafana 13.2.4；需要安装系统采集器时使用 node_exporter 1.12.1。其他硬件、系统和架构需先核对兼容性并替换 `software.json`。

Skill 与 Windows 向导主要包含轻量文本和脚本，体积以构建结果和 Release 文件为准。仓库和 ZIP **不包含监控软件二进制、模型权重、密码、私钥或实际服务器配置**。软件包在部署时下载并校验 SHA-256，典型全量下载约 550 MB，也支持提前准备离线文件。

基础监控验证过 20 个面板和双机 6 个采集目标；启用队列指标后，本轮存量部署验证过 7 个采集目标及统一队列、白名单和持续拦截界面。本地测试覆盖 NPU 进程筛选、归属、配置、解压边界、安装/重复安装、凭据保护和 Stop 权限；另检查 Windows 语法、向导、本机凭据 ACL，并使用隔离 Grafana 实例验证真实初始化和登录。**没有完成对全新服务器的完整安装验收，也没有在受限代理下完成全量在线下载验收。**

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
