# 评审：guard-hooks 批次文档（签认前独立评审）

- 日期：2026-11-05
- 评审对象：本目录三件文档（`requirements.md` 55 行 / `design.md` 152 行 / `plan.md` 30 行），design 状态＝**草案（未签认）**，工作区未提交内容（目录未跟踪）
- 评审轮：独立评审 AI（GLM-5.3，ZCode 会话）；与作者自审独立，结论冲突以本文证据为准呈报
- 对照基准：① `aixcoding/docs/standards/DEVELOPMENT.md`（R1–R13 摘要）② 根 `AGENTS.md` ③ 现基线工作区（`main_aixcoding_cli`，上游 0.33.0）④ 旧分支 `main_aixcoding_cli_0.28.0_final`（护栏资产来源，`git show/ls-tree` 只读取证）
- 评审方式：三项独立检查——R2/R11 声明面 vs 实际必要面比对；全部代码锚点定义查证（file:line 证据）；边界/异常/并发缺陷模式扫描。`uv run python aixcoding/tools/check_docs.py` 实跑 PASS（19 ADR / 27 design / 25 self-review）

> 本文为签认前评审记录，落批次目录但不属签认/R2 批准池文件（`check_docs.py` 仅扫 `**/design.md` / `**/self-review.md` / `**/adr/*.md`，本文不在扫描面）。

## 一句话结论

文档结构与锚点质量高：约 25 个代码锚点**全部属实、无幻觉 API**，流程要件（R1 自答、细则一二三、R12 专题、假设 anchor、C136 登记、ADR 判定四问）齐全。但按当前草案直接开发会在两处卡住——**F1**（R2 清单很可能缺 `pyproject.toml` 一行，与"上游 pyproject 零改动"表述冲突）与 **F2**（`selftest.py` 落 `src/` 树将撞两道上游架构守卫 + ruff S101，文档未给适配策略）。建议补齐 F1/F2 并顺手修 F3/F4/F5 三处小误后再签认开工；方案本体（bootstrap 单点接线、tier 内单路径、去 force-include/双路径 fallback）技术上成立且优于旧形态。

## 一、评审范围

| 项 | 值 |
|---|---|
| 评审对象 | `aixcoding/docs/batches/guard-hooks/{requirements,design,plan}.md`（工作区未提交版本，2026-11-05） |
| 性质 | 签认前文档评审（批次尚无代码 diff；三项检查适配为：R2 声明面完整性比对 / 锚点幻觉查证 / 设计级缺陷模式扫描） |
| 代码基线 | `main_aixcoding_cli` 工作区（上游 0.33.0 基线）；资产来源 `main_aixcoding_cli_0.28.0_final` |
| INDEX 登记 | 已登记（`aixcoding/docs/INDEX.md:38`） |

## 二、R2 / R11 比对（声明面 vs 实际必要面）

| 声明 | 取证 | 判定 |
|---|---|---|
| R2 表仅 1 行：`orchestration/startup.py` +6~8 行（lazy import + 全吞错 + logger.warning） | `bootstrap_runtime` 确在 `src/chrys/orchestration/startup.py:182`；接线形态可行；旧形态（app.py try/except + `sys.stderr.write`，旧 `src/chrys/app/cli/app.py:27-32`）描述准确 | 清单内、成立 |
| 方案概述："无需 force-include（**上游 pyproject 零改动**）" | 仅对 force-include 成立。旧分支 pyproject 为**同一包内容**配了 3 类门禁豁免（force-include 旧 :97-102、ruff per-file-target `guard_hooks/* = "py39"` 旧 :145、ruff per-file-ignores `["RUF001","RUF002","RUF003"]` 旧 :205、ty overrides unresolved-import=ignore 旧 :251-255）——这些需求源于包内容本身（中文全角标点 / payload 需跑在旧 PATH python / payload 经 sys.path 导入 `_guard_core`），**不因落点从 repo-root 移入 src/ 而消失**。现行 pyproject per-file-ignores 仅覆盖 `app/aixcoding/tui/*` 与 `service/aixcoding/**`（各仅 RUF001，:208/:210），无 kernel/aixcoding 条目；ty overrides 机制在（:222+）但无 pack 条目 | **候选：清单外变更（F1）** |
| R11 新增第三方依赖 | pack 纯标准库；`guard_install` 仅依赖 `foundation.platform.get_platform`（`src/chrys/foundation/platform/__init__.py:187`）+ yaml（`pyyaml==6.0.3`，pyproject:32，既有） | 清单内、无新增 |

## 三、锚点验证（幻觉检查）——通过项清单

以下锚点全部在现基线或旧分支定位到定义，**未发现幻觉**：

**现基线（0.33.0）**

| 文档声称 | 证据 |
|---|---|
| `bootstrap_runtime()` 为统一引导，serve/install/uninstall 豁免 | `orchestration/startup.py:182`；`tests/architecture/test_entrypoint_bootstrap.py` 存在；AGENTS.md 同述 |
| `pyapp_main` 亦经 `main()` 分发 | `app/cli/app.py:228-230`（`raise SystemExit(main())`） |
| `get_platform` 提供 `config_dir`（real_path） | `foundation/platform/__init__.py:187` |
| `user_prompt_submit` / `before_tool_call` 同属 `GATED_EVENTS` | `service/hooks/events.py:161`（⚠ anchor 写成 schema.py，见 F4） |
| hooks YAML 契约：`id` / `run.type: command` + argv/env/cwd `${chrys_home}` / `execution.blocking` + `on_error: block` | `service/hooks/schema.py:197-216`（HookConfig）、`:116-151`（HookRun）、`:154-196`（HookExecution，on_error :185）；`service/hooks/validation.py:314`（block 需 blocking+gated） |
| settings 键（`shutdown_grace_seconds` / `max_parallel_hooks`） | `service/hooks/schema.py:239/:245`（上游默认 5.0 / 4，见 F7） |
| CLI `hook` 命令仅 list/validate | `app/cli/hooks.py:35` |
| hook 子进程 stdin=DEVNULL（STREAM_CHUNK 非目标理由） | `service/hooks/runner.py:198` |
| `PYAPP_PROJECT_NAME=chrys` 未随品牌切换改名 | `scripts/build.ps1:135`、`scripts/build.sh:151` |
| hatch `packages = ["src/chrys"]`，非 .py 资产随包有先例 | `pyproject.toml:109-110`；`src/chrys/app/aixcoding/tui/login.tcss` 等 tcss 在包内（docs 需 force-include 仅因其位于包外，:112-115） |
| `aixcoding/tests/`（R4 测试落点）、`aixcoding-tests.yml`（ADR-0004）、`src/chrys/kernel/aixcoding/`（ADR-0011 tier 落位先例）、ADR-0009/0012/0015、`aixcoding/tools/check_docs.py`、`gen_customizations.py` | 全部存在（目录/文件实查） |

**旧分支（`main_aixcoding_cli_0.28.0_final`，资产来源）**

| 文档声称 | 证据 |
|---|---|
| 旧仓库根 `guard_hooks/` 规则包 | `git ls-tree` 实数 **13 文件**（design 写 14，见 F3）；payload/hooks.yaml/selftest/_install_merge/四文档/ruff.toml/_launcher.ps1 全在 |
| 旧 `src/chrys/app/guard_install.py` 及算法（幂等、按 id 原位替换、settings 键覆写、损坏备份重置、tempfile+os.replace 原子写、`GUARD_ASSET_DIR`/`_SOURCE_ASSET_DIR` 双路径） | 文件在；docstring 与函数体逐一印证；仅依赖 foundation.get_platform + yaml |
| 旧 hooks.yaml 两条目 `security-prompt-guard` / `security-tool-guard`（command 型 + PowerShell 启动器 + `${chrys_home}/hooks` cwd + blocking + on_error: block + timeout 120） | 原文实读，与现基线 schema 契约完全相容 |
| 旧测试与 CI：`tests/app/test_guard_install.py`、`tests/architecture/test_guard_hooks_packaging.py`、`util/verify_guard_deployed.py`、旧 ci.yml `guard_selftest` 三步（安装 → selftest 46 断言 → MANUAL_TESTS 64 例） | 全部在位（旧 ci.yml:324-350） |
| C136 未被占用 | C130/C131（branding-and-command design.md:287-288）、C132（catalog-auth-gating :366/370）、C133-C135（upstream-merge-v0.33.0 :122-129；C134/C135 catalog-readonly-coverage :42-56）——C136 空闲且顺次 |

## 四、候选 findings（只列候选与依据，裁决归责任人）

### F1【高】R2 清单不完整：`pyproject.toml` 很可能是第 2 个上游文件

依据：见第二节第二行。旧分支为同一包内容所配的 ruff（RUF001/2/3、py39 target）与 ty（unresolved-import）豁免需求随包内容平移；落点移入 src/ 后现行 pyproject 无任何条目覆盖新路径。出路只有两条：① 动 pyproject（per-file-ignores / per-file-target / ty overrides）——R2 表缺行，且与方案概述"上游 pyproject 零改动"直接冲突；② 改写 payload 内容（去全角标点、限语法子集、消除 sys.path 导入）——超出声明的"仅品牌词与环境变量名修订"，与"等价迁入"目标形成张力。D5/plan 任务 4/requirements 验收已预留 R13 兜底，但把很可能发生的 pyproject 变更留作"实施时发现"，会造成签认的 R2 审批范围与实际 diff 范围错位。

### F2【高】`selftest.py` 落 `src/chrys/kernel/aixcoding/guard/pack/` 撞两道架构守卫 + ruff S101，文档未给策略

依据：
- `tests/architecture/test_test_hygiene.py:116-119` `_SRC_HYGIENE_RULES` 含 `_assert_no_source_asserts` 与 `_assert_subprocess_stdin_is_explicit`，经 `_src_sources` 对 src 全量 `*.py` 分片扫描（`tests/architecture/_hygiene_core.py:47-49`），meta-guard 强制 sweep 无 skip/早退（test_test_hygiene.py:221）；
- assert 守卫**不认 noqa**（`test_hygiene_source_asserts.py:37` 参数化用例明确拒绝 `# noqa: S101` 与文件级 noqa）；旧 `selftest.py` 有 **157 处 assert / 784 行**；
- 旧 `selftest.py:697` `subprocess.run(...)` 未传 `stdin=`，撞 stdin 守卫（AGENTS.md：每个非交互子进程显式 DEVNULL）；
- ruff 对 src 启用 S（pyproject:168），per-file-ignores 的 S 豁免仅限 `aixcoding/tests/**`（:212）与 `tests/**`（:214）；
- plan 任务 4 只写"payload 适配主仓 lint/ty"——selftest 的 assert/stdin 适配或另行落位在全部三份文档中缺席。

可行出路（供裁决时参考）：改写 selftest（assert → 显式 raise、补 `stdin=subprocess.DEVNULL`，属测试载具代码不动规则内容）；或 selftest 移出 src/ 树（如 `aixcoding/tests/guard/`，需处理 pytest 收集语义与运行方式——其 46 断言 unittest 形态可能被 aixcoding 收集器误收）；最差路径是改上游 sweep（新 R2 + meta-guard 修改，应避免）。

### F3【低】事实笔误："旧仓库根 `guard_hooks/`（14 文件）"——`git ls-tree` 实数 13，design 自身的枚举也是 13 项。

### F4【低】假设 3 anchor 不精确：GATED_EVENTS 定义在 `service/hooks/events.py:161`，非 `service/hooks/schema.py`（后者只承载 HookConfig/run/match/on_error 契约）。升级核对按现 anchor 找 GATED_EVENTS 会落空；建议 anchor 改 events.py 或并列两处。

### F5【低】接线理由失准：design 称旧形态"stderr 在 ACP stdio 场景有协议污染风险"——与项目自身约定相反（ACP 中 stdout 才是 JSON-RPC，stderr 即日志通道，`app/cli/acp.py::_configure_logging` 把日志写 stderr）。改 logging 本身合理（统一配置、可分级、避免裸 write），但 stated rationale 会误导后续评审与升级排查。

### F6【信息，并发，预存在】安装器随每次进程启动运行：两进程并发首装时 read-modify-write 竞态理论上可丢失一份并发写入的无关 hook（temp+os.replace 保证文件不损坏，不保证不丢并发修改）。与旧分支行为等同（旧同为每入口运行），迁移未放大；fail-open 语义下风险自限。仅记录。

### F7【信息，边界，预存在】"settings 键随包同步"会把文件级 `shutdown_grace_seconds` 全局覆写为 30（上游默认 5.0，schema.py:239），影响用户 `hooks.yaml` 中**所有** hook（含无关 hook）的关闭等待。旧设计同等行为；确认系有意等价即可。

## 五、对「按本文档开发是否可以」的评审意见（非裁决）

**补齐 F1/F2 前不建议开工；补齐后可以。**

- 成立的部分：bootstrap 单点接线（D1-A）优于旧 app.py 挂点且受 `test_entrypoint_bootstrap` 守护；tier 内单路径（D2-A）消除 force-include 与双路径 fallback，上游接触面确为最小；事件契约五点核验与现基线 schema 逐一相容；C136 登记与回滚方案完整。
- 阻塞的部分：F1（R2 审批范围 vs 实际 diff 范围错位——签认前必须补 pyproject 条件行或写实 payload 适配承诺）；F2（任务 4 按现文本执行必在架构守卫处卡住）。
- 建议动作：① design R2 表补 `pyproject.toml` 条件行（预期 per-file-ignores/ty overrides，R13② 留痕）或明确"payload 内容适配替代豁免"；② 明确 selftest.py 适配策略与落点；③ 修 F3/F4/F5。三处均为文档修订，不动方案结论。
