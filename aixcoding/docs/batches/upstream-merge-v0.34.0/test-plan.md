# 测试方案：追上游 v0.34.0（upstream-merge-v0.34.0）

> 活文档：随时更新、不签认；其质量由双 AI 自审（SELF-REVIEW-HOWTO）与行为锚定测试本身保障。

## 测试范围与策略

升级批次不新增功能代码，测试 = **三层回归 + 流程产物验证**（对应 design「升级决策交付物」与
runbook §3/§4）：

1. **结构层（机检）**：marker 存活 + anchor 行为——`aixcoding/tests` 全套（含
   `diagnostics/test_panel_anchor.py` 行为锚定）+ `gen_customizations.py --strict`
   （98 行/135 marker 基线，新增 marker 按实际增补登记）；
2. **语义层（全量）**：`aixcoding/tools/verify.py --full`（fork-full：定制套件 + 委托上游全量，
   CodeBuddy 环境配方：清 PYTHONPATH、PYTHONUTF8=1、大写盘符）+ ruff/format/ty 三平台 +
   i18n 管线（extract→update→compile→check 二次幂等）；
3. **冲突适配验证**：5 处 merge 冲突逐项复核（screen.py import / mo 重生成 / **registry 融合** /
   oracle 计数 / rollback 测试 C102）+ merge 后验证暴露的新测试冲突（若有）分别处理；
4. **registry 并发专项（本批特有，评审 B1/B2）**：catalog 快照与 reload 交错时序断言
   （B1 窗口回退后下一轮 sync 自愈的终态收敛，裁决口径 2026-11-06 接受+自愈）+
   catalog 快照后紧跟 reload 的收敛性断言（B2：条目不丢失不重复）+ catalog 写盘目录与
   `_user_dir` 一致性核对（B2 核对项①）；
5. **流程产物**：升级决策清单（冲突点+处理意见、定制化改变清单、淘汰检查逐组结论，
   重点 C030/C031 〔上游等价〕对比分析）经责任人裁决留痕于 `verification.md`；
   CI 证据归属 PR checks 本身（§7）。

## 覆盖矩阵

| 验收标准（requirements.md） | 测试入口 | 类型 |
|---|---|---|
| merge 后 `--strict` 0 error/0 warning，在册定制点无丢失 | `gen_customizations.py --strict`（98 行/135 marker 基线） | 工具/结构 |
| runbook §4 矩阵全绿 | `verify.py --full` + 定制四门（aixcoding/tests / check_docs / check_import_boundaries / --strict）+ R2/R3 以新 merge-base（05679bec）+ ty 三平台 + ruff/format | 全量回归 |
| 行为锚定测试绿 | `uv run pytest aixcoding/tests/diagnostics/test_panel_anchor.py -n0 -q` | 行为锚定 |
| oracle 计数实测 2705 | oracle 断言等价物（pot/po/有效条目集合 == EXPECTED，@CI_LINUX_ONLY 在 Windows 跳过故手动执行） | 工具/语义 |
| i18n 幂等 | extract/update/compile/check 第二轮，pot/po/mo 三文件 hash 前后一致 | 工具/语义 |
| registry 融合正确性 | C030/C031 融合后 catalog 契约测试（aixcoding/tests catalog 域）+ 上游 reload 测试（21dee050 配套）双跑 | 语义/并发 |
| B1 窗口自愈 | 交错时序断言：`install_snapshot` 落 `reload_base`→`install_reload` 窗口 → 下轮 sync 整替终态收敛 | 语义/并发 |
| B2 收敛性 | catalog 快照后紧跟 reload，终态条目集不丢失不重复；写盘目录一致性核对 | 语义/并发 |
| C013–C017 登录接线 | marker 逐行核对 + auth 定制测试（on_mount anchor 未被 reload 重构绕过） | 语义核对 |
| `/man` 断言融合 | `test_command_suggestions_i18n.py`（上游新增命令行 + 我方 /login /logout 品牌，C024/C086） | 语义核对 |
| AGENTS.md 定制段 | 三段 marker 逐段比对（auto-merge 过≠语义对） | 人工检查 |
| UPGRADE-RUNBOOK「当前基线」更新 | `git diff aixcoding/docs/standards/UPGRADE-RUNBOOK.md` 头部两行（版本→0.34.0、日期） | 人工检查 |
| PR 双 CI 绿 | aixcoding-tests.yml + 上游 ci.yml 桥接（PR checks） | CI 门禁 |
| TUI 冒烟 | headless 冒烟（`aixcoding-cli --version` 等）+ F6 aixcoding tab / 登录徽标人工补验（v0.33.0 批次先例） | 手工端到端 |
| 升级决策清单裁决留痕 | verification.md 裁决记录节（追加不删旧） | 流程检查 |

## 异常与降级路径

- **registry 融合并发错**（锁域遗漏 / revision 不一致 / 交错断言红）→ 按融合方案逐条复查；
  无法收敛则**暂停呈报**；
- **借壳 key 丢失**（oracle ≠ 2705）→ i18n 管线重走复测；仍缺 = 上游 msg 定义处变更 → **暂停呈报**；
- **anchor 符号漂移**（ty / 定制测试红）→ 按 design 假设表逐 anchor 核对；适配需触上游文件 →
  停下按 R2 增补报批；
- **auto-merge 语义静默错** → 三层兜底：98 行矩阵逐组核对 + 行为锚定 + 全量；
- **上游新增测试与我方定制语义互斥** → 对照实验归因（纯上游 vs fork 树 vs 变量隔离），
  确证后按 R2 函数级/参数级 skip + marker，禁止静默 deselect（v0.33.0 批次 C133–C135 先例）；
- **POSIX-only 测试盲区**（win32 skip 的测试）→ Windows 仅做收集与静态门禁，
  收集成功不等于参数级 skip 生效；实际运行结果以 Unix PR checks 为准；
- **上游自身回归** → 记录在案随决策清单呈报，不擅自修上游；
- **已知环境性噪音在案**：`tests/app/test_install_scripts.py` 满载 flaky（verify.py 不透传 deselect，
  若出现单独重跑确认）；`test_gc_freeze_calibration` 满载偶发 flaky。

## 手工验证步骤（若需）

1. `uv run aixcoding-cli`（大写盘符）→ 启动正常，无崩溃告警；
2. F6 → 日志面板出现 aixcoding tab；
3. 顶栏徽标显示登录态；`/login` `/logout` 命令可发现（`/man` 索引含二者）；
4. F4 模型管理入口按 catalog 定制保持禁用（预期行为，非回归）；
5. 手工编辑 settings（live-reload 新特性）后 catalog 条目仍在（B1/B2 收敛性人工补验）；
6. 结果记入 verification.md。
