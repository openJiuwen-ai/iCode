# AIxCoding 内网安装与卸载支持手册（开发 / 技术支持）

> 本文供开发人员和技术支持同事排查安装、升级、卸载问题，**不是一般用户的默认阅读入口**。
> 一般用户使用 [../release/INSTALL.md](../release/INSTALL.md)；发布方镜像操作见
> [batches/internal-installer/publishing.md](batches/internal-installer/publishing.md)。
> 本文描述当前实现，不代替批次设计、真实制品验证记录或镜像服务端配置。

## 1. 支持时的默认原则

1. 普通用户优先使用默认 prod 在线安装；test、指定版本、镜像覆盖和手工 PATH 处理只在
   确认需要时提供。
2. 升级不需要先卸载，配置和会话保留。
3. 默认卸载保留用户数据；只有用户明确要求删除配置、登录信息、模型配置和会话并完成
   备份后，才提供 `--purge`。
4. 不删除 PATH 上属于外部程序的 `aixcoding-cli`，不推荐使用旧安装脚本，不让用户在
   Linux/macOS 运行 Windows `.exe` 包。
5. 收集信息至少包括：系统及版本、CPU 架构、安装方式、完整包名（离线时）、实际命令、
   完整 stdout/stderr、是否曾存在其他 `aixcoding-cli`。

## 2. 命令、文件与数据目录

### 2.1 用户命令与底层程序

| 阶段 | Linux / macOS | Windows |
|---|---|---|
| 离线包根目录可执行文件 | `aixcoding-cli` | `aixcoding-cli.exe` |
| 包内 `install` 复制的主体 | `~/.local/bin/chrys` | `%LOCALAPPDATA%\chrys\bin\chrys.exe` |
| 安装后产品命令 | `~/.local/bin/aixcoding-cli` → `chrys` 软链接 | `aixcoding-cli.exe`：优先硬链接，失败时复制 |
| 冲突场景备用命令 | `chrys` | `chrys.exe` |

两者不是两套独立安装。普通用户使用 `aixcoding-cli`；只有外部命令冲突或 PATH 排查时
才使用安装目录内 `chrys` 的绝对路径。

### 2.2 用户数据

| 平台 | 默认数据目录 |
|---|---|
| Linux / macOS | `~/.chrys` |
| Windows | `%APPDATA%\chrys` |

其中包括配置、API keys、模型配置、agents、skills 和会话等。普通安装、升级和普通卸载
均保留这些数据。下载的脚本、压缩包、解压目录与 PyApp 运行缓存不属于该数据目录。

### 2.3 运行环境与服务配置

AIxCoding 的共享配置文件为上述数据目录下的 `aixcoding.toml`：

- 顶层 `environment = "local" / "dev" / "prod"` 选择 auth 与模型目录的部署环境；
  `AIXCODING_ENVIRONMENT` 的优先级高于 TOML，均未设置时为 `prod`。
- `[model_catalog].base_url` 可选，覆盖当前 tier 的模型目录服务基址；完整请求路径仍由产品拼接。
- `[logging]` 支持 `level` 与 `panel`，分别控制文件日志级别和 F6 面板摘要冒泡。

本地 mock 验证使用 `AIXCODING_ENVIRONMENT=local`，详细配方见
[../mock/README.md](../mock/README.md)。旧 `AIXCODING_AUTH_ENVIRONMENT`、
`CHRYS_AUTH_ENVIRONMENT` 与旧 Settings 键 `model.catalog.base_url` 均不生效，也不提供检测、
别名或迁移警告；排障时不得继续建议这些旧配置。

## 3. 平台支持矩阵

| 系统 | 支持范围 |
|---|---|
| Linux（含银河麒麟） | x86_64：glibc ≥ 2.17；ARM64（aarch64）：glibc ≥ 2.18；不支持 Alpine 等 musl 系统 |
| macOS | Apple 芯片（ARM64）或 Intel（x86_64）；`uname -m=x86_64` 且 `sysctl.proc_translated=1` 时按 Rosetta 选择 ARM64 包 |
| Windows | 64 位 Windows；下载 `windows-x86_64`。Windows on Arm 接受 x64 仿真，无 Arm 原生制品 |

## 4. 镜像协议与下载地址

### 4.1 默认地址与仓内物料

默认镜像根地址：

```text
http://82.187.34.98/aixcoding/file/aixcoding
```

| 仓内物料 | 镜像根目录名称 |
|---|---|
| `aixcoding/release/install-internal-mirror.sh` | `install.sh` |
| `aixcoding/release/install-internal-mirror.ps1` | `install.ps1` |
| `aixcoding/release/INSTALL.md` | 用户安装/卸载手册，可随制品分发 |

上游 `scripts/install.sh/ps1` 是另一套公网安装器，不得拿来做内网协议判断或覆盖。

### 4.2 channel 与 releases

```text
channels/cli/prod.txt
channels/cli/test.txt
releases/cli/<完整版本>/aixcoding-cli-<平台>-v<完整版本>-offline.tar.gz
releases/cli/<完整版本>/aixcoding-cli-windows-x86_64-v<完整版本>-offline.zip
```

- channel 文件首行给出完整版本，允许开头 `v`、CRLF 和首尾空白。
- releases 版本目录不带 `v`，制品文件名中的版本带 `v`。
- POSIX 在线安装先尝试当前命名的 `.tar.gz`，缺失时尝试当前命名的 `.zip`；不回退到
  `icode-*`，也不回退到公网源。
- 例如版本 `0.33.0` 的 Linux ARM64 包：

  ```text
  http://82.187.34.98/aixcoding/file/aixcoding/releases/cli/0.33.0/aixcoding-cli-linux-aarch64-v0.33.0-offline.tar.gz
  ```

- 查询当前正式版本可读取 `channels/cli/prod.txt`，测试版本读取 `test.txt`。这是一项
  支持/发布操作，不要求普通用户执行。

### 4.3 参数与环境变量优先级

| 配置 | 优先级（从高到低） |
|---|---|
| 镜像地址 | `--base-url` / `-BaseUrl` → `AIXCODING_BASE_URL` → `ICODE_BASE_URL` → 默认 IP |
| 渠道 | `--channel` / `-Channel` → `AIXCODING_CHANNEL` → `ICODE_CHANNEL` → `prod` |

`ICODE_VERSION`、`ICODE_SOURCE` 是上游公网安装器的变量，内网安装器不消费它们。

### 4.4 SHA-256 口径

- POSIX 只在显式传入 `--sha256 <64位十六进制摘要>` 时校验；支持在线和 `--from`。
- 不匹配时拒绝执行包内程序；大写摘要先归一化再比较。
- 未传参数时不自动请求或消费 `SHA256SUMS.txt`。该文件可由发布方提供用于人工核对。
- Windows 脚本无 `--from` 和 SHA-256 参数，Windows 手工安装可在解压前用 `Get-FileHash`。

## 5. 全部安装方式

### 5.1 Linux / macOS 在线安装

默认 prod：

```bash
curl -fsSL http://82.187.34.98/aixcoding/file/aixcoding/install.sh | bash
```

test、切回 prod、指定版本：

```bash
curl -fsSL http://82.187.34.98/aixcoding/file/aixcoding/install.sh | bash -s -- --channel test
curl -fsSL http://82.187.34.98/aixcoding/file/aixcoding/install.sh | bash -s -- --channel prod
curl -fsSL http://82.187.34.98/aixcoding/file/aixcoding/install.sh | bash -s -- --version v0.33.0
```

更换镜像时，脚本 URL 和 `--base-url` 都要改：

```bash
curl -fsSL http://<镜像地址>/aixcoding/file/aixcoding/install.sh | bash -s -- --base-url http://<镜像地址>/aixcoding/file/aixcoding
```

如需先检查脚本：

```bash
curl -fsSL -o install.sh http://82.187.34.98/aixcoding/file/aixcoding/install.sh
bash install.sh --channel prod
```

### 5.2 Windows 在线安装

默认 prod：

```powershell
irm http://82.187.34.98/aixcoding/file/aixcoding/install.ps1 | iex
```

test 与切回 prod：

```powershell
$env:AIXCODING_CHANNEL = 'test'; irm http://82.187.34.98/aixcoding/file/aixcoding/install.ps1 | iex
Remove-Item Env:AIXCODING_CHANNEL; irm http://82.187.34.98/aixcoding/file/aixcoding/install.ps1 | iex
```

显式参数和镜像覆盖使用文件执行：

```powershell
Invoke-WebRequest http://82.187.34.98/aixcoding/file/aixcoding/install.ps1 -OutFile .\install.ps1
.\install.ps1 -Channel test
.\install.ps1 -Version v0.33.0
.\install.ps1 -BaseUrl http://<镜像地址>/aixcoding/file/aixcoding
```

本地执行脚本仍可能在线下载安装包；是否为离线安装取决于包是否已在本地且安装过程零网络。

### 5.3 Linux / macOS 离线脚本安装

先在可联网机器取得脚本和匹配包，再复制到离线机器。版本可由发布方提供，也可查询目标
channel：

```bash
# 查询正式渠道；测试渠道将 prod.txt 换成 test.txt
curl -fsSL http://82.187.34.98/aixcoding/file/aixcoding/channels/cli/prod.txt
```

以查询结果为 `0.33.0`、目标为 Linux ARM64 为例，下载脚本和安装包：

```bash
curl -fsSL -o install.sh http://82.187.34.98/aixcoding/file/aixcoding/install.sh
curl -fsSL -o aixcoding-cli-linux-aarch64-v0.33.0-offline.tar.gz http://82.187.34.98/aixcoding/file/aixcoding/releases/cli/0.33.0/aixcoding-cli-linux-aarch64-v0.33.0-offline.tar.gz
```

实际使用时替换版本、平台和扩展名；`.zip` 包将 URL 与输出文件名中的 `.tar.gz` 都改为
`.zip`。通过合规介质将两个文件复制到离线机器的同一目录后执行：

```bash
bash install.sh --from ./aixcoding-cli-linux-aarch64-v<版本>-offline.tar.gz
bash install.sh --from ./aixcoding-cli-linux-aarch64-v<版本>-offline.tar.gz --sha256 <摘要>
```

- 支持 `.tar.gz`、`.tgz`、`.zip`；`--from` 与 `--version` 互斥。
- `icode-*` 命名包直接拒绝；当前命名包内只有历史 `icode` 也会因缺少根目录
  `aixcoding-cli` 而拒绝。
- 本地文件不存在、格式不支持、摘要非法或不匹配均在执行包内程序前失败。

### 5.4 Linux / macOS 无脚本手工安装

此方式只需取得安装包，不需要 `install.sh`。按 §5.3 的版本查询和包下载步骤取得匹配包，
复制到离线机器并解压，然后进入解压目录：

```bash
chmod +x ./aixcoding-cli
./aixcoding-cli --version
./aixcoding-cli install
```

包内安装器复制到 `~/.local/bin/chrys` 并创建 `aixcoding-cli` alias。它只打印 PATH 指引，
不自动修改 rc；必要时由用户添加：

```bash
export PATH="$HOME/.local/bin:$PATH"
```

### 5.5 Windows 无脚本手工安装

此方式只需 Windows ZIP，不需要 `install.ps1`。以版本 `0.33.0` 为例，可在可联网机器
下载并复制到离线机器：

```powershell
Invoke-WebRequest http://82.187.34.98/aixcoding/file/aixcoding/releases/cli/0.33.0/aixcoding-cli-windows-x86_64-v0.33.0-offline.zip -OutFile .\aixcoding-cli-windows-x86_64-v0.33.0-offline.zip
```

解压后进入解压目录：

```powershell
.\aixcoding-cli.exe --version
.\aixcoding-cli.exe install
```

安装到 `%LOCALAPPDATA%\chrys\bin`，创建产品命令并尝试更新用户 PATH；PATH 更新失败时
安装器发出警告。Windows 离线不经过 `install.ps1`。

### 5.6 只解压直接启动

```bash
# Linux / macOS
chmod +x ./aixcoding-cli
./aixcoding-cli

# macOS 隔离提示，确认来源可信后
xattr -d com.apple.quarantine ./aixcoding-cli
```

```powershell
# Windows
.\aixcoding-cli.exe
```

直接启动会准备 PyApp 运行时并可能产生用户数据，但不创建全局命令或修改 PATH。

> 历史包事实提示：旧版包内可执行文件可能叫 `icode` / `icode.exe`，手工直接启动按实际
> 文件名处理。当前安装脚本不安装旧包；需要安装命令时获取最新 `aixcoding-cli` 制品，
> 不推荐旧安装脚本。

### 5.7 手工 SHA-256

```bash
# macOS
shasum -a 256 ./<安装包文件名>

# Linux
sha256sum ./<安装包文件名>
```

```powershell
Get-FileHash -Algorithm SHA256 .\<安装包文件名>
```

输出与发布方摘要不一致时，不得执行包内程序。

## 6. 安装期行为与故障处理

### 6.1 外部 `aixcoding-cli` 冲突两分支

- 无自有可执行 `chrys`：下载前中止，避免包内 alias 保护留下外部命令而“安装成功”假象。
- 自有 `chrys` 存在且可执行：警告续装，外部文件保持不变；安装后用安装目录内
  `chrys --version` 验证并提示 `chrys` 启动。
- 不调用 PATH 上外部 `aixcoding-cli` 作为续装成功证据。Windows 使用 `chrys.exe` 同构处理。

### 6.2 幂等与渠道切换

- POSIX 目标版本与已安装 `chrys --version` 相同且 `aixcoding-cli` alias 存在时，读取
  channel 后跳过包下载和安装。
- alias 缺失时重装修复。
- 不采用公网脚本的自动防降级；从较高 prod 切到较低 test 仍允许安装目标渠道版本。

### 6.3 PATH 与安装后验证

| 方式 | PATH 行为 |
|---|---|
| POSIX 内网脚本 | 按需写入 `~/.bashrc` / `~/.zshrc`，必要时补 `.bash_profile` |
| POSIX 包内手工 `install` | 只打印指引，不自动改 rc |
| Windows 包内 `install` | 尝试更新用户 PATH |
| 只解压直接运行 | 不修改 PATH |

内网脚本正常路径验证安装目录内 `aixcoding-cli --version`，冲突续装验证 `chrys --version`。

### 6.4 常见安装错误

| 现象 | 处理 |
|---|---|
| channel / 包下载失败 | 检查内网；镜像迁移时用 `--base-url` / `-BaseUrl` 或 `AIXCODING_BASE_URL` |
| `musl` 不支持 | 换 glibc 发行版；x86_64 ≥ 2.17，aarch64 ≥ 2.18 |
| 退出码 126 / 临时目录不可执行 | 将 `TMPDIR` 指向允许执行且有空间的目录 |
| 包内缺 `aixcoding-cli` | 核对包平台、根目录结构和是否为历史包 |
| Windows 缺根目录 exe / 自检或 install 非零 | 安装失败，临时目录由 finally 清理；保留完整错误 |
| Rosetta 下载错架构 | 检查 `sysctl.proc_translated=1` 时应选择 `macos-aarch64` |
| POSIX 脚本以 root / `sudo` 运行 | 安装目录与 rc 写入随 `$HOME` 落到 `/root`（如 `/root/.local/bin`、`/root/.bashrc`），实际用户不可见；脚本无 root 检测（按 internal-installer 评审 C4 裁决，仅文档提示）。处置＝`su` 到目标用户重装；ps1 走 `LOCALAPPDATA` 无此问题 |

## 7. 卸载与清理

### 7.1 卸载命令选择

| 场景 | 命令 |
|---|---|
| 正常安装 | `aixcoding-cli uninstall` |
| 外部命令冲突 / 产品命令不在 PATH | `~/.local/bin/chrys uninstall` 或 `& "$env:LOCALAPPDATA%\chrys\bin\chrys.exe" uninstall` |
| 非交互执行 | 上述命令加 `--yes`，否则退出码 1 且零删除 |
| 只解压直接运行 | 在解压目录执行 `./aixcoding-cli uninstall` 或 `.\aixcoding-cli.exe uninstall`，再删除解压目录 |

### 7.2 普通卸载

默认删除离线安装主体、属于本安装的 alias、可识别 PyApp 运行缓存和 Windows 用户 PATH
项，保留用户数据。外部程序和归属外部程序的命令不删除。Windows 对正在使用的文件可能
在命令退出后完成删除；支持时先让用户退出其他实例。

### 7.3 `--purge`

```bash
aixcoding-cli uninstall --purge
aixcoding-cli uninstall --purge --yes
```

删除程序及默认数据目录，并处理可识别的自定义绝对路径 sessions 子目录。自定义相对
session root 不会跨工作目录逐个删除，卸载器会提示；支持人员不得宣称这些会话已清理。
执行前必须确认备份和用户意图。

### 7.4 直接下载包的缓存

直接运行包后执行 `uninstall` 可清理其 PyApp 运行缓存，但下载包本身不会被删除；命令
结束后仍需删除压缩包和解压目录。从未运行过的解压包可直接删除。

### 7.5 POSIX rc 残留

包内卸载器不清理内网 POSIX 脚本写入的配置。仅删除脚本新增部分：

```bash
# Added by AIxCoding installer
export PATH="$HOME/.local/bin:$PATH"
```

`.bash_profile` 中加载 `.bashrc` 的行如果是脚本此前补入，可按需清理；不得删除用户原有
或其他程序仍使用的 PATH 配置。纯手工安装由用户按实际修改记录清理。

### 7.6 下载物

`install.sh`、`install.ps1`、ZIP/tar.gz 和解压目录不会被卸载器一律自动删除；支持人员
应将其与已安装程序、PyApp 缓存、用户数据分开说明。

## 8. 支持处置清单

1. 先判断用户是“未安装”“已安装”“直接运行”“外部命令冲突”中的哪一种，再提供命令。
2. 普通升级不给卸载命令；普通卸载不给 `--purge`；数据删除前二次确认。
3. 下载问题先核对完整 URL、channel 指向和 releases 实际包名，不让用户自行猜版本。
4. 命令不存在时先重开终端，再检查安装目录内绝对路径；不要把外部 PATH 命令当成本次安装。
5. 需要完整清理时按“程序 → 运行缓存 → 用户数据 → PATH/rc → 下载物”逐项确认，
   不用一条强制删除命令覆盖所有情况。
6. 真实制品验收状态见
   [batches/internal-installer/verification.md](batches/internal-installer/verification.md)；
   文档示例和 fake 契约测试不等于某平台已实测通过。
