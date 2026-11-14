---
name: a5-reservation
description: Create, query, and manage A5 NPU reservations using the user's configured employee ID and name, including agent-led task monitoring for A5-118 and A5-124.
---

# A5 预约

通过 `scripts/a5_client.py` 调用 A5 预约接口。工号和姓名由本人的本机配置或环境变量 `A5_EMPLOYEE`、`A5_NAME` 提供，系统按用户要求默认其真实；不要冒用其他人。首次使用若未配置，询问用户本人工号、姓名和连接地址，然后运行 `configure`。不要把这些配置加入代码仓库。

默认连接 `http://127.0.0.1:13000`：在个人电脑先启动现有 Open-Monitor 隧道；Agent 直接运行在 A5 中央服务器时可用 `http://127.0.0.1:3000`。其他服务器使用 SSH 隧道，或使用证书可验证的 HTTPS 入口。可通过 `A5_QUEUE_URL` 覆盖；自定义可信 CA 可用 `A5_CA_FILE`。不关闭证书校验。

- 配置：`python <skill目录>/scripts/a5_client.py configure --employee 工号 --name 姓名 --url 地址`
- 查询：`python <skill目录>/scripts/a5_client.py query`
- 共同使用人：仅预约申请人可管理。用 `shared list --job 预约编号` 查询，`shared search --job 预约编号 --query 姓名或工号` 搜索名单，`shared add --job 预约编号 --employee 工号` 添加，`shared remove --job 预约编号 --employee 工号` 删除；命令前加 `python <skill目录>/scripts/a5_client.py`。添加时客户端核对名单上的姓名，可用 `--name 姓名` 校验用户指定的姓名；搜索有多个结果时先让用户确定。操作结果未知时先查询名单核对，不要盲目重试。
- 普通预约：`python <skill目录>/scripts/a5_client.py create --machine auto --cards 2 --minutes 60`。`--machine` 可用 `auto`、`A5-118`、`A5-124`、`dual`，双机卡数为总数且平均分配。
- 夜间预约：可提前创建，增加 `--night-start "YYYY-MM-DD HH:MM"`（北京时间）；开始时间为 00:00–09:00 前，统一在当天 09:00 结束登记，最长 9 小时。避免自行猜测日期、时间、机器或卡数。普通任务单次最长 1 小时，跨午夜截短至 00:00；00:00–09:00 不开始普通任务，等待中的预约保留。
- 夜间预约的登记时长由服务端按 09:00 自动确定；如果用户要求的占用时段不同，创建前说明这一点并确认，任务可按约定提前结束登记。
- 用户明确允许短时插空时可增加 `--backfill-minutes N`，否则不启用。普通任务的避开时段沿用服务端默认值。其他修改和取消暂用预约网页或已有小鲁班功能。

用户明确要求预约并给出必要条件后，直接创建，无需小鲁班私聊确认；小鲁班照常发送业务提醒。返回任务编号、预约状态、分配卡位或队列位置；未委托 Agent 继续执行时，`ready` 提醒用户及时开始使用，预约本身不启动业务进程。

创建前客户端会保存唯一请求编号。网络中断或结果未知时，使用 `recover 编号` 查结果；需要重试时仅用 `retry 编号` 重发同一请求，不要重新执行 `create`。结果为 404 时可能仍在处理中，使用同编号 retry 可以安全重试。不要把请求失败或未知说成预约成功。

当用户要求 Agent 在预约到号后继续执行任务，或要求绑定已有预约并自动开始、延长、结束时，读取 [自动执行说明](references/agent-loop.md)。布置任务时就确认卡被他人占用和任务超时未完成时的处理；用户已经明确的选择无需重复询问。该流程由 Agent 负责任务管理；只有运行环境具备可靠的原会话唤醒能力时才能承诺无人值守执行。

Agent 创建预约成功或成功绑定已有预约后，系统会通过小鲁班私聊通知预约人；无需私聊确认。使用旧版接收程序绑定时不会触发绑定通知，请升级本 Skill。
