---
name: a3-reservation
description: Create, query, and manage reservations on either A3 101/103 or A3 139/140, including authorized agent task monitoring and shared users.
---

# A3 预约

使用 `scripts/a3_client.py` 操作用户指定的 A3 队列。先确认是 101/103 还是 139/140，配置该队列的可信 HTTPS 地址或 SSH 隧道本机地址；不要沿用 A5 配置或猜测隧道端口。在对应中央服务器上运行时，101/103 可用 `http://127.0.0.1:3002`，139/140 可用 `http://127.0.0.1:3000`。工号、姓名和连接配置保存在 `~/.a3-reservation`，不要加入代码仓库。首次运行 `python <skill目录>/scripts/a3_client.py configure --employee 工号 --name 姓名 --url 地址`；103 队列还支持其既有扩展工号格式。系统按用户要求信任本人提供的身份，不要冒用他人。

- `query` 查询本人或共同使用人的预约及实际节点名；每台 16 卡，双机最多 32 卡。`create --machine 节点名 --cards 数量 --minutes 分钟` 创建，机器也可为 `auto` 或 `dual`。普通任务最长 120 分钟。
- 夜间使用 `--night-start "YYYY-MM-DD HH:MM"`，默认按北京时间解析。101/103 开始时间为北京时间 02:00–09:00 前（莫斯科前一日 21:00–当日 04:00 前）；139/140 为 00:00–09:00 前。夜间最长 9 小时，超过北京时间 09:00 截短。普通任务分别在 02:00 或 00:00 截短；夜间时段普通任务等待，不挤占夜间预约。
- 共同使用人仅由申请人管理：`shared list --job 编号`；`shared search --job 编号 --query 姓名或工号`；`shared add --job 编号 --employee 工号`，可加 `--name 姓名` 校验；`shared remove --job 编号 --employee 工号`。搜索有歧义时请用户选择。网络结果不明确时先查询名单。
- 上述命令前加 `python <skill目录>/scripts/a3_client.py`。创建及开始、延长、结束的结果不明时按原请求编号 `recover`/`retry` 或 `action-retry` 核对，不要新建操作。

Agent 创建或绑定成功后，小鲁班异步提示预约人，无需私聊确认。布置任务时确认卡被他人占用和到期未完成的处理；读取 [自动执行说明](references/agent-loop.md) 完成绑定和事件处理。101/103 不自动停止容器，资源被占用时须等待或按用户授权协调。登记结束不等于停止业务进程。Codex 桌面会话适配已做隔离测试，其他 Agent 的会话唤醒须按实际环境验证。
