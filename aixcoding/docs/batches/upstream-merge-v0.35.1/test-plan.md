# 测试方案：追上游 v0.35.1（upstream-merge-v0.35.1）

> 活文档：随时更新、不签认；其质量由双 AI 自审（SELF-REVIEW-HOWTO）与行为锚定测试本身保障。

## 测试范围与策略

升级批次不新增功能代码，测试 = **三层回归 + 流程产物验证**（对应 design「升级决策交付物」与
runbook §3/§4）：

1. **结构层（机检）**：marker 存活 + anchor 行为——`aixcoding/tests` 全套（含
   `diagnostics/test_panel_anchor.py` 行为锚定）+ `gen_customizations.py --strict`
   （升级前基线 100 行；merge+C028 退役后 99 行/136 marker；C143/C144 增补后
   101 行/138 marker/6 退役）；
2. **语义层（全量）**：`aixcoding/tools/verify.py --full`（fork-full：定制套件 + 委托上游全量，
   CodeBuddy 环境配方：清 PYTHONPATH、PYTHONUTF8=1、大写盘符）+ ruff/format 全目录 +
   ty 三平台 + i18n 管线（extract→update→compile→check，**三轮 hash 幂等**）；
3. **冲突适配验证**：4 处 merge 冲突逐项复核（AGENTS.md 三段 marker / po obsolete 条目
   两侧保留 / mo 接受删除 / oracle 去计数化接受）+ merge 后验证暴露的新测试冲突
   （C143/C144 品牌断言）分别处理；
4. **i18n 新机制专项（本批特有）**：mo 由 hatch hook/editable 构建生成（gitignored）、
   上游 `test_catalog_artifacts.py` / `test_catalog_build_hook.py` /
   `test_the_generated_catalog_is_never_tracked` 机检绿；oracle id 集合含我方 4 借壳 key
   （C027 存活）；豁免表 mo 行移除后 strict 收敛；
5. **流程产物**：升级决策清单（冲突点+处理意见、定制化改变清单、淘汰检查逐组结论，
   重点 C004/C140 与上游扩展 tool hooks 的对比分析）经责任人裁决留痕于 `verification.md`；
   CI 证据归属 PR checks 本身（§7）。

## 覆盖矩阵

| 验收标准（requirements.md） | 测试入口 | 类型 |
|---|---|---|
| merge 后 `--strict` 0 error/0 warning，在册定制点无丢失 | `gen_customizations.py --strict`（101 行/138 marker/6 退役终态） | 工具/结构 |
| runbook §4 矩阵全绿 | `verify.py --full` + 定制四门（aixcoding/tests / check_docs / check_import_boundaries / --strict）+ R2/R3 以新 merge-base（7c423b06）+ ty 三平台 + ruff/format | 全量回归 |
| 行为锚定测试绿 | `uv run pytest aixcoding/tests/diagnostics/test_panel_anchor.py -n0 -q` | 行为锚定 |
| i18n 新管线全通 | extract→update→compile→check + 三轮 pot/po hash 逐位一致 + `test_catalog_artifacts.py` 等上游机检 | 工具/语义 |
| oracle id 集合含 4 借壳 key | `_catalog_oracle_ids.py` C027 marker 行 + oracle 测试（@CI_LINUX_ONLY，本地不 skip 实跑） | 工具/语义 |
| C013–C017 登录接线 | marker 逐行核对 + auth 定制测试（on_mount anchor 未被 turn 转向/discard 重构绕过） | 语义核对 |
| `/man` 断言融合 | `test_command_suggestions_i18n.py`（上游新增命令行 + 我方 /login /logout 品牌，C024/C086） | 语义核对 |
| 固定关注文件复核（上批次留痕） | test_manual_reload / test_reload_profiles / test_models_dialog_deletion / test_prompt_shortcut_send 的 skip marker 与 reason 前缀逐点核对；**排障单文件单跑口径**（合跑污染上游固有） | 语义核对 |
| AGENTS.md 定制段 | 三段 marker 逐段比对（auto-merge 过≠语义对） | 人工检查 |
| UPGRADE-RUNBOOK「当前基线」更新 | `git diff aixcoding/docs/standards/UPGRADE-RUNBOOK.md` 头部两行（版本→0.35.1、日期） | 人工检查 |
| PR 双 CI 绿 | aixcoding-tests.yml + 上游 ci.yml 桥接（PR checks） | CI 门禁 |
| TUI 冒烟 | headless 冒烟（`aixcoding-cli --version` = 0.35.1）+ F6 aixcoding tab / 登录徽标人工补验（先例） | 手工端到端 |
| 升级决策清单裁决留痕 | verification.md 裁决记录节（追加不删旧） | 流程检查 |

## 异常与降级路径

- **i18n 新机制适配不全**（借壳 key 缺失 / mo 生成链路失效 / 豁免表适配遗漏）→ i18n 全管线
  重走复测 + 上游新增机检（artifacts/build hook/mo 禁跟踪守卫）定位；仍红则**暂停呈报**；
- **auto-merge 语义静默错**（登录接线/徽标特例被 turn 重构吞并）→ 三层兜底：矩阵逐组核对 +
  行为锚定 + 全量；
- **anchor 符号漂移**（ty / 定制测试红）→ 按 design 假设表逐 anchor 核对；适配需触上游文件 →
  停下按 R2 增补报批；
- **上游新增测试与我方定制语义互斥** → 对照实验归因（纯上游 vs fork 树 vs 变量隔离），
  确证后按 R2 函数级/参数级 skip + marker，禁止静默 deselect（C133–C135/C136–C139 先例；
  本批实际命中 = 2 处品牌断言 C143/C144）；
- **--strict 草案窗口期**（评审 F1）：草案带登记表报 1 error 属 fail-closed 预期，签认后收敛，
  勿误判回归；
- **R3 mo 瞬时 error**：UD 未暂存中间态 `git diff base` 仍列出 unmerged 路径，merge add/rm 后
  复跑收敛，非真实违规；
- **POSIX-only 测试盲区**（win32 skip 的测试）→ Windows 仅做收集与静态门禁，实际运行结果以
  Unix PR checks 为准；
- **上游自身回归** → 记录在案随决策清单呈报，不擅自修上游；
- **本机环境性噪音在案（2026-11-09 实证）**：① **WinError 448 junction 策略族 ×31**
  （本机禁遍历装入点，OS 级对照复现，与代码无关，实跑归 PR CI）；② guard symlink ×2
  （升级前基线同失败）；③ mo UD 中间态 ×1（add 后消失）；
  另历史在案：`test_install_scripts.py` 满载 flaky、`test_gc_freeze_calibration` 偶发 flaky。

## 手工验证步骤（若需）

1. `uv run aixcoding-cli`（大写盘符）→ 启动正常，无崩溃告警；
2. F6 → 日志面板出现 aixcoding tab；
3. 顶栏徽标显示登录态；`/login` `/logout` 命令可发现（`/man` 索引含二者）；
4. F4 模型管理入口按 catalog 定制保持禁用（预期行为，非回归）；
5. 新特性抽查：interrupt 卡片 discard turn 入口存在（上游新交互与我方登录接线共存）；
6. 结果记入 verification.md。
