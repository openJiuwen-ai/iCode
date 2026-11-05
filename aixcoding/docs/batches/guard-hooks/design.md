# 设计：安全护栏（guard hooks）迁入

<!--
签认规则（R10）：责任人本人编辑本头部（状态→已确认、填日期，确认意见可选），AI 不得代填。
签认后修改正文需重新签认。校验：uv run python aixcoding/tools/check_docs.py
-->

- 责任人：李海潮
- 签认人：李海潮
- 签认日期：2026-11-05 15:10:58
- 状态：已确认
- 确认日期：2026-11-05
- 确认意见：优先不改少改尽量少而集中的改上游代码，可确认执行（可选；审阅中形成的判断、附加条件或保留）

> **最终实施方案（2026-11-05，责任人 F1–F6 裁决）**：规则包迁回 tier 内
> `src/chrys/kernel/aixcoding/guard/pack/`，仓库根不保留副本，不新增 force-include；
> 仅在门禁无法通过且无替代写法时保留最小豁免。R2 第 2 行同步为最小 lint/type 配置。

## 方案概述

判定＝**有改动的迁**（2026-11-05 呈报裁决）：核心逻辑照迁，挂接形态按 0.33.0 基线重审。
签认前独立评审（`independent-review.md`）F3/F4/F5 已修正，F6/F7 记录为预存在等价行为；
实施后第二 AI 自审的 F1–F6 处置见 `self-review.md`。

- **规则包**：payload `_prompt_guard.py` / `_tool_guard.py` / `_guard_core.py`、`rules.json`、
  `_launcher.ps1`、`hooks.yaml` 模板全部落 kernel 档定制包 `guard/pack/`，随
  `packages = ["src/chrys"]` 自动进 wheel；不再使用仓库根 `guard_hooks/`，也无第二份副本。
- **测试与运维资产**：规则自检迁 `aixcoding/tests/guard/guard_pack_selftest.py`；部署验证迁
  `aixcoding/tests/guard/verify_guard_deployed.py`；手工安装工具迁
  `aixcoding/tools/guard_install_merge.py`；四份运维文档迁本目录 `rule-pack/`。均符合 R4。
- **自装器**：`guard/guard_install.py`（kernel 档，仅 foundation 依赖）。算法保持幂等、
  按 id 原位替换、settings 键同步、原子写、legacy payload 清理；资产路径简化为唯一
  `pack/`，删除源码 checkout 双路径 fallback。合法 flow-style YAML 也必须保留无关
  hooks/settings，不再因块解析为空而丢弃。
- **接线（上游改动之一）**：`bootstrap_runtime()` 内一处调用：函数内 lazy import +
  `try/except Exception` 全吞错 + `logger.warning`（fail-open）。护栏未装的状态核查走
  `aixcoding-cli hook list` / `hook validate`（签认确认：警告通道＝纯 logger）。
- **规则引擎自审修复**：block 决策写入失败时非零退出，让 `on_error: block` 生效；规则
  结构、检测器类型与 disposition 加载时严格校验，非法配置抛 `GuardError`，不得静默跳过；
  `T-SHELL-05` 补齐重定向/zip/PowerShell 目标/symlink 越界拦截。
- **CI**：旧分支 `guard_selftest` job 三步挂入 `aixcoding-tests.yml`（定制 CI，ADR-0004），
  命令改为新模块/脚本路径。

上游接触面合计 **2 文件 18 行**：`orchestration/startup.py`（+8）+ `pyproject.toml`
（C137 最小 lint/type 配置 +10）。

结构：

```
src/chrys/kernel/aixcoding/guard/
├── guard_install.py                  # 自装器：唯一资产路径 pack/
├── __init__.py
└── pack/                             # 随 packages 自动进 wheel
    ├── _prompt_guard.py / _tool_guard.py / _guard_core.py
    ├── rules.json / _launcher.ps1 / hooks.yaml
    └── __init__.py

aixcoding/tests/guard/
├── guard_pack_selftest.py            # 70 条规则自检
├── verify_guard_deployed.py          # 部署副本 MANUAL_TESTS
├── test_guard_install.py / test_anchors.py / test_packaging.py
aixcoding/tools/guard_install_merge.py # 特殊环境手工安装通道
aixcoding/docs/batches/guard-hooks/rule-pack/  # 运维文档
```

## 方案演进说明（最终回到 D2-A，2026-11-05）

初版 D2-A 把规则包放 tier 内 `pack/`；实施期曾以扫描面成本改选方案 X（仓库根 +
force-include）。第二 AI 自审 F1 指出仓库根落点与 R4 冲突，责任人裁决迁回 D2-A，
并把可分离资产按 R4 分落到测试、工具和文档目录：

- 进入 `src/` 的只剩 6 个运行时文件 + 包锚点，不再包含 157 处 assert 的 selftest、
  subprocess 验证脚本、手工安装器和四份文档——此前“src 全扫描面”的主要成本被分流，
  而不是靠仓库根绕过门禁；
- 仅保留三类无法靠改写法消除的最小豁免：payload 的 py39 语法目标、中文文案的
  RUF001/2/3、独立脚本对同目录 `_guard_core` 的两处 ty import 抑制。无 T201、无
  `# noqa`、无 ty 文件级 override；
- 打包由 `packages = ["src/chrys"]` 自动覆盖，force-include 条目全部撤销；
- 合法 flow-style YAML 与 flow settings 通过数据保留路径合并，避免删除用户已有配置。

## 备选方案对比

| 决策点 | 方案 | 优点 | 缺点 | 升级冲突面（R1） | 结论 |
|---|---|---|---|---|---|
| D1 接线点 | **A. `bootstrap_runtime()` 内一处** | 覆盖全部会话入口；与基线统一入口机制同构（R12），受 `test_entrypoint_bootstrap` 守护 | `serve` / `install` / `uninstall` 不触发安装 | startup.py 1 文件 +8 行 | **选 A** |
| | B. app.py `main()` 顶部照旧形态 | 所有子命令都装 | 绕开基线 bootstrap，R12 反例风险 | app.py 1 文件 ~10 行 | 否决 |
| D2 规则包落点 | **A. tier 内 `kernel/aixcoding/guard/pack/` 随包** | 源码树＝安装树，单一资产路径；符合 R4/R9 | payload 落入 src 扫描面，需要最小 lint/type 豁免 | pyproject 10 行（C137） | **选 A（F1 责任人裁决）** |
| | B. 仓库根 + force-include | 天然出圈，旧分支验证形态 | 违反 R4 新文件落点，需规范例外 | pyproject force-include 10 行 | 否决 |
| D3 运维文档落点 | **A. `aixcoding/docs/batches/guard-hooks/rule-pack/`** | 符合 R4 文档落点；维护者随批次查阅 | 不进 wheel | 零 | **选 A** |
| | B. 随 wheel 分发 | 用户包内可查 | 文档无运行价值，且需额外打包配置 | pyproject 增条目 | 否决 |
| D4 手工安装器 | **A. `aixcoding/tools/guard_install_merge.py`** | 保留排查/特殊环境通道，符合 R4 | 非运行时资产 | 零 | **选 A** |
| | B. 退役 | 目录更干净 | 丢手工复现通道 | 零 | 否决 |
| D5 lint 配置 | **A. 主仓最小例外（C137）** | 单一 lint 体系；例外可在 pyproject 集中审计 | R2 增 pyproject 10 行 | pyproject 10 行 | **选 A** |
| | B. pack 内自带 ruff.toml | 不污染主配置 | 在 src 内制造第二套门禁，遮蔽主仓规则 | 零上游行数但治理冲突 | 否决 |

**R12 专题（目标基线下是否有比旧实现更好的方案）**：
- 结构——规则包进 kernel 档定制包，测试/工具/文档按 R4 分流；比旧仓库根平铺更符合目录规范；
- 挂点——app.py 顶部 → `bootstrap_runtime()`：同构跟随基线统一入口机制；
- 上游行数——旧 app.py ~10 行 + pyproject force-include 6 条目 → 新 startup.py 8 行 +
  pyproject 10 行最小 lint/type 配置，接触面持平且无业务逻辑残留上游。
结论：上游 0.33.0 仍无 hook 规则包分发/安装机制；Windows 商店存根仍未过滤，PowerShell
启动器仍必要；`PYAPP_PROJECT_NAME=chrys` 未变，启动器兜底路径有效。

## 上游改动清单（R2）

| 文件 | 改什么 | 预估行数 | 标记注释位置 |
|---|---|---|---|
| `src/chrys/orchestration/startup.py` | `bootstrap_runtime()` 函数体开头加护栏安装：lazy import `chrys.kernel.aixcoding.guard.guard_install.install_guard_hooks` + `try/except Exception` 全吞错 + `logger.warning`（fail-open） | +8 行 | 调用处上方单行标记（C136） |
| `pyproject.toml` | 最小 lint/type 配置：payload 的 py39 per-file target、中文文案 RUF001/2/3 per-file ignores；无打包映射 | +10 行（含注释与 C137 标记） | 新增条目段首单行标记（C137） |

**R1 自查（必答）**：业务逻辑全部外置定制包（guard_install 与 payload 均在
`src/chrys/kernel/aixcoding/guard/`；测试/工具/文档按 R4 分流）？是。接线点最小化
（能一处不两处）？是——代码接线仅 `bootstrap_runtime` 一处；pyproject 仅门禁配置
（无可外置逻辑）。

**迁移批次细则回答（细则单源见 DEVELOPMENT.md R1）**：

- **细则一（基线重审）**：已完成（呈报 + 独立评审核证 + 实施复核 + 第二 AI 自审）。
  事件契约五点与 0.33.0 兼容（`user_prompt_submit` / `before_tool_call` 同属
  `GATED_EVENTS`；`run.type: command` + argv / env / `${chrys_home}` cwd；
  `execution.blocking` + `on_error: block`；settings 键）；旧接线点（app.py argparse）已失效，
  重审为 bootstrap。
- **细则二（逐行归属）**：R2 表第 1 行＝上游只留挂点，业务逻辑零行残留；第 2 行＝
  最小 lint/type 配置，无可归属业务逻辑。`guard_install.py`、payload、rules、launcher、
  测试、工具和文档 100% 在定制落点。
- **细则三（消费方视野）**：公共 API 唯一（`install_guard_hooks`），无跨特性共享消费方；
  规则包资产路径常量供测试断言复用。

## R13 新增豁免清单（责任人 2026-11-05 裁决：仅必须项）

| 形态 | 位置 | 为什么必须 | 批准指向 |
|---|---|---|---|
| pytest/ruff per-file target `py39` | `pyproject.toml`（C137） | hook 子进程使用 PATH 上任意 Python（3.9+）；主仓 py314 format 会把带括号 except 改写为 PEP 758，py<3.14 语法错误导致 hook fail-closed | design 本文 + C137 |
| RUF001/2/3 per-file ignores | `pyproject.toml`（C137） | 规则用户文案、运维输出与中文注释使用全角标点；改 ASCII 会改变用户可见文案 | design 本文 + C137 |
| `# ty: ignore[unresolved-import]` ×2 | `pack/_prompt_guard.py`、`pack/_tool_guard.py` | 脚本复制到用户 hooks 目录后以同目录名导入 `_guard_core`，静态解析无法成立 | design 本文 |

未新增：T201 豁免（手工工具改用 `sys.stdout.write`）、`# noqa`、ty 文件级 override、
force-include、仓库根例外。`aixcoding/tests/**` 原有的 RUF001 复用，不扩大。

## 对上游隐式行为的假设

1. **bootstrap_runtime 是会话入口的统一引导**（`test_entrypoint_bootstrap.py` 守护；`serve` /
   `install` / `uninstall` 豁免）→ 接线于此覆盖全部会话入口 → 失效表现：新增入口绕过
   bootstrap 时护栏不装（fail-open 无害，下次经正常入口装上）。anchor＝
   `orchestration/startup.py::bootstrap_runtime`。
2. **`chrys.foundation.platform.get_platform()` 提供 `config_dir`**（`~/.chrys` /
   `%APPDATA%/chrys`，经 `real_path`）且 hooks 运行时读 `config_dir/hooks/hooks.yaml` →
   失效表现：安装位置与读取位置分叉，护栏静默不生效（`hook list` 可侦测）。anchor＝
   `foundation/platform/__init__.py::get_platform`。
3. **hooks 事件与配置契约**（`service/hooks/events.py::GATED_EVENTS` 事件门控集合 +
   `service/hooks/schema.py` 的 HookConfig / HookRun / HookExecution / `on_error: block` /
   `${chrys_home}` 模板变量 / settings 键）→ 上游改名/收紧时 hooks.yaml 加载失败或语义漂移
   → `hook validate` / selftest 侦测。anchor＝`service/hooks/events.py::GATED_EVENTS` 与
   `service/hooks/schema.py` 并列。
4. **PyApp 数据目录名**：构建脚本 `PYAPP_PROJECT_NAME=chrys`（品牌切换未改，与「wire 名
   保持 chrys」的品牌边界一致）→ 启动器兜底路径 `%LOCALAPPDATA%\pyapp\data\chrys\...`
   有效 → 失效表现：打包版 Windows 且 PATH 无真 python 时启动器找不到解释器 → hook
   fail-closed（每句被拦，可观测）。anchor＝`scripts/build.ps1` / `build.sh`。
5. **hatch 自动打包包内非 Python 资产**：`packages = ["src/chrys"]` 把
   `guard/pack/` 下的 `.json` / `.yaml` / `.ps1` 一起装进 wheel（无 force-include）→
   失效表现：wheel 缺 payload，安装器无源（fail-open 警告）；打包测试与 CI Build 侦测。
   anchor＝`pyproject.toml [tool.hatch.build.targets.wheel] packages`。

**预存在等价行为确认（评审 F6/F7，非本批次引入）**：
- settings 键随包同步会把文件级 `shutdown_grace_seconds` 覆写为 30（上游默认 5.0），影响
  用户 `hooks.yaml` 中所有 hook——**旧设计有意行为**（guard_install docstring 明示「every
  upgrade carries them」），等价迁移维持，OPERATIONS.md 已载。
- 并发首装：两进程同时首启时安装器的 read-modify-write 存在理论竞态（temp+os.replace 保证
  文件不损坏，不保证不丢并发无关修改）——与旧分支行为等同，fail-open 语义下风险自限。
- `T-SHELL-05` 写入范围增强（第二 AI 自审 F7，责任人 2026-11-05 决定修复）：词法器拆分
  紧贴参数的重定向；`zip` 检查归档目标；`Copy-Item`/`Move-Item` 检查 `-Destination` 或
  第二个位置参数；范围判定前解析 native symlink/junction。对应正例加入 selftest，
  junction/symlink 用真实文件系统 pytest 覆盖。

## 定制点登记预告

| id | 落点 | marker 锚 | 预期计数 | anchor | risk | removal_condition |
|---|---|---|---|---|---|---|
| C136 | src/chrys/orchestration/startup.py | AIxCoding guard: install bundled security guard hooks | 1 | `bootstrap_runtime`（orchestration/startup.py，含入口豁免清单） | 中（改核心引导函数） | 〔上游机制替代〕上游自带 hook 规则包分发/安装机制，或安全护栏特性整体退役（届时按 UPGRADE-RUNBOOK §5 淘汰检查呈报裁决） |
| C137 | pyproject.toml | AIxCoding guard: bundled rule pack lint profile | 1 | `[tool.ruff.per-file-target-version]` 与 `[tool.ruff.lint.per-file-ignores]` 的 guard 条目（门禁配置，静态声明） | 高（lint/type 门禁配置） | 〔上游机制替代〕payload 最低解释器提升到 3.14 且文案改为 ASCII 后摘除；或护栏整体退役时摘除 |

批次锚注：guard_install 依赖 `foundation/platform::get_platform`（假设 2）、hooks 事件与
配置契约（假设 3）、PyApp 数据目录名（假设 4）、hatch 包内资产打包（假设 5）——升级时
随本批次一并核对。

## 风险与最坏情况

- **安装器故障**：fail-open（全吞错 + logger.warning），最坏＝护栏缺席（拦截功能缺失，非
  主流程故障）；无条件幂等重装保证升级自愈。
- **hook 层 fail-closed 与安装层 fail-open 是两层独立语义**（前者为旧设计刻意选择：规则
  引擎起不来时宁可拦下也不放行），迁移不改变该分层。
- **上游 hooks 契约漂移**（升级 0.34+）：`hook validate` + selftest + 部署验证三道侦测；
  登记点 C136/C137 anchor 提示核对。
- **决策错了会怎样**：接线点选 bootstrap 若未来上游重构 bootstrap（拆分/更名），C136 冲突
  面为 8 行一处标记，重挂成本低；C137 仅 lint/type 配置，上游若改 ruff/ty 配置结构，
  门禁测试立即暴露，回滚即删条目。
- **回滚**：删 startup.py 接线 8 行 + 删 pyproject C137 条目 + 删
  `src/chrys/kernel/aixcoding/guard/` + 删 `aixcoding/tests/guard/` +
  `aixcoding/tools/guard_install_merge.py` + 本目录 `rule-pack/` + CI 摘除 guard 步骤；
  用户环境清理：删 `config_dir/hooks/guard/` 并从 `hooks.yaml` 移除两条目（或还原
  `.pre-guard` 备份）。无数据迁移、无上游格式变更。

## ADR 判定

（按 DEVELOPMENT.md §6 判定 checklist）

1. 有被认真考虑但否决的备选方案？——落点方案在实施中经两轮反转（记录于「方案演进说明」），
   但结论回到初版 D2-A，属常规方案比选的修正，无架构级决策。**否**；
2. 撤销代价高？——单接线点 + 单目录资产 + 最小 lint/type 配置，回滚见上节。**否**；
3. 约束未来工作模式？——不约束（hook 生态照上游机制走）。**否**；
4. 依赖上游非公开接口或行为？——`bootstrap_runtime` / `get_platform` / hooks 事件与配置
   契约 / hatch 包内资产打包均为公开契约，漂移风险以假设节 + 登记 anchor 承载。**否**。

结论：不出 ADR（唯一合理路径的照规范执行，R4 tier 落位 + R12 重审挂点；落点反转属实施期
方案修正，证据与裁决留档于本文「方案演进说明」与 plan.md）。
