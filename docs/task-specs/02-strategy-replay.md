# 市场状态、策略筛选、历史回放与版本治理

状态：local-ready-for-agent

## 问题

短线候选若使用未来数据、临时阈值或覆盖旧版本，就无法复现和评估。

## 方案

使用纯函数从截至目标交易日的规范化历史快照与 v3 本地事实生成市场状态、证据和最多三个候选；
使用逐日向前回放、候选后异步结果证据和不可变版本治理。提案、认证与激活都必须由人显式发起。

## 用户故事

- 防御市场可以返回零候选，进攻市场也不得超过三个。
- 强势回踩为主策略，放量突破为独立的次策略。
- 每个候选携带版本、证据、确认和失效条件。
- 历史候选始终引用生成它的策略版本。

## 实现决策

- `v0.1-proposed` 与 v0.2 的历史记录都是不可变审计事实；不得删除、改写或把旧回放证明移作 v3 的证明。
- v3 使用规则引擎 3 和本地构建的研究事实：主板 10% 涨跌停、点时点复权收盘链、市场宽度、行业分类来源与映射哈希。行业仅作为结构化解释证据，不得直接改变候选分数、排名或数量。
- v3 候选筛选只读取目标交易日及之前的输入；候选的后续 `outcome` 是在候选证据完成后异步计算的独立审计证据，绝不反向参与该日筛选或排名。
- 治理回放是单一不可变策略版本在完整、已记录交易日历上的逐日向前处理。固定范围 `2023-08-08` 至 `2026-08-07` 需要精确覆盖 727 个交易日；前 60 个交易日仅预热，不生成候选结论，第 61 日才可产生候选证据。每个交易日的输入哈希、输出哈希、预热标记和结构化结果持久化后不可覆盖。
- 回放任务是异步的持久化作业，状态只能是 `queued`、`running`、`completed` 或 `failed`。服务重启会将中断的 `running` 任务重新排队，已持久化的日期不重跑、不改写；后台每步最多处理一个交易日。
- 数据库启动及受控 Windows 更新自动执行 Schema v10 迁移。该迁移在保留 Schema v9 历史的前提下，增加 v3 涨跌停事实、快照特征、策略关系/生命周期、回放输入与结果哈希、预热、`outcome_hash` 和候选结果表；迁移拒绝高于当前支持版本的数据库，不能通过手工降级绕过。
- `compare_strategy_versions` 是纯只读比较：同版本比较一律拒绝，且不会运行回放、写入作业或生成证明。它只能用于两个不同的不可变版本的研究对照，不能作为激活证据。
- v3 完整认证除精确覆盖 727 个交易日外，还要求候选回放已完成、异步 `outcome` 已完成且其 `outcome_hash` 已写入。认证绑定策略参数哈希、数据集哈希、结果哈希、`outcome_hash`、起止日期和会话数；证明永久保留。激活只消耗主机侧一次性批准，不删除或消费证明。

### v0.3 候选提案（不自动创建）

本轮只记录两个待人工审阅的提案模板；服务、部署脚本和 ChatGPT 都**不自动创建提案、不自动认证、不自动激活**。如用户决定创建，分别用新的、不含密钥的幂等键调用 `create_strategy_proposal`，两者都显式传入 `supersedes_version="v0.2-proposed"`。这只记录不可变的 `supersedes` 关系，不会立即改变活动版本。

| 参数 | `v0.3-policy-1` | `v0.3-policy-2` |
| --- | ---: | ---: |
| `rule_engine_version` | 3 | 3 |
| `regime_policy` | 1 | 2 |
| `offensive_min_bps` / `defensive_max_bps` | 5500 / 4000 | 5500 / 4000 |
| `neutral_pullback_limit` / `neutral_breakout_limit` | 1 / 1 | 1 / 1 |
| `offensive_pullback_limit` / `offensive_breakout_limit` | 2 / 1 | 2 / 1 |
| `min_median_amount_fen` | 5000000000 | 5000000000 |
| `liquidity_lookback_sessions` / `trend_lookback_sessions` | 20 / 60 | 20 / 60 |
| `pullback_peak_lookback_sessions` | 20 | 20 |
| `pullback_min_prior_gain_bps` / `pullback_max_drawdown_bps` / `pullback_max_amount_ratio_bps` | 1200 / 350 / 10000 | 1200 / 350 / 10000 |
| `breakout_lookback_sessions` / `breakout_amount_lookback_sessions` / `breakout_min_amount_ratio_bps` | 60 / 20 / 15000 | 60 / 20 / 15000 |
| `recent_limit_up_lookback_sessions` / `required_warmup_sessions` | 5 / 60 | 5 / 60 |

`regime_policy=1` 在进攻市使用 2 个回踩加 1 个突破配额、中性市使用 1 加 1、防御市为零；`regime_policy=2` 使用进攻市的 2 加 1 配额。两者必须分别回放和认证，不能共享证明。

## MCP 回放接口与查看方式

MCP 固定提供 `start_strategy_replay`、`get_strategy_replay`、`list_strategy_replays`、`get_strategy_replay_days` 和 `certify_strategy_replay` 五个治理回放工具。创建和认证写入都必须提供持久化幂等键；查询工具不写入数据库。

- `start_strategy_replay` 只接受一个提案版本和完整的起止日期，校验交易日历后返回已排队的作业标识；它不在请求内同步执行回放。
- `get_strategy_replay` 返回单个作业的状态、已处理会话数、下一交易日、哈希、摘要和失败原因；调用方应按需轮询，服务不会为 ChatGPT 建立连续盘中监控。
- `list_strategy_replays` 可按版本筛选，并以受限 `limit` 返回最新作业；`get_strategy_replay_days` 使用 `after_trade_date` 游标和受限 `limit` 分页返回逐日证据，避免一次读取整段历史。
- `certify_strategy_replay` 只能对 `completed` 的完整作业执行，并要求 `confirmed=true` 的显式确认。v3 的 `outcome` 未完成、认证失败、覆盖不足或证明冲突必须返回结构化错误，不能补写缺失日期或伪造结果。

## 激活门禁

顺序固定为：人工创建提案版本 → `build-v3-facts` 成功 → 运行并审阅完整单版本回放及 `outcome` → 人工认证该回放 → 管理员在主机执行一次性批准 → 用户通过 MCP 显式确认激活。任一数据缺口、事实冲突、哈希不匹配、回放/结果失败、覆盖不足、认证缺失、主机批准缺失或用户未确认均是失败门禁，必须停止并保留证据。

认证不等于激活；MCP 的 `confirmed=true` 也不能代替主机侧批准。激活时必须同时匹配不可变参数哈希、永久保留的认证证明和未消费的主机批准；当 v3 激活取代 v0.2 时，活动指针切换和 v0.2 标记为 `superseded` 必须在一个原子事务中完成。v0.2 的日报、候选、回放、认证及策略参数均历史保留，不能重新激活或被删除。

## 测试决策

- 在输入中加入未来 bar 不得改变目标日输出。
- 对同一快照重复运行必须字节级等价。
- 回放逐日裁剪输入，并比较候选数、状态和结构化证据。
- 使用固定、脱敏且带来源时间的夹具验证 Schema v10 自动迁移、v3 事实离线构建、60 个交易日预热、727 个交易日覆盖、异步状态恢复、分页游标、`outcome_hash`、认证覆盖、原子 superseded、证明不可变和同版本比较拒绝；标准测试不访问实时行情。

## 非目标

收益承诺、黑箱新闻评分、自动选参、自动激活策略。

## v0.4 预注册研究

v4 使用独立 `v4-input-v1`、`v4-result-v1`、`v4-outcome-v2`、`v4-benchmark-v1` 和 `v4-statistics-v1`，不得改写 v3 黄金输出。主 manifest 使用完整 Tushare 价格、新浪点时流通股本、BaoStock 状态和版本化行业映射；最后 25 个会话只用于确认、入场和 outcome，不产生信号。

2026-08-12 的 Sina 回填证据确认 41 只证券无法取得股本。v4 主研究允许通过版本化
治理清单显式排除这些证券，但不得把缺失事实伪装成成功。不可变 manifest 必须绑定
原始回填宇宙、纳入集、排除集、数量、覆盖率、各集合哈希、排除原因和原始回填
manifest hash。任何未获准证券缺少点时股本都必须拒绝生成研究 manifest；研究执行
只能读取已经冻结的纳入集，不得运行时动态排除。该例外不改变 Sina provider 资格、
shadow 完整性或生产 fallback 门禁。

Schema v10 历史快照没有独立的 `daily_security_status` 表。升级后可通过离线
`build-v4-status-facts` 将旧快照已经证明的 BaoStock 可交易、非 ST 资格迁移为带
推导 Schema 和批次哈希的 v11 不可变状态事实；该命令不联网、不扩充旧证券宇宙，
遇到既有事实冲突时必须原子失败。

旧快照未记录停牌证券，因此还必须运行联网、可断点续跑的
`backfill-baostock-statuses`，按持久化交易日历保存完整 `tradeStatus=0/1` 状态。
v4 manifest 门禁要求纳入证券逐日状态全覆盖；价格与状态双缺失不得推断为停牌。

研究固定一条 v0.3-policy-1 基线及六个单因素 challenger，不做网格、组合赢家或自动调参。统计固定 20 会话圆形 moving-block bootstrap、10,000 次和 White Reality Check。没有 challenger 同时通过多重检验、CI、完整性、可执行率和新浪复制门禁时，合法结论是保留 v0.3。

研究服务不得仅创建 `queued` 记录后永久搁置。当前逐日 worker 会在冻结 manifest 上按
信号日批量计算七个研究臂，并支持服务重启续跑；每步只读取本地 SQLite，不触发网络
采集。一个信号日共享市场输入，重复候选 outcome 只计算一次，同日七臂结果在一个事务中
写入。完成时数据库按 manifest 精确核对七臂信号日历和不可变统计证据。六个 challenger
必须在完整合格池上先执行单因素筛选或重排，再应用分形态配额，禁止只过滤 v0.3 已选出的
前三名。

报告规则修正不得要求重新读取行情或覆盖旧研究。受支持的窄范围修订可以只读消费已经
持久化的逐日结果，输出绑定 source study/result/day hashes 的独立 amendment；无法识别的
不完整证据仍须显式失败。统计输出必须单独给出基线绝对主指标均值，并把 challenger 的
绝对均值、相对基线的配对日差值及配对 bootstrap 区间明确命名；兼容字段
`mean_primary_bps` 仍表示配对差值，不能误读为 challenger 的绝对均值。

主研究没有独立的 Sina 价格 replication 证据时，完整运行的合法终态仍是
`retain_baseline`，不得生成 proposal。该状态表示主研究执行成功但胜出门禁未满足，不表示
Sina replication、provider 资格、策略认证或激活已经完成。
