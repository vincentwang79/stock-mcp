# Research Program v5：跨机制研究与独立验证

## 目标与非目标

Research Program v5 扩大沪深主板候选研究范围，但不把“测试更多规则”误当作能力提升。
它首先建立不可删除的终身试验账本，再把探索、独立确认、跨源复制和策略治理分开。

- 当前 `v0.3-policy-1` 保持不变；`retain_baseline` 不表示基线本身优秀。
- `no-recent-limit-up-v1` 冻结为待独立验证假设，不是策略 proposal。
- `2023-08-08` 至 `2026-08-07` 已用于假设选择，标记为
  `discovery_exhausted`，不得重新包装为样本外证据。
- 本计划不创建、认证、批准或激活任何新策略，不接入交易、账户、仓位或盘中监控。

## 研究依据

在线研究显示，中国 A 股的价值、规模和盈利能力具有相对明确的研究基础，而传统价格
动量并不能直接从成熟市场移植。大规模 A 股异常复现中，多数候选变量未产生显著价差，
因此所有失败与未通过项目也必须永久计入试验次数。

主要参考：

- [Replicating and Digesting Anomalies in the Chinese A-share Market](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4365416)
- [Anomalies in Chinese A-Shares](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2955144)
- [Size and Value in China](https://hub.hku.hk/handle/10722/273695)
- [Q-theory, Mispricing, and Profitability Premium: Evidence from China](https://doi.org/10.1016/j.jbankfin.2017.10.001)
- [Investor attention, aggregate limit-hits, and stock returns](https://doi.org/10.1016/j.irfa.2022.102142)
- [A Reality Check for Data Snooping](https://doi.org/10.1111/1468-0262.00152)
- [Stepwise Multiple Testing as Formalized Data Snooping](https://doi.org/10.1111/j.1468-0262.2005.00615.x)

## 四层证据

1. **探索**：可以使用已耗尽的三年数据理解方向、机制和数据质量；不能晋级。
2. **独立确认**：只能使用以后取得的更长历史，或从 `2026-08-08` 开始积累的冻结前向
   数据。结果窗口重叠时必须 purge/embargo。
3. **跨源复制**：使用完整同源 Sina 价格验证数据源稳健性；不能代替独立时间样本。
4. **治理**：只有前述门禁通过后，才可以另行创建 proposal 并进入回放、认证和激活流程。

## 终身试验账本

Schema v12 首先增加不可变研究事实；Schema v13 将前向事实纠正为证券级主键并增加成熟
结果：

- `research_hypotheses`：研究族、机制、冻结公式、数据要求、状态和首次登记时间；
- `research_trials`：每次实际检验的 manifest、样本角色、结果哈希和终态；
- `research_forward_observations`：冻结假设在新交易日、逐证券的证据；
- `research_forward_outcomes`：与观察结果哈希绑定的 5/10/20 会话诊断结果；
- `point_in_time_fundamentals`：以实际公告日为可见边界的基本面事实。

记录不得删除或覆盖。同一主键同内容写入幂等，不同内容必须冲突。多重检验的试验总数
来自整个账本，不能按一次 study 或一次发布重置。

## 第一批研究族

| 研究族 | 第一批冻结代表 | 数据边界 | 当前角色 |
| --- | --- | --- | --- |
| attention-overreaction | `no-recent-limit-up-v1` | 目标日前 5 日涨停触及次数 | 前向确认候选 |
| salience-turnover | `extreme-return-abnormal-turnover-v1` | 行业相对收益、20 日换手基线 | 探索 |
| downside-liquidity-risk | `downside-tail-liquidity-v1` | 60 日下行波动、跳空和换手稳定性 | 探索 |
| overnight-intraday | `overnight-intraday-separation-v1` | 前一收盘、当日开盘和收盘 | 探索 |
| value | `earnings-price-point-in-time-v1` | 公告日可见盈利和当日市值 | 数据准备 |
| profitability | `profitability-quality-point-in-time-v1` | 公告日可见 ROE/ROA/毛利/现金流 | 数据准备 |

每个研究族首轮只允许一个代表假设。不得做阈值网格、自动组合赢家或按事后最优市场状态
筛选。行业分类必须绑定版本；`unavailable` 不得形成虚假行业。

上一轮 v4 的另外五个 challenger 也必须以 `discovery_exhausted` 保存：
`breadth-five-day-median-v1`、`breakout-overextension-cap-v1`、
`signal-quality-rank-v1`、`size-bottom-30pct-filter-v1` 和 `trend-quality-v1`。
它们不是下一轮优先方向，但其失败不能从终身试验次数中消失。

## 点时基本面规则

- `ann_date` 或更严格的 `f_ann_date` 是事实首次可见时间；报告期结束日不能替代公告日。
- 同一报告期的调整、修订和原始版本分别保存；不得把后来修订值写回较早交易日。
- Tushare `daily_basic` 的每日估值、换手率和市值必须绑定交易日及原始字段哈希。
- 财务指标、利润表、资产负债表和现金流量表必须保存接口、公告时间、报告类型、
  `update_flag` 和 payload 哈希。
- 缺失点时事实只能使该证券/研究日显式不可用，不能用当前值或同业均值填补。

## 统计与晋级

主指标继续使用 `20d_25bps_market_cap_matched_excess_bps`。探索报告同时输出绝对表现、
相对基线配对差值、成本、P05、最差连续 20 信号日、年度/形态/排名/市场状态拆解。

确认性晋级至少要求：

- outcome 与 benchmark 完整率 100%；
- challenger 绝对主指标大于 0；
- 相对基线配对均值大于 0 且 95% 区间下界严格大于 0；
- 终身试验族上的 family-wise `p <= 0.05`；
- 可执行率、不可执行率和尾部风险不恶化；
- 独立时间样本通过；
- 独立 Sina 复制完整且主指标非负。

White Reality Check 保留为家族整体门禁；Romano-Wolf step-down 用于识别具体通过者。
Deflated Sharpe Ratio 和 Probability of Backtest Overfitting 仅作为辅助诊断，未在单独 ADR
冻结阈值前不得成为可调整的晋级开关。

## 第一批实施边界

1. 建立 Schema v12 终身账本、点时基本面和前向观察持久化；
2. 登记现有 v4 六臂及失败结果，冻结 `no-recent-limit-up-v1`；
3. 提供三类价格/交易行为探索信号的纯函数，不启动真实全量研究；
4. 提供 Tushare `daily_basic` 与 `fina_indicator_vip` 的离线规范化入口；
5. 提供终身试验计数、White RC 与 Romano-Wolf 结构化诊断；
6. MCP 第一批只增加只读研究账本查询，不公开自动注册、自动选参或自动晋级写操作。

第一批必须用本地真实 SQLite v12 小数据集完成端到端验证：迁移、不可变事实、点时可见性、
三类信号、前向观察、终身试验计数、统计诊断和 MCP 查询必须在同一固定数据链上通过。
标准测试全部使用固定夹具且不得联网。Windows 全量数据迁移、真实点时数据回填和前向
观察属于后续外部门禁，不能由本地离线测试宣称完成。

初始化账本并可选导入现有 v4 诊断的本地管理入口为：

```text
stock-mcp initialize-research-program --root PATH [--study-id V4_STUDY_ID]
```

不带 `--study-id` 时只登记 11 个冻结定义；带入已完成研究 ID 时，还会从持久化诊断生成
六条不可变 `discovery_exhausted` trial。该命令不联网、不启动研究，也不创建或激活策略。

## 第二批：前向事实与点时数据链

第二批只扩大研究证据，不改变当前策略：

- `no-recent-limit-up-v1` 使用目标日前恰好 5 个交易日的涨停触及布尔事实；
- `extreme-return-abnormal-turnover-v1` 保存行业相对收益与换手异常连续值；
- `downside-tail-liquidity-v1` 保存下行半偏差、最差收益、最差隔夜跳空和换手离散度；
- `overnight-intraday-separation-v1` 分离隔夜与日内收益；
- 每条观察绑定假设 ID、交易日、来源时间、原始输入哈希和结果哈希；同日不同内容冲突；
- 这些观察不改变 v0.3 的资格、分数、排名或候选数量，也不等同于 20 日 outcome。

Tushare 点时收集以一个交易日为原子批次：先同时取得 `daily_basic` 与当日公告的
`fina_indicator_vip`，检查键唯一、交易日和公告日边界，再一次性规范化并写入。任一接口失败、
出现重复键或未来公告时整批不得写入。显式运行入口为：

```text
stock-mcp collect-research-facts --root PATH --trade-date YYYY-MM-DD
```

该入口会联网，因此标准测试只能使用注入的固定客户端。它不会自动调度、回填未来数据、
启动策略研究或生成候选。Windows 实机调用仍属于外部门禁；在此之前必须通过本地临时
SQLite、假客户端、重复行、未来公告、修订隔离和 MCP 只读回归。

## 第三批：证券级历史观察与成熟结果

第三批修正了“同一假设、同一日期只能保存一条观察”的横截面建模缺陷：

- Schema v13 主键固定为 `hypothesis_id + trade_date + symbol`；
- v12 旧观察保留为 `symbol=legacy-unspecified`，不伪造其证券身份；
- outcome 主键增加 `horizon_sessions`，只允许 5、10、20 会话；
- 每个 outcome 必须引用已存在观察的精确 `result_hash`；
- 观察和全部 outcome 在同一个 SQLite 事务中写入，任何冲突整批回滚；
- 重跑时间不进入结果身份；同一持久化行情今天或以后重跑必须幂等。

首个离线执行器只处理已有完整价格事实即可支持的两个研究族：

- `no-recent-limit-up-v1`：读取严格相邻的前 5 个交易日涨停事实；存储层仍禁止把
  `2026-08-07` 及以前数据伪装成新的前向确认样本；
- `overnight-intraday-separation-v1`：读取信号日同源前收、开盘和收盘。

结果路径为 `signal-close-diagnostic`，只用于研究诊断，不代表可交易入场。它使用后续恰好
20 个持久化交易会话，输出个股 5/10/20 日毛收益、等权主板非 ST 基准和超额收益；不含
滑点、成交约束或策略候选语义。缺交易日、缺证券行情、混合价格源、缺涨停事实或空基准
均显式失败。

离线入口为：

```text
stock-mcp build-research-forward-evidence --root PATH --symbol 600001.SH \
  --trade-date YYYY-MM-DD --through YYYY-MM-DD \
  --hypothesis-id overnight-intraday-separation-v1
```

`extreme-return-abnormal-turnover-v1` 与 `downside-tail-liquidity-v1` 仍保留纯函数和证券级
持久化能力，但自动执行必须等点时 `daily_basic` 覆盖完整后再接入；不得使用当前换手率
回填历史。标准验收继续只使用固定的本地 SQLite 小数据集，不访问 Tushare 或 Windows。

## 第四批：逐日等权前向报告

第四批把证券级成熟 outcome 汇总为只读证据报告，但不把报告伪装成策略晋级：

- 先在每个信号日、每个组内等权，再跨信号日等权；证券数量多的日期不得获得更高权重；
- `no-recent-limit-up-v1` 使用已经冻结的 `passes_no_recent_limit_up` 布尔事实，比较
  “无近期涨停”与“存在近期涨停”两个同期横截面组；只有同日两组都存在时才进入配对差值；
- 其他尚未冻结方向或对照定义的连续因子只能生成 `descriptive-only` 报告，不能事后选择
  分位点、方向或阈值；
- 只使用查询时已经可见、与 observation `result_hash` 精确绑定的 5/10/20 会话 outcome；
  未成熟 outcome 单独计为 pending，v12 的 `legacy-unspecified` 观察单独排除；
- discovery 冻结日及以前的观察不进入 forward 报告；重叠日序列使用冻结的 circular moving
  block 方法描述相关性，不把普通独立同分布区间用于重叠 outcome；
- manifest 绑定假设定义、horizon、观察哈希和 outcome 哈希；相同证据、不同读取顺序或读取
  时间生成相同报告和结果哈希；
- 报告固定返回 `status=evidence_only` 与 `promotion_eligible=false`。独立审阅、跨源复制、
  proposal、治理回放、认证和激活仍是互不替代的后续动作。

本地离线入口为：

```text
stock-mcp derive-research-forward-report --root PATH \
  --hypothesis-id no-recent-limit-up-v1 --horizon-sessions 20
```

ChatGPT 只读入口为 `get_research_forward_report`。开发验收使用固定临时 SQLite，覆盖同日多证券、
不同日期证券数不等、配对/非配对日期、未成熟结果、legacy 排除、哈希冲突、重复读取和 CLI
子进程；不访问实时行情，也不宣称 Windows 全量规模已经通过。

## 第五批：盘后增量观察与成熟结果

第五批把前四批的离线能力接入现有盘后任务，但仍只积累证据：

- 观察范围固定为已经发布的 `v0.3` 日报候选，不扫描或事后选择全市场证券；
- 从 `2026-08-08` 以后开始，使用当日已持久化的完整 Tushare 快照、前 5 个交易日的
  `daily_price_limits` 和候选身份生成 `no-recent-limit-up-v1`、
  `overnight-intraday-separation-v1` 两类证券级观察；
- 实时规范化快照在同一个发布事务中追加当天交易日历和确定性 10% 主板涨跌停事实，确保
  新交易日不依赖历史回填命令；
- 每次盘后调用只查询已经满足 20 个后续会话、但尚无 20 日 outcome 的观察，并一次追加
  5/10/20 日结果；重复调用不覆盖事实，也不会再次成熟同一观察；
- 单个候选缺少未来同源行情时保持 pending 并计入 `blocked_observations`，不阻塞其他候选，
  也不以补值、跨源价格或后来恢复交易的价格冒充目标会话收盘；
- 批处理不联网，研究失败只写入脱敏服务日志，不降低已发布日报的状态；下一次人工运行可
  基于不可变事实安全续跑。

管理员可对某个已经发布的交易日手动、安全地重跑：

```text
stock-mcp run-research-forward-batch --root PATH --trade-date YYYY-MM-DD
```

命令输出候选数、新观察数、成熟观察数、新 outcome 数和阻塞观察数。服务端盘后任务只在
当日使用 Tushare 完整快照并已经生成日报时调用同一入口；Sina 降级快照不会进入这条前向
证据链。该批处理不创建 trial、proposal、认证或激活，也不改变当前候选和分数。

尚处于 20 日生产观察期的 `degraded_observation` 即使已经生成只读观察日报，也不得调用
前向研究批处理；只有状态为 `ready` 的正式发布日报才能新增或成熟研究观察。观察期完成前
返回零条前向观察是正确结果，不得为了填充样本绕过发布门禁。

本地验收使用固定 26 会话、2 证券 SQLite：先证明信号日只写观察，再证明第 20 个后续会话
一次性写入 5/10/20 日 outcome、重启重跑为零新增，并证明一个证券缺未来行情时其他证券仍
可成熟。Windows 服务调度、真实新交易日和长期资源占用仍是外部门禁。
