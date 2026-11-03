# 验证：功能品牌与命令切换（branding-and-command / C4）

> 活文档：记录验证证据，随补随更。验证环境：Windows 11 + Python 3.14.8 + uv（大写盘符、
> PYTHONPATH 清空、PYTHONUTF8=1 本地配方）。分支 `feat/branding-and-command`。
> 验证日期：2026-11-03。

## 验收标准对照

| # | 验收标准 | 结果 | 证据 |
|---|---|---|---|
| 1 | 品牌常量、散点、Code/QA、banner、安装提示 | 通过 | `aixcoding/tests/branding/test_branding_runtime.py` 14 点行为锚定全绿；serve/acp help 实跑显示 AIxCoding |
| 2 | pyproject 唯一 scripts 入口 + test_app 断言 | 通过 | `test_aixcoding_cli_is_the_only_command` 通过；`uv run aixcoding-cli --version` = 0.32.2 |
| 3 | check_wheel 三平台、分发名不变 | 本地 Windows 通过；三平台归 CI | 本地 `check_wheel.py`：Installed 1 executable: aixcoding-cli；13 help screens render；包探测通过；`iCode-TUI`/`icode_tui-*` 未变 |
| 4 | build/CD 逐跳名称一致 | 静态契约 + Windows 实跑通过；tagged CD 未实跑 | `test_command_contract.py` 逐跳断言；本地离线构建产出 `dist/aixcoding-cli.exe`（85.9 MB）；CD 为静态证据，未冒称实跑 |
| 5 | install 脚本新包名/alias 全生命周期 | 通过 | `tests/app/test_install_scripts.py` 18 passed / 27 skipped（POSIX 用例 Windows skip，归 Linux CI）；全量套件含该文件零失败 |
| 6 | 离线迁移矩阵 | 通过 | `tests/app/test_installer.py` 新增：旧进程识别、POSIX symlink 清理、冲突时保留、外部旧命令保留+警告、Windows hard link 清理、copy 无法证明归属保留+警告、运行锁保留+警告 |
| 7 | 卸载清理新旧自有 alias、Windows deferred 只处理自有目录 | 通过 | `tests/app/test_uninstaller.py` 新增双清理与外部旧命令保留；Windows 仍整删自有 `chrys\bin`（上游行为未改） |
| 8 | approval 命令文案 + i18n 全链 + Linux oracle | i18n 全链通过；oracle 归 Linux CI | extract→update→translate→compile→check 全绿；hosted 翻译保留 `{app}` 占位匹配源 schema；`test_catalog_oracle` 为 CI_LINUX_ONLY，Windows skipped 不记为通过 |
| 9 | 全量测试 + fork 门禁 + strict | 通过 | 全量 PASSED：tests 33262 passed / 620 skipped + architecture 1237 passed（672s+149s）；`--strict`：94 行登记 / 131 marker，0 error 0 warning |
| 10 | 开发入口清零 + 冒烟 | 通过 | `test_command_contract.py` 扫描 AGENTS/mock 现行配方/UPGRADE-RUNBOOK 无 `uv run icode`；`uv run aixcoding-cli --version` 通过 |
| 11 | 发布说明（迁移与旧脚本限制） | 待 PR 描述承载 | 见下方"发布说明要点"；文档窗口与旧脚本过渡登记为已知限制 |

## 验证执行记录

| 项 | 命令 | 结果 |
|---|---|---|
| 全量测试 | `uv run python scripts/chrys_test.py --full` | Summary: PASSED（tests 33262 / architecture 1237，零失败） |
| 定制测试 | `uv run pytest aixcoding/tests/branding -n0` | 25 passed |
| 工具自身测试 | `uv run pytest aixcoding/tests/tools -n0` | 101 passed |
| 登记对账 | `gen_customizations.py --strict` | 94 rows / 131 markers，0 error 0 warning |
| 文档检查 | `check_docs.py` | OK |
| ruff | `ruff check` + `ruff format --check`（src/tests/aixcoding/scripts） | 全部通过 |
| i18n | `scripts/i18n.py extract/update/compile/check` | 全链通过 |
| wheel 探测 | `uv build` + `check_wheel.py dist/icode_tui-0.32.2-...whl` | 唯一命令 aixcoding-cli、13 help screens、包探测通过 |
| 离线构建 | `build.ps1 -Offline`（LF 临时副本 + 本地缓存，已清理副本） | `dist/aixcoding-cli.exe` 85.9 MB；rg 去重 SyntaxError 为在案非致命噪音 |
| 离线冒烟 | `dist/aixcoding-cli.exe --version`（首跑+warm）、`serve --help`、`acp --help` | 0.32.2；usage 均为 aixcoding-cli、品牌 AIxCoding |
| 升级探测 | 隔离 UV_TOOL_DIR/BIN_DIR：PyPI 旧版 0.32.2 → 强制装本地新 wheel | 旧 `icode.exe` 被移除，仅余 `aixcoding-cli.exe`，`--version` = 0.32.2 |
| 评审修复（F-1/F-2/F-3） | `pytest tests/app/test_installer.py test_uninstaller.py test_install_scripts.py` | 57 passed / 68 skipped；新增 4 例（own 分支迁移、dangling 迁移/卸载、ps1 旧 alias 重装） |

## 实施期工具修复（经责任人裁决，方案 A）

`gen_customizations.py` 的 `EXCLUDED_DIRS` 按任意路径段排除，误伤上游源码目录
`tests/orchestration/engine/build/`，致 C090/C102 永远无法对账。按方案 A 修复：
`build`/`dist`/`target` 改为仅根级排除（`ROOT_EXCLUDED_DIRS` + `_is_excluded()` 共享判定，
scan_markers 与 custom_assets 两处消费点统一），其余名称保持任意段排除。
修后 strict 全绿、工具测试 101 项通过。

## 已知限制与遗留

- **tagged CD 未实跑**：cd.yml 改动为静态契约证据（`test_command_contract.py` 逐跳断言），
  release 收集、SHA256SUMS、CD 安装 smoke 需 tagged 运行时核对，不冒称已跑。
- **三平台 check_wheel / POSIX install 脚本 / Linux catalog oracle**：分别归三平台 CI 与
  Linux CI；Windows skipped 项不记为通过。
- **纯文档窗口**：docs/README 及随包用户指南仍可能写作 iCode/`icode`，归"用户指南文档
  定制化"工作项；程序功能已切换，不属未切换。
- **旧安装脚本过渡**：已保存的旧 install.sh/ps1 不能发现新 release 资产，须重新获取；
  不发布双命名资产。
- **回滚手册**：revert PR 不恢复已迁移机器的旧 alias；回滚须先用新版卸载器处理新
  alias，再重装旧版恢复 `icode`。
- **分发身份二次返工（已知成本）**：若 C5 裁决 `iCode-TUI` 更名，build/CD/install 链将
  因包身份再次调整（ADR-0019 复审登记的已接受成本）。

## PR #87 评审记档（2026-11-03，随对账提交）

- **C1（外部 `icode` 进程名误伤面）**：接受记档，零代码改动。命中动作经代码确认为
  "安装流程提示用户退出运行中的进程"（`_is_chrys_process` → `_find_running_chrys_processes`
  的上游既有防覆盖保护），不杀进程、不删外部文件；该保护对 `chrys`/新命令的外部同名
  程序本就存在，纳入 `icode` 属 design #27 已签认的迁移接线，过渡行为随旧命令退出
  自然消解。
- **C2（verification 数字口径）**：环境差异，非用例选择差异。本机 powershell + pwsh
  双全（`where pwsh` = `C:\Program Files\PowerShell\7\pwsh.exe`），-n0 复跑
  18 passed / 28 skipped；评审副本 9 passed / 37 skipped 为缺 pwsh 环境下 `_SHELLS`
  的 pwsh 参数化用例整体 skip 所致（9 例之差即一组 shell 参数化）。

## 发布说明要点（供 PR 描述与 release notes）

1. 用户规范命令由 `icode` 切换为 `aixcoding-cli`，运行时品牌为 AIxCoding；wheel 仅安装
   新命令，离线制品与 release 资产同步改名。
2. 外部调用方（脚本、ACP 客户端、桌面端）需同步改用 `aixcoding-cli`。
3. 既有离线安装升级后，本产品旧 `icode` alias 在新 alias 可用后自动清理；外部同名
   程序不受影响；无法证明归属或运行中的旧文件保留并警告。
4. 旧版 install.sh/install.ps1 无法发现新 release 资产，请重新获取新版安装脚本。
5. 随包文档旧命令示例的集中切换由后续文档批次处理。
