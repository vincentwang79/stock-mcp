# 本地可执行任务规格

当前仓库尚未连接远程 Issue Tracker，因此这里保存与 `ready-for-agent` Issue
同构的本地任务规格，作为 Agent 任务卡的来源。它们不是已发布的 Issue，也不代表
外部标签或权限已经配置完成。

原始产品规格：
`/Users/VincentWang/Documents/Codex/2026-08-07/ys/outputs/a-share-chatgpt-mcp-spec.md`

所有任务共享最高测试 seam：

`normalized market snapshot + immutable strategy version -> validated daily review`

## 任务目录

1. `01-data-pipeline.md`：采集、标准化、持久化与盘后发布。
2. `02-strategy-replay.md`：市场状态、策略筛选、回放与版本治理。
3. `03-mcp-workflow.md`：MCP 工具、观察列表、候选事件与复盘。
4. `04-windows-release.md`：Windows 发布、Tunnel、备份、升级与诊断。
