# A 股私有 MCP

面向个人短线复盘的确定性 A 股研究服务。服务在 Windows Server 上运行，通过 OpenAI Secure MCP Tunnel 私有接入 ChatGPT，不提供下单、账户或仓位管理能力。

产品规格来源：`/Users/VincentWang/Documents/Codex/2026-08-07/ys/outputs/a-share-chatgpt-mcp-spec.md`。

## 项目文档

- [项目基本规则](GROUND_RULES.md)
- [Agent 开发规则](AGENTS.md)
- [总体架构决策](docs/adr/0001-architecture.md)
- [本地任务规格](docs/task-specs/README.md)
- [Windows Server 部署手册](deploy/windows/README-WINDOWS.md)

## 核心测试边界

`规范化市场快照 + 不可变策略版本 → 已验证的每日报告`

所有数据源、策略、MCP 和部署实现最终都必须在该行为边界上验收。

## 本地验证

```bash
uv sync --locked --extra dev
uv run python -m unittest discover -s tests -t . -p 'test_*.py'
uv run ruff check src tests
uv build
```

标准测试全部使用固定夹具，不访问实时行情。生产服务仅监听
`127.0.0.1:8765/mcp`，同时在本机回环地址提供 `/healthz` 与 `/readyz`。

## Windows 发布

源码仓库不提交 `stock-mcp-windows-x64.zip`；它是发布构建产物。发布材料位于
`deploy/windows`，发布工程先运行 `fetch-tools.ps1` 获取并验证
固定版本的 uv、WinSW 和 OpenAI tunnel-client，再用 `build-release.ps1` 生成
`stock-mcp-windows-x64.zip` 及其外部 SHA-256。服务器侧先核对外部摘要，再从解压目录运行
`install.ps1 -PackageArchive <zip> -PackageSha256 <digest>` 与 `configure.ps1`；配置阶段会
幂等回填三年 Tushare 日线，此后的前 20 个成功交易日保持“仅观察”状态。具体升级、诊断、回滚和卸载流程见
[Windows Server 部署手册](deploy/windows/README-WINDOWS.md)。

策略版本由 MCP 创建，并需完成完整三年交易日历的逐日向前回放比较（前 20 个交易日只作预热）；激活还需要 Windows 主机上的一次性本地批准；
这使模型无法仅靠传入 `confirmed=true` 绕过人的最终确认。
