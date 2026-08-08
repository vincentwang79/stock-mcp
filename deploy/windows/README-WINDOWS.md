# Stock MCP Windows Server 部署手册

本项目采用原生 Windows x64 部署，不要求 Docker、WSL、Node、系统 Python 或独立数据库服务器。MCP 进程只监听 `127.0.0.1:8765/mcp`；Secure MCP Tunnel 作为独立服务，仅建立出站 HTTPS 连接，因此不需要配置入站防火墙规则。发布 ZIP 安装不需要 Git；本手册的首选“源码直接安装”方式需要 Git。

所有安装和运维操作都必须遵守仓库根目录的[项目基本规则](../../GROUND_RULES.md)。

> **重要：源码仓库不包含 `stock-mcp-windows-x64.zip`。**
> 服务器已通过 Git 克隆源码时，应使用下文的“源码直接安装”，不需要手工打包或解压 ZIP。该入口会拒绝未提交的源码改动、记录 Git 提交、验证工具哈希，并将源码复制为 `E:\StockMcp\releases` 下的不可变运行版本。没有 Git 或需要离线发布时，才使用“发布 ZIP 安装”。

## Windows 路径约定

Windows Server 上的源码固定检出到 `E:\code\stock-mcp`。服务程序、配置、运行数据、日志和备份固定放在 `E:\StockMcp`。两个目录必须分离：源码目录只用于 Git 工作区和部署脚本，运行数据只能由服务写入 `E:\StockMcp`。

```powershell
git clone https://github.com/vincentwang79/stock-mcp.git E:\code\stock-mcp
cd E:\code\stock-mcp
```

## 源码直接安装（推荐）

在管理员 PowerShell 中，从干净的 Git 工作区执行：

```powershell
cd E:\code\stock-mcp
git pull --ff-only
.\deploy\windows\install-from-source.ps1 -InstallRoot E:\StockMcp
.\deploy\windows\configure.ps1 -InstallRoot E:\StockMcp
```

`install-from-source.ps1` 不生成或解压发布 ZIP。它要求 Git 工作区没有已修改或未跟踪的文件，记录当前提交和远程地址，下载并验证固定版本的 `uv`、WinSW 与 `tunnel-client`，然后建立可回滚的 `E:\StockMcp\releases\<版本+提交>` 运行副本。每次 `git pull` 到新提交后，再次运行同一安装命令即可升级；失败时仍保留旧版本和数据库备份。

配置脚本通过安全交互提示采集生产密钥，并只将其保存到 ACL 保护的主机配置文件。它完成数据权限检查、三年历史回填、Tunnel 自检、MCP 本地就绪检查和 Tunnel 服务身份就绪检查后，才启动 `StockMcpService` 与 `StockMcpTunnel`。

## 发布 ZIP 安装（无 Git 或离线环境）

1. 从可信发布渠道获取 `stock-mcp-windows-x64.zip`，并通过独立渠道获取对应的 `.sha256` 摘要。**在解压 ZIP 或执行任何脚本之前**，先运行以下命令，并将结果与发布摘要逐字符比较：

   ```powershell
   Get-FileHash E:\path\stock-mcp-windows-x64.zip -Algorithm SHA256
   ```

   当前引导脚本未使用 Authenticode 签名，无法自证可信。只有在外部摘要校验成功后，才能解压全新的发布包并执行其中的脚本。

2. 在管理员 PowerShell 窗口中运行：

   ```powershell
   .\install.ps1 `
     -PackageArchive E:\path\stock-mcp-windows-x64.zip `
     -PackageSha256 <发布渠道提供的六十四位十六进制摘要>
   ```

   安装器会再次验证原始压缩包和外部摘要，然后检查 x64 系统、可用磁盘空间、出站 HTTPS、全部发布文件校验和，以及固定版本 `uv`、WinSW 和 `tunnel-client` 的 SHA-256。默认安装目录为 `E:\StockMcp`。

3. 运行：

   ```powershell
   .\configure.ps1 -InstallRoot E:\StockMcp
   ```

   脚本通过安全交互提示采集生产密钥，并只将其保存到 ACL 保护的主机配置文件。脚本完成数据权限检查、三年历史回填、Tunnel 自检、MCP 本地就绪检查和 Tunnel 服务身份就绪检查后，才启动 `StockMcpService` 与 `StockMcpTunnel`。

Tunnel 服务使用官方 `tunnel-client run --config ...` 流程。YAML 配置只保存 `api_key: file:...` 文件引用，不保存运行密钥本身；自检使用 `--explain`，Tunnel 的健康接口和管理界面只监听本机 8766 端口。

完成配置前，两个服务会保持已注册但停止的状态，`state/service-status` 的值为 `configuration_required`（需要配置），从而避免反复崩溃重启。

## 策略激活

策略激活设有主机侧批准门禁。提案版本必须先在完整三年交易日历上完成逐日向前回放，前 20 个交易日只用于预热。随后管理员运行：

```powershell
stock-mcp approve-strategy `
  --root E:\StockMcp `
  --version <版本号>
```

管理员还必须在提示中再次输入版本号。批准与已保存的参数哈希绑定，只能消费一次，并且不能通过 MCP 创建。完成主机批准后，用户才能从 ChatGPT 调用 `activate_strategy_version`。

## 升级、恢复与卸载

使用源码直接安装时，升级命令就是重新拉取已提交的代码并重跑安装入口：

```powershell
cd E:\code\stock-mcp
git pull --ff-only
.\deploy\windows\install-from-source.ps1 -InstallRoot E:\StockMcp
```

使用发布 ZIP 时，先核对独立发布的 SHA-256 摘要，然后在管理员 PowerShell 窗口中运行：

```powershell
.\update.ps1 `
  -PackagePath E:\path\stock-mcp-windows-x64.zip `
  -PackageSha256 <发布渠道提供的六十四位十六进制摘要> `
  -InstallRoot E:\StockMcp
```

升级脚本会验证压缩包，暂存新版本、工具和 WinSW 服务配置，创建程序与数据库备份，停止两个服务，执行迁移和 `stock-mcp doctor`，切换 `current` 目录联接，并验证 MCP 与 Tunnel 就绪状态。如果任何检查失败，脚本会恢复旧程序、工具、服务配置和数据库，再次验证旧版本的两个服务；恢复失败时写入明确的 `rollback_failed`（回滚失败）状态。

运行 `diagnose.ps1` 可生成脱敏诊断 ZIP，其中包含系统和服务状态、最近日志、版本信息、备份清单、数据库完整性以及应用和 Tunnel 自检结果。脚本会排除 `secrets.env` 和密钥文件，对所有外部命令输出执行脱敏，并在压缩前扫描裸密钥、认证头、URL 凭证和查询参数密钥模式。

运行 `uninstall.ps1` 会删除服务和程序运行环境，但默认保留配置、数据、日志和备份。只有显式运行 `uninstall.ps1 -PurgeData` 并完成第二次确认后，才会删除保留的数据。

## 构建发布包

从源码根目录运行 `fetch-tools.ps1`，根据 `tools-manifest.json` 下载固定版本的工具资产，同时验证压缩包和解压后二进制文件的 SHA-256，并生成 `tools-cache`。然后运行：

```powershell
.\deploy\windows\fetch-tools.ps1

.\deploy\windows\build-release.ps1 `
  -Version <版本号> `
  -ToolsManifest .\deploy\windows\tools-manifest.json `
  -ToolsDirectory .\deploy\windows\tools-cache
```

构建器会拒绝占位内容、缺失的 `uv.lock`，以及哈希与清单不一致的二进制文件。成功后生成固定文件名 `stock-mcp-windows-x64.zip`、需要独立发布的 `stock-mcp-windows-x64.zip.sha256`，以及压缩包内部的 `checksums.txt`。

外部发布的 SHA-256 才是信任根。`checksums.txt` 只能在 ZIP 已通过外部摘要验证后检测内部损坏，不能替代解压前的外部校验。

## 权限与密钥规则

两个服务分别使用 Windows 虚拟账户 `NT SERVICE\StockMcpService` 和 `NT SERVICE\StockMcpTunnel`。程序目录和版本目录对两个账户都只读；只有应用服务账户可以修改数据、备份和状态，只有 Tunnel 服务账户可以读取 Tunnel 运行密钥。

不得将 Token、代理凭证或其他生产密钥写入 PowerShell 命令历史、服务 XML、日志、问题单、Agent 提示、交接文档或本手册。

## 必须在真实 Windows Server 验证的事项

静态测试不能替代以下目标环境验收：

- Windows PowerShell 5.1 对全部脚本的实际解析和执行。
- WinSW 服务注册、刷新、停止、启动和卸载。
- Windows 虚拟服务账户与 ACL 权限隔离。
- `current` 目录联接切换、数据库恢复、失败回滚和服务器重启恢复。
- 真实 `tunnel-client` 的自检、运行、就绪接口和 ChatGPT 工作区授权。
- 使用哨兵密钥验证诊断 ZIP 不包含原始敏感值。
- 下载文件的 Windows 区域标记和执行策略行为。
