# 测试方案：内网安装器与发布约定迁移（internal-installer）

> 活文档：随时更新、不签认；其质量由双 AI 自审（SELF-REVIEW-HOWTO）与行为锚定测试本身保障。
> 依据：本目录 requirements.md 的用户场景与验收口径、design.md（已签认 2026-11-05）§3/§6/§9/§10/§13。
> 下文 `test_install_internal_mirror_scripts.py` 均指 `aixcoding/tests/internal_installer/test_install_internal_mirror_scripts.py`。

## 测试范围与策略

分四层，脚本级契约测试不得替代真实制品验收：

1. **脚本级契约测试**：新建 `test_install_internal_mirror_scripts.py`，复刻上游 `_Server`、fake curl、
   PowerShell web cmdlet/`Start-Process` stand-ins 模式。直接执行新脚本，使用临时目录中的
   fake 离线包和可执行文件；不访问真实镜像、不修改开发者真实安装目录。
2. **静态契约与零漂移检查**：检查新脚本/文档的命名、品牌上下文、参数宣传面和上游零触碰；
   以 `git diff`、上游测试文件零改动及 `gen_customizations.py --strict` 为证据。本批无 C id、
   无 marker，不为纯 aixcoding 树资产新增登记。
3. **真实制品端到端验收**：用 0.33.0 真实离线制品走 POSIX 在线 prod/test、`--from` 离线、
   解压直接启动，以及 Windows 在线、手工 exe 安装/启动。fake 包返回码、URL 断言和静态扫描
   均不得写成这些路径已经实测通过。
4. **文档对账**：一般用户 `INSTALL.md` 只保留默认操作与数据安全提示，不出现内部高级细节；
   `aixcoding/docs/install-uninstall-support.md` 覆盖完整命令、版本/URL、变量优先级、IP、
   渠道、显式 SHA-256、Windows 能力边界、卸载清理和旧包指引；`publishing.md` 对账上传协议。

### 测试基建与隔离

- `_Server` 只伺服既有镜像协议：`channels/cli/{prod,test}.txt` 与
  `releases/cli/<完整版本>/aixcoding-cli-<平台>-v<版本>-offline.{tar.gz,zip}`；
  记录完整 URL 请求序列。**不伺服 `SHA256SUMS.txt`**，所有在线用例反断言零 SUMS 请求。
- fake 包根目录只放当前可执行文件名 `aixcoding-cli` / `aixcoding-cli.exe`；记录
  `--version`、`install`、stdin 和退出码，支持构造损坏包、缺失二进制及 3/126 等失败。
- 每用例隔离 `HOME`、POSIX 安装目录、Windows `LOCALAPPDATA`/`APPDATA`、`TMPDIR`/`TEMP`/`TMP`
  和 PATH；清除继承的 `AIXCODING_BASE_URL`、`AIXCODING_CHANNEL`、`ICODE_BASE_URL`、
  `ICODE_CHANNEL`，防止开发机环境改变优先级结果。
- 外部命令冲突用例对外部 `aixcoding-cli` 文件先做字节快照，结束后逐字节比对；不能只断言
  返回码或警告。安装验证使用安装目录内绝对路径，禁止把 PATH 上的外部命令当作新安装结果。
- POSIX 脚本沿用其实际 Bash 运行形态；文件执行与 `bash -s --` 管道执行都要覆盖。
  ps1 同时覆盖 `irm | iex` 对应的脚本块执行和带显式参数的文件执行。

### 平台与豁免边界

- design.md §10 已批准两项平台测试豁免：ps1 真实路径在非 Windows 环境 `skipif`，以及
  POSIX 真实路径在 Windows 环境使用与上游 `_POSIX_ONLY` 同构的 `skipif`（责任人于
  2026-11-05 补充批准）。实施时就地注释记录理由与批准指向；跳过项不计作对应平台通过证据。
- POSIX 用例的通过证据必须来自 Linux/macOS 实跑，ps1 用例的通过证据必须来自 Windows 实跑；
  不得以其他平台上的兼容层执行或 skip 结果替代。
- Windows on Arm 只承诺 x64 仿真路径与文档口径；没有 Arm 实机时明确记为未实测，不得把
  Windows x64 结果写成 Arm 验证结果。

## 覆盖矩阵

| 验收标准（requirements.md / design.md） | 测试入口 | 类型 |
|---|---|---|
| 内网在线 prod/test、显式版本与镜像 URL 协议 | `test_install_internal_mirror_scripts.py::test_install_sh_online_channel_contract`、`*_explicit_version_skips_channel`、ps1 对应用例 | 契约 |
| `--from` 离线、显式 SHA-256、解压直接启动 | `test_install_sh_offline_from_local_archive`、`test_install_sh_explicit_sha256`；真实制品手工步骤 2/3 | 契约 + 端到端 |
| 显式参数 > `AIXCODING_*` > `ICODE_*` > 默认值 | `test_install_sh_base_url_precedence`、`test_install_sh_channel_precedence` 及 ps1 对应参数化用例 | 契约 |
| 渠道切换不被自动防降级阻断 | `test_install_sh_channel_switch_allows_lower_test_version`；真实 prod/test 切换手工步骤 1 | 契约 + 端到端 |
| 旧 `icode-*` 制品不经新脚本安装 | `test_install_sh_rejects_legacy_named_archive`、`test_install_sh_rejects_legacy_binary`、在线候选负向断言 | 契约 |
| 正常路径 C4 命令契约，无 `aixcoding` 软链 | `test_install_sh_normal_install_command_contract`、ps1 对应用例；真实制品手工步骤 1/4 | 契约 + 端到端 |
| 外部 `aixcoding-cli` 冲突两分支且外部文件不变 | `test_install_sh_external_command_conflict`、`test_install_ps1_external_command_conflict`；安装目录内绝对路径验证 | 契约 |
| 普通新装零旧品牌；必要历史诊断按上下文豁免 | `test_install_sh_normal_install_has_no_legacy_brand_output`、ps1 对应用例、`test_distribution_materials_brand_context` | 契约 + 静态 |
| 0.33 采纳项：冲突预检、Rosetta、幂等、stdin、126/TMPDIR | `test_install_sh_rosetta_selects_aarch64`、`test_install_sh_idempotency`、`test_install_sh_piped_stdin_is_closed`、`test_install_sh_install_failure` | 契约 |
| 连接失败提供 `--base-url` / `AIXCODING_BASE_URL` 逃生门 | `test_install_sh_download_failure_mentions_base_url_escape`、ps1 对应用例 | 契约 |
| 上游公网脚本、测试及 C111-C124 零改动零漂移 | 验证命令 2/3：上游路径 `git diff` + `gen_customizations.py --strict`；上游 `ICODE_VERSION/SOURCE` 消费面不改 | 零漂移检查 |
| 用户 INSTALL.md 简洁可用、支持手册覆盖完整细节、publishing.md 与行为一致 | `test_distribution_materials_static_contract` + 文档对账清单 + 真实制品手工步骤 | 静态 + 人工 |
| DEVELOPMENT/DIRECTORY/README/INDEX/ADR-0020 规范闭环 | `check_docs.py`、DIRECTORY 判定树人工核对、`gen_customizations.py --strict` | 文档门禁 |
| 0.33 真实制品在线/离线/直接启动验收 | 手工验证步骤 1-4；结果逐平台记入 verification.md | 端到端 |

## 用例清单

### A. POSIX `install-internal-mirror.sh`

| 用例（建议名） | 构造与关键断言 |
|---|---|
| `test_install_sh_online_channel_contract` | 参数化 file/piped、prod/test；channel 首行允许 `v` 前缀与 CRLF/空白。精确断言请求仅为 channel → 当前 `aixcoding-cli` 包；fake 二进制依次收到 `--version`、`install`；零 SUMS 请求 |
| `test_install_sh_explicit_version_skips_channel` | `--version v0.33.0` 与等号形式；不请求 channel，下载 `releases/cli/0.33.0/` 下正确包；非法、空版本在安装前失败 |
| `test_install_sh_base_url_precedence` | 参数化默认、仅旧变量、新旧并存、显式参数与新旧并存；分别断言默认 IP、旧地址、新地址、显式地址，覆盖 `--base-url` 与等号形式 |
| `test_install_sh_channel_precedence` | 参数化默认 prod、仅 `ICODE_CHANNEL=test`、`AIXCODING_CHANNEL=prod` 压过旧 test、显式 `--channel test` 压过两个变量；断言实际 channel URL |
| `test_install_sh_current_asset_candidates_only` | tar.gz 缺失时仅回退当前命名的 zip；两种当前包均缺失则失败；请求中无任何 `icode-*`，无公网源回退、无 SUMS 请求 |
| `test_install_sh_offline_from_local_archive` | 参数化 tar.gz/zip；`--from` 与等号形式均成功，零网络请求，解压根当前二进制通过自检并安装；`--from` 与 `--version` 同用、空路径、不存在路径和不支持扩展名均失败 |
| `test_install_sh_rejects_legacy_named_archive` | `--from` 传 `icode-*` 命名包；解包/自检/install 均未发生，错误只引导从镜像获取最新版制品，不推荐旧安装脚本 |
| `test_install_sh_rejects_legacy_binary` | 当前文件名压缩包内仅放历史 `icode`；报包内缺少根目录 `aixcoding-cli`，不执行历史二进制 |
| `test_install_sh_explicit_sha256` | 在线和 `--from` 各至少一条：匹配摘要（含大写输入）成功；不匹配时拒绝执行包内二进制；非法长度/字符在下载前失败；全程零 SUMS 请求 |
| `test_install_sh_external_command_conflict` | ①外部命令+无自有 `chrys`：下载前中止；②外部命令+自有可执行 `chrys`：警告续装；③自有 `chrys` 存在但不可执行：按无自有安装中止。三例均断言外部文件字节不变；续装例以安装目录内 `chrys --version` 验证，不调用外部 `aixcoding-cli` |
| `test_install_sh_idempotency` | 同版本且 alias 完整：可读 channel 但不下载包、不重装；同版本 alias 缺失：重新执行安装修复；不得引入上游自动防降级 |
| `test_install_sh_channel_switch_allows_lower_test_version` | 已装 prod 高版本，test channel 指向较低版本；仍下载并安装 test 包，断言没有“较新版本已安装而跳过”的行为 |
| `test_install_sh_piped_stdin_is_closed` | 以 `bash -s --` 从 stdin 读脚本；fake 包在 `install` 时尝试读 stdin，必须立即得到 EOF，不能读到脚本余文或阻塞 |
| `test_install_sh_install_failure` | fake 包 install 返回 3：脚本失败并报告安装失败；返回 126：脚本失败且提示将 `TMPDIR` 换到允许执行的目录；两种失败都不伪装成功 |
| `test_install_sh_rosetta_selects_aarch64` | macOS `uname -m=x86_64` 且 `sysctl.proc_translated=1`：选择 `macos-aarch64` 包；非 Rosetta x86_64 仍选 x86_64 |
| `test_install_sh_platform_and_libc_guards` | Linux glibc 下限维持 0.28 口径；musl 拒绝且文案使用 AIxCoding；未知平台/架构在下载前失败 |
| `test_install_sh_normal_install_command_contract` | 正常路径不创建 `aixcoding` 软链；包内 install 后验证安装目录内 `aixcoding-cli --version`；帮助、完成提示与 rc 新增注释只使用当前命令/品牌，临时目录前缀为 `aixcoding-install.*` |
| `test_install_sh_normal_install_has_no_legacy_brand_output` | 普通新装的 stdout/stderr、help、rc 新增内容不出现 `iCode`/`icode` 旧品牌或推荐命令；不扫描存量 rc 注释，不要求清理用户既有内容 |
| `test_install_sh_download_failure_mentions_base_url_escape` | channel 或制品连接失败；错误包含 `--base-url <地址>` 与 `AIXCODING_BASE_URL=<地址>` 指引，不建议公网源或旧脚本 |

### B. Windows `install-internal-mirror.ps1`

| 用例（建议名） | 构造与关键断言 |
|---|---|
| `test_install_ps1_online_channel_contract` | Windows PowerShell 5.1 可用时纳入参数化；默认 prod 与 test 均精确请求 channel → `aixcoding-cli-windows-x86_64-v<版本>-offline.zip`；根目录 exe 自检后以 `install` 启动；零 SUMS 请求 |
| `test_install_ps1_explicit_version_skips_channel` | `-Version v0.33.0` 不请求 channel，版本 trim 后 URL 正确；空/非法版本失败 |
| `test_install_ps1_base_url_precedence` | 与 POSIX 同四级：显式 `-BaseUrl` > `AIXCODING_BASE_URL` > `ICODE_BASE_URL` > 默认 IP |
| `test_install_ps1_channel_precedence` | `-Channel` > `AIXCODING_CHANNEL` > `ICODE_CHANNEL` > prod；非法 channel 在下载前失败 |
| `test_install_ps1_external_command_conflict` | 外部命令+无自有 `chrys.exe` 时中止；有自有安装时警告续装；两例外部文件字节不变，续装提示以 `chrys` 启动，不用 PATH 外部命令验证成功 |
| `test_install_ps1_rejects_bad_archive_and_failed_install` | zip 根目录缺 `aixcoding-cli.exe`、自检非零、install 非零分别失败；不得打印安装完成；临时目录按既有 finally 语义清理 |
| `test_install_ps1_download_failure_mentions_base_url_escape` | channel/制品下载失败时给出 `-BaseUrl` 或 `AIXCODING_BASE_URL` 逃生门；无 GitHub/GitCode 回退 |
| `test_install_ps1_current_brand_and_asymmetry` | 普通新装输出无旧品牌/旧推荐命令；脚本参数与支持手册均不宣称 Windows 脚本支持 `--from`、SHA-256 或 Arm 原生包；64 位 Windows 固定下载 x86_64 包，Windows on Arm 仅文档承诺仿真路径 |

### C. 静态契约与文档对账

`test_distribution_materials_static_contract` 与 `test_distribution_materials_brand_context`
只检查稳定的公共契约，不逐字锁死整份文档：

- 两个新脚本默认根地址均为 `http://82.187.34.98/aixcoding/file/aixcoding`；帮助/参数宣传面
  只推荐 `AIXCODING_*` 与显式参数，旧 `ICODE_*` 只作为兼容输入，不作为推荐配置。
- 制品命名只认 `aixcoding-cli-*`；无 `create_launch_command`、无创建 `aixcoding` 软链、无
  GitHub/GitCode 公网回退、无自动消费 `SHA256SUMS.txt`。
- 品牌检查按行/语境分类：当前品牌与启动指引使用 AIxCoding / `aixcoding-cli`；允许
  `iCode-TUI` 分发身份提示、旧 alias 迁移/保护诊断，以及支持手册旧包事实节中的必要历史
  引用；用户 INSTALL.md 不出现旧品牌。普通新装行为测试仍是主证据，静态白名单不得扩大成
  全局字符串豁免。
- 用户 INSTALL.md 只含默认操作、数据安全与求助路径，不含 `--channel`、变量、内部 `chrys`
  或镜像协议；支持手册明示平台差异、Windows on Arm x64 仿真、显式 SHA-256、版本/URL、
  卸载清理及旧包事实；`publishing.md` 与实际上传协议一致。
- 上游 `scripts/install.sh`、`scripts/install.ps1`、`tests/app/test_install_scripts.py` 不在本批
  diff；上游 `ICODE_VERSION`/`ICODE_SOURCE` 不因本批新增双读而改变。

## 异常与降级路径

- **channel 不可达/为空/版本非法**：不下载或执行制品；报错指出对应 channel，并保留显式
  `--version` 或 `--from`（仅 POSIX）的可行动指引。
- **制品缺失与连接失败分开**：当前 tar.gz 缺失可按既有协议尝试当前 zip；不得因此请求旧
  `icode-*` 或公网源。非缺文件类网络错误按脚本既有失败语义处理，并展示地址逃生门。
- **校验失败**：显式摘要 mismatch 时不得解包执行；未传 `--sha256` 不自动下载 SUMS，也不得
  把“未校验”描述成“校验通过”。
- **外部命令冲突**：无自有安装在下载前中止；有自有安装只警告续装。两条路径都不能改外部
  文件，冲突续装不能用外部 PATH 命令作为成功证据。
- **包结构/自检/install 失败**：缺当前二进制、版本无输出、自检非零、install 非零均失败；
  POSIX 126 额外给 TMPDIR noexec 指引。
- **幂等与渠道切换**：同版本完整安装跳过下载；alias 缺失要修复；自动 channel 解析不得因
  目标版本更低而阻断合法 prod/test 切换。
- **品牌降级**：历史诊断可以出现必要旧名，但普通新装、帮助和完成提示不得把旧名作为当前
  品牌或推荐命令；测试不得用全局“任何位置零旧字符串”替代上下文判断。
- **回滚**：镜像回滚只能重传最近一份符合 C4 契约的新脚本改名产物；不得以 0.28 旧脚本为
  回滚目标。回滚清单须同时覆盖本批修改的规范与导航文件。

## 真实制品手工验证步骤

前置：使用既有构建链准备 0.33.0 真实制品；在隔离镜像或内网 test 通道按
`releases/cli/<完整版本>/` → `channels/cli/*.txt` 顺序布置。记录 OS、架构、制品版本、
实际 base URL 和是否使用显式 SHA-256；所有结果记入 verification.md。

1. **POSIX 在线 prod/test**：分别经镜像根 `install.sh`（仓内 `install-internal-mirror.sh` 的上传改名
   产物）安装 prod、切换 test；新开终端执行 `aixcoding-cli --version`，检查安装目录内
   `chrys` 与 alias，无 `aixcoding` 软链。重跑同版本验证幂等；如有外部命令冲突场景，另验
   外部文件哈希不变并以安装目录内 `chrys --version` 验证。
2. **POSIX 离线**：下载当前包后执行 `bash install-internal-mirror.sh --from <包> --sha256 <摘要>`；
   用不可用 base URL 或断网证明零网络依赖。再以错误摘要重跑，必须拒绝安装。
3. **解压直接启动**：解压真实包后直接执行根目录 `aixcoding-cli --version`；macOS 如经浏览器
   下载，按支持手册核对隔离标记提示。旧包只核对手工事实提示，不用新脚本安装。
4. **Windows 在线与手工 exe**：经镜像根 `install.ps1` 完成在线安装，新开 PowerShell 验证
   `aixcoding-cli --version`；离线场景解压 zip 后直接运行 `.\aixcoding-cli.exe install` 并验证。
   记录实测为 x64 还是 Windows on Arm 仿真；无 Arm 实机则明确写未实测。
5. **发布文档走查**：以 `publishing.md` 操作一份测试发布，核对脚本上传改名、先制品后指针、
   prod/test 灰度、可选 SUMS 仅供手工核对；再按 IP 迁移操作序做桌面推演，不直接改生产指针。
6. **文档/品牌走查**：用户 INSTALL.md 命令可由一般用户直接执行且无内部细节；支持手册
   技术命令与脚本一致；普通新装无旧品牌推荐，合法历史诊断不被误删，旧包指引不推荐旧脚本。

## 本地验证与证据命令

在仓库根以大写盘符启动；Windows cmd 先执行 `set "PYTHONPATH="` 与 `set "PYTHONUTF8=1"`。
实际执行结果按命令逐条追加 verification.md，未执行项保持“未运行”。

1. 定制套件与受影响上游测试：
   `uv run python aixcoding/tools/verify.py --smart --paths <本批全部新增/修改路径>`。
   POSIX 与 Windows 各自的实跑范围分开记录，skip 不计通过。
2. 上游零触碰：
   `git diff --exit-code -- scripts/install.sh scripts/install.ps1 tests/app/test_install_scripts.py`，
   预期零输出、退出码 0。
3. 登记零漂移：`uv run python aixcoding/tools/gen_customizations.py --strict`，预期
   0 error / 0 warning，登记基线保持 98 行/135 marker，无新增 C id。
4. 文档门禁：`uv run python aixcoding/tools/check_docs.py`，预期 OK。
5. 新 Python 测试文件格式与 lint：
   `uv run ruff check --no-fix aixcoding/tests/internal_installer/test_install_internal_mirror_scripts.py`；
   `uv run ruff format --check aixcoding/tests/internal_installer/test_install_internal_mirror_scripts.py`。
6. 将上述自动证据与真实制品手工结果合并对照 design.md §13 六项验收；任何未跑平台、
   未跑真实路径或环境性跳过均作为覆盖缺口明示，不以契约测试替代。
