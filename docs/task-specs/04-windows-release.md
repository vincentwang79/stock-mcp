# Windows 发布、Tunnel、备份、升级与诊断

状态：local-ready-for-agent

## Problem

正式主机不能依赖预装开发工具、入站端口或人工拼装服务；密钥和升级失败必须安全处理。

## Solution

发布一个 x64 ZIP；以 PowerShell 完成预检、隔离 Python、ACL、两个 WinSW 服务、配置、
升级回滚、诊断和保留数据的卸载。MCP 仅 loopback，Tunnel 仅出站 HTTPS。

## User Stories

- 干净 Windows Server 解压后只需运行 `install.ps1`。
- 未配置密钥时状态为 `configuration_required`，而非反复崩溃。
- 升级前备份，健康检查失败自动回滚。
- 诊断包自动排除密钥；卸载默认保留配置、数据和备份。

## Implementation Decisions

- 版本目录不可变，`current` junction 指向当前版本。
- `uv` 安装固定 Python 3.12；服务直接执行版本 venv 的 Python。
- `StockMcpService` 和 `StockMcpTunnel` 使用低权限服务账户。
- 所有二进制和发布文件必须在使用前校验 SHA-256。

## Testing Decisions

- 静态脚本契约加干净 Windows VM 冒烟测试。
- 覆盖重复安装、配置、重启、升级、失败回滚、诊断和卸载。

## Out of Scope

Docker、WSL、Git、Node、系统 Python、入站防火墙规则、默认启用 S3。
