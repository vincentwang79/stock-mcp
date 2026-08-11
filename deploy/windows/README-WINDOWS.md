# Stock MCP Windows Server 部署手册

本项目采用原生 Windows x64 部署，不要求 Docker、WSL、Git、Node、系统 Python 或独立数据库服务器。MCP 进程只监听 `127.0.0.1:8765/mcp`；Secure MCP Tunnel 作为独立服务，仅建立出站 HTTPS 连接，因此不需要配置入站防火墙规则。正式服务器的默认入口是可信发布 ZIP 中的一条 `install.ps1` 命令；密钥配置仍单独进行安全交互，不能合并为命令行参数。下文“源码直接安装”只适用于已受控 Git 工作区的维护升级，不是无 Git 服务器的前置条件。

Schema v11 加入可选新浪数据链路和 v0.4 研究表，但默认不启动新浪网络任务。`config\app.toml` 的 `[sina] shadow_enabled=false` 是安全默认值；安装、配置和升级均不会自动回填新浪、启动 20 日 shadow、激活数据源、创建 v0.4 策略或改变当前 v0.3 日报。

所有安装和运维操作都必须遵守仓库根目录的[项目基本规则](../../GROUND_RULES.md)。

> **重要：源码仓库不包含 `stock-mcp-windows-x64.zip`。**
> 服务器已通过 Git 克隆源码时，应使用下文的“源码直接安装”，不需要手工打包或解压 ZIP。该入口会拒绝未提交的源码改动、记录 Git 提交、验证工具哈希，并将源码复制为 `E:\StockMcp\releases` 下的不可变运行版本。没有 Git 或需要离线发布时，才使用“发布 ZIP 安装”。

## Windows 路径约定

Windows Server 上的源码固定检出到 `E:\code\stock-mcp`。服务程序、配置、运行数据、日志和备份固定放在 `E:\StockMcp`。两个目录必须分离：源码目录只用于 Git 工作区和部署脚本，运行数据只能由服务写入 `E:\StockMcp`。

```powershell
git clone https://github.com/vincentwang79/stock-mcp.git E:\code\stock-mcp
cd E:\code\stock-mcp
```

## 源码直接安装（已有受控 Git 工作区时）

从普通 PowerShell 先打开管理员窗口：

```powershell
Start-Process PowerShell -Verb RunAs
```

随后在新打开的管理员 PowerShell 中，从干净的 Git 工作区执行：

```powershell
cd E:\code\stock-mcp

# 仅对当前 PowerShell 窗口生效；关闭窗口后自动恢复。
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force

# 此服务器经本机代理访问 api.openai.com:443。
$env:HTTP_PROXY = "http://127.0.0.1:7897"
$env:HTTPS_PROXY = "http://127.0.0.1:7897"

git pull --ff-only
.\deploy\windows\install-from-source.ps1 -InstallRoot E:\StockMcp
.\deploy\windows\configure.ps1 -InstallRoot E:\StockMcp
```

如果 Windows PowerShell 的安全输入提示不能一次粘贴完整的 Tunnel runtime API key，请先在本机复制完整密钥，然后改用下列命令。开关只让脚本在进程内读取剪贴板；密钥不会成为命令参数、命令历史或日志的一部分。运行后可执行 `Set-Clipboard -Value ''` 清空剪贴板。

```powershell
.\deploy\windows\configure.ps1 `
  -InstallRoot E:\StockMcp `
  -TunnelRuntimeKeyFromClipboard
```

脚本仍会安全提示输入 Tushare Token、Tunnel ID、代理和可选 CA 路径，但不会再次提示输入 Tunnel runtime API key。

### 使用单个配置文件（推荐）

如果不希望逐项交互输入，可由脚本在受 ACL 保护的位置生成模板：

```powershell
.\deploy\windows\configure.ps1 `
  -InstallRoot E:\StockMcp `
  -WriteConfigurationTemplate

notepad E:\StockMcp\config\configure-input.psd1
```

在记事本中填写 `TushareToken`、`TunnelId`、`TunnelRuntimeApiKey`、`HttpsProxy` 和可选的 `CustomCaFilePath`；字符串必须保留单引号。此服务器的 `HttpsProxy` 填写 `http://127.0.0.1:7897`。保存后执行：

```powershell
.\deploy\windows\configure.ps1 `
  -InstallRoot E:\StockMcp `
  -ConfigurationFile E:\StockMcp\config\configure-input.psd1
```

如果配置文件中的 Tushare Token 曾被错误内容污染，可先只复制官网显示的完整 Token，再使用剪贴板值覆盖配置文件中的 `TushareToken`。其他配置仍从文件读取，Token 不会进入命令历史：

```powershell
.\deploy\windows\configure.ps1 `
  -InstallRoot E:\StockMcp `
  -ConfigurationFile E:\StockMcp\config\configure-input.psd1 `
  -TushareTokenFromClipboard
```

脚本会在写入主机配置前要求 Tushare Token 是 56 位十六进制字符串；格式不符时立即停止，不会再用错误值覆盖当前配置。完成后可执行 `Set-Clipboard -Value ''` 清空剪贴板。

脚本会先将输入文件限制为 Administrators 和 SYSTEM 可读，再读取它。只有配置、三年回填、本地 MCP 与 Tunnel 验证全部成功后，才删除该临时输入文件；失败时会保留文件供修正后重试。运行时密钥仍只会写入专用的受限密钥文件，不会出现在服务命令行或日志中。

`install-from-source.ps1` 不生成或解压发布 ZIP。它要求 Git 工作区没有已修改或未跟踪的文件，记录当前提交和远程地址，下载并验证固定版本的 `uv`、WinSW 与 `tunnel-client`，然后建立可回滚的 `E:\StockMcp\releases\<版本+提交>` 运行副本。每次 `git pull` 到新提交后，再次运行同一安装命令即可升级；失败时仍保留旧版本和数据库备份。

如果 `Get-ExecutionPolicy -List` 显示 `MachinePolicy` 或 `UserPolicy` 已由组策略强制，当前窗口的设置也会被拒绝；此时应由 Windows Server 管理员为该受控仓库配置相应的脚本执行策略。不要将机器级策略设为 `Unrestricted`，也不要对不可信目录使用绕过命令。

### 工具下载故障排查

安装器会下载并校验 `uv`、WinSW 和 `tunnel-client`。若提示下载失败，先更新脚本后重试：

```powershell
cd E:\code\stock-mcp
$env:HTTP_PROXY = "http://127.0.0.1:7897"
$env:HTTPS_PROXY = "http://127.0.0.1:7897"
git pull --ff-only
.\deploy\windows\install-from-source.ps1 -InstallRoot E:\StockMcp
```

新版下载器会强制 TLS 1.2 并自动重试三次。若仍失败，请确认服务器可对以下主机建立出站 HTTPS 连接：

```powershell
Test-NetConnection github.com -Port 443
Test-NetConnection release-assets.githubusercontent.com -Port 443
Test-NetConnection persistent.oaistatic.com -Port 443
Test-NetConnection api.openai.com -Port 443
```

如果网络要求代理或 TLS 解密，应由服务器网络管理员配置受信任的系统代理和根证书；不要关闭 TLS 校验，也不要修改工具清单中的固定 SHA-256。对于本服务器，在 `configure.ps1` 的 `Optional HTTPS proxy` 提示中输入 `http://127.0.0.1:7897`；该值会写入受 ACL 保护的应用配置，并且只用于 Tunnel 访问 OpenAI control plane，不会代理 Tunnel 到 `127.0.0.1:8765/mcp` 的本机连接。当前会话的环境变量不会自动传给 Windows 服务。

配置脚本通过安全交互提示采集生产密钥，并只将其保存到 ACL 保护的主机配置文件。它完成数据权限检查、三年历史回填、Tunnel 自检、MCP 本地就绪检查和 Tunnel 服务身份就绪检查后，才启动 `StockMcpService` 与 `StockMcpTunnel`。

## 发布 ZIP 安装（无 Git；仍需出站 HTTPS）

当前 ZIP 包含应用、固定工具和锁文件，但不包含完整 Python/wheel 离线缓存。安装仍需出站 HTTPS 以获取固定 Python 与锁定依赖；它不是完全离线安装包。

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

策略激活设有主机侧批准门禁。v3 提案必须先以本机研究事实完成完整三年交易日历的逐日向前回放；固定范围 `2023-08-08` 至 `2026-08-07` 为 727 个交易日，前 60 个交易日仅预热，第 61 日才可产生候选证据。候选回放完成后还要异步计算 outcome；认证证明永久保留，绑定参数哈希、数据集哈希、结果哈希、`outcome_hash`、日期范围和会话数。激活只消费一次性主机批准，不会删除证明。

规则引擎 v3 的行业分类、涨跌停和快照特征不是在线临时查询：行业 JSON 随发布包提供，在 `E:\StockMcp\current\a_share_mainboard_code_name.json` 可用；`build-v3-facts` 只读取它和本机 SQLite 中已记录的同源日线。行业事实仅用于可追溯解释，缺失必须显示为 `unavailable`，不能编造行业或改变候选分数、排名和数量。

本轮只记录 `v0.3-policy-1` 和 `v0.3-policy-2` 两个待审阅模板；**不自动创建提案、不自动认证、不自动激活**。两者创建时都必须显式给出 `supersedes_version="v0.2-proposed"`，且各自使用新的、无密钥的幂等键。两者共同参数为：`rule_engine_version=3`、`offensive_min_bps=5500`、`defensive_max_bps=4000`、中性配额 `1/1`、进攻配额 `2/1`、`min_median_amount_fen=5000000000`、流动性/趋势窗口 `20/60`、回踩参数 `20/1200/350/10000`、突破参数 `60/20/15000`、近期涨停窗口 `5` 以及 `required_warmup_sessions=60`。两者唯一差异是 `regime_policy`：policy-1 为 `1`（中性 1+1、防御为零），policy-2 为 `2`（使用进攻 2+1 配额）。每个版本必须独立回放、审阅、认证，不能复用另一个版本或 v0.2 的证明。

`compare_strategy_versions` 仅供纯只读研究对照。它要求两个不同版本；同版本比较被拒绝，不启动回放、不写入任何作业或证明，不能作为激活依据。

### 升级后的 v3 事实构建与治理回放（人工发起）

Git 更新后，先以管理员 PowerShell 更新受控源码安装。已有安装会进入带备份和回滚的更新路径，并在切换服务前自动执行 Schema v10 数据库迁移；不要手工编辑 SQLite 或跳过迁移。

```powershell
cd E:\code\stock-mcp
git pull --ff-only
.\deploy\windows\install-from-source.ps1 -InstallRoot E:\StockMcp
```

随后先在 Windows 主机上执行离线事实构建；这一步成功只是事实就绪，**不是**提案、回放、认证或激活。

```powershell
& 'E:\StockMcp\current\.venv\Scripts\python.exe' -m stock_mcp.cli build-v3-facts `
  --root E:\StockMcp `
  --start 2023-08-08 `
  --end 2026-08-07
```

命令报告任一数据缺口、行业 JSON 缺失、不可变事实冲突或非零退出码时，停止后续流程，保留报告、备份和现有历史；不得手工改库或跳过失败门禁。

只有用户明确要求后，才在已连接到本机 MCP 的 ChatGPT 中按下列顺序处理某一个 v0.3 提案；每一次写调用使用新的、可追溯但不含密钥的幂等键。

1. 调用 `create_strategy_proposal` 创建 `v0.3-policy-1` 或 `v0.3-policy-2`，给出上文完整参数、理由和 `supersedes_version="v0.2-proposed"`。不得修改或覆盖 v0.2。
2. 调用 `start_strategy_replay`，传入所创建的版本、`start_date="2023-08-08"`、`end_date="2026-08-07"` 与新的幂等键。成功只表示持久化作业已进入 `queued` 或 `running`，不表示回放、审阅或认证已通过。
3. 用返回的 `replay_id` 按需调用 `get_strategy_replay` 查看 `queued`、`running`、`completed` 或 `failed` 状态、727 个交易日的会话进度、输入/结果/结果证据哈希和错误；无需也不得要求 ChatGPT 持续轮询。用 `get_strategy_replay_days` 审阅逐日证据时，以 `after_trade_date` 传入上一页最后日期，并设置受限 `limit`，确认前 60 个交易日都标为预热，并审阅候选与 outcome 状态。
4. 只有候选作业为 `completed`、727 个交易日完整、异步 outcome 为完成状态并已有 `outcome_hash`，且逐日证据经人工审阅后，才调用 `certify_strategy_replay`，并明确传入 `confirmed=true` 和幂等键。认证失败或覆盖不足时停止，保留证据并排查数据/环境，不得补造结果。
5. 认证成功后，管理员在本机运行下列命令，并在提示中再次完整键入同一版本号：

```powershell
& 'E:\StockMcp\current\.venv\Scripts\python.exe' -m stock_mcp.cli approve-strategy `
  --root E:\StockMcp `
  --version v0.3-policy-1
```

6. 最后才由用户在 ChatGPT 调用 `activate_strategy_version`，显式传入 `confirmed=true`、所认证的版本和新的幂等键。该确认不能替代上一步主机批准；批准与已保存参数哈希绑定、只能消费一次，也不能通过 MCP 创建。成功时活动指针切换和 v0.2 的 `superseded` 状态以原子事务写入；v0.2 的日报、候选、回放和证明仍历史保留。

上述日期范围和流程是操作说明，不是已完成的环境验收。真实 Windows Server 上该范围的 727 个交易日事实构建、回放、outcome 审阅、MCP 审阅、认证和激活仍为**外部门禁**：在目标主机、目标数据源、受控服务身份与实际 ChatGPT 工作区连接中完成并保留证据前，任何人不得将其描述为已验证或已认证。

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

## v0.4 新浪数据与研究门禁

升级到 Schema v11 不会自动访问新浪，也不会改变当前 v0.3 日报。`config\app.toml` 中 `[sina].shadow_enabled` 默认保持 `false`。只有在独立测试窗口中，才按以下顺序操作：生成并人工核对 manifest、低速回填、验证 checkpoint/事实哈希、启用当日 16:35 后 shadow、累计 20 个完整收盘交易日、审阅差异和非商业用途条款、生成资格报告、主机批准，最后才允许用户通过 MCP 明确登记 provider 能力。该登记只写入治理 registry；当前 v0.3 不读取新浪，只有以后显式兼容 Sina 的 v4 生产组合才可消费该登记。

```powershell
& 'E:\StockMcp\current\.venv\Scripts\python.exe' -m stock_mcp.cli `
  prepare-sina-backfill-manifest --root E:\StockMcp `
  --start 2023-08-08 --end 2026-08-07 `
  --manifest E:\StockMcp\config\sina-backfill-manifest.json

& 'E:\StockMcp\current\.venv\Scripts\python.exe' -m stock_mcp.cli `
  backfill-sina --root E:\StockMcp `
  --manifest E:\StockMcp\config\sina-backfill-manifest.json

& 'E:\StockMcp\current\.venv\Scripts\python.exe' -m stock_mcp.cli `
  verify-sina-backfill --root E:\StockMcp `
  --manifest E:\StockMcp\config\sina-backfill-manifest.json
```

压缩 KLC 已由纯 Python 位流解码器和固定录制夹具覆盖，但真实端点单位人工对照、Windows 断点续跑和 20 日 shadow 仍是外部门禁。任何 KLC 完整性校验失败都必须终止，不能改用远端复权脚本、执行 JavaScript 或拿 Tushare 填补新浪缺口。资格未达到 `qualified_for_manual_approval` 时，不得执行 `approve-provider-source` 或 `activate_provider_source`。

v4 研究入口和查询 DTO 已公开，但研究启动在完整 outcome/benchmark worker 未安装时会明确返回 `v4_research_rejected`，不会留下永远停在 `queued` 的假作业。不得将 Schema、CLI 或 DTO 存在描述为已完成研究、Sina replication、proposal、认证或激活。

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

两个服务使用彼此独立的受限内置账户：`StockMcpService` 使用 `NT AUTHORITY\LocalService`，`StockMcpTunnel` 使用 `NT AUTHORITY\NetworkService`。程序目录和版本目录对两个账户都只读；只有应用服务账户可以修改数据、备份和状态，只有 Tunnel 服务账户可以读取 Tunnel 运行密钥。这一组合兼容不允许 `NT SERVICE\…` 虚拟账户登录的 Windows Server 策略。

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
