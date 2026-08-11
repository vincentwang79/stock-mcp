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

正式 Windows Server 的默认入口是经过独立 SHA-256 校验的发布 ZIP：运行其中的 `install.ps1` 即可原生安装，不需要 Docker、WSL、Git、Node 或系统 Python；密钥配置保留为随后的安全交互。服务运行根目录为 `E:\StockMcp`。已有受控源码工作区时，源码目录为 `E:\code\stock-mcp`，可运行
`deploy\windows\install-from-source.ps1 -InstallRoot E:\StockMcp` 将干净 Git 提交复制为可回滚的运行版本。源码仓库不提交 `stock-mcp-windows-x64.zip`；发布工程先运行 `fetch-tools.ps1` 获取并验证
固定版本的 uv、WinSW 和 OpenAI tunnel-client，再用 `build-release.ps1` 生成 ZIP 及其外部 SHA-256。发布包同时包含
行业 JSON；在三年同源日线回填成功后，管理员显式运行 `E:\\StockMcp\\current\\.venv\\Scripts\\python.exe -m stock_mcp.cli build-v3-facts --root E:\\StockMcp --start 2023-08-08 --end 2026-08-07`，只以本机 SQLite 与发布内行业 JSON 构建不可变 v3 事实。直接运行 Python 模块可避免 Windows 的 `current` 目录联接与生成的 CLI 启动器不兼容。具体升级、诊断、回滚和卸载流程见
[Windows Server 部署手册](deploy/windows/README-WINDOWS.md)。

v3 候选提案 `v0.3-policy-1` 与 `v0.3-policy-2` 都显式声明 `supersedes_version="v0.2-proposed"`，但本轮不自动创建提案、不自动认证、不自动激活。每个提案必须独立完成 727 个交易日的逐日向前回放（前 60 个交易日仅预热）、异步 outcome 审阅和认证；激活还需要 Windows 主机的一次性本地批准与用户明确确认。成功时会原子地将 v0.2 标为 `superseded`，同时历史日报、候选和证明继续保留。
