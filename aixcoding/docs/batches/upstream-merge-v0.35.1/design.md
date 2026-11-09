# 设计：追上游 v0.35.1（upstream-merge-v0.35.1）

<!--
签认规则（R10）：责任人本人编辑本头部（状态→已确认、填日期，确认意见可选），AI 不得代填。
签认后修改正文需重新签认。校验：uv run python aixcoding/tools/check_docs.py
-->

- 责任人：周彬
- 状态：已确认
- 确认日期：2026-11-09
- 确认意见：无特别意见

## 方案概述

按 `standards/UPGRADE-RUNBOOK.md` 将 `main_aixcoding_cli` 从上游 0.34.0 升至 **v0.35.1**
（7c423b06；merge 目标 ref = release tag，责任人 2026-11-09 裁决）。

```
上游 v0.35.1 = 7c423b06（本地 tag 已 fetch，2026-11-09）
        │
        ├─ ff 同步镜像：main_iCode 05679bec → 7c423b06 → push origin/main_iCode
        ▼
特性分支 chore/merge_upstream_v0.35.1（自 main_aixcoding_cli @a7efff2e 拉出）
        │  git merge v0.35.1 → 解冲突（干跑预判 4 处）→ 三层核对 → 全量验证
        ▼
PR → main_aixcoding_cli（双 CI 绿后责任人合并）
```

与 runbook §0 标准动作的说明：

- 镜像需前移一步：本地/远端 `main_iCode` 现均在 05679bec（v0.34.0），本次先 ff 至
  7c423b06 再 push（git 写操作逐条授权）；R2/R3 门禁 base 合并后 = 新 merge-base
  （预期 7c423b06，以实测为准）；
- 升级前基线检查已做（2026-11-09）：工作树干净、check_docs OK、定制测试 562 passed /
  2 failed（guard symlink ×2，本地 Windows 环境性在案，详见 requirements 末节——
  merge 后同口径复跑对比）；
- **--strict 草案窗口期说明（评审 F1）**：本 design 草案含退役登记表，草案期
  `gen_customizations.py --strict` 报 1 error（registration table present but sign-off
  is '草案' — ignored）属工具 fail-closed 既定机制、**预期信号非回归**，签认后自动收敛；
  本批次文档进入任何 commit 的时点必在签认后（CI 对草案带表必红）；
- 定制点全景已取（100 登记行 / 142 编号含退役 5），时点快照将附 verification.md。

### 干跑实测（merge-tree --write-tree，2026-11-09，未动工作区）

409 个变更文件中冲突 **4 处**（v0.34.0 批次为 5 处），逐点预判：

| # | 冲突文件 | 形态（两侧改动） | 解法 |
|---|---|---|---|
| 1 | `AGENTS.md`（C001/C130/C131 在册） | 上游 trim 30% + i18n 新机制段落（区间 112+/125−）vs 我方三段 marker 入口段 | 保留我方三段 marker，合入上游正文改写，逐段比对（高发区既定解法） |
| 2 | `locales/zh-Hans/LC_MESSAGES/chrys.po`（豁免表在册） | 上游区间新增/改写条目 vs 我方 4 借壳 key 中文条目 | 借壳条目保留 + 上游条目融合，merge 后重走 i18n 全管线（extract→update→compile→check）实测收敛 |
| 3 | `src/chrys/foundation/i18n/_catalogs/zh-Hans/LC_MESSAGES/chrys.mo` | **modify/delete**：上游 b787c974 删除跟踪（改构建时编译）vs 我方随 po 重编的编译产物 | **接受上游删除**（跟随上游新机制，mo 此后为 gitignored 构建产物，由 hatch hook / `i18n.py compile` 生成）；豁免表 mo 项随机制失效适配（见下"i18n 机制适配"） |
| 4 | `tests/foundation/i18n/test_catalog_oracle.py`（C028 在册） | 上游去计数化删 `len(EXPECTED_MESSAGE_IDS) == N` 行 vs 我方计数行 2704 + marker（`+4 borrow-shell keys`） | 接受上游新机制（计数行删除）；**C028 载体消失，处置提请裁决**（建议：登记行移入退役记录，见"i18n 机制适配"） |

其余 405 文件 auto-merge 成功（含 pyproject.toml、main/screen.py、commands.py、
config_actions.py、chrys.tcss、diff/screen.py、startup.py、_catalog_oracle_ids.py、
test_command_suggestions_i18n.py、trajectory_wait_manifest.json 等）。

高发区与打包环境核查：

- `logs/screen.py`（C007，F6 tab）：上游区间**零改动**，无忧；
- `registry.py`（C030/C031，上批次融合点）：上游区间**零改动**——v0.34.0 融合成果
  本次无直接暴露；
- `wire_client.py`（C021/C022）、`app/cli/app.py`（C004 接线行）：上游区间**零改动**；
- `scripts/fetch_rg.sh/.ps1`：零改动（vendored ripgrep 版本不变，无需重取）；
- `pyproject.toml`：version 行 + `[tool.uv].cache-keys` 新增 + hatch hook +
  build-system 增 `babel==2.18.0`（均上游自身变更）；合并后 `uv sync --frozen` 对齐，
  editable install 将因 cache-keys 重建（预期行为）；
- `.python-version`：3.14.8→**3.14.10**——本地 Python 需升级（uv python install /
  官方安装器）；`scripts/build.ps1` 区间 2 行变化已坐实 =
  `PYTHON_BUILD_STANDALONE_RELEASE` 20261003→**20261026**（配套 3.14.10，
  `.github/workflows/cd.yml` 同步 bump 为上游自带）——离线打包需更新
  `dist\downloads\` 缓存的 python-build-standalone 制品（环境适配项，不阻塞 merge）。

### i18n 机制适配（本批次核心机制变化，b787c974）

上游将 mo 编译与 oracle 校验机制重构，我方适配口径：

1. **mo 生命周期**：接受上游"构建时编译 + gitignore"机制。merge 后 mo 不再入 git；
   editable 构建（uv sync）自动生成 src/ 下副本，wheel 构建由 hatch hook 生成；
   `scripts/i18n.py compile` 语义变为 check + 同编译器。我方义务从"po 变更后重编 mo 并
   提交"变为"po 变更后跑 i18n 管线，mo 由构建自动生成"——维护成本下降。
2. **豁免表适配**（`aixcoding/tools/_common.py::MARKER_EXEMPTIONS`，我方工具文件，
   非上游 R2 项）：mo 行（"编译产物：随 po 重编"）随机制失效**移除**；pot/po 两项保留
   （仍是生成/登记产物）。gen_customizations --strict 复跑收敛。
3. **oracle 去计数化**：上游删计数行、保留 id 集合并拆分区域文件（agent/model/MCP/ACP
   编辑器与 trajectory 的 id 移入独立区域文件）。我方 4 借壳 key 的 id 集合登记
   （C027，`_catalog_oracle_ids.py` 行尾 marker ×1——login/logout 两组 4 key 共 1 个
   行尾 marker）auto-merge 通过，merge 后核对
   4 key 仍在 id 集合且 oracle 实测绿（新机制下 id set 一致即固定 size，无需计数适配）。
4. **C028 处置（提请裁决）**：C028 = 计数行 marker，其 removal_condition 为
   "〔机制退役〕随 C027 一并裁决（oracle 计数）"。本次是**上游侧删除计数机制**、
   载体消失，非我方借壳机制退役（C027 及 4 借壳 key 存续）。
   **建议：C028 登记行移入本批次 design「退役记录」小节（id 永不复用），理由注记
   "上游 v0.35.1 去计数化，计数行载体消失；借壳机制本身存续（C027）"**；
   tui 批次 design 的 R2 表 9 号（mo 重编）/11 号（size 断言同步）口径已过时，
   在 verification.md 留痕说明，tui design 正文修订建议随本批次一并呈签（R10）。
5. **新增机检红利**：上游新增 `test_catalog_artifacts.py`（各 OS 校验安装态 mo 与新编译
   逐字节一致）与"mo 不得重新入 git"守卫——merge 后若误 `git add` mo 会被上游测试拦下，
   冲突 #3 解法有上游机检兜底。

### 已预见的语义适配点（auto-merge 通过但需人工融合核对）

- `main/screen.py`（+101 行，turn 转向/discard/变更预览）：C013–C017（静默登录校验
  `on_mount` 接线 / teardown 取消登记）——核对 marker 行在 + 登录检查未被新交互重构
  绕过；`test_panel_anchor.py` + auth 定制测试双重机检；
- `diff/screen.py`（区间 244 行变化，会话变更预览主战场）：C006（diff 屏无徽标特例）
  marker 行核对 + tui 定制测试；
- `commands.py`（17 行）：C008–C012 `/login` `/logout` 注册表孪生组 marker 逐行核对；
  `test_command_suggestions_i18n.py`（C024/C086）auto-merge 过——上游若新增命令索引行，
  `/man` 期望断言融合 = 上游新增行 + 我方 `/login` `/logout` + AIxCoding 品牌行，
  本地实跑（该测试本地不 skip）；
- `orchestration/startup.py`（+57 行）：C140 guard hooks 安装接线行核对 + guard 定制测试；
- `config_actions.py`（6 行）：C035 恒只读未被上游改动绕过；
- `chrys.tcss`（2 行）：C005 登录徽标样式行核对；
- `skills/provider.py`、`approval.py`：branding marker 行核对；
- **上批次遗留固定关注文件（v0.34.0 裁决留痕，本次区间全部 auto-merge 过）**：
  `test_manual_reload.py` / `test_reload_profiles.py`（C136–C138 skip 点）与
  `test_models_dialog_deletion.py`（C134）/ `test_prompt_shortcut_send.py`（C135）——
  逐点核对 skip marker 与 reason 前缀在、上游追加用例是否显式暴露需复核；
  **排障按单文件单跑口径**（v0.34.0 PR #99 评审实证：两文件合跑存在跨文件状态污染，
  上游固有，合跑失败≠本批次回归）；
- `trajectory_wait_manifest.json`（豁免产物）：上游自身刷新，随 fork-full 验证；
- `pyproject.toml`（C002/C003/C141）：per-file-ignores 豁免行与 guard lint profile 行
  在上游新增 cache-keys/hook 段落旁保留核对；
- AGENTS.md：见冲突 #1，auto-merge 过≠语义对，逐段比对。

## 备选方案对比

| 方案 | 优点 | 缺点 | 升级冲突面（R1） | 结论 |
|---|---|---|---|---|
| A. merge v0.35.1（release tag，本设计） | 稳定 release；干跑实测冲突 4 处、解法明确；i18n 新机制消除历史高频冲突源 | 需本地 Python 升级 3.14.10 | 小（实测） | **选定**（责任人已裁决目标 ref） |
| B. 等待后续版本再升级 | 可能少一次升级成本 | 违反 §8 节奏条款（需显式决策）；债务持续累积 | — | 否决 |
| C. rebase 定制提交到 v0.35.1 | 历史线性 | 改写已发布历史 + 需 force push（纪律禁止） | — | 否决 |
| D. 重建法（0.28→0.31 旧路线） | 无冲突 | 冲突面实测可控，完全不需要；丢 PR 历史可追溯性 | — | 否决 |

## 上游改动清单（R2）

**本批次我方对上游文件的新增改动 = merge 后实证增补 2 处测试品牌断言适配**（C143/C144，
责任人 2026-11-09 批准；登记见「定制点登记预告」）：

| 文件 | 改什么 | 标记注释位置 |
|---|---|---|
| tests/service/agent_middleware/test_extension_tool_hooks.py | 上游 v0.35.1 新扩展机制失败提示断言硬编码 `so iCode withheld this result`，我方运行时输出 AIxCoding——断言品牌适配（C143，同 C139 先例） | `AIxCoding branding: extension after_tool failure note assertion` |
| tests/orchestration/test_startup.py | 上游新代理警告文案断言硬编码 `iCode can use`，我方 AIxCoding——断言品牌适配（C144，同 C133 先例） | `AIxCoding branding: invalid proxy warning assertion` |

merge 文本冲突解决部分（4 处）全部落在在册定制点
（C001/C130/C131 AGENTS.md、C027/C028 oracle、po/pot 豁免产物）与跟随上游机制删除
（mo、oracle 计数行）：

| 文件 | 改什么 | 标记注释位置 |
|---|---|---|
| AGENTS.md | 冲突解：保留 C001/C130/C131 三段 marker + 合入上游正文 | 既有三段 marker 原样保留 |
| tests/foundation/i18n/test_catalog_oracle.py | 冲突解：接受上游去计数化（删计数行）；C028 marker 随载体消失退役（提请裁决） | —（退役方向，非新增） |
| locales/zh-Hans/LC_MESSAGES/chrys.po | 冲突解：4 借壳条目保留 + 上游条目融合（豁免产物，管线重生成） | 豁免表在册，无行内 marker |

配套适配（非上游文件，不占 R2）：

- `aixcoding/tools/_common.py`：MARKER_EXEMPTIONS 移除 mo 行（机制失效适配）；
- 本批次 design「退役记录」小节：C028 登记行迁入（若裁决通过）。

其余口径不变：

- `pyproject.toml` version 行随 merge 自动 0.34.0→0.35.1（上游自身变更）——四段机制
  仍未启用（versioning.md 2026-11-09 口径：暂保持上游三段），基线升级只随上游同步三段；
- R11 消歧：build-system 新增 `babel==2.18.0` 为**上游自身**构建依赖变更（merge 带入），
  非我方添加第三方依赖，不触发 R11 报批；
- 若后续验证发现需要新的上游文件改动：停下，按 R2 增补报批后再动（铁律）；
- merge 后实证若需增补测试适配（v0.34.0 批次 C136–C139 先例），新 marker 编号自
  **C143** 起顺延，随 design 重签呈报。

## 对上游隐式行为的假设

| 假设 | 我方依赖方式 | 失效表现与对策 |
|---|---|---|
| 上游 0.35.1 未重构 tui 基建符号（`BaseDialog`/`localized_binding`/`AppHeader`/`MainScreen.on_mount` 等 anchor） | 定制 tui 挂点编译期依赖（本区间 main/screen.py +101 行、diff/screen.py 244 行为最大暴露面） | ty check / 定制测试红 → 按 runbook §3 逐 anchor 核对后跟随适配（触及上游文件则回 R2 报批） |
| 4 个借壳 key（C027）在 0.35.1 的 pot 抽取中仍全部生成，且新 oracle（id set 机制）接受其登记 | `_catalog_oracle_ids.py` auto-merge 保留我方 4 key 行 | oracle / i18n check 红 → 重走 i18n 全管线后复测；仍缺则查上游 msg 定义处变更 |
| mo 构建时编译机制在我方 uv 环境正常工作（cache-keys 触发 editable 重建） | `uv sync --frozen` + `uv run` 全链路 | 启动报缺 mo / `test_catalog_artifacts.py` 红 → 按上游提示跑 `scripts/i18n.py compile` 修复 |
| 上游扩展 per-turn/tool hooks（ff22b004）不改变我方 startup 接线与 guard 安装时机的既有行为 | C004/C140 接线行所在文件区间零改动（app.py）/ 小改（startup.py +57） | 定制测试红 → 核对上游 hook 注册链与我方接线交互；淘汰对比分析另行提请裁决 |
| `CHRYS_PET_MODEL` 停读不影响我方运行与测试 | environment-config 批次模型目录（service/aixcoding/models/catalog）不消费该变量；实际改动在 `settings.py`（10+/10−：`buddy_model`→`buddy_model_profile`、env 改名、迁移函数），与我方定制落点（C034 = app/tui/app.py、模型目录）无交叠（2026-11-09 评审复核） | Buddy 相关上游测试红 → 查我方模型 profile 定制交互 |
| `chrys.kernel/__init__` 包级导出（`get_login_session` 等）签名未变 | auth 接线 lazy import | 定制 auth 测试红 → anchor 核对后跟随适配 |

已验证非假设（不列入）：registry.py / logs/screen.py / wire_client.py / app.py /
fetch_rg 区间零改动；pyproject、po、_catalog_oracle_ids.py 等 auto-merge 通过
（语义核对列入核对矩阵）。**v0.33.0 裁决的 `{{!command}}` 信任链路（选 A）本区间
未被触碰**：`command_credentials.py` / 我方 `catalog.py` 零改动，`clients.py`
20 行改动 = `_build_client_stack` async 化 + charset 校验移 worker 线程 + observer
连接事实上报，与凭据占位符机制无交互——选 A 裁决继续有效，本区间无新增信任路径。

## 定制点登记预告

原稿"预判无新增定制点"——merge 后 fork-full 实证暴露 2 处上游新测试品牌断言与我方
运行时品牌冲突，适配产生 **2 个新 marker**，编号顺延 **C143/C144**（责任人 2026-11-09
批准，随本 design 重签生效；v0.34.0 批次 C136–C139 同型先例）。冲突适配其余部分均落在
在册定制点。

| id | 落点 | marker 锚 | 预期计数 | anchor | risk | removal_condition |
|---|---|---|---|---|---|---|
| C143 | tests/service/agent_middleware/test_extension_tool_hooks.py | AIxCoding branding: extension after_tool failure note assertion | 1 | 扩展 after_tool 失败提示 note 断言（`so AIxCoding withheld this result`，branding.py APP_DISPLAY_NAME 消费方） | 低（测试断言品牌适配，同 C139 先例） | 随 C050（品牌政策变更时逐点裁决） |
| C144 | tests/orchestration/test_startup.py | AIxCoding branding: invalid proxy warning assertion | 1 | 代理警告 message 断言（`AIxCoding can use`，branding.py APP_DISPLAY_NAME 消费方） | 低（同 C143） | 随 C050（品牌政策变更时逐点裁决） |

### 退役记录（本批次提请裁决）

| id | 原落点 | 退役日期 | 依据 |
|---|---|---|---|
| C028 | tests/foundation/i18n/test_catalog_oracle.py | 2026-11-09 | 上游 v0.35.1 去计数化（b787c974），计数行载体消失——〔机制退役〕为上游侧计数机制删除，非我方借壳退役；借壳机制存续（C027 及 4 key 保留在 id 集合） |

## 既有定制点存活核对矩阵（runbook §3 结构层展开）

> 输入 = 升级前 `gen_customizations.py --report` 时点快照（100 登记行 / 142 编号含退役 5，
> 借壳 key 4 个）。核对按定制组全覆盖，逐组记录存活结论并入 verification.md。

| 定制组（编号域，边界以 report 为准） | 核对内容 |
|---|---|
| 文档入口（C001 / C130 / C131，AGENTS.md） | 三段 marker 行文本不变、段完整（上游 112+/125− 大改，冲突 #1 逐段比对） |
| 日志面板（C007） | `logs/screen.py` MODULE_ORDER 行含 `"aixcoding"`（上游零改动，低风险） |
| 启动接线（C004） | `app.py` startup 接线行在（上游零改动，低风险）；淘汰对比分析另列 |
| TUI 登录/徽标/命令（C005–C029） | badge/dialog/commands/screen/app_header marker 行在；**重点 C013–C017 与 turn 转向/discard 重构同文件**（on_mount anchor + 登录校验接线）、C006 与 diff 屏 244 行重构同文件；`/man` 断言融合（C024/C086，本地实跑） |
| LLM 认证（C021/C022） | `wire_client.py` token 头注入（上游零改动，低风险） |
| catalog-sync（C030–C049） | registry.py 上游零改动（上批次融合成果无暴露）；config_actions.py read_only（C035，6 行改动核对）；process_settings.py 字段核对；全组 marker 逐一对照 report |
| branding-and-command（C050–C131） | 全组 marker 在；重点核对本区间 auto-merge 过的 `skills/provider.py`、`approval.py`、`test_agent_builder_rollback.py`（C102）、`chrys.tcss`、`models/screen.py` 等 |
| guard（C140/C141/C142） | startup.py +57 行中 C140 安装接线行核对；pyproject lint profile（C141）与 .gitattributes（C142）行在；guard 定制测试复跑（升级前 2 个 symlink 本地环境性失败同口径对比） |
| i18n（C002 / C027 / C028 / C132 + 豁免表 4 项） | **机制适配主战场**：id 集合含 4 借壳 key（C027 存活）；C028 退役处置裁决；mo 删除后 git 状态干净 + build hook 生成 + `test_catalog_artifacts.py` 绿；豁免表 mo 项移除后 --strict 收敛 |
| ruff 豁免（C003） | pyproject.toml per-file-ignores 豁免行在（上游改 version/cache-keys/hook，低风险） |
| 机检兜底 | `pytest aixcoding/tests/diagnostics/test_panel_anchor.py -n0 -q` 绿 + merge 后 `gen_customizations.py --strict` 0 error / 0 warning |

## 升级决策交付物（runbook §5）

以下清单随升级产出，**最终决策全部交责任人裁决**，记录于本目录 `verification.md` 与 PR 描述：

1. **冲突点全清单 + 逐点处理意见**（含 auto-merge 通过但语义核对有疑义的点）——干跑已预判
   4 处（上表），merge 后按实际补全；
2. **定制化改变清单**：需新增 / 建议删除（淘汰候选，附对比分析）/ 需适配——预判重点：
   - **C028 退役**（载体消失，建议已列「退役记录」，提请裁决）；
   - **C004 淘汰候选对比分析**（〔消费者退役〕判据："上游提供官方插件/hook 机制可承载"）：
     上游 v0.35.1 扩展机制 = per-turn context + tool hooks（turn 级/工具调用级）vs 我方
     C004 = 每次启动 startup hooks（进程生命周期级，承载 auth 静默登录检查、guard 安装等
     启动期初始化）——时机维度不同（startup ≠ per-turn），须出对比分析提请裁决，
     不自行处置；
   - **C140 淘汰候选对比分析**（〔上游机制替代〕判据）：上游 tool hooks 可检查/拒绝工具
     调用（单点规则逻辑可表达）vs 我方 guard = 规则包**分发/安装机制** + 运行时拦截——
     上游是否自带规则包分发安装通道需查证，名似≠等价，对比分析后提请裁决；
   - i18n 机制适配（mo/豁免表/oracle）属"需适配"项，方案已列 design；
   - merge 后实证增补（若有，C143 起）；
3. **淘汰检查逐组结论**：以 `--report` 全景登记为输入，逐组记录每点 `removal_condition`
   的**命中/未命中及依据**；命中即出对比分析——**不自行删除任何定制**（C028 为上游侧
   载体消失的退役呈报，同样需裁决后执行）。

## 风险与最坏情况

- **最大风险：i18n 新机制适配不全**（借壳 key 在新 oracle 下缺失 / mo 生成链路在 uv
  环境失效 / 豁免表适配遗漏致 --strict 红）——对策 = i18n 全管线实跑 + 上游新增
  `test_catalog_artifacts.py` / build hook 测试 / mo 禁跟踪守卫多重机检；仍红则暂停呈报；
- **次风险：`main/screen.py` / `diff/screen.py` 交互重构吞并登录接线或徽标特例**
  （auto-merge 静默错）——对策 = marker 逐行核对 + auth/tui 定制测试 +
  `test_panel_anchor.py` 机检；
- po 冲突融合后借壳条目丢失 → oracle/i18n check 红，重走管线；仍红则升级暂停呈报；
- 本地 Python 3.14.10 升级影响其他批次开发环境——对策 = uv 管理解释器、
  离线打包缓存版本核对单独立项记录，不阻塞 merge；
- **uv cache-keys 分支切换坑（上游 pyproject 注释在案）**：从本分支切回 mo 仍被跟踪的
  旧分支再切回时，mo 目录可能消失而 uv 未察觉（keys 在构建前记录）—— editable
  安装缺 mo 时按上游提示跑 `scripts/i18n.py compile` 恢复；评审用临时 worktree /
  多分支切换场景同样适用，`test_catalog_artifacts.py` 会显式暴露（fail with hint）；
- 回滚：merge 在特性分支上进行，任何时候可丢弃分支从 `main_aixcoding_cli` 重来；
  PR 合并后发现问题的回滚 = revert 该 merge PR（merge commit 单点可逆）。

## ADR 判定

按 §6 checklist 逐项——① 被认真考虑但否决的备选：B/C/D 均为 runbook/纪律既定路径的
常规排除，非本批次新决策；② 撤销代价高：否（revert merge 单点可逆）；③ 约束未来工作
模式：否（i18n 适配为跟随上游机制，非我方新立约束）；④ 依赖上游非公开接口：无新增
（anchor 均已在册）。C004/C140 淘汰候选若裁决"迁移/退役"，届时由独立批次设计承载
（本 design 仅定对比分析与裁决流程）。**结论：不出 ADR。**

（"目标取 release tag 还是镜像头"已经责任人 2026-11-09 开工裁决 = release tag
v0.35.1，本节连同上方备选对比表即留痕。）
