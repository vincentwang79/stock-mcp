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
- 用户可以人工创建 v0.3 提案，发起完整三年单版本回放，查看异步进度、逐日证据和候选后的 outcome，完成审阅后显式认证，再完成主机批准和激活。

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

数据库初始化与 Windows 更新会自动应用 Schema v10 迁移。该迁移在保留旧记录的前提下持久化 v3 事实、作业、日期级输入/输出哈希、60 个交易日预热、候选 `outcome`、`outcome_hash`、策略关系和生命周期；认证证明永久保留，包含不可变策略参数哈希、数据集哈希、结果哈希、`outcome_hash`、覆盖范围和会话数。激活时只消费主机侧一次性批准，不删除证明。

`compare_strategy_versions` 是纯只读研究工具，必须传入两个不同版本；同版本比较返回结构化拒绝，且不会启动回放、写入证据或产生证明。比较输出不能替代单版本治理回放认证。

### 治理流程

1. 用户明确决定后，ChatGPT 才可用 `create_strategy_proposal` 创建 `v0.3-policy-1` 或 `v0.3-policy-2`；每个请求包含完整参数、理由、幂等键和 `supersedes_version="v0.2-proposed"`。本轮不自动创建提案、不自动认证、不自动激活。
2. 仅当 Windows 主机已成功运行离线 `build-v3-facts` 后，ChatGPT 才以固定日期范围 `2023-08-08` 至 `2026-08-07` 调用 `start_strategy_replay`。这必须覆盖 727 个交易日，前 60 个交易日只是预热；返回 `queued` 或 `running` 只是受理/执行状态，不是通过认证。
3. ChatGPT 按需调用 `get_strategy_replay`，并以 `after_trade_date` 和受限 `limit` 调用 `get_strategy_replay_days` 审阅分页面的完整性、失败原因、哈希、预热标记、候选证据和 outcome 状态；不得建立持续轮询。
4. 仅当候选作业为 `completed`、727 个交易日完整、异步 outcome 已完成并存在 `outcome_hash` 时，用户明确要求后调用 `certify_strategy_replay(confirmed=true)`；任何事实缺口、失败或认证冲突均必须保留原始状态并显式报告。
5. 管理员在 Windows 主机上运行 `approve-strategy` 并再次键入版本号，取得与参数哈希绑定的一次性批准。
6. 用户再调用 `activate_strategy_version(confirmed=true)`。缺少认证、outcome、主机批准或显式确认均必须失败；成功时原子切换活动版本并将被取代的 v0.2 标记为 `superseded`，历史记录保留。

## 测试决策

- 断言公开模式、安全注解、结构化错误、幂等和输入边界。
- 使用模拟应用服务证明读取不改写排名、次日检查不会后台轮询。
- 使用离线夹具验证五个回放工具、闭合 Schema、异步状态、`after_trade_date` 分页、认证显式确认、Schema v10 自动迁移、60 个交易日预热、outcome 门禁、永久证明、原子 superseded 和同版本比较的只读拒绝。

## 非目标

公共插件、多用户授权、下单、持仓、推送和持续盯盘。

## Provider 与 v4 研究工具

Schema v11 增加只读 `get_provider_qualification`，以及必须消费主机一次性批准的 `activate_provider_source`。数据源激活只把已认证能力登记到 provider registry，供以后兼容 Sina 的 v4 生产组合读取；当前 v0.3 仍固定忽略 Sina，因此登记不会改变当前日报。数据源激活和策略激活是两个独立事务；MCP 的 `confirmed=true` 不能代替主机批准。

v4 研究公开 `start_v4_research`、`get_v4_research`、`get_v4_research_arms`、`get_v4_research_days` 和 `get_v4_research_report`。读取接口不写数据库；完整逐日执行器尚未接入时，启动必须返回 `v4_research_rejected`，不得留下不会运行的排队作业。执行器和 Sina replication 门禁以后接入后，启动也只允许创建持久研究作业，不联网采集；研究成功最多生成不可变 proposal artifact，不直接写 `strategy_versions`，也不认证或激活。
