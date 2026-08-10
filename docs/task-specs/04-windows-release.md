# Windows 发布、Tunnel、备份、升级与诊断

状态：local-ready-for-agent

## 问题

正式主机不能依赖预装开发工具、入站端口或人工拼装服务；密钥和升级失败必须安全处理。

## 方案

发布一个 x64 ZIP；以 PowerShell 完成预检、隔离 Python、ACL、两个 WinSW 服务、配置、
升级回滚、诊断和保留数据的卸载。MCP 仅监听本机回环地址，Tunnel 仅使用出站 HTTPS。

## 用户故事

- 干净 Windows Server 解压后只需运行 `install.ps1`。
- 未配置密钥时状态为 `configuration_required`，而非反复崩溃。
- 升级前备份，健康检查失败自动回滚。
- 诊断包自动排除密钥；卸载默认保留配置、数据和备份。
- 受控升级会应用 Schema v10，验证 `stock-mcp doctor`、本机 `/readyz` 与 Tunnel 就绪；任一检查失败则恢复旧版本、工具、服务配置和数据库备份。

## 实现决策

- 版本目录不可变，`current` 目录联接指向当前版本。
- `uv` 安装固定 Python 3.12；服务直接执行版本虚拟环境中的 Python。
- `StockMcpService` 和 `StockMcpTunnel` 使用低权限服务账户。
- 所有二进制和发布文件必须在使用前校验 SHA-256。
- 发布包在应用目录中携带版本化行业 JSON；部署后由管理员显式运行 `stock-mcp build-v3-facts --root E:\\StockMcp --start 2023-08-08 --end 2026-08-07`。该离线步骤失败、数据有缺口或事实冲突时，禁止开始 v3 回放、认证或激活。
- 安装、配置和升级不自动创建策略提案、不自动认证、不自动激活。727 个交易日回放、60 个交易日预热、outcome 审阅、主机批准和用户确认仍须按治理流程人工完成。

## 测试决策

- 静态脚本契约加干净 Windows VM 冒烟测试。
- 覆盖重复安装、配置、重启、Schema v10 升级、备份、`/readyz` 验证、失败回滚、诊断和卸载。

## 非目标

Docker、WSL、Git、Node、系统 Python、入站防火墙规则、默认启用 S3。
