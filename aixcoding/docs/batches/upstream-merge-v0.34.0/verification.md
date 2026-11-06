# 验证记录：追上游 v0.34.0（upstream-merge-v0.34.0）

## 一、验证矩阵结果总表（2026-11-06）

| 验证项 | 结果 | 证据 |
|---|---|---|
| 升级前基线：定制套件 | **511 passed, 50 skipped**（104s） | 基线 HEAD 28b7d1c7 |
| merge 后定制全套（含 B1/B2 新断言） | **513 passed, 50 skipped**（91s） | 基线 511 + 2 新增 |
| `gen_customizations.py --strict`（冲突适配后） | 98 行/135 marker，0 error 0 warning（与基线零漂移） | merge 后实测 |
| `gen_customizations.py --strict`（C136–C139 适配后） | **102 行/139 marker，0 error 0 warning**（+4 登记与预告精确一致） | R2 增补后实测 |
| 行为锚定 `test_panel_anchor.py` | 4 passed | 机检 |
| R2 `check_r2_diff.py --base 05679bec` | OK（C136–C139 适配后复跑：upstream_changed=83 全落 approved_pool） | 新 merge-base |
| R3 `check_r3_markers.py --base 05679bec` | OK（适配后复跑：upstream_changed=83 / marker_checked=79 / exempt=4；适配前时点为 80/76/4，差值=3 个适配文件） | 新 merge-base |
| ruff check（--no-fix）+ format（全目录含 aixcoding/） | 全绿（2938 files already formatted） | CI 同口径 |
| ty 三平台（linux/darwin/win32） | 全绿 | — |
| i18n 管线 extract→update→compile→check | 全绿；**第二轮幂等零 diff**（locales/i18n 目录 git status 无残留） | 两轮实测 |
| oracle 计数 | **实测 2705** = 上游 2700 + 我方 5 借壳 key（与 design 预估一致） | `_catalog_oracle_ids` 实测 |
| rollback 测试（冲突 #5 融合） | 35 passed | C102 断言融合 |
| catalog 测试（含 B1/B2 断言） | 53 passed | — |
| fork-full `verify.py --full`（首轮） | FAILED：custom 513 绿 / upstream **4 failed**, 34868 passed / architecture 未跑 | 4 失败定位见第二节 |
| fork-full `verify.py --full`（C136–C139 适配后复跑） | **PASSED exit 0**：tests **34869 passed, 734 skipped**；architecture **1247 passed, 3 skipped, 1 deselected**；custom 513 passed | 上游套件清零 |
| 依赖对齐 `uv sync --frozen` | 已对齐（icode-tui 0.34.0 / pypdf 6.21.4 / python-dotenv 移除） | — |
| TUI 冒烟 | **通过**（2026-11-06 责任人人工验证：mock :7777/:7778 + TUI 双可见窗口；启动/F6 aixcoding tab/登录徽标与命令/F4 禁用/reload 后 catalog 条目收敛均正常） | test-plan 手工步骤 |
| PR 双 CI | 待 PR | — |

## 二、冲突点全清单 + 逐点处理意见（runbook §5 交付物 1）

### merge 文本/二进制冲突（5 处，与干跑预判一致）

| # | 文件 | 处置 | 验证 |
|---|---|---|---|
| 1 | `src/chrys/app/tui/screens/main/screen.py` | TYPE_CHECKING import 融合：`DraftReference`（上游）与 `AccountInfo`（我方）两行都保留，按字母序插入 | strict 135 marker 对账 + 定制全套绿 |
| 2 | `src/chrys/foundation/i18n/_catalogs/zh-Hans/LC_MESSAGES/chrys.mo` | 不手工解，`i18n.py compile` 重新生成 | 管线绿 + 二轮幂等 |
| 3 | `src/chrys/service/profiles/models/registry.py`（C030/C031） | 按 design 融合方案：①`__init__` 两侧字段并存（`_lock`+`sources`/`revision`）；②`register`/`remove`/`install_profiles` 上游 sources/revision 维护全部入锁；③`install_snapshot` 补 `sources={}` 清空 + `revision+=1`；④`install_reload` 入锁与 `install_snapshot` 同锁串行化；⑤C030/C031 marker 原文保留 | 53 catalog passed + B1/B2 断言 + ty/ruff 绿 |
| 4 | `tests/foundation/i18n/test_catalog_oracle.py`（C028/C043） | 计数融合 2628→**2705**（上游 2700+5 借壳 key），两行 marker 保留 | 实测 2705 |
| 5 | `tests/orchestration/engine/build/test_agent_builder_rollback.py`（C102） | 融合：上游新断言方式（`[result.web.hosted for result in captured]`）+ 我方 `app: "AIxCoding"` 品牌绑定；**未新增 marker**（C102 计数 1 保持，311 行原 marker 不动） | 35 passed |

### auto-merge 通过但人工语义核对（design 预判点，全部闭环）

- **AGENTS.md（C001/C130/C131）**：三段 marker 完整（L3 branding、L6 entry point、L7 customization 段），上游正文合入；
- **`main/screen.py` C013–C017**：marker 全在位（L536 任务声明 / L1586 silent validate / L1599 teardown 取消 / L890、L933 命令接线），登录接线未被 reload 重构（+288 行）绕过；auth 定制测试绿；
- **`config_actions.py` C035**：`read_only=True`（L283-286 marker+L286 赋值）存活，恒只读未被上游改动绕过；
- **`settings.py`/`process_settings.py`**：`model_catalog_base_url` 字段（L46-47/L60）存活；.env 停载 breaking 不影响我方（aixcoding/ 零 dotenv 引用，2026-11-06 已查证）；
- **`test_command_suggestions_i18n.py` C024/C086**：/man 断言融合正确——上游 /btw、/usage + 我方 /login、/logout + AIxCoding 品牌行并存；win32 侧 3 passed+跳过项，POSIX 实跑归 PR checks；
- **`test_ci_test_partitions.py`**（a3cad3d9 CI 分片）：随 fork-full 验证。

### merge 后验证暴露的新冲突（4 处，R2 增补已批准 2026-11-06）

fork-full 首轮：custom 513 全绿；upstream **4 failed / 34868 passed**。逐一定位归因，
全部为 v0.34.0 新增测试，与 v0.33.0 批次 C133–C135 同型：

| # | 测试 | 根因 | 处置 | 登记 |
|---|---|---|---|---|
| A1 | `test_manual_reload.py::test_manual_model_continues_through_chdir_and_model_dialog` | L193 `press("f4")` 打开 ModelConfigScreen 断言链一环，F4 被我方 catalog 禁用 → 5s 超时 | 函数级 skip + marker，reason 同 C134 前缀 | C136 |
| A2 | `test_manual_reload.py::test_deleted_default_with_unwritable_settings_still_chooses_a_model_and_rebuilds` | 经 `_delete_in_the_models_dialog`（C134 同 helper）对话框删除默认模型 | 函数级 skip + marker，reason 与 C134 逐字一致 | C137 |
| A3 | `test_reload_profiles.py::test_later_model_editor_save_rebuilds_children_from_the_shared_registry` | 后半段 F4 模型编辑器编辑保存 | 函数级 skip + marker，同前缀；前半段 reload 覆盖由同文件其余测试保留 | C138 |
| B1 | `test_discovery.py::test_entries_that_are_not_extensions_are_ignored_and_bad_ids_are_skipped` | 断言硬编码 `iCode's own tool categories` vs 我方运行时 `AIxCoding's`（branding 绑定） | 断言品牌适配 + marker（同 C133 先例） | C139 |

适配验证：3 skipped + 1 passed（见第一节复跑行）。

## 三、定制化改变清单（runbook §5 交付物 2）

- **需新增**：2 个测试断言（`aixcoding/tests/catalog/test_catalog_sync.py`，我方测试目录，非上游文件、非新定制点）：
  - `test_reload_after_snapshot_keeps_catalog_entries`（B2 收敛性：快照后 reload 条目不丢失不重复）；
  - `test_reload_window_rollback_recovers_on_next_snapshot`（B1 窗口：回退存在 + 下轮 sync 自愈终态收敛）；
- **需适配**：registry.py 融合（C030/C031 域内存量适配，见冲突 #3）；**merge 后实证增补 4 处测试适配**（C136–C139，见第二节新冲突清单——3 函数级 skip + 1 品牌断言，已批准）；
- **建议删除（淘汰候选）**：见第五节 C030/C031 对比分析——**已裁决保留（2026-11-06）**。

## 四、98 行矩阵逐组存活核对结论（runbook §3 结构层）

| 定制组 | 结论 |
|---|---|
| 文档入口（C001/C130/C131） | 存活：三段 marker 完整（auto-merge 后人工逐段比对） |
| 日志面板（C007） | 存活：上游区间零改动 |
| 启动接线（C004） | 存活：strict 对账 + 定制测试绿 |
| TUI 登录/徽标/命令（C005–C029） | 存活：C013–C017 逐行核对（见第二节）；tui 定制测试绿 |
| LLM 认证（C021/C022） | 存活：上游 wire_client.py 零改动 |
| catalog-sync（C030–C049） | 存活：registry 融合 + C035 read_only + 字段核对；53 passed 含 B1/B2 |
| branding-and-command（C050–C131） | 存活：strict 135 marker 全对账；C102 融合 35 passed |
| i18n（C002/C027/C028/C043/C132+豁免 4 项） | 存活：oracle 2705 + 管线幂等 |
| ruff 豁免（C003） | 存活：pyproject per-file-ignores 行在（ruff 全绿佐证） |
| 机检兜底 | test_panel_anchor 4 passed + strict 0/0 |

## 五、淘汰检查逐组结论（runbook §5 交付物 3）

以 `--report` 98 行登记全景为输入，逐组 hit/not-hit：

| 定制组 | 结论与依据 |
|---|---|
| 文档入口 / ruff 豁免 / i18n | **未命中**（〔常驻〕/政策未变；上游仅 AGENTS.md 正文与 po 内容演进，不构成政策或机制变化） |
| 日志面板 C007 | **未命中**：上游 logs/screen.py 区间零改动，无第三方模块分层原生机制出现 |
| 启动接线 C004 | **未命中**：消费者（startup 接线）仍在服役；上游 v0.34.0 新增的是 **Python extensions**（3998c2b9，扩展为 agent 加工具）——其机制面向"扩展作者写 Python 包"，非应用启动接线 hook，不构成 C004 的官方承载通道 |
| TUI 登录域（C005–C029） | **未命中**：上游 v0.34.0 无账号登录/徽标/命令功能（reload 系列不涉登录语义） |
| LLM 认证 C021/C022 | **未命中**：上游 wire_client.py 零改动 |
| **catalog-sync C030/C031** | **命中候选 → 对比分析如下（AI 建议：不迁移、保留）** |
| catalog-sync 其余（C032–C049） | **未命中**：F4 禁用/恒只读/字段等〔政策变更〕类条件未触发 |
| branding C050–C131 | **未命中**：品牌政策未变；installer 旧版迁移支持未退役 |

### C030/C031 对比分析（〔上游等价〕命中判定）

removal_condition 原文："上游自带跨线程安全且整替语义有官方通道，经对比分析确认可迁移（提请裁决）"——**两条件需同时满足**。

| 维度 | 上游 v0.34.0 reload 协议 | 我方 C030/C031 需求 |
|---|---|---|
| 跨线程安全 | **不满足**：`read_profiles` docstring 明示单事件循环安装假设（worker 线程仅磁盘 I/O，install 回主循环）；`sources`/`revision` 无锁保护；B1 窗口（reload_base→install_reload 交错回退）正是该假设差的产物 | 后台 catalog sync 线程与 UI 线程并发读写，必须互斥 |
| 整替语义官方通道 | 满足：`install_reload` view 整替 + `reload_base` 拷贝剔除 disk_ids（评审已定论其语义） | `install_snapshot` items 整替（语义相近，入口不同） |
| 触发源 | 用户手工编辑本地文件 → TUI 热重载 | 服务端 catalog 周期推送 → 后台线程整替 |

**AI 建议：不迁移、保留 C030/C031**——"跨线程安全"条件不满足（上游明确不覆盖后台线程并发场景），即使整替语义相近也不构成等价命中；融合后两机制并存已全绿，B1 窗口已经裁决接受+自愈。**最终裁决待责任人。**

## 六、裁决记录（追加不删旧）

- **2026-11-06 B1 窗口处置口径**（design 签认阶段）：采纳建议案——接受窄窗口短暂回退 + 下轮 catalog sync 自愈；互斥/revision 检测两备选否决留痕（design 融合方案第 4 点）。
- **2026-11-06 C030/C031 淘汰候选**：**责任人裁决保留**（第五节对比分析成立——上游 reload 协议不满足"跨线程安全"条件，不构成〔上游等价〕命中；融合并存维持）。
- **2026-11-06 R2 增补批准**：fork-full 暴露的 4 处上游新测试冲突适配（C136–C139）经责任人批准；skip reason 统一 C134 同款前缀（组内一致便于下次升级 grep）。
- **2026-11-06 实现评审（外部 AI）三 finding 处置**：
  - I1（docstring 口径超前）：**已消解**——评审信息滞后，B1 裁决与 design 签认均已完成，docstring 与事实一致，不改；
  - I2（reload_base 无锁读）：**记录在案不改**——评审论证成立：CPython 引用整替下迭代器绑定旧 dict 对象，catalog sync 整替不影响读方迭代；`register` 原地插入仅发生在 TUI 事件循环，与 `reload_base` 同线程串行。与 B1 窗口同域同级别，后续如需补锁走独立批次；
  - I3（B1 测试空 view 简化）：**接受简化**——回退→自愈断言语义成立；真实时序 view 含旧磁盘条目不影响断言目标（窗口回退与自愈收敛）。
- （merge 后无新增待裁决项；若 fork-full/CI 暴露新冲突，追加记录。）

## 七、时点快照（gen_customizations，2026-11-06 C136–C139 适配后）

```
gen_customizations: check OK — 102 registration row(s) / 139 marker(s) across 11 design(s), 0 retired id(s), 0 warning(s)
```

（升级前基线 98/135 → 升级后 102/139：+4 登记行/+4 marker = C136–C139，全部为本批次
design 登记预告内的新测试适配点；在册旧定制点零丢失。）

## 八、遗留项（收尾跟踪）

- TUI 冒烟与 PR 双 CI 待流程推进；
- UPGRADE-RUNBOOK.md「当前基线」节更新（0.34.0 / 2026-11-06）随本 PR 一并提交；
- `reload_base()` 无锁读观察项（实现评审 I2）：其读 `_profiles`/`sources` 构建视图在锁外（上游原样保留）；评审论证成立（CPython 引用整替下迭代器绑定旧 dict；`register` 原地插入仅 TUI 事件循环与 reload_base 同线程）——记录在案不单独处置，后续如需补锁走独立批次。
