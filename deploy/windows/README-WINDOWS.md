# Stock MCP Windows Server 部署手册

本项目采用原生 Windows x64 部署，不要求 Docker、WSL、Git、Node、系统 Python 或独立数据库服务器。MCP 进程只监听 `127.0.0.1:8765/mcp`；Secure MCP Tunnel 作为独立服务，仅建立出站 HTTPS 连接，因此不需要配置入站防火墙规则。正式服务器的默认入口是可信发布 ZIP 中的一条 `install.ps1` 命令；密钥配置仍单独进行安全交互，不能合并为命令行参数。下文“源码直接安装”只适用于已受控 Git 工作区的维护升级，不是无 Git 服务器的前置条件。

Schema v11 加入可选新浪数据链路和 v0.4 研究表，但默认不启动新浪网络任务。`config\app.toml` 的 `[sina] shadow_enabled=false` 是安全默认值；安装、配置和升级均不会自动回填新浪、启动 20 日 shadow、激活数据源、创建 v0.4 策略或改变当前 v0.3 日报。

Schema v14 追加 v3 历史生产仿真证据表；升级只迁移 SQLite 元数据，不会自动生成仿真、
不会改变策略状态，也不会将历史结果伪装为真实观察。

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

Windows PowerShell 5.1 可能破坏传给原生 `python -c` 的嵌套引号。不要用长 `-c` 命令检查数据库。更新源码后、安装前使用独立只读脚本；安装后使用正式 CLI：

```powershell
& 'E:\StockMcp\current\.venv\Scripts\python.exe' `
  E:\code\stock-mcp\scripts\database_preflight.py `
  --database E:\StockMcp\data\stock-mcp.sqlite3

& 'E:\StockMcp\current\.venv\Scripts\python.exe' -m stock_mcp.cli `
  inspect-database --root E:\StockMcp
```

两条命令都以只读模式打开 SQLite，返回 Schema、`integrity_check`、Tushare 交易日数和日线行数，不执行迁移或修改事实。

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

`StockMcpService` 会对按需的东财确认行情主机 `82.push2.eastmoney.com` 设置仅该主机的直连例外；确认工具只请求对应证券的一条东财报价，不通过 AKShare 拉取全市场分页数据。Tunnel 仍使用其独立配置的代理。若服务器不能直连该主机，候选的“次日确认”会明确返回“暂无法核验”，不会影响盘后日报或把网络异常伪装成确认结果。

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

长时间回填应通过 `start-sina-backfill.ps1` 启动。新版 worker 会把脱敏的
`sina-backfill-stage` JSON 事件立即写入本轮 `run.log`，记录每只证券的
checkpoint、历史请求、股本请求、转换和数据库写入阶段及耗时，不记录响应正文、
凭证或来源时间戳。可随时只读汇总当前日志：

```powershell
$runRoot = 'E:\StockMcp\state\sina-backfill-runs'
$runDir = (Get-Content (Join-Path $runRoot 'latest-run.txt') -Raw).Trim()

& 'E:\StockMcp\current\.venv\Scripts\python.exe' `
  E:\code\stock-mcp\scripts\sina_backfill_stage_summary.py `
  --log (Join-Path $runDir 'run.log')
```

如果汇总中的 `last_event` 长时间停在某个 `event=start`，该 `stage` 就是当前
阻塞位置。回填期间不要并行启动第二个 worker；网络故障恢复后应保留旧 evidence，
使用同一 manifest 重新启动以从 checkpoint 续跑。

新浪按证券写入多年日线时，不可变冲突检查会按证券和传入日期直接命中 SQLite
主键；不会再逐日读取该日全市场数据后在 Python 中过滤。若阶段日志显示
`database_write` 持续达到数十秒，应停止回填并保留日志，不能通过提高并发规避
数据库瓶颈。

压缩 KLC 已由纯 Python 位流解码器和固定录制夹具覆盖，但真实端点单位人工对照、Windows 断点续跑和 20 日 shadow 仍是外部门禁。任何 KLC 完整性校验失败都必须终止，不能改用远端复权脚本、执行 JavaScript 或拿 Tushare 填补新浪缺口。资格未达到 `qualified_for_manual_approval` 时，不得执行 `approve-provider-source` 或 `activate_provider_source`。

### v4 研究的股本覆盖例外

本轮真实回填确认 41 只证券的新浪股本端点稳定返回空值。它们不得被记为抓取成功，
也不降低 provider 资格对完整 shadow 的要求；但可以从 v4 主研究宇宙中显式排除。
获准清单保存在 `deploy\windows\v4-sina-capital-exclusions-20260812.json`。

生成 v4 研究 manifest 时，必须同时提供完成回填时使用的原始 Sina manifest 和这份
排除清单：

```powershell
& 'E:\StockMcp\current\.venv\Scripts\python.exe' -m stock_mcp.cli `
  build-v4-status-facts --root E:\StockMcp `
  --start 2023-08-08 --end 2026-08-07

& 'E:\StockMcp\current\.venv\Scripts\python.exe' -m stock_mcp.cli `
  backfill-baostock-statuses --root E:\StockMcp `
  --start 2023-08-08 --end 2026-08-07

& 'E:\StockMcp\current\.venv\Scripts\python.exe' -m stock_mcp.cli `
  prepare-v4-study-manifest --root E:\StockMcp `
  --sina-backfill-manifest E:\StockMcp\config\sina-backfill-manifest.json `
  --capital-exclusions E:\code\stock-mcp\deploy\windows\v4-sina-capital-exclusions-20260812.json `
  --manifest E:\StockMcp\config\v4-study-manifest.json
```

`build-v4-status-facts` 不联网，只把旧历史快照中已经通过 BaoStock
`tradeStatus=1`、非 ST 门禁的证券，确定性迁移到 Schema v11 状态事实表。它不会
为旧快照中已被排除的停牌或 ST 证券猜补状态；若存在内容冲突则整次事务回滚。

`backfill-baostock-statuses` 随后按持久化 Tushare 交易日历逐日联网读取 BaoStock
完整证券状态，明确保存 `tradeStatus=0` 停牌日。它使用持久 checkpoint，可安全重跑；
逐日回填只要求 BaoStock 覆盖当天 Tushare 快照以及处于已记录快照生命周期内的证券；
已经越过最后记录日期的退市或离场证券不再被错误要求出现在后续 BaoStock 清单中。
若生命周期中间仍有缺失，命令会打印日期、缺失数量和最多 10 个代码样本并显式失败，
不会猜成停牌。manifest 门禁要求 3,046 只纳入证券在其研究所需日期内均有状态事实，未完成前拒绝生成研究
manifest，而不是把价格和状态双缺失猜成停牌。

成功输出必须记录完整证券宇宙、纳入集、排除集、三组数量和 SHA-256、覆盖率、排除
原因和原始 Sina manifest hash。当前预期为原始宇宙 3,087 只、排除 41 只、研究
纳入 3,046 只，覆盖率 `9867 bps`（98.67%）。任何不在获准 41 只中的新增股本缺口
都会使命令失败；程序不会在运行时动态排除证券。清单写入 SQLite 后不可变，修改
任一集合、计数或哈希都必须生成新的 manifest，不能改写旧研究。

v4 逐日研究 worker 和查询 DTO 已接入。`start_v4_research` 只接受已持久化且完整的
`v4-study-manifest.json`；它创建可恢复作业，不访问新浪或其他实时端点。worker 以一个
信号日为工作单元，共享七臂的市场输入并只计算一次重复候选 outcome；同日缺失的研究臂
在一个事务中原子写入。在盘后窗口让路；Windows 服务重启后从最早缺失信号日续跑。终态
必须通过七臂精确日历和统计证据门禁。

旧研究因已修正的报告终态规则而显示不完整时，不要重跑行情研究。可从不可变逐日结果
生成独立、只读、带来源哈希的修订报告；命令不会修改原研究或 SQLite：

```powershell
& 'E:\StockMcp\current\.venv\Scripts\python.exe' -m stock_mcp.cli `
  derive-v4-study-report --root E:\StockMcp `
  --study-id <已完成的 study_id> `
  --destination E:\StockMcp\state\v4-study-amendment.json
```

修订报告确认完整后，可继续从同一批持久化逐日结果生成只读诊断，不重跑行情、不联网、
不写数据库，也不会创建、认证或激活策略：

```powershell
$studyId = '<已完成的 study_id>'
$diagnostic = "E:\StockMcp\state\$studyId-diagnostic.json"

& 'E:\StockMcp\current\.venv\Scripts\python.exe' -m stock_mcp.cli `
  derive-v4-study-diagnostics --root E:\StockMcp `
  --study-id $studyId `
  --destination $diagnostic

Get-Content -LiteralPath $diagnostic -Raw | Set-Clipboard
```

诊断给出七臂绝对主指标的 20 会话 bootstrap 区间、正收益日比例、日收益下侧 5%
分位、最差连续 20 个信号日区块，以及年度、形态、排名和 10/25/50 bps 成本拆解。
形态和排名拆解以完整信号日日历为分母；某分组当天无候选时计 0，避免只挑有候选的
日期造成虚高。报告同时逐项列出统计、完整性、可执行率和 Sina replication 晋级门禁。
旧研究日没有持久化市场状态，因此该维度明确返回 `unavailable`，程序不会事后猜测。
更新 ChatGPT Connector 后也可直接只读调用 `get_v4_research_diagnostics(study_id=...)`
取得相同内容，无需把本地文件复制进对话。

在没有独立、完整的 Sina 价格 replication artifact 时，正常研究报告应为保留 v0.3、
winner 不合格、proposal 为空。不得把研究完成描述为 Sina replication、provider 资格、
proposal、策略认证或激活已经完成。首次 Windows 全量运行前仍需先通过开发机固定小数据集
E2E；Windows 全量耗时和资源占用属于外部门禁。

## 前向研究证据的盘后增量运行

服务从 `2026-08-08` 之后的已发布日报候选中自动记录两个只读研究假设。日常发布事务会
同时保存当天 Tushare 交易日和确定性主板涨跌停事实；盘后备份前，服务记录候选观察，并为
已经经过 20 个后续交易会话的旧观察追加 5/10/20 日 outcome。研究失败不会撤销日报，日志
只打印日期和异常类型。Sina 降级日报、没有正式发布的观察期日报和零候选日不会伪造观察。

规则引擎 v3 的实时输入按 BaoStock 日历固定前 60 个交易会话。某只股票缺少历史价格时，
只有 SQLite 已记录同日 BaoStock `tradeStatus=0` 才按合法停牌单独排除；状态为可交易或没有
状态证据时，任务必须失败并等待重试。程序不会用第 61 个更早价格补位，也不会因为一只合法
停牌证券而丢弃全市场日报。新上市证券按自身资格排除；防御市场和零候选均可正常发布。
当天 v3 行业解释、涨跌停、Tushare 快照和日报在同一事务内保存。

更新含该修复的版本后，先顺序重启 MCP 与 Tunnel，并验证本机就绪：

```powershell
Stop-Service StockMcpTunnel -Force -ErrorAction SilentlyContinue
Restart-Service StockMcpService -Force

$stable = 0
for ($attempt = 1; $attempt -le 24; $attempt++) {
    Start-Sleep -Seconds 5
    try {
        $mcp = Invoke-RestMethod http://127.0.0.1:8765/readyz -TimeoutSec 3
        if ((Get-Service StockMcpService).Status -eq 'Running' -and $mcp.status -eq 'ready') {
            $stable++
        } else {
            $stable = 0
        }
    } catch {
        $stable = 0
    }
    if ($stable -ge 6) { break }
}
if ($stable -lt 6) { throw 'StockMcpService 未能连续稳定运行 30 秒。' }

Start-Service StockMcpTunnel
Invoke-RestMethod http://127.0.0.1:8766/readyz -TimeoutSec 10
```

预期：两个服务均为 `Running`，8765 返回 `status=ready`，8766 返回 `ready`。不要删除或
改写修复前的失败流水线记录；它是有效审计证据。

### v3 历史生产仿真与真实观察门禁

若盘后流水线曾因 Tushare 历史缺日或 BaoStock 状态缺失失败，先在停服维护窗口使用共享的
生产证据同步器一次性修复完整滚动窗口。该命令只补生产评审和最近 20 日仿真所需的有界
窗口，先自动备份数据库，再使用与真实盘后任务相同的 Tushare/BaoStock、v3 输入构建和
规则引擎完成补数、20 日仿真及目标日 dry-run：

```powershell
$py = 'E:\StockMcp\current\.venv\Scripts\python.exe'

Stop-Service StockMcpTunnel -Force -ErrorAction SilentlyContinue
Stop-Service StockMcpService -Force -ErrorAction SilentlyContinue

& $py -m stock_mcp.cli reconcile-live-observation `
  --root E:\StockMcp `
  --through 2026-08-27

if ($LASTEXITCODE -ne 0) {
    throw 'Live observation reconciliation failed.'
}
```

成功 JSON 必须同时包含 `window_status="ready"`、`price_gap_days_after=0`、
`status_gap_days_after=0`、`simulation_session_count=20`、
`historical_simulation_sessions=20`、`required_live_observation_sessions=3`、
`evidence_class="operator_reconciliation_not_live"`、64 字符的 `input_hash/result_hash`，
以及实际存在的 `backup_path`。`requested_through` 是管理员请求的上界；如果它恰好是上海
本地当天，但 Tushare 和 BaoStock 的完整盘后事实尚未同时发布，命令只在此前 79 个会话均
完整时退到上一交易日，并通过 `trade_date` 与 `skipped_incomplete_trade_dates` 明确记录，
不把当天伪装成成功。旧日期、旧窗口或部分来源异常不会触发该回退，仍会显式失败。

它不会改写旧 `schedule_outcomes`、旧 `pipeline_runs`、日报、
候选或真实观察计数；2026-08-25/26 等既有失败记录必须继续保留。失败时异常中的
`remaining_price_dates`、`remaining_status_dates`、`repair_errors.details.price_failures` 或
`live-v3-evidence-audit-v1` 报告是完整剩余缺口，不得用 Sina、AKShare、较早收盘价或手工
改库绕过。`repaired_*_dates` 只列出复核后确实完整的日期，不再把“尝试过但仍缺失”记为已修复。

命令成功后按顺序启动服务并检查就绪：

```powershell
Start-Service StockMcpService
Start-Sleep -Seconds 30
$mcp = Invoke-RestMethod 'http://127.0.0.1:8765/readyz' -TimeoutSec 5
if ($mcp.status -ne 'ready') { throw 'MCP is not ready.' }

Start-Service StockMcpTunnel
Start-Sleep -Seconds 5
$tunnel = Invoke-RestMethod 'http://127.0.0.1:8766/readyz' -TimeoutSec 5
$tunnelReady = ($tunnel -eq 'ready')
if (-not $tunnelReady -and $tunnel.PSObject.Properties.Name -contains 'status') {
    $tunnelReady = ($tunnel.status -eq 'ready')
}
if (-not $tunnelReady) {
    throw 'Tunnel is not ready.'
}
```

当天可立即运行只读状态检查。因为旧失败审计不会被改写，脚本可能仍以退出码 2 报告
`schedule_is_not_a_successful_observation_or_publication`；但必须显示
`historical_simulation_sessions=20`、`required_live_observation_sessions=3`、
`historical_reconciliation_covers_trade_date=true`，且 `validation.failures` 不再包含
`unresolved_unrecorded_history_gap`：

```powershell
& $py E:\code\stock-mcp\scripts\live_observation_status.py `
  --database E:\StockMcp\data\stock-mcp.sqlite3 `
  --after 2026-08-21
```

生产 v3 输入同时读取 BaoStock 当日的 `tradeStatus` 与 `is_st`。已记录停牌和已记录 ST
都只让对应证券失去当日资格：停牌缺价计入 suspension 证据，ST 缺价计入 eligibility
exclusion；它们不会被误报为可交易证券缺价，也不会降低其他证券的市场覆盖门槛。
`tradeStatus=1` 且 `is_st=0` 的证券若缺少 Tushare 价格仍会聚合失败，不能以 ST 或停牌名义
绕过。

如果最近 20 个**已记录、连续且完整**的 Tushare 交易日及其 BaoStock 状态事实已经可用，
管理员可在停服维护窗口执行一次只读历史生产仿真。它逐日复用生产 v3 输入构建、停牌处理、
行业参考和规则引擎，保存不可变输入/结果哈希；不访问实时行情，不写入历史日报、候选、
流水线运行或前向研究事实。例如，若数据库完整覆盖 2026-07-27 至 2026-08-21 的恰好
20 个交易日：

```powershell
$py = 'E:\StockMcp\current\.venv\Scripts\python.exe'

& $py -m stock_mcp.cli bootstrap-live-observation `
  --root E:\StockMcp `
  --start 2026-07-27 `
  --end 2026-08-21
```

成功 JSON 必须显示 `evidence_class="historical_simulation_not_live"`、`session_count=20`、
`source="tushare"` 和不可变 `manifest_hash`。任一日期缺失、非交易日范围、少于 60 个前序
交易会话、无记录的停牌、行业文件缺失或事实冲突都会非零退出；不得以手工改库、补未来行情或
其他价格源绕过。

**历史仿真不等于真实观察。** 它只证明已入库的历史事实可以按当前部署代码完整重放，不能
声称服务当时真实运行过。因此，满足最近 20 日仿真后，后续仍须积累至少 **3 个**成功真实
盘后会话，期间流水线应为 `degraded_observation` 且存在对应 `observation` 日报；第 4 个
成功真实会话开始才允许状态进入 `ready` 和前向研究批处理。若没有有效仿真，仍保持原来的
20 个真实会话门槛。仿真证据在目标日期之后 35 天失效，数据/策略更新或时间过期后需重新
生成，不能长期绕过真实运行验证。

每个交易日 16:30 后可只读检查：流水线具有非空
`strategy_version=v0.3-policy-1`，观察期状态与证据计数一致：

```powershell
& $py E:\code\stock-mcp\scripts\live_observation_status.py `
  --database E:\StockMcp\data\stock-mcp.sqlite3 `
  --after YYYY-MM-DD
```

输出中的 `historical_simulation_sessions=20` 与 `required_live_observation_sessions=3` 表示
两段式门禁已被识别；`live_observation_sessions` 只统计真实服务的成功盘后会话。合法停牌
证券不会成为候选，但其他证券继续评审。任何 `missing without a recorded suspension` 表示
真实价格/状态证据仍不完整，必须补数据而不是放宽门禁。

安装新版本并重启服务后，可对某个已经发布的日期人工幂等重跑：

```powershell
$py = 'E:\StockMcp\current\.venv\Scripts\python.exe'

& $py -m stock_mcp.cli run-research-forward-batch `
  --root E:\StockMcp `
  --trade-date YYYY-MM-DD
```

成功 JSON 包含 `candidate_count`、`observations_recorded`、`matured_observations`、
`outcomes_recorded` 和 `blocked_observations`。相同日期重跑时新增计数应为 0；缺少目标会话
行情的候选保留为 pending，并计入 blocked，不使用未来恢复交易价格或其他来源补齐。

积累满 20 个后续会话后，用 ChatGPT 的只读 `get_research_forward_report` 或 CLI 查询：

```powershell
& $py -m stock_mcp.cli derive-research-forward-report `
  --root E:\StockMcp `
  --hypothesis-id no-recent-limit-up-v1 `
  --horizon-sessions 20
```

报告仍固定为 `evidence_only`，不能代替独立审阅、Sina 复制、proposal、认证或激活。

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

从源码重复安装时，uv、WinSW 和 tunnel-client 会缓存在
`E:\StockMcp\cache\tools`。该目录仅允许 Administrators 和 SYSTEM 访问；每次复用前
仍按 `tools-manifest.json` 校验最终可执行文件 SHA-256。首次启用缓存时会优先从当前
`runtime\tools` 中播种哈希匹配的文件；只有缓存缺失、损坏或清单版本变化时才重新
下载。缓存不会绕过发布包内部校验或依赖锁验证。

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
