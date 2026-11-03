# 测试方案：功能品牌与命令切换（branding-and-command / C4）

> 活文档：随时更新、不签认；其质量由双 AI 自审（SELF-REVIEW-HOWTO）与行为锚定测试本身保障。
> 依据：本目录 requirements.md 验收标准 11 条、design.md（已签认 2026-11-03）R2 清单 A–F 节
> 与定制点登记 C050–C131。

## 测试范围与策略

分四层，静态契约不得替代行为验证（design F 节）：

1. **行为锚定（定制层）**：新建 `aixcoding/tests/branding/test_branding_runtime.py`，
   真实调用上游符号断言 AIxCoding / aixcoding-cli 渲染，不 mock 被锚定符号本身
   （DEVELOPMENT.md §5）。覆盖 C050–C062 与 C118 共 14 个源码定制点。
2. **静态契约（定制层）**：新建 `aixcoding/tests/branding/test_command_contract.py`，
   检查 wheel 唯一入口、build/CD/install 新名称链、AGENTS 与现行开发配方新命令，
   以及明确保留的分发名/内部名（负向断言），防止只改其中一跳。
3. **上游测试跟改 + 新增迁移行为用例**：原品牌红面按 design D 节跟改；
   `tests/app/test_install_scripts.py`、`test_installer.py`、`test_uninstaller.py`
   新增迁移/卸载/外部命令保护场景。迁移行为由这些真实测试承担。
4. **端到端（构建与发布链）**：wheel 三平台 check_wheel、Windows 离线 PyApp 构建冒烟、
   release 逐跳核对。PR CI 未真实执行 release 时，verification 区分静态证据与 tagged
   CD 证据，不得冒称已跑（design 验证门禁第 4 条）。

## 覆盖矩阵

| 验收标准（对应 requirements.md） | 测试入口（文件::用例） | 类型（单元/契约/端到端/行为锚定） |
|---|---|---|
| 1. 品牌常量、散点、Code/QA、banner、安装提示 | `aixcoding/tests/branding/test_branding_runtime.py`（C050–C062 逐点锚定：常量值 / DocumentationProvider prefix 与后段 / context_management `_INSTRUCTIONS` / hosted fallback+bind / filesystem read description / skills 模板+provider 两次 format / OTLP ImportError / Code.yaml / QA.yaml / serve banner）；安装提示由 install 脚本跟改用例覆盖 | 行为锚定 |
| 2. pyproject 唯一 scripts 入口 + test_app 断言 | `tests/app/cli/test_app.py`（字典断言 `{APP_COMMAND: ...}` 保留，测试名/docstring 跟改）；`test_command_contract.py` 静态核对 scripts 唯一键 | 单元 + 契约 |
| 3. check_wheel 三平台、分发名不变 | `scripts/check_wheel.py`（CI Build and Install wheel 三平台 job）；`test_command_contract.py` 负向断言 `DISTRIBUTION_NAME="iCode-TUI"` 与 `icode_tui-*` 前缀不变 | 端到端 + 契约 |
| 4. build/CD 逐跳名称一致 | `test_command_contract.py` 静态逐跳（matrix artifact → binary output → collect glob → 包内 binary → smoke）；Windows 本地 `build.ps1 -Offline` 产出 `aixcoding-cli.exe` 冒烟；tagged CD 证据单列 | 契约 + 端到端 |
| 5. install 脚本新包名/alias 全生命周期 | `tests/app/test_install_scripts.py` 跟改矩阵：fake 包名、包内 binary、release URL、alias fixture、冲突、同版本完整性、修复、防降级 | 单元（脚本级） |
| 6. 离线迁移矩阵（symlink/hard link/copy、失败保留、外部保护、无法证明归属警告） | `tests/app/test_installer.py` 新增：旧进程识别（`_CHRYS_PROCESS_BASENAMES` 含 icode）、新 alias 成功后清理、失败时保留、外部旧命令保护、POSIX symlink、Windows hard link/copy、运行中旧 alias；`test_install_scripts.py` 新增"只有旧 alias 触发迁移""外部旧 icode 不阻塞" | 单元（真实文件操作） |
| 7. 卸载清理新旧自有 alias、Windows deferred 只处理自有目录 | `tests/app/test_uninstaller.py` 新增：新旧自有 alias 双清理、外部旧命令保留；iCode-TUI 卸载命令断言不动 | 单元 |
| 8. approval 命令文案 + i18n 全链 + Linux oracle | `aixcoding/tests/branding/test_branding_runtime.py`（C118 approval fallback 锚定）；`tests/app/cli/test_approvals.py` usage 断言跟改；`uv run python scripts/i18n.py check`；`test_catalog_oracle`（CI_LINUX_ONLY，Windows skipped 不记为通过） | 行为锚定 + 端到端（Linux CI） |
| 9. 全量测试 + fork 门禁 + strict | pyproject 变更触发上游全量；fork 本地全量配方（清 PYTHONPATH / PYTHONUTF8=1 / 大写盘符 / deselect flaky 项）；`uv run python aixcoding/tools/gen_customizations.py --strict`；ruff/format/ty、check_docs | 端到端 |
| 10. 开发入口清零 + 冒烟 | `test_command_contract.py` 扫描 AGENTS、mock 现行配方（`__main__.py`、三 server.py、两 README）、UPGRADE-RUNBOOK，断言无指导旧入口的 `uv run icode`；手工 `uv run aixcoding-cli --version` | 契约 + 手工 |
| 11. 发布说明（迁移与旧脚本限制） | PR 描述与发布说明人工核对；verification.md 登记已知限制（文档窗口、旧脚本过渡） | 人工核对 |

## 异常与降级路径

- **新 alias 创建失败**：旧可用 alias 必须保留，不得先删后建（installer 用例：模拟新 alias
  失败，断言旧 alias 仍在）。
- **copy fallback 无法证明归属**：保留并警告，不删除（Windows copy 形态用例）。
- **运行中的 Windows 旧 alias**：不得强删，走 deferred/警告路径。
- **外部同名 `icode`**：不在自有位置或归属证明失败的文件一律保留；外部旧命令不阻塞
  安装、不被卸载器清理（负向用例）。
- **不得扫描 PATH 任意 `icode`**：清理仅限自有位置（POSIX `~/.local/bin/icode`、
  Windows 自有 `chrys\bin\icode.exe`）。
- **skills 模板转义**：两次 `.format()` 均绑定 `app=`，且 `{{skill_script_name}}` 转义
  不被破坏；漏绑一处即模板非法，行为测试覆盖探针与实际两条路径。
- **旧安装脚本失效**：非代码降级路径，由发布说明承载"重新获取脚本"提示；不发布双资产。
- **回滚非机器状态回滚**：revert 不恢复已迁移机器的旧 alias；回滚手册步骤记入
  verification（新版卸载器处理新 alias → 重装旧版恢复 icode）。

## 手工验证步骤（若需）

1. 开发入口冒烟：`uv run aixcoding-cli --version`，预期正常输出版本号。
2. Windows 离线构建：`scripts/build.ps1 -Offline`（注意 CRLF 坑：生成 LF 临时副本执行），
   预期产出 `dist/aixcoding-cli.exe`；完成无网首跑、warm run、`serve --help`、
   `acp --help` 冒烟。
3. wheel 升级探测：先装旧版 wheel（含 `icode`），再升级新版，确认旧 console script
   被移除、仅余 `aixcoding-cli`，全部 help usage 一致——不只测干净安装。
4. release 逐跳：有 tagged CD 运行时核对 matrix artifact、collect glob、tar/zip 内
   binary、SHA256SUMS、安装 smoke；无 tagged 运行时 verification 明确标注"静态证据，
   CD 未实跑"。

以上结果记入 verification.md。
