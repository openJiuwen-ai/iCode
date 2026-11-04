# 验证记录：追上游 v0.33.0（upstream-merge-v0.33.0）

> 活文档：追加不删旧。merge commit = `33b55cf9`（双亲 `2bde5abe` + `8d5a3208`）。

## 一、验证矩阵结果总表（2026-11-04）

| 验收项 | 结果 | 证据 |
|---|---|---|
| 升级前绿基线 | ✅ | `aixcoding/tests` 454 passed / 4 skipped（52s）；`check_docs` OK |
| 6 处 merge 冲突解决 | ✅ | 冲突清单见下节；冲突标记零残留 |
| oracle 计数实测 | ✅ **2628** | pot/po/有效条目集合 == EXPECTED（2628），无单向差集（= 上游 2623 + 借壳 5，C028/C043 双 marker 在） |
| i18n 管线幂等 | ✅ | extract/update/compile/check 第二轮，pot/po/mo hash 前后一致 |
| `gen_customizations --strict` | ✅ | 97 登记行 / 134 marker / 0 error / 0 warning（重签后复跑同绿） |
| 行为锚定测试 | ✅ | merge 后初检含 `test_panel_anchor.py`，73 passed / 1 skipped |
| `/man` 断言融合 | ✅ | 上游 /btw、/usage + 我方 /login、/logout + 品牌行 + C024/C086 marker 全在（test_command_suggestions_i18n.py 绿） |
| R2/R3 新 merge-base | ✅ | base=8d5a3208；r2_diff OK（77 上游文件 ⊆ approved_pool=80）；r3_markers OK（73 checked + 4 exempt） |
| import 边界 | ✅ | `check_import_boundaries` OK（1170 文件） |
| 定制套件 merge 后重跑 | ✅ | 454 passed / 4 skipped（与基线逐位一致） |
| ty 三平台 | ✅ | linux / darwin / win32 全过 |
| ruff check + format | ✅ | 2623 文件合规（含 2 个适配文件单独复核） |
| fork-full（verify.py --full） | ⚠️ 首跑 5 failed → ✅ 复跑全绿 | 首跑 34445 passed / 5 failed（10:49）；2 适配后复跑：custom 454 + tests **34446 passed / 724 skipped** + architecture **1247 passed / 3 skipped**，`Summary: PASSED (exit 0)` |
| TUI 冒烟 | ✅ headless + 人工联调 | `aixcoding-cli --version` = 0.33.0、`agent list` 正常；mock（:7777/:7778）+ TUI 按 test-plan 手工步骤及联调清单检查，责任人 2026-11-04 确认无问题 |
| PR 双 CI | ⏳ 待 push | aixcoding-tests.yml + 上游 ci.yml 桥接 |

## 二、冲突点全清单 + 逐点处理意见（runbook §5 交付物 1）

### merge 文本/二进制冲突（6 处，与干跑预判一致）

| # | 文件 | 处理 | 复核 |
|---|---|---|---|
| 1 | `locales/zh-Hans/LC_MESSAGES/chrys.po` | 冲突仅在**过时条目区**（`#~` 存档）：我方旧 /login /logout man 文案与上游 `tui.input.sending` 按字母序融合；随后重走 i18n 全管线 | oracle 全等 + 幂等 ✅ |
| 2 | `src/chrys/app/tui/screens/main/config_actions.py` | 保留我方 `read_only=True` + marker（catalog 恒只读，语义覆盖上游 `_config_read_only()` 动态只读） | 决策交付物 3 记录 ✅ |
| 3 | `src/chrys/foundation/config/process_settings.py` | 两字段并存：上游 `credential_command_ttl_seconds` + 我方 `model_catalog_base_url`（dataclass 字段区与 from_settings 各一处，marker 保留） | ty 三平台 ✅ |
| 4 | `.../zh-Hans/LC_MESSAGES/chrys.mo` | 二进制不手工解，`i18n.py compile` 重生成 | 幂等 hash ✅ |
| 5 | `tests/app/cli/test_hooks.py` | 融合：上游 `Settings → Safety` + 我方 `aixcoding-cli hook validate`（C100–C101 marker 保留） | 初检 73 passed 含此文件 ✅ |
| 6 | `tests/foundation/i18n/test_catalog_oracle.py` | 计数 2504/2623 → **2628**，C028/C043 双 marker 保留 | oracle 实测全等 ✅ |

### auto-merge 通过但人工语义核对（design 预判点）

- `AGENTS.md`（C001/C130/C131）：上游改 6 行 auto-merge 过，入口段 marker 与段完整性核对通过（--strict 含）；
- `tests/app/tui/i18n/test_command_suggestions_i18n.py`（C024/C086）：`/man` 完整等值断言融合核对通过（上表）。

### merge 后全量验证暴露的新冲突（2 处，R2 增补报批）

| # | 文件 | 归因（实验链） | 处理（责任人 2026-11-04 批准） | 复核 |
|---|---|---|---|---|
| 7 | `tests/service/tools/web/test_tools.py` | 上游新增 keyless 测试硬编码 `X-Keenable-Title == "iCode"` vs 我方品牌 | 断言改 `AIxCoding` + marker（**C133**） | 1 passed ✅ |
| 8 | `tests/app/tui/screens/test_models_dialog_deletion.py` ×4 | 上游新增"F4 打开对话框删除模型"测试；我方 catalog-sync F4 禁用（screen.py:264）+ 只读隐藏删除按钮——**语义互斥非回归**。实验：纯上游 4 passed → fork 树 4 failed → 还原 read_only 参数仍失败（排除该变量）→ 动态诊断无异常（F4 禁用无动作） | 函数级 `@pytest.mark.skip` + marker（**C134**；外部 AI 评审微调：函数级优于模块级——上游追加用例将显式暴露而非静默吞没；reason 带 AIxCoding 前缀） | 4 skipped ✅ |

## 三、定制化改变清单（runbook §5 交付物 2）

- **需新增**：C133（keyless 品牌断言）、C134（models_dialog_deletion skip）——已登记入本批次
  design「定制点登记预告」表，`--strict` 97/134 对账绿；
- **需适配的既有定制点**：C028/C043（oracle 计数 2628）、C100–C101（Safety+aixcoding-cli 融合）、
  catalog-sync 的 `read_only=True` 与 `model_catalog_base_url`（冲突 #2/#3 保留）；
- **建议删除（淘汰候选）**：**无**——逐组淘汰检查见下节，95 行登记全部 not-hit；
- 讨论选项 D（catalog 只读行为正向覆盖）留独立批次，待办已登记
  `fork\temp\aixcoding-todos\2026-11-04-catalog-readonly-test-coverage.md`。

## 四、95 行矩阵逐组存活核对结论（runbook §3 结构层）

| 定制组 | 结论 | 依据 |
|---|---|---|
| 文档入口（C001/C130/C131） | 存活 | AGENTS.md 三段 marker 在（--strict 对账含） |
| 日志面板（C007） | 存活 | 上游区间 `logs/screen.py` 零改动；`test_panel_anchor` 绿 |
| 启动接线（C004） | 存活 | `app.py` main() 接线行在（--strict 含） |
| TUI 登录/徽标/命令（tui 域） | 存活 | badge/commands/screen/app_header marker 全在；/man 断言融合绿；Security→Safety 未波及定制挂点（定制测试 454 全绿） |
| LLM 认证（C021/C022） | 存活 | 上游对 wire_client.py 仅 observer +5/−1，stamp 登录 token 域零触碰；`{{!command}}` 分层不抢道（design 影响结论节） |
| catalog-sync（C030–C049） | 存活 | 冲突 #2/#3 按设计保留；registry 整替等全组 marker 在 |
| branding-and-command（C050–C131） | 存活 | 全组 marker 在（--strict 含 wheel/installer/uninstaller/approval/OSC/hooks 测试契约）；keyless 新断言已适配（C133） |
| i18n（C002/C027/C028/C043/C132 + 豁免表 4 项） | 存活 | oracle 2628 全等 + pot/po/mo 重生成幂等 |
| ruff 豁免（C003） | 存活 | 上游区间 pyproject 仅 version 行变化，豁免行在 |
| 机检兜底 | ✅ | `--strict` 97/134 + `test_panel_anchor` 绿 |

> 本节即 runbook §6.3 的 **anchor/risk 重评记录**：全部定制点 anchor 未漂移（行号纯移位不影响
> 文本锚对账），risk 等级维持原评，无需调整登记行。

## 五、淘汰检查逐组结论（runbook §5 交付物 3）

以 `--report` 95 行登记全景为输入，逐组记录 `removal_condition` 命中/未命中及依据：

| 组 | 结论 | 依据 |
|---|---|---|
| tui（C005–C029 等） | **not-hit** | 上游无原生账号登录体系（/btw、/usage 与登录命令无重叠；Security→Safety 为 tab 改名非登录 UI）；不构成〔上游等价〕 |
| catalog-sync（C030–C049） | **not-hit** | `{{!command}}` 非 catalog 等价物（选 A 裁决在案）；上游 in-flight 只读被我方恒只读语义覆盖而非等价替代；registry 整替无上游官方通道 |
| branding-and-command（C050–C131） | **not-hit** | 上游品牌仍为 iCode（pyproject/AGENTS/文案均原样），无品牌政策变化 |
| auth-logging（C021/C022 等） | **not-hit** | `{{!command}}` 是本地命令取凭据，非"登录会话/凭据存储注入请求头"，C021 removal_condition 不命中 |
| diagnostics（C007） | **not-hit** | `logs/screen.py` 上游零改动，MODULE_ORDER 机制未变 |
| i18n（C002/C027/C028/C043/C132） | **not-hit** | ADR-0010 政策未变，上游 i18n 管线正常扩容 |
| ruff 豁免（C003） | **not-hit** | `aixcoding/tests` 目录未退役，豁免迁移待办仍独立在案 |
| 其余（agents-md-note / 治理类登记） | **not-hit** | 机制均存续 |

**结论：零淘汰候选，本批次不删除任何定制。**

## 六、裁决记录（追加不删旧）

1. **目标 ref = release tag v0.33.0（8d5a3208）**——责任人 2026-11-04 认可；镜像头 24 个未发版提交留下次；本批次不触碰镜像分支（评审 Minor 采纳）。
2. **Blocker 1（`{{!command}}` × catalog 新信任路径）选 A**——责任人 2026-11-04 终裁：内网安全受控环境、服务端保证永不下发、不考虑被攻破场景；残余风险接受；**不出 ADR**（沿用 ADR-0018 内网信任基线，复审尊重裁决）；弃用备选 B（`src/chrys/service/aixcoding/catalog.py` 清洗 + 两入口回归测试）留痕可随时启用。配套 C021 优先级契约验证结论落 design。
3. **三轮文档评审收敛**：初审 5 项（信任路径/95 行口径/oracle 2628/man 融合/镜像）、复审 4 项（ADR 终裁/C003 补入/引用对齐/路径写全）、终审通过。
4. **R2 增补报批（2026-11-04）**：keyless 品牌断言（C133）+ models_dialog_deletion 函数级 skip（C134，外部 AI 讨论后微调），均已批准实施并验证。
5. **design 增补重签（2026-11-04）**：R2 清单 + C133/C134 登记 + 决策交付物三节增补后重签完成。

## 七、时点快照（gen_customizations --report，2026-11-04 重签后）

```
gen_customizations: check OK — 97 registration row(s) / 134 marker(s) across 10 design(s), 0 retired id(s), 0 warning(s)
```

95 行全景（C001–C132）详见 `aixcoding/docs/batches/upstream-merge-v0.33.0/design.md`
「既有定制点存活核对矩阵」的逐组映射；实时视图以 `uv run python aixcoding/tools/gen_customizations.py --report` 为准。

## 八、遗留项（收尾跟踪）

- [x] fork-full 复跑：0 failed（custom 454 + tests 34446 + architecture 1247，PASSED exit 0）；
- [x] TUI headless 冒烟（--version=0.33.0、agent list 正常）+ mock/TUI 人工联调（责任人确认无问题）；
- [x] UPGRADE-RUNBOOK.md 头部「当前基线」更新（0.32.2→0.33.0、2026-11-04）；
- [ ] 收尾提交（2 测试适配 + INDEX + 批次文档 + RUNBOOK；pot 假脏排除）→ push → PR 双 CI。
