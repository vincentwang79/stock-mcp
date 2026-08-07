# Agent 开发规则

## 产品边界

- 只做沪深主板 A 股研究、候选解释、观察列表和复盘记录。
- 不做自动交易、券商接入、仓位、余额、推送或连续盘中监控。
- ChatGPT 只能解释服务端证据，不能自行添加候选或改变分数。

## 协作规则

- 主 Agent 独占公共类型、数据库核心 Schema、依赖版本和跨模块接口。
- Sub-agent 只修改任务卡明确授权的目录，不得生成下级 Agent。
- Reviewer 只报告问题，不直接修改代码。
- 主 Agent 是唯一创建 commit 的 Agent。
- 并行写任务必须目录不重叠；不清楚边界时停止写入并报告。
- 每个功能遵循 RED-GREEN-REFACTOR，先展示正确失败的测试，再写生产代码。
- 标准测试不得访问实时行情；使用脱敏、固定、带来源时间的夹具。
- 不在代码、日志、Issue、Agent 提示或 handoff 中存放真实密钥。

## 任务交接

- 任务以 `ready-for-agent` Issue 或自包含任务卡为准。
- 只有任务跨会话、被外部条件阻塞或需要换 Agent 时才使用 `$handoff`。
- handoff 保存在操作系统临时目录，只引用规格、Issue、ADR、commit、diff 和证据路径，不复制已有内容。

## 验收

- 最高 seam：`normalized market snapshot + immutable strategy version -> validated daily review`。
- 同一输入和策略版本必须产生确定性输出。
- 单个日报不得混合不同价格源。
- 历史回放不得读取目标日期之后的数据。
- 防御市场允许合法返回零候选。
