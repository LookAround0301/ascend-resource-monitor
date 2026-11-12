# Ascend Resource Monitor

**把昇腾服务器的资源、NPU 卡上进程、工号线索和工作目录集中到一个实时面板。**

[English](README.en.md) · [下载 Windows 部署向导](https://github.com/LookAround0301/ascend-resource-monitor/releases/latest/download/ascend-monitor-setup.zip) · [下载 Skill](https://github.com/LookAround0301/ascend-resource-monitor/releases/latest/download/ascend-resource-monitor-skill.zip)

面向 Linux 昇腾服务器的可复用 Skill，使用 Grafana、Prometheus、NPU Exporter 与轻量进程采集器。默认推荐本地一键脚本访问：Windows 向导完成部署验证后，生成当前环境的 SSH 隧道启动脚本和安装包。纯网页访问作为可选方式。

部署步骤与配置位置见 [监控面板使用与部署总览](docs/monitoring-guide.zh.md)，预约的完整规则见 [统一队列参考](skills/ascend-resource-monitor/references/task-queue.md)，可选手机查询和提醒见 [消息汇总服务](skills/ascend-resource-monitor/assets/welink-hub/README.md)。Release 包对应其发布版本，已有服务和已下载的访问包不会随仓库提交自动升级。

当前 main 已包含 A3 每节点 16 逻辑卡位、A5/A3 个人登录隔离、工号与姓名免密码登录、共同使用人姓名显示，以及管理员自动容器管控开关。

夜间跨日登记截止支持按部署配置到分钟，例如 `night_carryover_cutoff_hour: 9.5` 表示配置时区的 09:30，只释放登记。`missed_claim_limit: 3` 可按部署启用“累计三次未开始后取消预约”；默认不启用。消息服务和群通知也需单独配置，不会随监控部署自动开放。

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

## 统一预约：A5 8 卡与 A3 16 卡

统一队列支持**两台容量相同的节点**。在中心代理配置中设置 `task_queue.unified_queue: true`；卡位容量由 `task_queue.cards_per_node` 决定，不自动根据机器型号切换。

| 配置与行为 | A5 默认模式 | A3 16 逻辑卡位模式 |
| --- | --- | --- |
| `cards_per_node` | 省略或 `8` | 显式设为 `16` |
| 每节点卡号 / 双节点总量 | 0–7 / 16 卡 | 0–15 / 32 卡 |
| 单机申请 | 1–8 卡 | 1–16 卡 |
| 双机合计申请，平均分配 | 2 / 4 / 8 / 16 卡 | 2 / 4 / 8 / 16 / 32 卡 |
| 每节点卡组 | 两组，每组 4 卡 | 两组，每组 8 卡 |

机器可选“任意空闲单机”、指定单机或双机。指定单机不会自动转到另一台。首页逐卡展示预约登记；实际占用以 NPU 指标和进程表为准。A5 默认分配逻辑和旧请求格式保持兼容；A3 的预约、进程卡号匹配与夜间结束判断统一使用 0–15。既有 A3 配置若把两张逻辑卡合成一格，需按[迁移步骤](skills/ascend-resource-monitor/references/task-queue.md#card-capacity-and-a3-migration)备份并显式迁移，不能只改容量或把该迁移用于 A5。

### 登录与使用

1. 在首页用**工号账号和姓名**登录，例如 `a12345678` 与对应姓名；工号格式为一个字母加八位数字。统一队列不再要求个人密码：首次登录创建登记，已有工号必须匹配原姓名。该登记是自报身份，不会授予 Grafana 管理权限或加入人员白名单。
2. 开启桌面通知并通过测试，再选择机器、卡数、普通或夜间任务及预计时长。默认 30 分钟，页面按 5 分钟档位选择；每人最多保留 5 个未结束预约。
3. 普通任务分配后，在显示的领取期限内点击“开始使用”，再自行启动业务。夜间任务到达计划时间且分配到资源后自动进入使用登记，无需领取；实际任务仍需自行启动。
4. 用完先自行停止业务，再确认“结束登记”。取消和结束需确认，“开始使用”直接生效。

同一浏览器可同时用相同工号、姓名登录 A5 和 A3。个人会话按部署数据库和入口主机/端口区分，登录、退出互不覆盖，代理重启保留会话。升级后刷新页面；旧登录若已被覆盖，重新登录一次即可，原 Windows 启动器可继续使用。**这项隔离针对个人预约账号，Grafana 管理员登录独立管理。**详见[多环境登录](skills/ascend-resource-monitor/references/task-queue.md#concurrent-logins-to-multiple-deployments)。

### 排队、夜间与提醒

- **普通任务**最长 2 小时，开始和预计结束不得晚于配置时区的次日上午 09:00。无法在次日 02:00 前开始的预约转为候补，不提前占用夜间资源；02:00 前释放资源时仍可重新安排。
- **领取窗口**通常为 10 分钟，遇更早的调度截止时间会缩短。分配时及等待领取期间每分钟提醒；超时释放本次分配并向后顺延一位，保留申请时间。分配编号、顺序和期限持久化，旧分配的点击会被拒绝。
- **夜间预约**选择 20:00 至次日 09:00 前的未来开始时间，最长 13 小时，预计跨过 09:00 会提示。夜间预约可无释放缓冲衔接；前一夜间登记到期且后一预约到达计算开始时间时，可交接登记而不等待进程退出。排队和交接都不会启动、停止或抢占实际业务。
- **夜间自动结束登记**依赖 `monitor_processes`：09:00 前，至少三次新鲜、完整采集、跨越至少 20 秒确认所有分配卡无 NPU 进程后可结束。预计结束前还必须曾观察到本人或共同使用人的占用；未知卡号、过期或不完整数据不会证明空闲。早晨续用记录保留手动结束。
- **顺序与超时**：本人可将自己的等待/待领取预约在同类任务中后移一位；管理员可置顶、调高或调低等待预约，不抢占使用中的登记。瓶颈任务按适用条件在到期前 10 / 5 分钟提醒；超时阻塞者每 5 分钟提醒，其他超时者每 15 分钟提醒，超过 5 分钟后通知直接受影响的最近等待者。
- **每日 09:00 清理**由 `daily_reset` 显式开启：清理普通登记（包括使用中的），取消已过去夜间仍未开始的预约，保留已开始的夜间预约为早晨续用，账号和业务进程保留。不是删除所有夜间记录。
- **通知条件**：页面需保持打开、账号已登录且浏览器/系统允许通知；验证结果按浏览器和个人账号保存，刷新可复用，权限撤销或发送失败后需重新验证。服务端分配与领取超时不依赖页面保持打开。

### 共同使用人与实际占用核对

预约本人可在等待、待领取或使用中登记最多 32 位共同使用人，按姓名或工号搜索，列表显示“姓名 · 工号”。新增须两台节点的人员表一致且提交时再次核对；共同使用人只在该预约的分配节点、卡位范围内参与登记匹配，不获得预约管理权，也不改变人员白名单或自动管控规则。

可选 `monitor_processes` 将当前 NPU 进程与使用中的预约按工号、节点和卡号核对：未登记占用提醒本人及受影响的待领取/使用中预约者，超出登记范围且占用他人资源时提醒受影响的使用者。身份不明、卡号不明或采集过期时显示待核实。统一队列记录资源安排，不执行业务命令，也不提供硬件独占；登记核对不会自动停止容器。完整边界见[统一队列参考](skills/ascend-resource-monitor/references/task-queue.md)。

## 管理员、白名单与容器控制

| 身份或功能 | 可用操作 | 启用与权限 |
| --- | --- | --- |
| 普通查看者 | 查看资源、NPU 进程和预约状态 | 按部署的 Viewer 访问配置 |
| 个人预约账号 | 提交和领取预约、结束自己的登记、后移自己的等待预约、管理自己的共同使用人 | 工号与姓名登录；提交前验证通知 |
| Grafana 管理员 | 调整等待队列、管理人员名单、操作已部署的 Stop 与自动管控开关 | 后台校验真实 Grafana Admin 会话；个人姓名/工号不会赋予管理员权限 |

可选的 [管理员 Stop 控制](skills/ascend-resource-monitor/references/container-control.md) 在汇总表显示 Stop，点击打开确认页。后台复核当前 NPU 进程、完整容器 ID、启动时间和保护名单，记录审计日志后停止**整个容器**；Docker 最多等待 30 秒正常退出，随后可能强制终止。该能力需单独部署受控后台，基础 Windows 向导保持只读。旧版每节点队列的进程/容器停止有[独立流程](skills/ascend-resource-monitor/references/task-queue.md#legacy-per-node-reservations-and-stop)，统一预约的“结束登记”不调用该流程。监控 API 不提供任意服务器命令执行。

**当前 main 的界面限制**：启用统一队列后，原 Stop 确认链接会返回主页，没有内置的统一队列停止确认页。上述手动停止流程适用于独立容器控制或旧版每节点队列；另行接入管理员控制页的部署需要单独验证该入口，不能仅凭管理员登录就认为网页 Stop 已可用。

[人员白名单](skills/ascend-resource-monitor/references/personnel-roster.md)仅 Grafana 管理员可查看、搜索、确认增删，并同步两台主机。相同记录重试不重复写入，同工号不同姓名不覆盖；部分同步失败会指出节点。加入名单可豁免自动拦截；删除会提示可能恢复拦截，最后一条记录不能删除。普通预约用户只能通过共同使用人搜索接口查找匹配人员，不能管理完整名单。

[自动容器管控](skills/ascend-resource-monitor/references/container-guard.md)默认关闭。管理员在人员名单页确认开启“自动停止容器”，页面显示各节点实际状态；开关持久化，升级保留已有策略。启用后，当前 NPU 作业具有明确工号但未匹配姓名的容器也会纳入检查并标记、停止；标记后同一容器再次启动，即使暂未使用 NPU 也可被拦截。人员或容器白名单优先生效，历史标记保留；删除豁免可恢复拦截。名单异常、归属不明、采集过期或受保护容器不会新增容器标记。可单独配置 `guard.unknown_npu_processes: true`，对明确没有工号线索的 NPU 进程进行采样拦截，只结束目标进程；身份冲突待确认不在此范围。该选项默认关闭，依赖自动管控开关及新鲜完整采样。关闭开关停止自动执行，保留标记，不会自动启动已停止的容器。该规则独立于预约登记和管理员手动 Stop。

## 访问方式：推荐本地脚本，纯网页可选

**优先使用 `Open-Monitor.cmd`**：连接公司内网 / VPN 并完整解压访问包后，双击脚本建立 SSH 隧道并自动打开浏览器，使用期间保持终端窗口运行。需要该 PC 能连接服务器 SSH 并有登录权限；不用直接访问服务器的 HTTPS 端口。`Install.cmd` 可安装桌面快捷方式。

| 访问方式 | 本地一键脚本（推荐） | 纯网页入口（可选） |
| --- | --- | --- |
| 地址示例 | `http://127.0.0.1:13000` | `https://192.0.2.10:8443` |
| 每台 PC 的操作 | 启动脚本并保持 SSH 隧道运行 | 连接公司内网 / VPN 后打开网页 |
| 账号 | SSH 连接凭据；预约用个人登录，管理用 Grafana 登录 | 开启匿名 Viewer 时可直接查看；预约、管理分别登录 |
| 限制 | 需要 SSH 网络和登录权限 | 可能受公司代理、端口权限和证书策略限制 |

如果已部署并验证 [HTTPS 统一入口](skills/ascend-resource-monitor/references/web-entry.md)，可同时提供 `Open-Web.cmd`，让用户自行尝试直接网页访问；两种方式连接同一套监控服务。纯网页出现 `ERR_TUNNEL_CONNECTION_FAILED`、连接超时或证书策略阻止时，优先回到本地脚本，不要求每位用户修改公司代理或证书设置。

统一入口需要单独配置 HTTPS 监听地址和证书，底层 Grafana / Prometheus 仍仅监听回环地址。推荐公司信任的证书；一台 PC 验证成功不代表其他 PC 的网络和浏览器策略相同。网页入口不依赖操作者电脑运行，但仍依赖服务器、网络和浏览器允许访问。默认 Windows 向导继续交付本地脚本。

使用当前仓库的 Skill 可以请求：

```text
部署监控并生成 Windows 本地一键访问脚本，作为推荐入口。
如果已有验证过的 HTTPS 网页入口，也附上 Open-Web.cmd 供用户选择。
普通用户只读查看，启用 Stop 时需管理员登录并二次确认。
```

同时打开多个部署时，为每个部署设置不同的 `id` 和客户端 `local_port`，例如 `13000`、`13001`，并分别运行各自的访问包。仅更换端口不会隔离旧版同名 Cookie，应同时使用包含个人会话修复的代理代码。

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

双节点各采集系统、NPU、进程指标时通常有 6 个采集目标，增加队列指标后为 7 个；实际面板与目标数按配置核对。测试覆盖 NPU 进程筛选与归属、配置与解压边界、重复安装、凭据与 Stop 权限，以及 A5 兼容性、A3 16 卡规划/迁移、共同使用人、夜间预约、通知、管控开关和多环境会话。本地 Windows 检查还覆盖 PowerShell 语法、向导与凭据 ACL；POSIX 专属检查需在 Linux 执行，不能把跳过项当作通过。**存量环境验证不等于全新服务器的完整安装验收；受限代理下的全量在线下载仍需按目标环境验证。**

## 升级已有部署

1. 先备份配置、当前运行代码、SQLite 数据库（使用一致性备份）、人员表、证书和管控状态，记录服务实际使用的版本与路径。
2. 部署匹配版本的代理及所需运行模块，保留账号、预约、会话命名空间、管理员凭据和现有管控策略。A5 继续使用默认 8 卡；A3 从配对 8 格切换 16 卡必须执行[显式迁移](skills/ascend-resource-monitor/references/task-queue.md#card-capacity-and-a3-migration)，容量不一致会拒绝启动。
3. 仅按改动重启监控组件；核对采集目标、进程新鲜度、卡号、预约保留和权限。刷新浏览器；旧 A3 页面提交缺失或错误容量时会要求刷新，不会静默创建错误卡数的预约。
4. 原 SSH 启动器可用于本次个人会话修复；修改入口或客户端配置时重新生成访问包。服务器运行版本、客户端包版本和仓库 main 应分别确认。

## 开发与打包

```powershell
python -m pip install aiohttp tzdata
$env:PYTHONUTF8 = '1'
python tools/check.py
python tools/build_release.py --output dist
```

开发检查使用 Python 3.9+。完整 HTTP/会话测试依赖 `aiohttp`，没有系统时区数据库的环境（如 Windows）还需 `tzdata`；不能把跳过的 HTTP 测试当作已验证。UTF-8 模式用于正确处理中文脚本和子进程输出。Windows 下会额外检查 PowerShell 语法、向导构建和凭据权限。生成的 `dist/` 包含通用部署向导、Skill ZIP 和 `SHA256SUMS.txt`，不写入 Git。仓库还提供 `.pre-commit-config.yaml`，安装 `pre-commit` 后可执行 `pre-commit run --all-files` 运行同一套检查。

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
