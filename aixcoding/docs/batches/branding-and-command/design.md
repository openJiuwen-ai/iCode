# 设计：功能品牌与命令切换（branding-and-command / C4）

<!--
签认规则（R10）：责任人本人编辑本头部（状态→已确认、填日期，确认意见可选），AI 不得代填。
签认后修改正文需重新签认。校验：uv run python aixcoding/tools/check_docs.py
-->

- 责任人：周彬
- 状态：已确认
- 确认日期：2026-11-03
- 确认意见：无特别意见

> 2026-11-03 责任人重裁：放弃原 `branding-ui` 的"文案先改、入口与安装别名后改"方案，
> 改为运行时品牌、用户命令、离线制品和安装/卸载链同批闭环；纯文档仍后置。原目录改名
> `branding-and-command`，原草案 ADR-0019 同步重写。此前两轮评审仅继续作为保留的运行时
> 文案设计依据，不构成对本次新增命令/发布/迁移范围的评审或签认；扩展后须重新评审。
> 2026-11-03 扩展方案外部 AI 评审处置：F-1 草案登记表在 strict 下升级为 error——采纳，
> 草案期围栏、签认前解除；F-2 重裁来源与草案状态经责任人对话确认，原 design/ADR 从未
> 签认；F-3 弯引号——采纳并统一 ASCII 直引号；F-4 开工授权歧义——采纳，requirements
> 与 ROADMAP 已明确仍须 ADR-0012 判定及 design/ADR 签认。复跑 check_docs 与 --strict 均绿。
> 2026-11-03 方案本体外部 AI 评审处置：B-1 草案 strict 红面已由上轮围栏修复并复验绿；
> B-2 AGENTS 开发命令失效——采纳，并扩展盘点至现行 mock 配方与 UPGRADE-RUNBOOK，最小
> 操作联动入 C4、完整文档仍后置；B-3 alias 归属复用两可——采纳，钉死迁入 kernel 助手
> 单源、installer/uninstaller 共同调用；B-4 引号已统一；B-5 重裁与草案状态已确认。
> 2026-11-03 ADR-0019 大方向复审：三项决策均获支持，无新阻断；AGENTS 与草案 strict 两项
> 落地缺口确认已由前序修订闭环。两项细化提示采纳：分发身份后续更名的发布链二次返工登记
> 为已知成本；分发身份裁决与内网体系允许在启动时按需求再拆分。

## 方案概述

本批次将显示品牌统一为 **AIxCoding**，将全渠道规范命令统一切换为 **`aixcoding-cli`**。
不再接受帮助文案、wheel 入口、离线 alias 和卸载行为之间的跨批次窗口。改动分八面：

1. **品牌常量与运行时文案**：保留原方案已收敛的 13 个源码定制点；两常量分别改为
   AIxCoding / aixcoding-cli，散点使用 `APP_DISPLAY_NAME` 插值，YAML 直改，serve 使用
   GBK 安全的 ASCII 字形。
2. **wheel 入口**：`[project.scripts]` 仅保留 `aixcoding-cli`，目标函数不变；
   `check_wheel.py` 继续执行唯一命令、版本和全部 help usage 检查。
3. **PyApp 与 release**：build.sh/build.ps1 输出新文件名；CD matrix、release 收集、
   包内 binary 和安装后 smoke 同步，分发名与内部 runtime 名不变。
4. **一键安装脚本**：install.sh/install.ps1 的包名解析、解包执行、冲突检测、完整性
   检查、修复和防降级统一使用新命令；相关用户提示同步品牌。
5. **离线迁移与卸载**：新增定制助手承载旧命令常量、alias 归属判断和安全清理；
   installer 在新 alias 成功后迁移旧自有 alias，uninstaller 同时识别新旧自有 alias。
6. **命令文案与 i18n**：approval fallback 的硬编码 `icode approval` 改为新命令，走
   extract→update→translate→compile 全链刷新 pot/po/mo。
7. **测试与发布说明**：原品牌红面跟改，补安装迁移、外部命令保护、release 命名契约和
   卸载测试；PR/发布说明明确命令切换及旧安装脚本需重新获取。
8. **最小开发操作联动**：root AGENTS 首段入口事实与 Commands、现行 mock 配方和
   UPGRADE-RUNBOOK 冒烟命令同步新入口；完整文档品牌和历史批次记录不在本批回写。

```
branding.py ──► AIxCoding / aixcoding-cli ──► TUI、CLI、ACP、安装提示与离线新 alias
pyproject ────► wheel 唯一 console script：aixcoding-cli
build + CD ───► aixcoding-cli[.exe] ──► aixcoding-cli-<平台>-...-offline.<tar.gz|zip>
install 脚本 ─► 新资产/新 alias 检查 ──► installer ──► 新 alias 成功后迁移旧自有 alias
uninstaller ──► 新旧自有 alias 均清理；外部同名命令保留
docs/README ──► 本批不改，由后续用户指南批次集中切换
```

## 备选方案对比

### 对比一：批次边界

| 方案 | 优点 | 缺点 | 结论 |
|---|---|---|---|
| **功能品牌与命令链同批，纯文档后置** | 无功能中间态；安装/升级/卸载一次验证；避免重复触碰同文件 | 本批 R2 面与验证面扩大 | **采用（责任人重裁）** |
| 原 C4a 文案先行、C5 入口/别名后补 | 单批较小 | `APP_COMMAND` 实际驱动安装/卸载，并非纯文案；产生不可执行帮助和安装生命周期错位 | 否决 |
| 全部品牌工作含 docs 一次完成 | 最终一致性最强 | 约千处文档替换掩盖功能变更，且目标上游版本未定 | 否决 |

### 对比二：wheel 入口形态

| 方案 | 优点 | 缺点 | 结论 |
|---|---|---|---|
| **仅 `aixcoding-cli`** | 保持上游"唯一命令"不变量；check_wheel 核心只换 COMMAND；无永久幽灵命令 | 既有调用方须迁移 | **采用** |
| `icode` + `aixcoding-cli` 双入口 | wheel 旧调用暂可运行 | check_wheel 两层 only-command 检查均须扩展；双入口帮助品牌相同，仍需迁移决策 | 否决 |
| 本批不改入口 | diff 最小 | 与已确定的命令切换目标冲突 | 否决 |

### 对比三：离线制品与旧 alias

| 方案 | 优点 | 缺点 | 结论 |
|---|---|---|---|
| **制品同批改名；新 alias 成功后清理自有旧 alias** | 功能链一次闭环；新装无旧命令；升级不留永久双轨 | 必须覆盖旧脚本过渡和 alias 归属证明 | **采用** |
| 制品保留 icode.exe，仅 wheel 改名 | build/CD 改动较少 | 用户仍接触旧制品名，install/release 链继续混合命名 | 否决 |
| 永久保留新旧 alias | 旧命令继续可用 | 永久兼容负担，卸载和冲突语义长期复杂 | 否决 |
| 直接按文件名删除所有旧 icode | 实现最短 | 可能删除外部程序或正在运行的文件，安全风险不可接受 | 否决 |

### 对比四：旧安装脚本过渡

| 方案 | 优点 | 缺点 | 结论 |
|---|---|---|---|
| **重新获取新版脚本；发布说明明示** | 发布链只维护新规范名；无双资产长期义务 | 已保存的旧脚本不能发现新 release | **采用** |
| 同期发布旧名兼容资产 | 旧脚本可发现下载 | 旧脚本还检查旧 alias，需额外兼容安装行为；形成双发布契约 | 否决 |
| 静默接受旧脚本失效且不提示 | 零发布说明工作 | 用户无法诊断升级失败 | 否决 |

### 对比五：保留的运行时文案实现选择

- serve banner 沿用旧分支纯 ASCII AIxCoding 字形，不改为 block 字形；内部函数名
  `_icode_serve_logo` 保留。
- 非 msg() 散点使用 `APP_DISPLAY_NAME` 插值；Code.yaml/QA.yaml 因无插值机制而直改。
- hosted fallback 使用 `{app}` 占位，消费处 `bind(app=APP_DISPLAY_NAME)`。
- skills 普通 format 模板新增 `{app}`，`provider.py` 探针和实际两次 `.format()` 均绑定；
  不得改整体 f-string，也不得破坏 `{{skill_script_name}}` 转义。
- C4 不替换 `DISTRIBUTION_NAME` 和 wheel 前缀；其品牌裁决与包身份迁移归 C5。仓库
  slug、`chrys` 内部名和持久数据 key 不参与任何机械替换。

## 上游改动清单（R2）

> 每处改动均使用单行英文 `AIxCoding ...` marker，遵循上游相邻语法；生成 catalog 按既有
> 豁免处理。以下行数为设计估算，实施以实跑 diff 和红面对账；新增红面必须补登记后再提交。

### A. 运行时品牌与 i18n（保留原方案，命令文案纳入）

| # | 文件 | 改什么 | 预估行数 | 标记注释位置 | 归属结论 |
|---|---|---|---|---|---|
| 1 | `src/chrys/foundation/branding.py` | `APP_DISPLAY_NAME="AIxCoding"`、`APP_COMMAND="aixcoding-cli"`；旧命令迁移常量放定制助手，不加入本文件 | +2/-2 | 常量区 marker×1 | 品牌单源，无法外置 |
| 2 | `src/chrys/service/context/providers/documentation.py` | prefix 4 个 iCode 改插值 + import | +4/-3 | prefix 处 | LLM 模板定义点 |
| 3 | 同上 | `_instructions` 后段 3 个品牌词 f-string 化插值 | +3/-3 | 后段赋值处 | 同文件独立语义点 |
| 4 | `src/chrys/service/context/providers/context_management.py` | `_INSTRUCTIONS` 两处插值 + import | +3/-2 | 常量定义处 | LLM 指令定义点 |
| 5 | `src/chrys/service/tools/builtins/web/hosted.py` | fallback 改 `{app}` | +1/-1 | fallback 处 | msg 模板定义点 |
| 6 | 同上 | 消费处 bind 增加 `app=APP_DISPLAY_NAME` + import | +2/-1 | bind 处 | 唯一渲染挂点 |
| 7 | `src/chrys/service/tools/builtins/filesystem.py` | read 工具 description 插值 + import | +2/-1 | description 处 | LLM 工具 schema |
| 8 | `src/chrys/service/skills/adapter.py` | 普通模板品牌词改 `{app}`，既有占位/转义不动 | +1/-1 | 模板行 | 技能提示模板 |
| 9 | `src/chrys/service/skills/provider.py` | 两次 `.format()` 均传 `app=` + import | +3/-2 | 函数定义处 | 探针与实际渲染必须同步 |
| 10 | `src/chrys/foundation/observability/setup.py` | ImportError 加 f 前缀并插值 + import | +2/-1 | raise 处 | 异常消息定义点 |
| 11 | `src/chrys/service/profiles/agents/builtins/Code.yaml` | instructions 自称 AIxCoding | +1/-1 | instructions 上方列 0 | YAML 无插值机制 |
| 12 | `src/chrys/service/profiles/agents/builtins/QA.yaml` | 同上 | +1/-1 | 同上 | 同上 |
| 13 | `src/chrys/app/cli/serve.py` | 仅改 docstring/ASCII 字形，函数名和消费方不动 | +6/-4 | 函数定义处 | banner 定义点 |
| 14 | `src/chrys/service/agent_middleware/control/approval.py` | fallback 的 `icode approval` 改 `aixcoding-cli approval`，保持 msg 字面量 | +1/-1 | fallback 处 | 实际用户命令文本，不能留旧命令 |
| 15 | `locales/chrys.pot` + `locales/zh-Hans/LC_MESSAGES/chrys.po` + 编译 MO | hosted 与 approval 两条源文案变化；中文两条均更新，解除 fuzzy 并重编 | 生成 | 既有生成物豁免 | 必须走 i18n 全链，不能手改指纹 |

### B. wheel、PyApp、release 与安装链（新增范围）

| # | 文件 | 改什么 | 预估行数 | 标记注释位置 | 归属结论 |
|---|---|---|---|---|---|
| 16 | `pyproject.toml` | `[project.scripts]` 键 `icode`→`aixcoding-cli`；name、urls、wheel 配置不动 | +1/-1 | scripts 表上方 | wheel 公开入口契约 |
| 17 | `scripts/check_wheel.py` | 仅功能常量 `COMMAND` 改新名；分发名/WHEEL_PREFIX 和内部变量不动 | +1/-1 | COMMAND 上方 | stdlib-only 探测脚本，不 import 运行时包 |
| 18 | `scripts/build.sh` | 输出 `dist/aixcoding-cli[.exe]` 及相邻输出说明；wheel/runtime 名不动 | +3/-3 | 输出赋值处 | 本地产物契约 |
| 19 | `scripts/build.ps1` | `$Output` 改 `aixcoding-cli.exe` 及相邻说明；PYAPP_EXEC_SPEC 不动 | +2/-2 | 输出赋值处 | 同上 |
| 20 | `.github/workflows/cd.yml` | 六个 matrix artifact 前缀改 `aixcoding-cli-` | +6/-6 | matrix 段上方 | Actions/release 公共资产名 |
| 21 | 同上 | binary output 改为 `aixcoding-cli[.exe]`，下游 package/smoke 自动跟随 | +3/-3 | name 步骤上方 | 构建产物名根 |
| 22 | 同上 | release 收集 glob、包内 binary、tar/zip 输入输出同步新名 | ~+8/-8 | collect 段上方 | 漏任一点即收集不到或包内断裂 |
| 23 | 同上 | release 后 POSIX/Windows 安装 smoke 改调用新 alias；chrys\bin 不动 | +2/-2 | smoke 命令上方 | 真实安装验收命令 |
| 24 | `scripts/install.sh` | release 包前缀、版本剥离、解包/chmod/执行改新名；iCode-TUI 分发名不动 | ~+6/-6 | 包名处理段 | 下载与执行契约 |
| 25 | 同上 | 冲突检测、同版本完整性、较新版修复均改查新 alias；相关用户提示改新品牌/命令 | ~+10/-10 | 对应函数段 | 安装生命周期功能点 |
| 26 | `scripts/install.ps1` | 包前缀、包内 exe、冲突检测、完整性和修复改新名；相关提示同步 | ~+12/-12 | 对应函数段 | Windows 同构闭环 |
| 27 | `src/chrys/app/installer.py` | `_CHRYS_PROCESS_BASENAMES` 纳入旧 `icode[.exe]`，避免迁移期漏识别旧入口进程 | +2/-0 | 常量区 | 旧进程识别属迁移接线 |
| 28 | 同上 | 新 alias 成功后调用定制迁移助手清理自有旧 alias；失败/冲突时不得先删旧 alias | ~+5/-1 | 调用点 | 上游只留生命周期挂接点 |
| 29 | `src/chrys/app/uninstaller.py` | alias 发现同时覆盖新旧命令并复用归属判断；Windows 仍只删自有目录 | ~+5/-3 | alias 收集处 | 防止直接卸载/中断迁移遗留旧 alias |

### C. 新增定制模块（非上游 R2）

新增 `src/chrys/kernel/aixcoding/command_migration.py`：承载 `LEGACY_APP_COMMAND="icode"`、
alias 归属证明与安全清理。该助手仅依赖标准库，按 ADR-0011 的最低依赖档原则落 kernel，
而非因调用方位于 app 就上移。将 uninstaller 现有 `_is_alias_of` 的实现迁入该助手，形成
单源归属判断；uninstaller 的新旧 alias 收集和 installer 的迁移清理都调用它，禁止复制
两份实现。这也避免在 `uninstaller→installer` 既有依赖上新增反向 import。上游两个文件
仅保留显式挂接点；不得扫描或删除 PATH 上任意 `icode`。

清理规则：仅检查安装器自有位置（POSIX `~/.local/bin/icode`、Windows 自有
`chrys\bin\icode.exe`）；symlink/hard link 须证明指向自有 `chrys`；copy fallback 无法
证明归属时保留并警告；正在运行的 Windows 文件不得强删。

### D. 上游测试跟改

| # | 文件 | 改什么 |
|---|---|---|
| 30 | `tests/app/cli/test_app.py` | 入口字典断言保留 `{APP_COMMAND: ...}`；测试名/docstring 改新命令；分发名断言不动 |
| 31 | `tests/app/cli/test_serve.py` | ASCII banner 断言与测试名跟改 |
| 32-45 | `tests/app/tui/terminal/test_program_status.py` | OSC app 字段断言跟改 |
| 32-45 | `tests/app/tui/terminal/test_program_status_lifecycle.py` | OSC app 字段断言跟改 |
| 32-45 | `tests/app/tui/widgets/test_hook_run_list.py` | hooks 空态文案断言跟改 |
| 32-45 | `tests/app/tui/screens/main/test_hooks_panel_flow.py` | hooks 面板断言跟改 |
| 32-45 | `tests/app/tui/i18n/test_command_suggestions_i18n.py` | man 页断言跟改 |
| 32-45 | `tests/app/tui/i18n/test_modal_screens.py` | /exit man 页断言跟改 |
| 32-45 | `tests/app/cli/test_tui_startup.py` | 启动消息断言跟改 |
| 32-45 | `tests/app/cli/test_run.py` | 离线错误文案断言跟改 |
| 32-45 | `tests/orchestration/engine/build/test_lifecycle_hooks.py` | 软重启警告断言跟改 |
| 32-45 | `tests/service/mcp/test_progressive.py` | 保留名冲突断言跟改 |
| 32-45 | `tests/orchestration/invoker/test_runtime_recipes.py` | 文档提示正/负向断言跟改 |
| 32-45 | `tests/orchestration/invoker/test_documentation.py` | 负向契约断言跟改 |
| 32-45 | `tests/orchestration/workflows/test_documentation.py` | 负向契约断言跟改 |
| 32-45 | `tests/orchestration/engine/build/test_agent_builder_rollback.py` | hosted bind 参数断言跟改 |
| 46 | `tests/app/cli/test_approvals.py` + `tests/app/cli/test_workflow.py` + `tests/app/cli/test_profiles.py` + `tests/app/cli/test_hooks.py` | help/诊断中的旧命令字面断言改新命令；任意 argv0 fixture 不做机械替换 |
| 47 | `tests/app/test_install_scripts.py` | POSIX/Windows fake 包名、包内 binary、release URL、alias fixture、冲突、同版本/修复/防降级矩阵跟改；新增"只有旧 alias 触发迁移"和"外部旧 icode 不阻塞"场景 |
| 48 | `tests/app/test_installer.py` | 新增旧进程识别、新 alias 成功后清理、失败时保留、外部旧命令保护；覆盖 POSIX symlink、Windows hard link/copy 和运行中旧 alias |
| 49 | `tests/app/test_uninstaller.py` | 新增新旧自有 alias 双清理、外部旧命令保留；iCode-TUI 卸载命令断言不动 |

原静态盘点为 18 文件 69 个品牌测试函数；扩展后以实跑红面为准。`_FAKE_ICODE`、
`icode_env`、任意 `sys.argv[0]="icode"`、审批 shell 示例和内部日志 marker 不构成入口
契约，不做无关改名。

### E. 最小开发操作说明联动

| # | 文件 | 改什么 | 预估行数 | 标记注释位置 | 归属结论 |
|---|---|---|---|---|---|
| 50 | `AGENTS.md` | 首段用户显示名/命令与 only-command 事实改 AIxCoding/aixcoding-cli；iCode-TUI 分发事实不动 | +1/-1 | 首段前 marker×1 | 上游开发入口事实，须随 pyproject 同步 |
| 51 | `AGENTS.md` | Commands 三条 `uv run icode` 及 inline workflow 示例改新命令，其余命令不动 | +4/-4 | Commands 代码块前 marker×1 | 防止后续开发按已删除入口操作 |

定制侧同步现行操作配方（非上游 R2）：`aixcoding/mock/__main__.py`、
`aixcoding/mock/{auth,model_catalog,update}/server.py`、mock auth/model_catalog README 和
`aixcoding/docs/standards/UPGRADE-RUNBOOK.md` 的 `uv run icode` 改为新命令。只改现行
操作说明；历史批次的 requirements/test-plan/verification 记录当时事实，不回改。

### F. 定制测试（非上游 R2）

- 保留 `aixcoding/tests/branding/test_branding_runtime.py`：真实消费 C050-C062 对应上游
  符号并断言 AIxCoding 渲染，不 mock 被锚定符号本身。
- 新增 `aixcoding/tests/branding/test_command_contract.py`：检查 wheel 唯一入口、build/CD/
  install 的新名称链、AGENTS/现行开发配方的新命令，以及明确保留的分发名/内部名，防止
  只改其中一跳。
- 迁移行为由上游 installer/uninstaller/install-scripts 真实测试承担；静态契约测试不得
  替代 Windows/POSIX 安装和升级行为验证。

## 对上游隐式行为的假设

| # | 上游行为 | 我方依赖方式 | 失效表现/检测 |
|---|---|---|---|
| 1 | 品牌常量驱动 136 个引用点 | 改两常量并保留散点插值 | 残留旧品牌；品牌行为测试与全量红面检测 |
| 2 | msg fallback 静态字面量、bind 渲染和 catalog 指纹链 | hosted/approval 改源文案后走完整 i18n | 指纹 stale、fuzzy 或 MO 旧文案；Linux oracle 检测 |
| 3 | skills 普通模板有既有占位和双大括号转义，provider 两次 format | 两次都绑定 app | 漏一处即模板误报非法；行为测试检测 |
| 4 | check_wheel 严格断言唯一 COMMAND，且 usage 来自 APP_COMMAND | 入口与常量同批改，COMMAND 同步 | 任一脱钩即归档/install/help 探测红 |
| 5 | PyApp 通过 PYAPP_EXEC_SPEC 直接启动，不依赖 console script | 只改输出文件名，dispatcher 不动 | 启动失败；本地离线 smoke 检测 |
| 6 | CD 名称经 matrix→binary output→artifact→release→安装脚本逐跳传递 | 全链同批改名 | 收集不到资产或解包找不到 binary；契约测试+CD 检测 |
| 7 | installer 的 alias 创建在新旧 Windows/POSIX 形态不同 | 新 alias 成功后才清理旧 alias | 外部命令误删或升级后无可用命令；迁移矩阵检测 |
| 8 | uninstaller 用 symlink/samefile 证明 alias 归属 | 新旧 alias 共用同一归属语义 | 外部命令误删；负向测试检测 |
| 9 | install scripts 的 fake binary 测试不执行真实 installer.py | 不把它当作真实迁移证据 | 真实 alias 迁移须由 installer 测试和 CD/本地 smoke 另行覆盖 |
| 10 | `test_catalog_oracle` 为 CI_LINUX_ONLY | Windows skipped 不记为通过 | Linux CI 为最终 oracle 证据 |

## 定制点登记预告

> id 自 C050 起；区间计数等于成员数。原 C050-C102 的运行时品牌锚点保留，C063 的语义
> 从"窗口期保持 icode"改为"唯一入口跟随 APP_COMMAND"。新增命令链使用 C103-C131。
> 实施后以 `uv run python aixcoding/tools/gen_customizations.py --strict` 对账。
> 草案评审期间表格暂置 fenced code block，避免未签认预告被 strict 当作正式登记而阻断 CI；
> 评审收敛后先解除围栏，再请责任人签认，确保签认正文包含生效的登记表。

| id | 落点 | marker 锚 | 预期计数 | anchor | risk | removal_condition |
|---|---|---|---|---|---|---|
| C050 | src/chrys/foundation/branding.py | AIxCoding branding: display name and user command | 1 | 两品牌常量 | 高 | 品牌政策变更时逐点裁决 |
| C051 | src/chrys/service/context/providers/documentation.py | AIxCoding branding: LLM prompt self-reference | 1 | DocumentationProvider prefix 模板 | 低 | 随 C050 |
| C052 | src/chrys/service/context/providers/documentation.py | AIxCoding branding: prompt tail self-reference | 1 | `_instructions` 后段 | 低 | 随 C050 |
| C053 | src/chrys/service/context/providers/context_management.py | AIxCoding branding: reminder instructions self-reference | 1 | `_INSTRUCTIONS` 常量 | 低 | 随 C050 |
| C054 | src/chrys/service/tools/builtins/web/hosted.py | AIxCoding branding: hosted-tools notice placeholder | 1 | hosted fallback | 低 | 随 C050 |
| C055 | src/chrys/service/tools/builtins/web/hosted.py | AIxCoding branding: bind app for hosted-tools notice | 1 | hosted bind 消费点 | 低 | 随 C054 |
| C056 | src/chrys/service/tools/builtins/filesystem.py | AIxCoding branding: read tool description | 1 | read 工具 description | 低 | 随 C050 |
| C057 | src/chrys/service/skills/adapter.py | AIxCoding branding: skill trust notice placeholder | 1 | `SKILLS_STATIC_PROMPT` | 低 | 随 C050 |
| C058 | src/chrys/service/skills/provider.py | AIxCoding branding: bind app for skill prompt | 1 | 两次 format 消费点 | 低 | 随 C057 |
| C059 | src/chrys/foundation/observability/setup.py | AIxCoding branding: OTLP error message | 1 | OTLP ImportError | 低 | 随 C050 |
| C060 | src/chrys/service/profiles/agents/builtins/Code.yaml | AIxCoding branding: built-in agent self-reference | 1 | Code instructions | 低 | 随 C050 |
| C061 | src/chrys/service/profiles/agents/builtins/QA.yaml | AIxCoding branding: built-in agent self-reference | 1 | QA instructions | 低 | 随 C050 |
| C062 | src/chrys/app/cli/serve.py | AIxCoding branding: ASCII serve banner | 1 | `_icode_serve_logo` 输出 | 低 | 随 C050 |
| C063 | tests/app/cli/test_app.py | AIxCoding branding: sole command follows APP_COMMAND | 1 | wheel 唯一入口契约断言 | 低 | 随 C050/C103 |
| C064 | tests/app/cli/test_serve.py | AIxCoding branding: banner assertion | 1 | banner 渲染断言 | 低 | 随 C062 |
| C065-C078 | tests/app/tui/terminal/test_program_status.py | AIxCoding branding: OSC app name assertions | 14 | OSC app 字段断言（14 个语义点） | 低 | 随 C050 |
| C079-C081 | tests/app/tui/terminal/test_program_status_lifecycle.py | AIxCoding branding: OSC app name assertions | 3 | OSC app 字段断言（3 个测试函数） | 低 | 随 C050 |
| C082 | tests/app/tui/widgets/test_hook_run_list.py | AIxCoding branding: hooks empty-state text | 1 | hooks 空态文案 | 低 | 随 C050 |
| C083-C085 | tests/app/tui/screens/main/test_hooks_panel_flow.py | AIxCoding branding: hooks panel states | 3 | hooks 面板 3 个测试函数 | 低 | 随 C050 |
| C086 | tests/app/tui/i18n/test_command_suggestions_i18n.py | AIxCoding branding: man page branding | 1 | man 页双语渲染 | 低 | 随 C050 |
| C087 | tests/app/tui/i18n/test_modal_screens.py | AIxCoding branding: exit man page | 1 | `/exit` 双语文案 | 低 | 随 C050 |
| C088 | tests/app/cli/test_tui_startup.py | AIxCoding branding: startup message | 1 | 启动消息 | 低 | 随 C050 |
| C089 | tests/app/cli/test_run.py | AIxCoding branding: offline error text | 1 | 离线错误文案 | 低 | 随 C050 |
| C090 | tests/orchestration/engine/build/test_lifecycle_hooks.py | AIxCoding branding: soft-restart warning | 1 | 软重启警告 | 低 | 随 C050 |
| C091-C093 | tests/service/mcp/test_progressive.py | AIxCoding branding: reserved tool conflict text | 3 | 保留名冲突 3 个测试函数 | 低 | 随 C050 |
| C094 | tests/orchestration/invoker/test_runtime_recipes.py | AIxCoding branding: prompt documentation title | 1 | 文档提示正向与负向断言 | 低 | 随 C051/C052 |
| C095 | tests/orchestration/workflows/test_documentation.py | AIxCoding branding: prompt documentation title absent | 1 | 负向契约 | 低 | 随 C051/C052 |
| C096 | tests/orchestration/invoker/test_documentation.py | AIxCoding branding: prompt documentation title absent | 1 | 负向契约 | 低 | 随 C051/C052 |
| C097 | tests/app/cli/test_approvals.py | AIxCoding branding: approval usage line | 1 | approval usage | 低 | 随 C050 |
| C098 | tests/app/cli/test_workflow.py | AIxCoding branding: workflow usage line | 1 | workflow usage | 低 | 随 C050 |
| C099 | tests/app/cli/test_profiles.py | AIxCoding branding: profiles usage line | 1 | profiles usage | 低 | 随 C050 |
| C100-C101 | tests/app/cli/test_hooks.py | AIxCoding branding: hook validate hint text | 2 | 2 个 hooks 诊断测试函数 | 低 | 随 C050 |
| C102 | tests/orchestration/engine/build/test_agent_builder_rollback.py | AIxCoding branding: hosted warning bound args | 1 | hosted bind 参数断言 | 低 | 随 C054/C055 |
| C103 | pyproject.toml | AIxCoding branding: wheel command entry | 1 | `[project.scripts]` 唯一键 | 高（公开命令契约） | 命令品牌政策变更时裁决 |
| C104 | scripts/check_wheel.py | AIxCoding branding: wheel command probe | 1 | COMMAND 及全部探测 | 高（三平台 CI） | 随 C103 |
| C105 | scripts/build.sh | AIxCoding branding: PyApp output name | 1 | POSIX dist 输出文件名 | 中 | 随 C103 |
| C106 | scripts/build.ps1 | AIxCoding branding: PyApp output name | 1 | Windows dist 输出文件名 | 中 | 随 C103 |
| C107-C110 | .github/workflows/cd.yml | AIxCoding branding: release command artifacts | 4 | matrix/binary output/release collect/install smoke | 高（发布链） | 随 C103 |
| C111-C112 | scripts/install.sh | AIxCoding branding: POSIX install command contract | 2 | 包名/执行与 alias 生命周期 | 高 | 随 C103 |
| C113-C114 | scripts/install.ps1 | AIxCoding branding: Windows install command contract | 2 | 包名/执行与 alias 生命周期 | 高 | 随 C103 |
| C115 | src/chrys/app/installer.py | AIxCoding branding: legacy command process recognition | 1 | `_CHRYS_PROCESS_BASENAMES` | 中 | 旧版迁移支持退役时裁决 |
| C116 | src/chrys/app/installer.py | AIxCoding branding: legacy alias migration hook | 1 | 新 alias 成功后的迁移调用 | 高（文件删除安全） | 旧版迁移支持退役时裁决 |
| C117 | src/chrys/app/uninstaller.py | AIxCoding branding: legacy alias uninstall hook | 1 | 新旧自有 alias 收集 | 高（文件删除安全） | 旧版迁移支持退役时裁决 |
| C118 | src/chrys/service/agent_middleware/control/approval.py | AIxCoding branding: approval command text | 1 | approval fallback | 低 | 随 C103 |
| C119-C124 | tests/app/test_install_scripts.py | AIxCoding branding: install script contract tests | 6 | POSIX/Windows 包、安装冲突、修复防降级六组 | 中 | 随 C111-C114 |
| C125-C127 | tests/app/test_installer.py | AIxCoding branding: legacy alias migration tests | 3 | 进程识别/POSIX/Windows 迁移组 | 高 | 随 C115-C116 |
| C128-C129 | tests/app/test_uninstaller.py | AIxCoding branding: legacy alias uninstall tests | 2 | 自有旧 alias 清理/外部旧命令保留 | 高 | 随 C117 |
| C130 | AGENTS.md | AIxCoding branding: development entry facts | 1 | 首段显示名/命令与 only-command 事实 | 中（后续开发入口） | 随 C103 |
| C131 | AGENTS.md | AIxCoding branding: development command examples | 1 | Commands 的可执行开发命令 | 中（后续开发入口） | 随 C103 |

（区间仅用于同一落点、同一 marker 文本的孪生组；实施时不得把多个分散函数合并成一个
无法定位的 marker。生成 catalog 和定制目录不入上游 C id 表。）

## 验证与发布门禁

1. **静态与全量测试**：pyproject 变化会触发上游全量套件；另跑 fork 全量、定制测试、
   ruff/format/ty、i18n check、文档检查和 `--strict`。不得用 Smart Test 局部通过代替全量。
2. **wheel 三平台**：构建 wheel 后运行 check_wheel；干净安装只有新命令，所有 help usage
   一致。增加旧版→新版升级探测，确认旧 console script 的移除行为，而非只测干净环境。
3. **PyApp 本地**：Windows 离线构建产出新 exe，完成无网首跑、warm run、serve/acp help；
   POSIX 由对应 CI/构建环境验证。
4. **release 逐跳**：核对 matrix artifact、上传目录、collect glob、tar/zip 内 binary、
   SHA256SUMS、安装脚本解析和 CD smoke；PR CI 未真实执行 release 时，verification 必须
   区分静态证据与 tagged CD 证据，不得冒称已跑。
5. **安装迁移矩阵**：全新、N-1→N、同版本修复、较新版防降级、alias 丢失、新 alias 冲突、
   外部旧命令、卸载；覆盖 POSIX symlink、Windows hard link/copy 及运行中文件。
6. **发布说明**：明示 `icode`→`aixcoding-cli`、外部调用方需同步修改、旧安装脚本需重新
   下载；docs 旧示例由后续批次处理。
7. **开发入口与现行手册**：执行 `uv run aixcoding-cli --version`；扫描 AGENTS、mock
   现行配方和 UPGRADE-RUNBOOK，无仍指导使用旧入口的 `uv run icode`。历史批次记录不纳入
   这一清零口径。

## 风险与最坏情况

- **公开命令切断**：升级后旧 wheel 命令消失，仓外桌面端/ACP/脚本可能受影响。实施前
  盘点可控消费方，发布说明给出迁移；不宣称仓内搜索覆盖全部用户。
- **旧安装脚本无法发现新资产**：采用重新获取脚本的一次性迁移，不发布双资产；这是本
  ADR 需要签认接受的明确破坏点。
- **alias 误删风险**：最坏情况是删除外部同名程序。以自有目录、归属证明、新 alias 成功
  后清理和负向测试防错；无法证明时宁可残留并警告。
- **回滚不是机器状态回滚**：revert PR 可恢复代码和后续制品，但已迁移机器不会因 revert
  自动恢复旧 alias。回滚手册须重装旧版恢复 `icode`，并先使用新版卸载器或明确步骤处理
  新 alias，避免旧卸载器不认识新命令。
- **发布链局部漏改**：可能构建成功但 release 收集为空或安装包内名称错误；用定制契约
  测试、逐跳核对和 tagged CD 分层验证。
- **分发身份后续更名的二次返工**：本批先改命令相关的 build/CD/install 链；若分发身份
  工作项后续裁决 `iCode-TUI` 更名，这些文件会因包身份再次调整。这是为解耦需求时点而
  接受的已知成本，不构成对本批命令闭环的否定。
- **纯文档窗口**：程序已切换而随包 docs 仍给旧命令，属用户指引失真；发布说明先提供
  可执行迁移口径，后续文档批次集中消解。
- **最坏情况回退**：若迁移安全性或 release 链不能闭环，不交付半切换版本；整批不合并，
  而不是退回原"先改 APP_COMMAND 文案"的窗口方案。

## ADR 判定（DEVELOPMENT.md §6）

1. 有被认真考虑但否决的备选？——有：跨批窗口、双入口、保留旧制品、永久旧 alias、双资产。
2. 撤销代价高？——**是**：变更公开命令、release 资产名和机器安装状态，不能仅靠代码
   revert 完成回滚。
3. 约束未来工作？——**是**：明确 C4/C5/独立文档项边界、单命令契约、旧脚本过渡和文档窗口。
4. 依赖上游非公开行为？——依赖 PyApp、安装/卸载和 CD 的既有内部结构，已在假设表与
   定制清单登记锚点。

结论：保留全局 **ADR-0019**，重写为"品牌定制阶段划分与命名边界决策"。新增定制助手
符合 ADR-0011 的 kernel tier 最低依赖档放置；无新增第三方依赖（R11 无涉），无新增环境变量
（DIRECTORY.md §9 无涉）。
