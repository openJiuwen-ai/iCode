# 自评审记录：功能品牌与命令切换（branding-and-command / C4）

<!--
签认规则（R10 · 记录档）：责任人本人编辑本头部（状态→已确认、填日期，确认意见可选），AI 不得代填。
签认一次后，后续追加评审记录不触发重签。
双 AI 规则：评审 AI 必须与主力开发 AI 不同（不同工具或不同模型），如实记录标识。
完整操作见 SELF-REVIEW-HOWTO.md。
-->

- 责任人：周彬
- 状态：已确认
- 确认日期：2026-11-03
- 确认意见：无特别意见
- 开发 AI：Kimi-K3.1-Pro（CodeBuddy）
- 评审 AI：GLM-5.3（CodeBuddy，独立评审模式）

## 2026-11-03 开发 AI 自查（实施完成，外部评审前）

### 范围核对

- 改动全部落在 design 已签认 R2 清单 A–F 节内；定制点登记 C050–C131 与实跑红面一致，
  `--strict` 94 行 / 131 marker 全绿。
- 范围外未触碰：`DISTRIBUTION_NAME`、wheel 前缀、内部 `chrys` 名、仓库 slug、
  `ICODE_*` 环境变量、docs/README 纯文档、历史批次记录、`_FAKE_ICODE`/`icode_env`/任意
  argv0 fixture 均保持原样。
- 新增唯一文件 `src/chrys/kernel/aixcoding/command_migration.py`（kernel 最低依赖档，
  ADR-0011），stdlib-only，无新第三方依赖（R11 无涉）、无新环境变量。

### 实施期偏差与处置

1. **C116 挂接形态**：design 写"调用点"单处，实施发现 alias 创建有 Windows/POSIX 两条
   路径，改为在 `_install_command_alias` 内两个成功返回点统一调用（提前返回与末尾），
   同一 marker 计数 1，与登记一致；语义"新 alias 成功后才清理"在两个分支均成立。
2. **C102 锚点落点**：design 锚"hosted warning bound args"正主是 569 行 bind 参数断言
   （补 `app=AIxCoding`）；309/319 的异常构造文本为同语义配套跟改。
3. **marker 计数自纠**：C125-C127 初放 7 个 marker，strict 计数报错后按"每组首测试
   1 个"收敛为 3 个；C083 同函数重复 marker 去除 1 个。
4. **i18n 翻译 schema**：hosted 中文翻译初改字面 AIxCoding 被 check 拦截（源 placeholders
   为 `["app","tools"]`），改回 `{app}` 占位后全链通过——翻译不硬编码品牌。
5. **工具修复（责任人裁决方案 A）**：`gen_customizations.py` 排除规则误伤
   `tests/orchestration/engine/build/`，已修并验证（见 verification.md 对应节）。

### 风险点自评

- **删除安全**：`remove_legacy_alias` 仅查 binary 同目录自有位置、归属证明失败即保留+
  警告、unlink 失败保留+警告，不扫描 PATH；负向用例覆盖外部文件、copy fallback、
  运行锁。
- **公开命令切断**：升级为唯一命令是 design 已签认的明确破坏点；升级探测实证旧
  console script 被 uv 移除，发布说明要点已备。
- **静态证据边界**：CD/三平台/Linux oracle 未实跑项在 verification.md 明确归 CI，
  未冒称通过。

### 待外部评审关注点

- cd.yml 逐跳改名的完整性（matrix→binary output→collect→包内→smoke）。
- `_install_command_alias` 两个成功返回点均调迁移助手是否完备（含 own/已存在分支）。
- 新增迁移测试矩阵是否有语义缺口（如 POSIX dangling legacy symlink 场景）。

## 2026-11-03 评审结论：无 blocker，处置完成待增量复审

- 轮次：第 1 轮（GLM 独立评审，12 项检查全过，findings 5 项：suggestion×2、nit×3）。
- F-1（suggestion，own/已存在分支迁移无锚定）→ **修**：补
  `test_posix_install_migrates_when_the_new_alias_already_exists`（test_installer.py）。
- F-2（suggestion，dangling legacy symlink 无红面）→ **修**：补迁移侧
  `test_posix_install_cleans_up_a_legacy_symlink_left_dangling_by_a_removed_binary` 与卸载侧
  `test_offline_uninstall_spares_a_dangling_legacy_symlink`（readlink 不匹配即保留）。
- F-3（nit，ps1 缺对称正测试）→ **修**：补
  `test_install_ps1_installs_when_only_the_legacy_icode_alias_exists`。
- F-4（nit，工具修复留痕链）→ **零代码行动**：责任人已确认 verification.md"实施期
  工具修复（方案 A）"裁决记录（2026-11-03"按你的建议处理"）。
- F-5（nit，临时名/注释/分发名指称品牌残留）→ **不修**（责任人拍板）：属 design 非目标，
  随 C5 分发身份裁决一并清理。
- 修复验证：`tests/app/test_installer.py + test_uninstaller.py + test_install_scripts.py`
  57 passed / 68 skipped；ruff check/format 绿；`--strict` 94 行 / 131 marker 全绿；
  check_docs OK。新增用例归既有登记组，未新增 marker。
- 待办：GLM 增量复审（仅复审 4 例新增测试 diff）通过后转"通过"。

## 2026-11-03 第 2 轮复审与例 2 处置

- GLM 复审结论：F-1、F-2 卸载侧、F-3 闭合；例 2（F-2 迁移侧集成用例）**锚定不成立**
  （断言时刻 legacy 已复活为活链接，与既有 own-alias 用例等价），未收敛。
- 处置（按 GLM 建议①）：删除该冗余集成用例，改新增
  `aixcoding/tests/branding/test_command_migration.py` 直测 `remove_legacy_alias` 两例——
  binary 不存在时 readlink 匹配即清理（真正锁定 readlink-only，加 `exists()` 前置即红）、
  异向 dangling 保留+警告。
- 验证：`aixcoding/tests/branding + test_installer + test_uninstaller` 64 passed / 41 skipped；
  ruff 绿；`--strict` 94 行 / 131 marker 不变。直测 2 例 Windows skip（POSIX symlink 语义），
  行为证据归 POSIX CI 腿，与"Windows skipped 不记为通过"口径一致。
- 待办：GLM 对例 2 替换单测做单点核对后收敛。

## 2026-11-03 评审结论：通过（收敛）

- 轮次：共 3 轮（第 1 轮全批评审 → 第 2 轮增量复审 → 第 3 轮例 2 替换单点核对）。
- GLM 第 3 轮结论：**收敛**——F-1、F-2 迁移侧（直测真正锁定 readlink-only）、F-2 卸载侧、
  F-3 全部闭合；F-4 裁决留痕已确认；F-5 拍板不修。全部 findings 处置闭环，无需再轮评审。
- blocker 处置：全程无 blocker。
- 未修项（次要问题，责任人拍板不修）：F-5（scripts/build/cd 中临时名、注释、分发名指称
  的品牌残留）——属 design 非目标与"不机械替换"口径，随 C5 分发身份裁决一并清理。
- 合并前提醒（GLM 附带核对，非 finding）：4 例 POSIX 行为用例（own 分支迁移、dangling
  迁移/卸载、ps1 旧 alias 重装）及 2 例 readlink 直测在 Windows 均 skip，行为证据以
  POSIX CI 腿通过为准（"Windows skipped 不记为通过"）。

## 2026-11-03 PR #87 评审处置（合并后对账提交）

- PR #87 独立评审（R2/R11 全清单内、无幻觉 API、3 组破坏性实验佐证）提出 2 候选，
  无 blocker：C1 外部 icode 进程名误伤面、C2 verification 数字口径。
- 责任人裁决：不为两候选单独追加 commit/CI，随合并后对账文档提交记档——C1 接受记档
  （命中动作=提示退出，与上游同名保护同构，design #27 已签认）；C2 环境差异
  （评审副本缺 pwsh），verification 补口径注记。详见 verification.md"PR #87 评审记档"。

## 2026-11-03 CI 门禁本地预跑（PR #70 教训逐项预跑）

- `check_r2_diff --base origin/main_aixcoding_cli`：首跑 16 ERROR——D 节 R2 表 #32-45 合并行
  与 #46 简写路径不被池解析器识别（文件本在签认清单内，纯表格格式问题）。已补全为完整
  路径（+15/-2，语义无变化，design 重签由责任人执行），复跑 **OK**（池 80，48 上游文件
  全 approved）。
- `check_r3_markers --base origin/main_aixcoding_cli`：**OK**（48 文件，marker_checked=45）。
- `check_import_boundaries`：首跑 1 违规——test_command_contract.py 误 import 上游
  `tests.support.paths`，已改为本地 `Path(__file__).parents[3]` 推导仓库根，复跑 **OK**
  （1140 files）；定制测试 25 passed 复验。
