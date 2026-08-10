# MCP 工具与个人复盘闭环

状态：local-ready-for-agent

## 问题

ChatGPT 需要结构化读取日报并记录个人观察，但不能改变排名、触发交易或获得账户信息。

## 方案

以 MCP 可流式 HTTP 暴露带输入/输出模式和准确安全注解的只读、追加写入工具。
所有工具只调用应用服务；机器事实以结构化结果返回。

## 用户故事

- 可读取日报、候选、次日确认、历史和策略版本。
- 可维护命名观察列表，记录候选事件和复盘说明。
- 次日行情只有显式调用时获取，来源及时间随响应返回。
- 策略激活必须携带明确确认，不允许历史重写。
- 用户可以发起完整三年单版本回放，查看异步进度和逐日证据，完成审阅后显式认证，再完成主机批准和激活。

## 实现决策

- 使用 Python MCP SDK v2 的 `MCPServer` 和可流式 HTTP。
- HTTP 只监听 `127.0.0.1:8765/mcp`。
- 写操作采用幂等键或追加事件，删除操作标注为破坏性操作。
- 不接受余额、仓位、券商凭证、订单、任意 URL 或命令行参数。

### 策略回放固定工具

以下五个工具是固定 MCP 公共面；工具名、输入输出模式和安全注解均受契约约束，不能由 ChatGPT 扩展为任意命令或数据读取。

| 工具 | 用途与输入 | 读写边界 |
| --- | --- | --- |
| `start_strategy_replay` | 提案版本、完整回放起止日期和幂等键 | 追加/幂等写入；只创建或返回异步作业，绝不在调用中同步完成回放。 |
| `get_strategy_replay` | `replay_id` | 纯只读；返回 `queued`、`running`、`completed` 或 `failed`，以及进度、下一日期、哈希、摘要或错误。 |
| `list_strategy_replays` | 可选版本和 `limit` | 纯只读；受限分页列表，按最新作业返回。 |
| `get_strategy_replay_days` | `replay_id`、`after_trade_date` 和 `limit` | 纯只读；以日期游标分页返回已持久化的逐日输入/输出哈希、预热标记、市场状态与候选证据。 |
| `certify_strategy_replay` | `replay_id`、`confirmed=true` 和幂等键 | 追加/幂等写入；仅认证已完成且覆盖完整治理范围的作业。 |

回放后台每次最多处理一个交易日；重启会把中断的 `running` 作业恢复为 `queued`，而已保存的逐日证据不可覆盖。调用 `get_strategy_replay` 和 `get_strategy_replay_days` 是按需查看，不会触发后台行情轮询或连续监控。

数据库初始化与 Windows 更新会自动应用 Schema v9 迁移。该迁移持久化作业、日期级证据和认证记录；认证证明永久保留，包含不可变策略参数哈希、数据集哈希、结果哈希、覆盖范围和会话数。激活时只消费主机侧一次性批准，不删除证明。

`compare_strategy_versions` 是纯只读研究工具，必须传入两个不同版本；同版本比较返回结构化拒绝，且不会启动回放、写入证据或产生证明。比较输出不能替代单版本治理回放认证。

### 治理流程

1. ChatGPT 以固定日期范围调用 `start_strategy_replay`，保存返回的 `replay_id`；返回 `queued` 或 `running` 只是受理/执行状态，不是通过认证。
2. ChatGPT 按需调用 `get_strategy_replay`，并以 `after_trade_date` 和受限 `limit` 调用 `get_strategy_replay_days` 审阅分页面的完整性、失败原因、哈希和候选证据。
3. 仅当作业为 `completed` 且覆盖完整交易日历时，用户明确要求后调用 `certify_strategy_replay(confirmed=true)`；认证失败必须保留原始状态并显式报告。
4. 管理员在 Windows 主机上运行 `approve-strategy` 并再次键入版本号，取得与参数哈希绑定的一次性批准。
5. 用户再调用 `activate_strategy_version(confirmed=true)`。缺少认证、主机批准或显式确认均必须失败；成功后只更新活动版本指针。

## 测试决策

- 断言公开模式、安全注解、结构化错误、幂等和输入边界。
- 使用模拟应用服务证明读取不改写排名、次日检查不会后台轮询。
- 使用离线夹具验证五个回放工具、闭合 Schema、异步状态、`after_trade_date` 分页、认证显式确认、Schema v9 自动迁移、永久证明和同版本比较的只读拒绝。

## 非目标

公共插件、多用户授权、下单、持仓、推送和持续盯盘。
