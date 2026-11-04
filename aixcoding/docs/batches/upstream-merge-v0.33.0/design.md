# 设计：追上游 v0.33.0（upstream-merge-v0.33.0）

<!--
签认规则（R10）：责任人本人编辑本头部（状态→已确认、填日期，确认意见可选），AI 不得代填。
签认后修改正文需重新签认。校验：uv run python aixcoding/tools/check_docs.py
-->

- 责任人：周彬
- 状态：已确认
- 确认日期：2026-11-04
- 确认意见：无特别意见

## 方案概述

按 `standards/UPGRADE-RUNBOOK.md` 将 `main_aixcoding_cli` 从上游 0.32.2 升至 **v0.33.0**（8d5a3208）。

```
origin/main_iCode（镜像，05679bec，纯上游，已含 v0.33.0）
        │  ← 本次只取其中的 v0.33.0 tag（8d5a3208，!112）
        ▼
特性分支 chore/merge_upstream_v0.33.0（自 main_aixcoding_cli @2bde5abe 拉出）
        │  git merge 8d5a3208 → 解冲突（干跑预判 6 处）→ 三层核对 → 全量验证
        ▼
PR → main_aixcoding_cli（双 CI 绿后责任人合并）
```

与 runbook §0 标准动作的说明：

- 本次目标是 **release tag v0.33.0**（责任人 2026-11-04 认可），镜像头领先的 24 个未发版提交
  （`8d5a3208..05679bec`）留待下次升级。runbook §1 第 1 步允许 "release tag / main 头
  commit" 两种目标，无流程违反。
- v0.33.0 的 commit 对象已在 fork 对象库（经镜像带入，`cat-file -t` 验证），**无需网络 fetch**；
  本地无 `v0.33.0` tag ref，merge 直接用裸 hash `8d5a3208`（如需 tag 引用留痕，可选
  `git fetch https://github.com/openJiuwen-ai/iCode.git tag v0.33.0`，单 tag 拉取、非必须）。
- 镜像不动：本批次不触碰镜像分支（评审 2026-11-04 Minor 采纳——requirements 已定"不追
  镜像头、无待决项"，本地 `main_iCode` 停留 918cb599 与远端 05679bec 的状态差不影响本批次，
  避免引入与本批次无关的 git 操作）。R2/R3 门禁 base =
  `merge-base(origin/main_iCode@05679bec, HEAD)` = 8d5a3208，无需镜像前移即正确；镜像追平
  留待下次升级前按 runbook §1.2 统一处理。

### 干跑实测（merge-tree，2026-11-04，未动工作区）

378 个变更文件中冲突 **6 处**（v0.32.2 批次为 2 处），逐点预判：

| # | 冲突文件 | 形态（两侧改动） | 解法 |
|---|---|---|---|
| 1 | `locales/zh-Hans/LC_MESSAGES/chrys.po` | 上游 po 更新（1032 行，新增 key/msgstr）vs 我方 i18n 定制（借壳 key + msgstr） | 以上游 po 为底重放我方定制；重走 i18n 全管线 extract→update→translate→compile→**check**，并复跑一轮确认幂等（第二轮零 diff） |
| 2 | `src/chrys/app/tui/screens/main/config_actions.py` | 我方 catalog 定制 `read_only=True`（模型列表服务端管控）vs 上游 read_only 逻辑改动（45 行，含 25456c6e in-flight 只读） | 保留我方 `read_only=True`（语义覆盖：catalog 管控恒只读），合入上游周边改动；列入决策交付物对比分析 |
| 3 | `src/chrys/foundation/config/process_settings.py` | 两侧各加一字段：上游 `credential_command_ttl_seconds: int`（`{{!command}}` TTL）/ 我方 `model_catalog_base_url: str`（catalog-sync 在册） | 无语义冲突，两字段都保留（dataclass 字段区 + `from_settings` 各自插行，marker 保留） |
| 4 | `src/chrys/foundation/i18n/_catalogs/zh-Hans/LC_MESSAGES/chrys.mo` | 二进制编译产物（豁免表在册） | 不手工解，`i18n.py compile` 重新生成 |
| 5 | `tests/app/cli/test_hooks.py`（C100–C101 在册） | 同一行两侧改：上游 `Settings → Security →` 改 `Safety`（tab 改名）/ 我方 `icode hook validate` 改 `aixcoding-cli`（branding） | 融合：`Safety + aixcoding-cli` 各取一侧（两个 marker 均保留）；另 1 处仅上游改动，auto-merge |
| 6 | `tests/foundation/i18n/test_catalog_oracle.py`（C028/C043 在册） | 计数行：我方基线 2504（+5 借壳 key）vs 上游 oracle 基数扩容 | 合后重算：预估 **2628** = 上游 oracle 基数 **2623** + 我方 **5** 借壳 key（tui ×4 [C028] + catalog ×1 [C043]，两个 marker 均保留；以 merge 后实测为准。评审 2026-11-04 Major 1 修正：2739 系含复数条目/头部的原始 POT 口径，2739→2623、4→5） |

其余 372 文件 auto-merge 成功（含 pyproject.toml、AGENTS.md、locales pot、TUI screens 等）。
高发区与打包环境核查：

- `AGENTS.md`（C001）：上游改 6 行，auto-merge 通过——**auto-merge ≠ 语义正确**，结构层核对不可省；
- `logs/screen.py`（C007，F6 tab）：上游区间**零改动**，无忧；
- `scripts/fetch_rg.sh/.ps1`：零改动（vendored ripgrep 版本不变，无需重取）;
- `uv.lock`：仅 2 行变化（无依赖漂移）；`.github/workflows/ci.yml`：零变化（桥接文件无需跟改）；
- `.python-version`：8d5a3208 处仍为 3.14.8，与本地环境一致。

### 已预见的语义适配点（auto-merge 通过但需人工融合核对）

- `tests/app/tui/i18n/test_command_suggestions_i18n.py`（C024 `/man` 索引期望 + C086 品牌行，
  完整文本等值断言）：上游新增 `/btw`、`/usage` 两行期望，与我方 `/login`、`/logout` 及
  AIxCoding 品牌行并存——融合结果 = 上游新增行 + 我方登录命令 + 我方品牌（运行时 man 索引由
  命令注册表生成，断言须同时含两侧）。auto-merge 预计通过，人工核对融合正确性，不依赖
  全量测试失败后补救（评审 2026-11-04 Major 2 采纳）。

### 升级引入的新信任路径（已裁决：选 A）

> 评审 2026-11-04 Blocker 1。原假设"`{{!command}}` 不与 catalog 抢接缝"不成立，已删除。
> **责任人 2026-11-04 裁决：选 A**——运行环境为内网安全受控环境，可保证内部 catalog
> 服务端永远不下发 `{{!command}}`，且不考虑服务端被攻破等安全场景；残余风险（服务端
> 配置失误/被攻破时客户端无防线）已知悉并接受。选 B（`parse_catalog` 清洗 + 两入口
> 回归测试）作为弃用备选留痕：`src/chrys/service/aixcoding/catalog.py` 为 fork 自有文件，
> 未来如需收紧可随时启用，不触 R2、不需重新升级。

v0.33.0 新增 `{{!command}}` 凭据占位符机制：profile 的 `api_key` / `http_headers` /
`chat_options` 字段值中的 `{{!command}}` 由 `clients.py::_command_credential_auth` 交给
`CommandCredentialAuth`，经 `command_credentials.py` **本地执行命令**取 token（不经工具审批）。
与 catalog 定制组合后形成新链路：

```
catalog 服务端响应 → parse_catalog 的 items/裸列表入口（字段白名单含上述三个字段，值原样入 ModelProfile）
                  → 发请求时 {{!command}} 本地 shell 执行
```

`models` 服务端形态经 `translate_models_payload` 主动不下发 `api_key`，是缓解因素，但覆盖不了
`items` 与裸列表两入口（`catalog.py` `parse_catalog`）。本裁决（A）下该链路依赖服务端承诺
不触发。

**对定制功能的影响结论（2026-11-04 核实，零功能冲突）：**

- C021/C022（登录 token 注入）：上游对 `wire_client.py` 区间改动仅 observer 上报 +5/−1，
  `RequestHeaders.stamp` 登录 token 域零触碰；两机制分层——我方 stamp 在请求组装层
  （SDK 之上），上游 `CommandCredentialAuth` 在 HTTP 传输层且只改写"值含 handle 的头"，
  登录 token 头（真实 token 值）不会被碰；上游另防反向污染（请求头中长得像 handle 的
  服务端下发文本不执行、不回传）。内网运行态（无 handle）下 `_command_credential_auth`
  返回 `None`，新机制整体休眠，定制零感知；
- 淘汰检查：`{{!command}}` 是"本地命令取凭据"，非"登录会话凭据注入"，不构成 C021/C022
  的〔上游等价〕命中；
- MCP headers / ACP launch 的占位符扩展（91151234/ce3059d6）：无我方在册定制点，零交集。

**配套验证（升级中执行）**：C021 契约——登录 token 与静态/命令生成的 `token` 头并存时
优先级不变（登录态：stamp 先覆盖 `token` 头，handle 值不达 HTTP 层；未登录且本地手配
handle：命令 token 生效——与 C021 "login credential overrides any static token" 语义
自洽）。

## 备选方案对比

| 方案 | 优点 | 缺点 | 升级冲突面（R1） | 结论 |
|---|---|---|---|---|
| A. merge v0.33.0 tag（本设计） | 稳定 release；干跑实测冲突 6 处、解法明确 | 镜像头 24 提交不跟进（留下次） | 小（实测） | **选定**（责任人 2026-11-04 认可） |
| B. merge origin/main_iCode 头（05679bec） | 一次到位、与镜像完全同步 | 含 24 个未发版提交，改动面与风险更大、非稳定版 | 6 处之上再叠加 | 否决 |
| C. rebase 定制提交到 v0.33.0 | 历史线性 | 改写已发布历史 + 需 force push（纪律禁止） | — | 否决 |
| D. 重建法（0.28→0.31 旧路线） | 无冲突 | 冲突面实测可控，完全不需要；丢 PR 历史可追溯性 | — | 否决 |

## 上游改动清单（R2；升级批次特例）

**本批次我方对上游文件的新增改动 = 2 处测试文件适配**（merge 后全量验证暴露，按 R2 增补报批，
责任人 2026-11-04 批准；登记见「定制点登记预告」C133/C134）：

- `tests/service/tools/web/test_tools.py`：上游新增 keyless 测试硬编码 `X-Keenable-Title == "iCode"`，
  与我方品牌 `AIxCoding` 冲突——断言品牌适配 + marker（C133）；
- `tests/app/tui/screens/test_models_dialog_deletion.py`：上游新增"F4 打开对话框并删除模型"测试
  ×4 参数，与我方 catalog-sync 既定语义互斥（F4 入口禁用 + 只读隐藏删除按钮）——函数级
  skip + marker（C134），失效模式安全（上游追加用例会显式暴露而非静默吞没）。

其余口径不变：

- merge 引入的上游自身变化不算"我们的改动"——runbook §4 条款：R2/R3 以**新 merge-base**
  （8d5a3208）起算，diff 区间从新分叉点重新起算，只反映定制侧；
- 冲突解决 = 在册定制点适配（catalog-sync 两点 / branding hint 文案 / C028/C043 i18n 计数），
  无新定制点；
- `pyproject.toml` version 行：随 merge 自动 0.32.2→0.33.0（上游自身变更）——四段机制仍未启用
  （versioning.md 后置，v0.32.2 批次同一裁决延续），基线升级只随上游同步三段；
- 若后续适配过程中**再**发现需要新的上游文件改动：停下，按 R2 增补报批后再动（铁律）。

## 对上游隐式行为的假设

| 假设 | 我方依赖方式 | 失效表现与对策 |
|---|---|---|
| 5 个借壳 key（C028 tui ×4 + C043 catalog ×1）在 0.33.0 的 pot 抽取中仍全部生成 | oracle 计数 = 上游基数 2623 + 5 = 2628（预估，以实测为准） | oracle 红 → 重走 i18n 全管线（extract→update→compile→check）后复测；仍缺则查上游 msg 定义处变更 |
| 上游 0.33.0 未重构 tui 基建符号（`BaseDialog`/`DialogButtonRow`/`localized_binding`/`SlashCommandDef`/`AppHeader`/`MainScreen.on_mount` 等 anchor） | 定制 tui 挂点编译期依赖（本区间 TUI 文件改动量大于 v0.32.2 批次：app.py/commands.py/screen.py/settings 等） | ty check / 定制测试红 → 按 runbook §3 逐 anchor 核对后跟随适配（触及上游文件则回 R2 报批） |
| Security→Safety 改名不波及我方定制挂点的 tab 引用 | settings 域定制挂点 | 定制测试红 → 挂点引用跟随改名适配 |
| `chrys.kernel/__init__` 包级导出（`get_login_session` 等）签名未变 | auth 接线 lazy import | 定制 auth 测试红 → 同上 |
| （已裁决选 A——见「升级引入的新信任路径」小节）内部 catalog 服务端承诺永不下发 `{{!command}}`，内网受控环境不考虑被攻破场景 | `parse_catalog` 的 `items`/裸列表入口原样保留 `api_key`/`http_headers`/`chat_options` | 服务端违背承诺（配置失误/被攻破）→ 客户端执行下发命令（已接受的残余风险）；如需收紧启用弃用备选 B |

已验证非假设（不列入）：ci.yml 零变化、`.python-version` 3.14.8 一致、uv.lock 无依赖漂移、
fetch_rg 版本不变。

## 定制点登记预告

原稿"无新增定制点"——2026-11-04 增补：merge 后全量验证暴露两处上游新测试与我方定制的语义冲突，
适配产生 **2 个新 marker**（R2 已报批），按下表点级登记（当前 `--report` 最大 C132，顺延 C133/C134）。
冲突适配其余部分均落在在册定制点（catalog-sync 两点 / branding hint 文案 / C028/C043 计数行），
登记行数值适配属存量维护。

| id | 落点 | marker 锚 | 预期计数 | anchor | risk | removal_condition |
|---|---|---|---|---|---|---|
| C133 | tests/service/tools/web/test_tools.py | AIxCoding branding: keenable title assertion | 1 | keyless 搜索 `X-Keenable-Title` 头断言（branding.py APP_DISPLAY_NAME 消费方） | 低（测试断言品牌适配，同 C100-C101 先例） | 随 C050（品牌政策变更时逐点裁决） |
| C134 | tests/app/tui/screens/test_models_dialog_deletion.py | AIxCoding catalog: upstream deletes profiles in the local dialog | 1 | 函数级 `@pytest.mark.skip` + reason 前缀（catalog-sync F4 禁用与只读删除隐藏的上游语义冲突留痕） | 低（显式 skip；上游追加用例将显式暴露需复核） | 〔随组〕catalog 只读管控语义整体淘汰获裁时恢复上游测试（测试支撑） |

## 既有定制点存活核对矩阵（runbook §3 结构层展开）

> 输入 = 升级前 `gen_customizations.py --report` 时点快照（当前 **95 行登记、编号 C001–C132**；
> v0.32.2 批次的 "C001–C029" 口径已过期——评审 2026-11-04 Blocker 2 采纳）。核对按定制组
> **全覆盖**，逐组记录存活结论并入 verification.md；`--strict` 仅证明 marker 与登记对账，
> 不替代每个 anchor 的语义核对与 `removal_condition` 的 hit/not-hit 结论。

| 定制组（编号域，边界以 report 为准） | 核对内容 |
|---|---|
| 文档入口（C001 / C130 / C131，AGENTS.md） | 三段 marker 行文本不变、段完整（上游改 6 行，auto-merge 过≠语义对） |
| 日志面板（C007） | `logs/screen.py` MODULE_ORDER 行含 `"aixcoding"` 项（上游零改动，低风险） |
| 启动接线（C004） | `app.py` `main()` 起始 startup 接线行在（app.py auto-merge 过） |
| TUI 登录/徽标/命令（tui 批次域，C005–C029 等） | badge/dialog/commands/screen/app_header 各 marker 行在；anchor 符号存在且签名未变（Security→Safety 改名重点核对 settings 挂点）；`/man` 断言融合核对（C024，见语义适配点） |
| LLM 认证（C021/C022） | `wire_client.py` token 头注入两行在（auto-merge 过）；登录 token 与 `{{!command}}` token 并存时优先级契约（见新信任路径小节） |
| catalog-sync（C030–C049） | 冲突点 #2 `config_actions.py` `read_only=True`、#3 `process_settings.py` `model_catalog_base_url` 之外，registry.py 整替线程（C030/C031）等全组 marker 逐一对照 report 核对；本区间上游动过 profiles/settings 域文件 |
| branding-and-command（C050–C131：branding.py / prompt / 工具描述 / serve banner / 命令与制品 C103 / wheel / installer C115–C116 / uninstaller C117 / approval C118 / 各测试契约） | 命令名 `aixcoding-cli`、wheel 入口、installer/uninstaller 旧别名迁移与删除安全、approval fallback 文案、OSC/hooks/usage 类测试断言全组 marker 在；重点核对本区间上游 auto-merge 过的 `filesystem.py`（C056 域）、`test_hooks.py`、`test_install_scripts.py`、`test_uninstaller.py` 等 |
| i18n（C002 / C027 / C028 / C043 / C132 + 豁免表 4 项） | oracle ids / 计数 2628 实测 / pot/po/mo 重生成后 marker 与条目齐全 / pyproject 中文常量豁免行与标点豁免行在 |
| ruff 豁免（C003，ruff-test-exemptions） | pyproject.toml per-file-ignores 的 aixcoding/tests 豁免行在（上游区间 pyproject 仅 version 行变化，低风险；评审 2026-11-04 复审 Major 补入） |
| 机检兜底 | `pytest aixcoding/tests/diagnostics/test_panel_anchor.py -n0 -q` 绿 + merge 后 `gen_customizations.py --strict` 0 error / 0 warning |

## 升级决策交付物（runbook §5）

以下清单随升级产出，**最终决策全部交责任人裁决**，记录于本目录 `verification.md` 与 PR 描述：

1. **冲突点全清单 + 逐点处理意见**（含 auto-merge 通过但语义核对有疑义的点）——干跑已预判
   6 处（上表），merge 后按实际补全；
2. **定制化改变清单**：需新增 / 建议删除（淘汰候选，附对比分析）/ 需适配——重点预判两项
   加 merge 后实证增补两项：
   - `config_actions.py` read_only：上游 in-flight 只读（发送中防并发，临时）vs 我方 catalog
     恒只读（服务端管控模型列表，永久）——我方语义覆盖上游，解法 = 保留我方 + 吸收上游周边；
   - 上游 `{{!command}}` 凭据占位符与 catalog 定制的组合（见「升级引入的新信任路径」小节）：
     已裁决选 A（2026-11-04，内网受控 + 服务端承诺不下发 + 接受残余风险），零功能冲突
     结论与 C021 契约验证已落盘；
   - （增补）`test_tools.py` keyless 标题断言：上游硬编码 `iCode` vs 我方品牌——适配为
     `AIxCoding` + marker（C133），已验证 1 passed；
   - （增补）`test_models_dialog_deletion.py` ×4：上游本地删除模型场景 vs 我方 catalog 只读
     管控（F4 禁用 + 删除隐藏）——语义互斥不可适配，函数级 skip + marker（C134），
     已验证 4 skipped；只读行为正向覆盖（原讨论选项 D）留独立批次，待办登记于
     `fork\temp\aixcoding-todos\`；
3. **淘汰检查逐组结论**：以 `--report` 95 行登记全景为输入，逐组（tui / catalog-sync /
   branding-and-command / auth-logging / diagnostics / i18n 等来源批次）记录每点
   `removal_condition` 的**命中/未命中及依据**；命中即出"上游实现 vs 我方定制需求"对比分析
   ——**不自行删除任何定制**。

## 风险与最坏情况

- **最大风险：auto-merge 语义静默错**（本区间 TUI 文件改动量大于上次，如 MODULE_ORDER 同类
  合并丢项、接线行被上游重构吞并）——对策 = 核对矩阵逐项 + 行为锚定测试 + 全量验证三层兜底；
- po 冲突重放丢借壳 key / msgstr → oracle 红，重走 i18n 管线；仍红则升级暂停呈报；
- 回滚：merge 在特性分支上进行，任何时候可丢弃分支从 `main_aixcoding_cli` 重来；
  PR 合并后发现问题的回滚 = revert 该 merge PR（merge commit 单点可逆）。

## ADR 判定

**一、merge 方案本身**：按 §6 checklist 逐项——① 被认真考虑但否决的备选：B/C/D 均为
runbook/纪律既定路径的常规排除，非本批次新决策；② 撤销代价高：否（revert merge 单点可逆）；
③ 约束未来工作模式：否；④ 依赖上游非公开接口：无新增（anchor 均已在册）。

**二、`{{!command}}` 信任边界选 A（评审 2026-11-04 复审 Blocker 提出，责任人同日终裁：
不出 ADR，没有必要）**。补足理由：

- 该决策沿用 **ADR-0018**（内网部署明文 HTTP 传输，已确认 2026-11-03）已确立的威胁模型
  口径——"受控内网人工分发，无中间人威胁模型诉求，适用于所有服务器地址"。选 A（服务端
  承诺不下发 + 不考虑被攻破）与 ADR-0018 同一信任基线，属既有决策对 catalog 响应内容的
  **适用**，不是新的全局决策；"为什么敢信任内网信道"已由 ADR-0018 回答；
- 否决备选 B 已完整留痕于「升级引入的新信任路径」小节（含启用路径：fork 自有文件、不触
  R2、不需重新升级），未来如需收紧按留痕启用，无需 ADR 承载；
- 决策记录三处落盘可追溯（本 design 小节 / requirements 开放问题 / 升级后 verification.md
  裁决记录），git 历史可查。

**结论：两项均不出 ADR。**

（"目标取 release tag 还是镜像头"是 runbook §1.1 已枚举的操作参数选择，由责任人逐次裁决即可，
不构成需留档的架构决策；本节连同上方备选对比表即留痕。）
