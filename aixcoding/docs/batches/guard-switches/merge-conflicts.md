# 护栏开关合入：冲突清单与取值影响（v2 对账版）

> 状态：决策材料（活文档，不签认）。用途：责任人逐项裁决"用他的值 / 用我的值 / 合成"。
> 取证口径：基线 = `main_aixcoding_cli_0.28.0_final`；同事交付 v2 = `D:\project\icode_new\guard_changes_20261105_v2`；
> 我方 = `lhc/安全护栏_0.33.2` @ `87934228`。差异数字为 `git diff --stat` 实测。
> 日期：2026-11-05（v2 对账）

---

## 0. 裁决状态

| 议题 | 裁决 | 备注 |
| --- | --- | --- |
| 是否合入开关功能 | 合入 | 同事 v1 + v2 的全部开关能力 |
| **C1 出厂写入范围** | **取他的（`allow_outside_workspace: true`）** | 已定：新版本不需要拦截工作区外写入 |
| 环境变量前缀 | 用我方 `AIXCODING_GUARD_*` | 含新增的 `STATE_PATH_ENV` |
| 目录结构 / 打包方式 / 文档落点 | 用我方 | 同事的 `guard_hooks/` 与 `src/chrys/app/guard_install.py` 在我树上不存在 |
| 冲突时的总体原则 | 同一处代码分歧取同事的；两边各自新增且不冲突的部分不得回退 | 见第五节 10 项回退清单 |

v1 文档里给的"组合 A / B / C"因 C1 已裁决而作废：只剩一种目标形态 —— **C1 取他 + 其余取我/合成**。

---

## 1. v2 相对 v1 的增量（这次新到的东西）

`_guard_core.py`（55039 字节）与 `rules.json`（25083 字节）**与 v1 完全一致**，开关机制本体没有变化。v2 新增的是"没有 uv 也能用"的入口与配套：

| 文件 | v1 | v2 | 说明 |
| --- | --- | --- | --- |
| `guard_hooks/guardctl.cmd` | 无 | **新增 1203B** | Windows 免 uv 入口：调 `_launcher.ps1` 现找解释器；无参双击时 `pause`；**必须 ASCII、无 BOM、CRLF** |
| `guard_hooks/_launcher.ps1` | 未交付 | **2355B（已带）** | 语义扩展：`GUARD_SCRIPT` 支持**绝对路径**（`IsPathRooted` 判断），空值报错退出。这是 `guardctl.cmd` 的前置条件 |
| `.gitattributes` | 无 | **新增 420B** | 我方同文件 399B，唯一差异是 `*.cmd text eol=crlf` 一行 |
| `.github/workflows/ci.yml` | 无 | **新增 19422B** | 上游 CI 加 `guard_selftest` job（三平台，`PYTHONUTF8: "1"`，跑 `guard_hooks/selftest.py` + `util/verify_guard_deployed.py`） |
| `selftest.py` | 48242B | 50370B | **+4 条**：cmd 随包、cmd 指向同目录启动器、启动器支持绝对路径、cmd 免 uv 跑通（后者 Windows-only + 无解释器时 skip） |
| `MANUAL_TESTS.md` | 18143B | 18091B | **新增「十一、开关（`guardctl.py`）」X01–X16**（16 条手工用例）；总用例行 106（我方 90） |
| `SWITCHES.md` | 10930B | 13545B | **新增「零、命令怎么敲起来（对方没有 uv 也能用）」**：cmd 双击 / POSIX / 连 python 都没有时直接改状态文件 |
| `guardctl.py` | 22426B | 22615B | docstring 补 cmd 用法与"不需要 uv"说明 |
| `src/chrys/app/guard_install.py` | 22050B | 22356B | payload 增 `guardctl.py` + `guardctl.cmd`（现为 7 个文件） |
| `_install_merge.py` | 23401B | 23625B | 同上，payload 7 个文件 |
| `tests/architecture/test_guard_hooks_packaging.py` | 2877B | 2898B | `PACK_FILES` 增 `guardctl.py` / `guardctl.cmd` |
| `pyproject.toml` | 11153B | 11220B | `force-include` 增 `guardctl.cmd` 一条 |
| `README/OPERATIONS/PORTING` | — | +68 / −22 / +367B | 补 cmd 入口说明 |
| `guard_hooks/.gitignore` | 107B | 107B | 未变，仅忽略 `guard_state.json` |

**仍未交付**（视为未改）：`hooks.yaml`、`_prompt_guard.py`、`_tool_guard.py`。
**被 ci.yml 引用但没交付**：`util/verify_guard_deployed.py` —— 他 CI 里的第二条命令在我树上找不到对应文件，我方对应物是 `aixcoding/tests/guard/verify_guard_deployed.py`（61 条机器用例）。

---

## 2. 真冲突：同一处代码两边都改过（6 项）

### C1 出厂写入范围 —— 已裁决：取他的 `true`

| | 值 |
| --- | --- |
| 基线 | `false`（拦截） |
| 同事 v1/v2 | `true`（不拦截） |
| 我方 | `false` |

**选定后的事实**：`T-FS-01`（文件写入白名单）与 `T-SHELL-05`（shell 写到工作区外）在**出厂状态**下不再触发；要拦截必须现场 `guardctl workspace restrict`，或在测试前置写状态文件。

**连带改动（必做）** —— 但成本比 v1 文档估的小得多，因为不用改任何"期望值"，只加前置：

| 对象 | v1 文档的估法（改期望） | 实际更省的改法（加前置，期望一行不动） |
| --- | --- | --- |
| `guard_pack_selftest.py` 3 方法 | 28 处断言改写 | 改用**他的** `config_with_write_scope(restricted=True)` helper 构造配置，断言原样保留 |
| `verify_guard_deployed.py` F01 | 期望 `True` → `False` | 部署后往 `GUARD_DIR/guard_state.json` 写 `{"write_scope": {"allow_outside_workspace": false}}`（约 10 行），F01 期望不动，跑完清理 |
| `MANUAL_TESTS.md` 六 / 六之二 | 17 行期望反转 | 给「六」F01 与「六之二」W01–W16 加一句前置"先 `guardctl workspace restrict`"，期望不动 |
| `OPERATIONS.md` §4.1 / §6 | — | 把"出厂限制、改 `rules.json` 放宽"改为"出厂不限制、`guardctl workspace allow/restrict` 切换" |

**安全口径说明**：这是主动放宽一条出厂规则，与 `batches/guard-hooks/requirements.md`「规则内容零增删改」冲突，需要在 design 里写成显式裁决（不写进"平移"里）。

> 裁决：☑ 他（`true`）

---

### C2 `_guard_core.py::GuardConfig` —— 构造与状态注入

| | 做法 |
| --- | --- |
| 同事 | `__slots__` 增 `state` / `state_path`；`__init__(data, *, path, state=None, state_path=None)` |
| 我方 | `__slots__` 无这两项；`__init__` 重写为 `_validate_settings` / `_validate_dictionaries` / `_validated_rules` + 规则 id 唯一性检查 |

- **选同事**：开关可用，但**丢失严格校验**（规则写错被静默跳过，与 `fail_closed: true` 相反）；且 `apply_state()` 在构造之后直接改 `settings["write_scope"]` 与 `rule["enabled"]`，状态值不做类型校验 —— 手写 `"false"`（字符串）会被 `bool("false") == True` 判成"允许"，静默反向。
- **选我方**：开关无处取值，`guardctl` 写了状态文件没人读。
- **必须合成**：加回 2 个 slot 与 2 个参数，`load_config()` 先构造（走校验）再 `apply_state()`，并对状态值补布尔类型校验。约 60 行。

> 裁决：☑ 合成

---

### C3 `_guard_core.py::_write_scope_evidence` —— 门闸与路径解析

| | 做法 |
| --- | --- |
| 基线 | 读 `scope.get("allow_outside_workspace", False)` + 词法路径比较 |
| 同事 | 门闸改为 `if not config.write_scope_restricted: return []`（状态文件可覆盖出厂值） |
| 我方 | 门闸仍读 `scope`；新增 `_canonical_scope()` 按宿主文件系统 `resolve()` 后比较，堵工作区内符号链接 / junction 逃逸 |

- **选同事**：写入范围可现场切换，但仍是词法比较 —— 工作区内放一个指向 `C:\Users\x\.ssh` 的 junction，写它会被判成"工作区内"放行。
- **选我方**：逃逸口堵上，但 `guardctl workspace restrict` 写了状态没人读（**命令存在却不生效，比没有命令更危险**）。
- **必须合成**：门闸改成他的属性版，函数体里的比较保留我的 `_canonical_scope`。约 10 行。
- 注意：C1 取 `true` 后，这条路径只在 `workspace restrict` 时才走到，但**恰恰那时最需要符号链接判定**。

> 裁决：☑ 合成

---

### C4 `guard_install.py::GUARD_PAYLOAD_FILES`

| | 值 |
| --- | --- |
| 同事 v2 | 展开 7 行：`_prompt_guard.py` / `_tool_guard.py` / `_guard_core.py` / `rules.json` / `_launcher.ps1` / `guardctl.py` / `guardctl.cmd` |
| 我方 | 单行 5 个文件，无 guardctl |

- **选同事（加）**：现场 `<config_dir>/hooks/guard/` 能直接 `guardctl.cmd status`，`SWITCHES.md` 命令成立。代价：`aixcoding/tests/guard/test_packaging.py` 的"pack 文件集必须恰好等于 payload + `hooks.yaml` + `__init__.py`"断言要同步加 2 个文件名。
- **选我方（不加）**：装到现场的护栏没有任何开关工具，用户只能手改 `guard_state.json`；且 C1 已放宽，现场再想收紧就完全没有合法途径。
- **合成**：保留我的单行元组 + 加 2 个文件名，docstring 两行说明。

> 裁决：☑ 加上 `guardctl.py` + `guardctl.cmd`

---

### C5 `aixcoding/tools/guard_install_merge.py::PAYLOAD_FILES`

同 C4；另我此文件已把 `print` 全改 `_write`（ruff T201）、资产目录改 `PACK_DIR`，取同事文件会回退这两处。

> 裁决：☑ 加上 2 个文件名，保留我方实现

---

### C6 `pack/_launcher.ps1` —— 绝对路径支持（v2 新增）

| | 做法 |
| --- | --- |
| 同事 v2 | `$script = $env:GUARD_SCRIPT`；空则报错退出；非绝对路径才 `Join-Path $hooksRoot` |
| 我方（= v1 未交付的那份） | `$script = Join-Path $hooksRoot $env:GUARD_SCRIPT`（**只认相对路径**） |

- **必须取他的**：`guardctl.cmd` 传的是 `%~dp0guardctl.py`（绝对路径）。用我方的启动器会把绝对路径再 `Join-Path` 一次，得到坏路径 → `guardctl.cmd` 直接不可用。
- **兼容性**：我方 `hooks.yaml` 传入的是相对路径 `guard\_prompt_guard.py`，新启动器走 `IsPathRooted=false` 分支，行为不变；空值从"拼成不存在的路径后失败"变成"立即报错退出"，对 hook 侧都是非零退出，可接受。
- 他的自检里 `test_launcher_accepts_an_absolute_script_path` 直接断言文件文本含 `IsPathRooted` —— 取测试就必须取这份启动器。

> 裁决：☑ 取他的

---

## 3. 半冲突 / 互斥断言（14 项）

### S1 环境变量常量区（`_guard_core.py` 顶部）

| | 值 |
| --- | --- |
| 基线 + 同事 | `ICODE_GUARD_RULES` / `ICODE_GUARD_AUDIT`（他没改）+ 新增 `ICODE_GUARD_STATE`；v2 文档 `README.md` 309–311 行仍是这三条 |
| 我方 | `AIXCODING_GUARD_RULES` / `AIXCODING_GUARD_AUDIT` / `AIXCODING_GUARD_HOOKS_DIR` |

- **用他的**：同文件一半旧前缀一半新前缀；外部脚本按 `AIXCODING_` 读不到；0.28 旧变量复活。
- **用我的**：新加 `STATE_PATH_ENV = "AIXCODING_GUARD_STATE"`；`guardctl.py` 通过 `guard.STATE_PATH_ENV` 取值自动跟随，代码 0 处改动；文档 3 处（README 环境变量表 / OPERATIONS / SWITCHES）。

> 已裁决：用我的。

---

### S2 自检脚本装配方式

| | 做法 |
| --- | --- |
| 同事 | `sys.path.insert(0, parent)` + 顶层 `import _guard_core / _install_merge / guardctl`；`run_ctl()` 直接调 `guardctl.main()`；helper 带 `config=None` 形参 |
| 我方 | `from chrys.kernel.aixcoding.guard.pack import _guard_core`、`from aixcoding.tools import guard_install_merge`；入口 `python -m aixcoding.tests.guard.guard_pack_selftest` |

- **用他的**：在我树里 `import guardctl` 直接 ImportError（脚本目录没有它），25 条新用例一条都跑不起来。
- **用我的**：25 条用例的导入与 helper 要改（`prompt_ids` / `shell` / `tool_ids` / `assertShell` 四个加 `config` 形参，否则无法表达"限制开启"前置），约 30 行机械改动，不改产品行为。

> 已裁决：用我的装配，搬运 25 条。

---

### S3 `guardctl.py` 自身要改的地方

| 位置 | 现状（同事 v2） | 合入后 |
| --- | --- | --- |
| docstring | `chrys.app.guard_install`、`uv run python guard_hooks/guardctl.py`、`$env:APPDATA\chrys\hooks\guard\guardctl.cmd` | 模块路径改 `chrys.kernel.aixcoding.guard.guard_install`；文档指向安装后的位置（`<config_dir>/hooks/guard/`） |
| 顶部导入 | `sys.path.insert(0, parent); import _guard_core` | 被包内导入时会把 `_guard_core` 加载成**第二个顶层模块对象**；且 ty 会报 `unresolved-import`。改法：保留 `sys.path.insert`（装机后确实需要），补一行 `# ty: ignore[unresolved-import]`（与我方 `_prompt_guard.py` 同一写法） |

- **用他的原样**：文档指向不存在的模块；`ty check` 红（我方的 ty 门禁是三平台 --error-on-warning）。
- **改**：2 处文案 + 1 行行内忽略，功能不变。

---

### S4 `pyproject.toml`

| | 值 |
| --- | --- |
| 同事 v2 | `force-include` 8 条（`guard_hooks/* → chrys/app/guard_hooks/*`）、`packages=["src/chrys","aixcoding"]`、`"guard_hooks/*" = "py39"`、`"guard_hooks/*" = [RUF001-003]`、`[tool.ty.overrides] include` 指向 `src/chrys/app/guard_hooks/prompt_guard.py`/`tool_guard.py`（旧文件名） |
| 我方 | 无 `force-include`、`packages=["src/chrys"]`、`"src/chrys/kernel/aixcoding/guard/pack/*.py" = "py39"`、`"src/chrys/kernel/aixcoding/guard/pack/*" = [RUF001-003]`、ty 用**行内** `# ty: ignore[unresolved-import]`（无 override 段） |

- **用他的**：wheel 会把 pack 复制成 `chrys/app/guard_hooks/` 第二份副本（历史上正是这份副本漂移过，导致 127.0.0.1 回环误拦）；我方 `test_no_second_rule_pack_copy_exists` 与 `test_hatch_packages_the_pack_without_a_force_include_mapping` 会红。
- **用我的**：`guardctl.py` 放 `pack/` 自动继承 `py39` 与 RUF 豁免，`pyproject.toml` **零改动**；ty 用行内忽略。

> 已裁决：用我的。

---

### S5 `test_packaging.py`（我方）↔ `test_guard_hooks_packaging.py`（同事）

互斥断言：他的要求"必须存在 `force-include`"，我的要求"必须不存在，且 pack 文件集恰好等于 payload + `hooks.yaml` + `__init__.py`"。v2 里他的 `PACK_FILES` 又多了 `guardctl.py`/`guardctl.cmd`。

- **取他的文件**：在我树上必红，且与 S4 冲突。
- **取我的**：在 `PACK_RUNTIME_FILES` 里多 2 个文件名（1 行）。

> 已裁决：取我的。

---

### S6 断言与文档的连带修改

见 C1 表格（已改为"加前置、期望不动"的省法）。

> 已裁决：随 C1 执行。

---

### S7 测试文件路径落点

`tests/app/test_guard_install.py`（10006B，与 v1 一致）、`tests/architecture/test_guard_hooks_packaging.py` 在我树上不存在。

- **用他的路径**：等于新建一套平行的旧位置测试，与 `aixcoding/tests/guard/` 双份维护。
- **用我的**：2 条新增用例并入 `aixcoding/tests/guard/test_guard_install.py`。

> 已裁决：用我的。

---

### S8 文档落点与措辞

| | 同事 v2 | 我方 |
| --- | --- | --- |
| 位置 | `guard_hooks/*.md`（目录在我树上已不存在） | `aixcoding/docs/batches/guard-hooks/rule-pack/*.md` |
| 命令 | `uv run python guard_hooks/_install_merge.py`、`icode hook validate`、`guardctl.cmd`（双击） | `uv run python -m aixcoding.tools.guard_install_merge`、`aixcoding-cli hook validate` |
| 新增节 | `SWITCHES.md` 全新（13.5KB，含「零、命令怎么敲起来」）；`MANUAL_TESTS`「十一、开关」 | 我方 `MANUAL_TESTS` 的「十一」已是"测出问题怎么办" → 新节须插在后面并重编号 |

- **用他的**：文档指向不存在的路径，命令照抄全失败；章节号撞车。
- **用我的**：`SWITCHES.md` 整体搬运 + 重写命令/路径（保留他新增的"零、命令怎么敲起来"结构，这是给无 uv 现场的说明，价值高）；四篇文档按我方措辞重写开关章节；`MANUAL_TESTS` 新增「十二、开关」，X01–X16 原样 + 命令换成本机可行的形态。

> 已裁决：用我的。

---

### S9 `.gitignore`

他的 `guard_hooks/.gitignore` 只有一行 `guard_state.json`。

- **用他的**：需连目录一起造，等于复活被删的根目录（S4 会红）。
- **用我的**：把该条并入 `src/chrys/kernel/aixcoding/guard/pack/.gitignore`（新建）或仓库顶层 `.gitignore`。建议前者：`guard_state.json` 只在本机安装目录产生，pack 里其实不会出现，但放着可防误建。

> 已裁决：用我的。

---

### S10 `.gitattributes`（v2 新增，**上游文件**）

| | 值 |
| --- | --- |
| 我方 | 399B，`# Shell` 段只有 `*.sh text eol=lf` |
| 同事 v2 | 420B，多一行 `*.cmd text eol=crlf` |

- **取（1 行）**：保证 `guardctl.cmd` 检出时是 CRLF，且不会被 `* text=auto` 规范成 LF。
- **不取**：Git 若把 `.cmd` 存成 LF，cmd.exe 大体仍能执行，但 `setlocal`/标签解析在老系统上有已知隐患；同时也是给后来人一个明确信号。
- **代价**：这是**上游根文件**，动它触发 R2 审批（1 行，需在 design 的 R2 表里登记）。

> 建议：取（R2 登记为 1 行新增）。

---

### S11 CI（v2 新增 `.github/workflows/ci.yml`，**上游文件**）

| | 值 |
| --- | --- |
| 同事 v2 | 上游 `ci.yml` 里加 `guard_selftest` job：三平台、`PYTHONUTF8: "1"`、`uv sync --extra all`、`from chrys.app.guard_install import ...`、`guard_hooks/selftest.py`、`util/verify_guard_deployed.py` |
| 我方 | `aixcoding-tests.yml` 里已有 `guard` job（三平台 + 质量门禁 `needs`）：`from chrys.kernel.aixcoding.guard.guard_install import ...`、`python -m aixcoding.tests.guard.guard_pack_selftest`、`verify_guard_deployed.py` |

- **取他的 ci.yml**：上游文件改动（R2）、与我的 job 重复跑两遍、且引用的 `util/verify_guard_deployed.py` 在我树上不存在 → 必红。
- **取我的 + 补一处**：只把他的 `PYTHONUTF8: "1"` 搬进我的 guard job（我这边缺）。这是真实缺陷：Windows 运行器控制台是 cp1252/cp936，`guard_pack_selftest` 与 `verify_guard_deployed` 都打印中文（后者已自带 `reconfigure`，前者没有）→ 我的 Windows guard job 会 UnicodeEncodeError。

> 已裁决：不取他的 ci.yml；把 `PYTHONUTF8: "1"` 加到我的 guard job（约 3 行）。

---

### S12 `MANUAL_TESTS` 的 X01–X16 与 verify 覆盖率

| | 值 |
| --- | --- |
| 同事 v2 | 106 条用例行，其中新增「十一、开关」X01–X16（命令行为、总开关、单规则、通配、reset、workspace、show） |
| 我方 | 90 条用例行；`verify_guard_deployed.py` 61 条**机器可回放**用例（P/S/F/N/O 系列），X 系列属人工执行 |

- **处理**：X01–X16 原样搬（命令换成 `guardctl.cmd` 或 `python guardctl.py` 双写法），新增章节为「十二」；`verify_guard_deployed.py` 的 61 条不变（X 系列不进脚本），文档注明"61 条自动 + 17 条手工"（X01–X16 共 16 条，加"开关不影响既有用例"1 条）。
- **风险**：他 CI 注释写"105 条"、MANUAL_TESTS 行 106，两边统计口径不同；不做对齐，只按我方 61+16 记账。

> 建议：搬用例，不追求数字对齐。

---

### S13 `pack/` 新增 `guardctl.py` 的 ty 与 lint

- `guardctl.py` 有 `sys.path.insert(...); import _guard_core` → ty 报 `unresolved-import`。我方入口脚本用行内 `# ty: ignore[unresolved-import]`（他的 `_prompt_guard.py` 里已有同款注释），照抄即可。
- ruff：`pack/*.py = "py39"` 与 `pack/* = [RUF001-003]` 已覆盖新文件，`pyproject.toml` 无需改。
- 3.9 语法：他声明兼容 3.9+，需在合入后跑一次 `ruff check --target-version py39`（由 per-file 配置自动生效）。

> 建议：补 1 行行内忽略。

---

### S14 交付缺口

- `util/verify_guard_deployed.py`：他的 CI 调它，但目录里没有 → 他的 CI 若真跑，第二步会 FileNotFoundError。我方对应物是 `aixcoding/tests/guard/verify_guard_deployed.py`（61 条），以我方为准。
- `hooks.yaml` / `_prompt_guard.py` / `_tool_guard.py` 仍未带（视为未改），但 `_launcher.ps1` 语义已变 → 我方的 `hooks.yaml` 需要复核一遍 GUARD_SCRIPT 形态（现为相对路径，兼容，无需改）。
- `rules.json` 与 `_guard_core.py` 与 v1 一致，说明他这两版没有再改开关语义 —— 他 v2 的自检里也仍有 `test_state_file_can_restore_the_workspace_restriction`（即"状态文件可把范围收紧"），与 C1 的 `true` 出厂值配套。

---

## 4. 目标形态与工作量

C1 已定后，唯一目标形态：

| 维度 | 结果 |
| --- | --- |
| 出厂写入范围 | 不拦截（`true`），`guardctl workspace restrict` 可收紧 |
| 总开关 / 单规则开关 | 可用（`guard_state.json`，重装保留） |
| Windows 免 uv 入口 | 可用（`guardctl.cmd` + 新 `_launcher.ps1`） |
| 既有加固 | 全部保留（10 项，见第五节） |
| 上游文件改动 | 2 处：`.gitattributes` +1 行、`aixcoding-tests.yml` +3 行（后者是我方定制 workflow，不算上游） |
| 既有断言改动 | 0 处期望值（只加前置） |

改动规模估算：

| 类别 | 量 |
| --- | --- |
| `_guard_core.py` 合成（state 三段 + 校验合成） | ~60 行 |
| 新增 `guardctl.py`（22.6KB）/ `guardctl.cmd`（1.2KB）/ 替换 `_launcher.ps1` | 新增 3 文件 |
| 安装器 / 手工安装器 payload | 各 +2 文件名 |
| 自检搬运用例 | 25 条 + 4 个 helper 形参（~30 行机械） |
| 我方测试改动 | `test_guard_install` +2 条；`test_packaging` +2 文件名；`verify_guard_deployed` +~10 行状态前置 |
| 配置 | `.gitattributes` +1 行；CI +3 行；`pack/.gitignore` +1 行 |
| 文档 | `SWITCHES.md` 新增（13.5KB 改写路径/命令）；`README` / `OPERATIONS` / `PORTING` / `MANUAL_TESTS` 增补；`MANUAL_TESTS` +16 行 X 用例 + 2 节前置说明 |
| 代码合计（不含文档） | 约 350 行新增/改动 |
| 验证 | 三平台自检 + 61 条部署验证 + `chrys_test --smart` + ruff/format/ty |

---

## 5. 不冲突、但"整文件覆盖"会静默回退的 10 项

取他的 `_guard_core.py` / `selftest.py` / 安装器整文件，会丢掉我这些改动（他的文件里没有对应行，不是冲突项）：

1. `_validate_rule` / `_validate_settings` / `_validate_dictionaries` 严格校验与规则 id 唯一性检查；
2. `write_decision()` 失败时 `return 1`（让 `on_error: block` 生效，否则"拦不住却当放行"）；
3. `_canonical_scope()` / `_native_resolved()` 符号链接与 junction 解析；
4. `zip` 写目标由 `last` 改 `first`；
5. `>` / `>>` / `>|` 从连写中拆词；
6. PowerShell `Copy-Item` / `Move-Item` 取 `-Destination` 而非 `-Path`；
7. flow-style `hooks: [...]` 与空 `settings:` 段在安装合并时保留（否则用户原有 hook 被吞掉）；
8. 环境变量前缀统一 `AIXCODING_GUARD_*`；
9. 全部迁移成果：tier 落位、无 `force-include`、CI 三平台 guard job、`verify_guard_deployed.py`、`test_anchors.py`；
10. 新增自检用例 7 条（T-MCP-01/02/03、必需规则 ID 集、非法规则拒绝、非法 `confirm_disposition` 拒绝、写决策失败返回非零）。

---

## 6. 决策记录

| 编号 | 议题 | 选项 | 裁决 |
| --- | --- | --- | --- |
| C1 | 出厂写入范围 | 他 `true` / 我 `false` | **他（`true`）** |
| C2 | `GuardConfig` 构造 | 合成 | ☑ 合成 |
| C3 | 写范围门闸与路径解析 | 合成 | ☑ 合成 |
| C4 | 安装 payload（py + cmd） | 合 | ☑ 合 |
| C5 | 手工安装器 payload | 合 | ☑ 合 |
| C6 | `_launcher.ps1` 绝对路径 | 他 / 我 | ☑ 他 |
| S1 | 环境变量前缀 | 我 | ☑ 我 |
| S2 | 自检装配 | 我 | ☑ 我 |
| S3 | guardctl 内部路径/导入 | 改 | ☑ 改 |
| S4 | pyproject 打包与 ty | 我 | ☑ 我 |
| S5 | 打包测试 | 我 | ☑ 我 |
| S6 | 连带断言与文档 | 随 C1 | ☑ 加前置、期望不动 |
| S7 | 测试文件落点 | 我 | ☑ 我 |
| S8 | 文档落点与措辞 | 我 | ☑ 我 |
| S9 | `.gitignore` | 我 | ☑ 我 |
| S10 | `.gitattributes` | 取（R2） | ☐ |
| S11 | CI 编码变量 | 取那 3 行 | ☐ |
| S12 | MANUAL_TESTS X01–X16 | 搬 | ☐ |
| S13 | guardctl 的 ty 行内忽略 | 加 | ☐ |
| S14 | 交付缺口 | 以我方为准 | ☐ |

整体路径（`DEVELOPMENT.md` §4.1「方案未签，不动代码」）：

- ☐ **A 完整管线**：我起草 `aixcoding/docs/batches/guard-switches/{requirements,design,test-plan,plan}.md` → 责任人签 `design.md` → 实施 → 换 AI 独立评审 → 签 `self-review.md`。
- ☐ **B 跳过管线直接实施**：责任人明确裁决；我不自行判定豁免。

---

## 7. 落地清单（14 项）

1. `src/chrys/kernel/aixcoding/guard/pack/_guard_core.py` —— 回移 state 三段 + 按 C2/C3 合成；
2. `.../pack/guardctl.py` —— 新增（改 docstring 路径、`# ty: ignore` 行）；
3. `.../pack/guardctl.cmd` —— 新增（原样，ASCII / 无 BOM / CRLF）；
4. `.../pack/_launcher.ps1` —— 替换为 v2 版（绝对路径支持）；
5. `.../pack/rules.json` —— `allow_outside_workspace: true`（C1）；
6. `.../pack/.gitignore` —— 新增，忽略 `guard_state.json`；
7. `src/chrys/kernel/aixcoding/guard/guard_install.py` —— payload 加 `guardctl.py` / `guardctl.cmd`；
8. `aixcoding/tools/guard_install_merge.py` —— payload 同上；
9. `aixcoding/tests/guard/guard_pack_selftest.py` —— 搬 25 条用例 + 4 个 helper 形参 + 3 个写范围方法改用 `config_with_write_scope(restricted=True)`；
10. `aixcoding/tests/guard/test_guard_install.py` —— +2 条（开关重装保留、guardctl 装到位）；
11. `aixcoding/tests/guard/test_packaging.py` —— pack 文件集 +2 个文件名；
12. `aixcoding/tests/guard/verify_guard_deployed.py` —— 部署后写限制态 `guard_state.json`（保持 F01 期望不变）；
13. `.gitattributes` —— +`*.cmd text eol=crlf`（R2 登记）；`.github/workflows/aixcoding-tests.yml` —— guard job +`PYTHONUTF8: "1"`；
14. 文档 —— `rule-pack/SWITCHES.md` 新增；`README` / `OPERATIONS` / `PORTING` / `MANUAL_TESTS` 增补（X01–X16 入「十二、开关」；六 / 六之二 加 `workspace restrict` 前置）。

验证：`uv run python -m aixcoding.tests.guard.guard_pack_selftest`、`uv run python aixcoding/tests/guard/verify_guard_deployed.py`、`uv run python scripts/chrys_test.py --smart --paths <改动文件>`、`uv run ruff check` / `ruff format --check` / `ty check`（三平台）。
