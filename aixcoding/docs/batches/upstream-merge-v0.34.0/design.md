# 设计：追上游 v0.34.0（upstream-merge-v0.34.0）

<!--
签认规则（R10）：责任人本人编辑本头部（状态→已确认、填日期，确认意见可选），AI 不得代填。
签认后修改正文需重新签认。校验：uv run python aixcoding/tools/check_docs.py
-->

- 责任人：周彬
- 状态：已确认
- 确认日期：2026-11-06
- 确认意见：无特别意见

## 方案概述

按 `standards/UPGRADE-RUNBOOK.md` 将 `main_aixcoding_cli` 从上游 0.33.0 升至 **v0.34.0**（05679bec）。

```
origin/main_iCode（镜像，05679bec = v0.34.0 = 本次目标，纯上游）
        │  ← 本次目标与镜像头同一对象（tag/镜像头/release commit 三合一）
        ▼
特性分支 chore/merge_upstream_v0.34.0（自 main_aixcoding_cli @28b7d1c7 拉出）
        │  git merge 05679bec → 解冲突（干跑预判 5 处）→ 三层核对 → 全量验证
        ▼
PR → main_aixcoding_cli（双 CI 绿后责任人合并）
```

与 runbook §0 标准动作的说明：

- 本次目标 **05679bec** 既是 release tag v0.34.0 也是镜像头（上游发版即打 tag 于 main 头），
  runbook §1 第 1 步 "release tag / main 头" 两种取法本次同一对象，无分歧；
- 本地无 `v0.34.0` tag ref（https 对 github.com 网络未通，单 tag 拉取失败）——与 v0.33.0
  批次同法：merge 直接用裸 hash `05679bec`（对象已随镜像在 fork 对象库，`cat-file -t` 可验；
  如需 tag 引用留痕，网络恢复后可选补拉）；
- 镜像不动：本地 `main_iCode` 已与 `origin/main_iCode` 同在 05679bec（2026-11-06 核实），
  本批次无需镜像前移动作；R2/R3 门禁 base = `merge-base(origin/main_iCode, HEAD)` =
  05679bec，合并后即新 merge-base。

### 干跑实测（merge-tree，2026-11-06，未动工作区）

384 个变更文件中冲突 **5 处**（v0.33.0 批次为 6 处），逐点预判：

| # | 冲突文件 | 形态（两侧改动） | 解法 |
|---|---|---|---|
| 1 | `src/chrys/app/tui/screens/main/screen.py` | 仅 1 处：TYPE_CHECKING import 区两侧各加一行——我方 `AccountInfo`（auth 定制）/ 上游 `DraftReference`（引用标签特性） | 两行都保留（排序插入）；文件内 +288 行 reload 重构全部 auto-merge，**语义核对不可省**（见下"已预见的语义适配点"） |
| 2 | `src/chrys/foundation/i18n/_catalogs/zh-Hans/LC_MESSAGES/chrys.mo` | 二进制编译产物（豁免表在册） | 不手工解，`i18n.py compile` 重新生成 |
| 3 | `src/chrys/service/profiles/models/registry.py`（C030/C031 在册） | **实质语义冲突**：我方 RLock + `install_snapshot` 整替（catalog 后台线程）vs 上游 live-reload 新协议 `sources` 映射 + `revision` 计数 + `reload_base`/`install_reload` | 融合设计（见下"registry 融合方案"）；列入决策交付物重点 |
| 4 | `tests/foundation/i18n/test_catalog_oracle.py`（C028/C043 在册） | 计数行：我方 2628（上游 2623+5 借壳 key）vs 上游 2700 | 合后重算：预估 **2705** = 上游 2700 + 我方 5 借壳 key（marker 两行保留；以 merge 后实测为准） |
| 5 | `tests/orchestration/engine/build/test_agent_builder_rollback.py`（C102 在册） | 上游重构断言方式（`registry.load_builtins.call_args` → `[result.web.hosted for result in captured]`）且丢 `app` 绑定 / 我方 C102 `app: "AIxCoding"` 品牌绑定 | 融合：**上游新断言方式 + 我方 app 绑定**（`{"tools": "web_search", "app": "AIxCoding"}`）；C102 marker 保留 |

其余 379 文件 auto-merge 成功（含 AGENTS.md、pyproject.toml、po、config_actions.py、
process_settings.py、settings.py、TUI screens 等）。
高发区与打包环境核查：

- `AGENTS.md`（C001）：上游改 10 行，auto-merge 通过——**auto-merge ≠ 语义正确**，结构层核对不可省；
- `logs/screen.py`（C007，F6 tab）：上游区间**零改动**，无忧；
- `scripts/fetch_rg.sh/.ps1`：零改动（vendored ripgrep 版本不变，无需重取）；
- `pyproject.toml`：version 行 + 移除 `python-dotenv` + `pypdf` 升级（上游自身变更）；
  合并后 `uv sync --frozen` 对齐依赖（uv.lock 随 merge 带入）；
- `.python-version`：上游区间零改动（3.14.8 与本地一致）。

### registry 融合方案（冲突 #3 展开）

上游 live-reload 为 `ModelProfileRegistry` 引入三件套：`sources: dict[Path, ModelProfile]`
（来源路径映射）、`revision: int`（版本计数，UI 侧据此感知重载）、`reload_base()`/
`install_reload()`（快照协议：后台读盘建 view → 主循环安装整替 `_profiles`）。
与我方 catalog-sync 的 RLock + `install_snapshot`（服务端推送 → 后台线程整替）同文件同职责。

融合原则（两机制并存、各服务其主）：

1. **字段层**：`__init__` 两侧都保留——`_lock`（我方）+ `sources`/`revision`（上游）；
2. **锁域扩展**：上游改写的方法（`register`/`remove`/`install_profiles`）全部纳入我方
   `_lock` 保护（原有锁语义不丢），锁内同时维护上游 `sources` 与 `revision`；
3. **两个整替入口并存**：`install_snapshot`（我方，catalog 整替——sources 清空置 `{}`
   + `revision += 1`）与 `install_reload`（上游，本地 reload 整替——同样入锁，保持与
   `install_snapshot` 一致的互斥口径）；
   - **sources 收敛性（评审 B2）**：catalog sync 先写盘再装内存（条目在磁盘上存在），
     清空 `sources` 后，`reload_base()` 拷贝视图会把 catalog 条目按"非磁盘来源"保留，
     再经磁盘重扫以 `source` 身份重新注册。**实施期核对项**：① catalog 写盘目录与
     `_user_dir` 扫描目录的一致性（catalog-sync design 写盘路径）；② `reload_base`
     的视图构建语义——**已定论（评审 2026-11-06 读上游全文）：从当前 registry 内存
     拷贝并剔除 `disk_ids`，非从磁盘全新构建**（实施时结论记入 verification）；
     ③ 补时序测试断言：catalog 快照后紧跟一次 reload，终态条目集不丢失不重复；
4. **并发正确性与 reload 三段式窗口（评审 B1）**：锁内串行化只保护 install 动作本身；
   上游 reload 协议是三段式 `reload_base()`（拷贝视图）→ worker 线程磁盘扫描 →
   `install_reload()`（整替安装），其正确性依赖上游"单事件循环安装"假设。我方 catalog
   后台线程打破该假设：**若 `install_snapshot` 落在 `reload_base` 与 `install_reload`
   之间的窗口内完成，且磁盘扫描先于 catalog 写盘，`install_reload` 会用旧视图整体覆盖
   ——catalog 新条目静默回退，`revision` 照常递增，UI 无法察觉**。处置口径
   （**已裁决 2026-11-06：采纳建议案——接受 + 下轮 sync 自愈**；依据：窗口需两个独立
   低频事件毫秒级交叠且满足特定时序，概率极低；后果仅内存版本短暂回退，无数据丢失、
   不影响在跑会话，下轮 sync 整替必然恢复；备选案代价不对等——互斥引入跨线程协调
   新风险，revision 检测改上游语义扩大 R2 面，均违背 R1 最小改动）：
   - **选定**：接受窄窗口短暂回退，自愈 = 下一轮 catalog sync 周期整替；
   - 备选一（否决留痕）：reload 与 catalog sync 互斥——跨线程协调两触发源，复杂度高
     且引入死锁/阻塞新风险；
   - 备选二（否决留痕）：`install_reload` 前检测 `revision` 窗口内变化则合并/重试——
     改上游方法语义，R2 面扩大，合并逻辑本身易引入新 bug；
   - 配套测试：catalog 快照与 reload 交错的时序断言（回退窗口存在时下一轮 sync 自愈
     的终态收敛）；未来若窗口实证频发可凭否决留痕重开裁决；
5. C030/C031 marker 行保留（注释文本按融合后语义微调时同步 design 登记行——存量维护，
   非新 marker）。

**淘汰检查联动（重要）**：C030 的 `removal_condition` 为〔上游等价〕"上游自带跨线程安全
且整替语义有官方通道，经对比分析确认可迁移（提请裁决）"。上游 v0.34.0 reload 协议
（`install_reload` 整替 + revision）与 C030/C031 的锁+快照在"整替"维度语义相近，构成
**命中候选**——本批次决策交付物须出"上游实现 vs 我方定制需求"对比分析（上游 reload
面向本地文件手工编辑重读、单事件循环安装；我方面向服务端 catalog 推送、后台线程与 UI
线程并发读写），**是否迁移/保留交责任人裁决，不自行处置**。若裁决保留，融合方案按上表执行。

### 已预见的语义适配点（auto-merge 通过但需人工融合核对）

- `main/screen.py` reload 重构（+288 行）：C013–C017（静默登录校验挂 `on_mount` /
  teardown 取消登记）与上游 settings/memory/skills reload 改动同文件 auto-merge——
  核对 marker 行在 + 登录检查接线未被 reload 生命周期重构绕过（`on_mount` anchor 签名）；
  `test_panel_anchor.py` + auth 定制测试双重机检；
- `config_actions.py`（C035 `read_only=True`）：上次冲突点，本次上游 +5 行 auto-merge——
  核对我方恒只读未被上游改动绕过（上游 live-reload 有动机动 models 编辑入口）；
- `settings.py`/`process_settings.py`：`.env` 停载 breaking（±34 行）与我方 catalog 定制
  字段（`model_catalog_base_url`）并存——核对字段未丢、无启动路径依赖 `.env`；
- `tests/app/tui/i18n/test_command_suggestions_i18n.py`（C024/C086）：上游若新增命令索引
  行，`/man` 期望断言融合 = 上游新增行 + 我方 `/login` `/logout` + AIxCoding 品牌行；
- `tests/architecture/test_ci_test_partitions.py`（a3cad3d9 CI 分片重平衡）：上游自身
  变更，merge 后随 fork-full 验证；
- AGENTS.md（C001）：保留我方三段 marker（AIxCoding 入口段），合入上游正文改写，
  逐段比对。

## 备选方案对比

| 方案 | 优点 | 缺点 | 升级冲突面（R1） | 结论 |
|---|---|---|---|---|
| A. merge v0.34.0（05679bec，本设计） | 稳定 release；与镜像头同一对象、零"镜像领先"尾巴；干跑实测冲突 5 处、解法明确 | — | 小（实测） | **选定** |
| B. 等待 v0.34.1 再升级 | 可能少一次升级成本 | 违反 §8 节奏条款（需显式决策）；债务持续累积 | — | 否决 |
| C. rebase 定制提交到 v0.34.0 | 历史线性 | 改写已发布历史 + 需 force push（纪律禁止） | — | 否决 |
| D. 重建法（0.28→0.31 旧路线） | 无冲突 | 冲突面实测可控，完全不需要；丢 PR 历史可追溯性 | — | 否决 |

## 上游改动清单（R2）

**本批次我方对上游文件的新增改动 = merge 后实证增补 4 处测试适配**（C136–C139，
责任人 2026-11-06 批准；登记见「定制点登记预告」）：

| 文件 | 改什么 | 标记注释位置 |
|---|---|---|
| tests/app/tui/screens/test_manual_reload.py | 上游新增 reload 测试 ×2 经 F4 打开模型对话框/删除默认模型，与 catalog F4 禁用+恒只读互斥——函数级 skip（C136/C137），skip reason 与 C134 同款前缀 | `AIxCoding catalog: upstream opens the local model dialog via F4` / `AIxCoding catalog: upstream deletes the default profile in the local dialog` |
| tests/app/tui/screens/test_reload_profiles.py | 上游新增 reload 测试后半段 F4 模型编辑器保存，同互斥——函数级 skip（C138），同款 reason 前缀 | `AIxCoding catalog: upstream edits models in the local editor via F4` |
| tests/service/extensions/test_discovery.py | 上游新增 extensions 跳过消息断言硬编码 `iCode's own tool categories`，我方运行时输出 AIxCoding——断言品牌适配（C139，同 C133 先例） | `AIxCoding branding: extension skip message assertion` |

其余口径不变：

- 5 处 merge 冲突解决全部落在在册定制点（C030/C031 registry 融合、C028/C043 计数行、
  C102 品牌断言、C013–C017 同文件的 `main/screen.py` import 融合）与编译产物（mo）；
- `pyproject.toml` version 行随 merge 自动 0.33.0→0.34.0（上游自身变更）——四段机制
  仍未启用（versioning.md 后置，v0.33.0 批次同一裁决延续），基线升级只随上游同步三段；
- 若后续验证**再**发现需要新的上游文件改动：停下，按 R2 增补报批后再动（铁律）。

## 对上游隐式行为的假设

| 假设 | 我方依赖方式 | 失效表现与对策 |
|---|---|---|
| 5 个借壳 key（C028 tui ×4 + C043 catalog ×1）在 0.34.0 的 pot 抽取中仍全部生成 | oracle 计数 = 上游基数 2700 + 5 = 2705（预估，以实测为准） | oracle 红 → 重走 i18n 全管线（extract→update→compile→check）后复测；仍缺则查上游 msg 定义处变更 |
| 上游 0.34.0 未重构 tui 基建符号（`BaseDialog`/`localized_binding`/`AppHeader`/`MainScreen.on_mount` 等 anchor） | 定制 tui 挂点编译期依赖（本区间 main/screen.py +288 行为最大暴露面） | ty check / 定制测试红 → 按 runbook §3 逐 anchor 核对后跟随适配（触及上游文件则回 R2 报批） |
| `.env` 停载不影响我方运行与测试路径 | `aixcoding/` 零 dotenv 引用（2026-11-06 已查证）；mock launcher 不依赖 `.env` | 定制测试红 → 查启动 bootstrap 链路是否被我方消费 |
| 上游 live-reload 不自动触发 catalog 重载（reload 面向本地文件） | catalog sync 仍由我方后台线程驱动 | catalog 契约测试红 → 核对 reload 调用链与 catalog 生命周期交互 |
| `chrys.kernel/__init__` 包级导出（`get_login_session` 等）签名未变 | auth 接线 lazy import | 定制 auth 测试红 → anchor 核对后跟随适配 |

已验证非假设（不列入）：`.python-version` 3.14.8 一致、fetch_rg 版本不变、
AGENTS.md/pyproject/po auto-merge 通过（语义核对列入核对矩阵）。

## 定制点登记预告

原稿"预判无新增定制点"——merge 后 fork-full 实证暴露 4 处上游新测试与我方定制的语义冲突，
适配产生 **4 个新 marker**，编号顺延 **C136–C139**（责任人 2026-11-06 批准，随本 design
重签生效；v0.33.0 批次 C133–C135 同型先例）。冲突适配其余部分均落在在册定制点。

| id | 落点 | marker 锚 | 预期计数 | anchor | risk | removal_condition |
|---|---|---|---|---|---|---|
| C136 | tests/app/tui/screens/test_manual_reload.py | AIxCoding catalog: upstream opens the local model dialog via F4 | 1 | 函数级 `@pytest.mark.skip` + reason 前缀（catalog F4 禁用与模型对话框断言互斥留痕） | 低（显式 skip；上游追加用例将显式暴露需复核） | 〔随组〕catalog 只读管控语义整体淘汰获裁时恢复上游测试（测试支撑） |
| C137 | tests/app/tui/screens/test_manual_reload.py | AIxCoding catalog: upstream deletes the default profile in the local dialog | 1 | 函数级 skip + reason 前缀（与 C134 同 helper 同互斥语义，reason 逐字一致） | 低（同 C136） | 〔随组〕同 C136 |
| C138 | tests/app/tui/screens/test_reload_profiles.py | AIxCoding catalog: upstream edits models in the local editor via F4 | 1 | 函数级 skip + reason 前缀（F4 模型编辑器保存互斥；前半段 reload 覆盖由同文件其余测试保留） | 低（同 C136） | 〔随组〕同 C136 |
| C139 | tests/service/extensions/test_discovery.py | AIxCoding branding: extension skip message assertion | 1 | extensions 跳过消息 `AIxCoding's own tool categories` 断言（branding.py APP_DISPLAY_NAME 消费方） | 低（测试断言品牌适配，同 C133 先例） | 随 C050（品牌政策变更时逐点裁决） |

## 既有定制点存活核对矩阵（runbook §3 结构层展开）

> 输入 = 升级前 `gen_customizations.py --report` 时点快照（当前 98 登记行 / 135 marker，
> 编号 C001–C135）。核对按定制组全覆盖，逐组记录存活结论并入 verification.md。

| 定制组（编号域，边界以 report 为准） | 核对内容 |
|---|---|
| 文档入口（C001 / C130 / C131，AGENTS.md） | 三段 marker 行文本不变、段完整（上游改 10 行，auto-merge 过≠语义对） |
| 日志面板（C007） | `logs/screen.py` MODULE_ORDER 行含 `"aixcoding"`（上游零改动，低风险） |
| 启动接线（C004） | `app.py` `main()` startup 接线行在（app.py +97 行 auto-merge 过） |
| TUI 登录/徽标/命令（C005–C029） | badge/dialog/commands/screen/app_header marker 行在；**重点 C013–C017 与 reload 重构同文件**（on_mount anchor + 登录校验接线）；`/man` 断言融合（C024，上游命令索引行） |
| LLM 认证（C021/C022） | `wire_client.py` token 头注入（上游区间该文件零改动，低风险） |
| catalog-sync（C030–C049） | **重点**：registry.py 融合（C030/C031，含 B1 窗口/B2 收敛性核对项与交错时序断言）、config_actions.py read_only（C035）、process_settings.py 字段；全组 marker 逐一对照 report |
| branding-and-command（C050–C131） | 全组 marker 在；重点核对本区间 auto-merge 过的 `skills/provider.py`、`approval.py`、`uninstaller.py`、`models/screen.py`、`chrys.tcss`、`test_agent_builder_rollback.py`（C102） |
| i18n（C002 / C027 / C028 / C043 / C132 + 豁免表 4 项） | oracle 计数 2705 实测 / pot/po/mo 重生成后 marker 与条目齐全 |
| ruff 豁免（C003） | pyproject.toml per-file-ignores 豁免行在（上游区间 pyproject 改 version/依赖，低风险） |
| 机检兜底 | `pytest aixcoding/tests/diagnostics/test_panel_anchor.py -n0 -q` 绿 + merge 后 `gen_customizations.py --strict` 0 error / 0 warning |

## 升级决策交付物（runbook §5）

以下清单随升级产出，**最终决策全部交责任人裁决**，记录于本目录 `verification.md` 与 PR 描述：

1. **冲突点全清单 + 逐点处理意见**（含 auto-merge 通过但语义核对有疑义的点）——干跑已预判
   5 处（上表），merge 后按实际补全；
2. **定制化改变清单**：需新增 / 建议删除（淘汰候选，附对比分析）/ 需适配——预判重点：
   - **C030/C031 淘汰候选对比分析**（〔上游等价〕命中判定）：上游 v0.34.0
     `reload_base`/`install_reload`/`revision` 整替协议 vs 我方 RLock +
     `install_snapshot` catalog 后台整替——线程模型（单事件循环安装 vs 后台线程与 UI
     并发）、触发源（本地文件编辑 vs 服务端推送）、一致性保证（revision 计数 vs 互斥锁）
     三维对比；裁决保留则按融合方案执行，裁决迁移则出迁移设计（独立于本 merge）；
   - registry 融合本身（若裁决保留）属"需适配"项，融合方案已列 design；
   - merge 后实证增补（上游新测试与我方定制语义冲突的处理，v0.33.0 批次 C133–C135 先例）；
3. **淘汰检查逐组结论**：以 `--report` 全景登记为输入，逐组记录每点 `removal_condition`
   的**命中/未命中及依据**；命中即出对比分析——**不自行删除任何定制**。

## 风险与最坏情况

- **最大风险：registry 融合的并发语义错**（锁域遗漏 / sources 与 revision 维护不一致 /
  catalog sync 与 reload 并发竞态）——对策 = 融合方案逐条落实 + catalog 契约测试 +
  上游 reload 测试（`21dee050` 配套）双重回归；仍红则暂停呈报；
- **registry 窗口风险（评审 B1/B2）**：reload 三段式（`reload_base`→磁盘扫描→
  `install_reload`）与我方 `install_snapshot` 交错的静默回退窗口（细节与处置口径见
  融合方案第 4 点，**已裁决 2026-11-06：接受 + 下轮 sync 自愈**）与 sources 清空后的
  reload 收敛性（融合方案第 3 点实施期核对项 ①③，②已定论）；两风险均有时序测试
  断言兜底，断言红且无法收敛则升级暂停呈报；
- **次风险：`main/screen.py` reload 重构吞并登录接线**（auto-merge 静默错）——对策 =
  C013–C017 marker 逐行核对 + auth 定制测试 + `test_panel_anchor.py` 机检；
- po auto-merge 过但借壳 key 丢失 → oracle 红，重走 i18n 管线；仍红则升级暂停呈报；
- 回滚：merge 在特性分支上进行，任何时候可丢弃分支从 `main_aixcoding_cli` 重来；
  PR 合并后发现问题的回滚 = revert 该 merge PR（merge commit 单点可逆）。

## ADR 判定

按 §6 checklist 逐项——① 被认真考虑但否决的备选：B/C/D 均为 runbook/纪律既定路径的
常规排除，非本批次新决策；② 撤销代价高：否（revert merge 单点可逆）；③ 约束未来工作
模式：否；④ 依赖上游非公开接口：无新增（anchor 均已在册）。
C030/C031 淘汰候选的裁决结果如为"迁移"，届时由独立批次设计承载（本 design 仅定对比
分析与裁决流程）。**结论：不出 ADR。**

（"目标取 release tag 还是镜像头"是 runbook §1.1 已枚举的操作参数选择，本次二者同一
对象无分歧；本节连同上方备选对比表即留痕。）
