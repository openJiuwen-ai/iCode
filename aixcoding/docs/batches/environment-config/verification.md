# 验证记录：定制环境配置与上游 Settings 解耦（environment-config）

> 活文档（不签认）。交付证据（R7）：实际执行的命令与输出摘要。每次验证追加一节，不删旧记录。

## 2026-11-09 聚焦行为与上游 Settings

命令：

```text
uv run python -m pytest aixcoding/tests/config aixcoding/tests/diagnostics aixcoding/tests/auth/test_auth_environments.py aixcoding/tests/catalog/test_catalog_sync.py -n0
uv run python -m pytest tests/foundation/config/test_process_settings.py tests/foundation/config/test_settings.py tests/app/test_settings_writers.py tests/foundation/i18n/test_catalog_oracle.py -n0
```

结果：

- 聚焦定制行为：**127 passed**。
- 上游 Settings/process projection/writers/i18n：**2774 passed, 12 skipped**。
- 初次使用 `uv run pytest` 时入口启动器解析到旧 `iCode-fork`；改用当前解释器 `uv run python -m pytest` 后进入真实用例，未重装依赖。

## 2026-11-09 登记、文档、生成物与静态门禁

命令：

```text
uv run python aixcoding/tools/gen_customizations.py --strict
uv run python aixcoding/tools/check_docs.py
uv run python -m tests.support.trajectory_wait_inventory
uv run python scripts/i18n.py extract
uv run python scripts/i18n.py update
uv run python scripts/i18n.py compile
uv run python scripts/i18n.py check
uv run ruff check --no-fix <全部受影响 Python 文件>
uv run ruff format --check <全部受影响 Python 文件>
uv run ty check --error-on-warning --python-platform linux src/chrys
uv run ty check --error-on-warning --python-platform darwin src/chrys
uv run ty check --error-on-warning --python-platform win32 src/chrys
git diff --check
```

结果：

- strict：**100 registration rows / 137 markers / 5 retired ids / 0 warning**；C032/C033/C039/C042/C043 退役，C004/C034 保留。
- `check_docs.py`：23 ADR / 32 design / 29 self-review，**OK**。
- wait manifest 仅更新 `chrys.service.aixcoding.models.catalog` 模块键及 wrapper target，无规则表漂移。
- i18n check 通过；当前脚本无 `translate` 子命令，实际执行 extract/update/compile/check。
- ruff：**All checks passed**；format：**51 files already formatted**；三平台 ty 均 **All checks passed**。
- `git diff --check` 通过，仅提示既有 autocrlf 的 LF→CRLF 警告。

## 2026-11-09 架构测试

命令：

```text
uv run python -m pytest tests/architecture -n0
```

结果：**1248 passed, 3 skipped**。tier 依赖、lazy import、wait manifest 等全绿。

## 2026-11-09 完整定制套件与上游 Smart Test

命令：

```text
uv run python aixcoding/tools/verify.py --smart --paths <本批次全部改动路径，含删除路径、新旧模块及生成物>
```

第一次结果（原始 PATH）：定制 **2 failed, 558 passed, 51 skipped**；上游
**31 failed, 34804 passed, 734 skipped**，architecture 因失败未运行。单独复现非 junction 的
`test_injection_abandoned_when_agent_finishes_first`，根因是 PATH 中
`C:\Users\Zhoubin\AppData\Local\Author Software\nvm\\.nodejs` 不可遍历，触发 WinError 448。

第二次结果（仅在该测试进程 PATH 中删除上述失效条目）：定制仍为 **2 failed, 558 passed, 51 skipped**；
上游降为 **5 failed, 34830 passed, 734 skipped**，architecture 因失败未运行（后已独立全绿）。

剩余失败均与 junction 行为有关，本批次未修改对应实现：

- 定制：`test_workspace_symlink_cannot_escape_shell_write_scope`、
  `test_workspace_symlink_cannot_escape_filesystem_write_scope`。
- 上游：`test_the_config_dir_is_the_real_folder_behind_a_link[home by a junction]`、
  `test_a_junctioned_data_folder_is_described_as_only_the_link`、
  `test_default_sessions_link_keeps_ownership_under_a_different_name[junction]`、
  `test_a_workflow_folder_that_is_a_link_is_reported_unresolved[junction]`、
  `test_a_link_whose_target_is_gone_is_not_a_deleted_file[junction]`。

结论：本批次受影响行为、上游 Settings 与架构证据通过；**完整 verify 不宣称全绿**，上述 7 个本机
junction 环境失败在案。过滤 PATH 后，原先非 junction 的 26 个失败全部消失。

## 2026-11-09 手工 mock 验证

操作与中间结果：

- 在独立可见 cmd 窗口启动 `uv run python -m aixcoding.mock`；`Test-NetConnection 127.0.0.1 -Port 7777`
  返回 **True**。
- 在独立可见 cmd 窗口以 `set "AIXCODING_ENVIRONMENT=local" && uv run aixcoding-cli` 启动环境变量路径 TUI，
  已核对实际窗口命令行，进程未立即退出。
- 在第二个独立可见 cmd 窗口清空 `AIXCODING_ENVIRONMENT` / `AIXCODING_MODEL_CATALOG_URL` /
  `AIXCODING_AUTH_SERVER_URL`，以隔离 APPDATA 中仅含 `environment="local"` 与
  `[model_catalog].base_url="http://127.0.0.1:7777/csas"` 的 TOML 启动 TUI；未改真实用户配置。

待责任人确认：两个窗口分别完成 mock 登录，模型目录同步成功，F4 保持禁用，F6 与文件日志行为正常。
确认后补充实际结果并清理隔离 APPDATA。

## 2026-11-09 仅 TOML 修正与最终手工结果

首次仅 TOML 窗口的隔离配置把 base 误写为 `http://127.0.0.1:7777/csas`，日志明确记录 mock
返回 HTTP 404；修正验证配置为 `http://127.0.0.1:7777` 并增加 `[logging] level="DEBUG" panel=true` 后，
关闭并重启该窗口以清理进程缓存。

最终结果：

- 恢复本机登录成功；模型目录请求返回 **HTTP 200、1 个模型**，详情日志记录版本与 `GPT-4o mini`。
- `models/openai-gpt-4o-mini.yaml` 已在隔离 APPDATA 实际落盘，证明同步与发布成功，无需依赖额外界面变化。
- 文件日志位置为 `<隔离 APPDATA>/chrys/aixcoding-logs/aixcoding.log` 与 `aixcoding.detail.log`，已直接核查。
- 责任人确认：第一个窗口全部通过；第二个窗口登录成功、F4 禁用、F6 正常。结合上述日志与落盘证据，
  **环境变量与仅 TOML 两条手工路径均通过**。
- 两个 TUI 与 mock 进程已关闭，隔离 APPDATA 随后清理，不进入提交。

## 2026-11-09 GLM-5.3 第一轮实现复审处置

处置范围：补齐 mock 当前配方与支持文档，修正 ROADMAP ADR 状态和 test-plan 用例名；补 base 尾斜杠用例；
恢复启动接线注释；非法 environment WARNING 不再包含配置值；统一 diagnostics 空输入构造路径。

命令：

```text
uv run python -m pytest aixcoding/tests/config aixcoding/tests/diagnostics aixcoding/tests/auth/test_auth_environments.py aixcoding/tests/catalog/test_catalog_sync.py aixcoding/tests/mock -n0
uv run ruff check --no-fix src/chrys/service/aixcoding/config aixcoding/tests/catalog/test_catalog_sync.py aixcoding/tests/diagnostics/test_entry_wiring.py aixcoding/mock
uv run ruff format --check src/chrys/service/aixcoding/config aixcoding/tests/catalog/test_catalog_sync.py aixcoding/tests/diagnostics/test_entry_wiring.py aixcoding/mock
uv run python aixcoding/tools/check_docs.py
uv run python aixcoding/tools/gen_customizations.py --strict
```

结果：**215 passed, 2 skipped**；ruff 全绿，20 files formatted；check_docs OK；strict 100 行 /
137 marker / retired=5 / 0 warning。mock 树旧环境变量与旧 Settings 键检索均为 0。

## 2026-11-09 提交前深度复核补强

前节“mock 树检索为 0”的结论不完整：复核发现 README 逃生门、launcher、update mock 和模型目录注释仍有
失效旧变量配方；同时发现 env/TOML 非法字符串覆盖及三方 drift 断言未完全兑现 test-plan。本节追加纠正，
不删改历史记录。

补强内容：清理全部现行 mock 旧配方并明确 update 尚无产品消费方；非法 environment 覆盖扩为
2 来源 × 2 值；drift 纳入 loader `VALID_ENVIRONMENTS`；同步活文档状态。

命令：

```text
uv run python -m pytest aixcoding/tests/config aixcoding/tests/diagnostics aixcoding/tests/auth/test_auth_environments.py aixcoding/tests/catalog/test_catalog_sync.py aixcoding/tests/mock -n0
uv run ruff check --no-fix aixcoding/tests/config/test_config.py aixcoding/tests/catalog/test_catalog_sync.py aixcoding/mock
uv run ruff format --check aixcoding/tests/config/test_config.py aixcoding/tests/catalog/test_catalog_sync.py aixcoding/mock
uv run python scripts/i18n.py check
uv run python -m tests.support.trajectory_wait_inventory
uv run python aixcoding/tools/gen_customizations.py --strict
uv run python aixcoding/tools/check_docs.py
git diff --check
```

结果：**217 passed, 2 skipped**；ruff All checks passed，17 files formatted；i18n check 通过；wait manifest
**2860 waits / 0 reopened**；strict **100 行 / 137 marker / retired=5 / 0 warning**；check_docs OK；
diff check 通过（仅既有 autocrlf 提示）。`aixcoding/mock/` 与 `src/` 的旧部署变量、旧模型目录变量、
旧 Settings 键和旧 catalog 模块路径检索均为 0。上述新增改动待 GLM-5.3 增量复审。

## 2026-11-09 PR #103 peer review C1 补测

PR peer review（head `f4dbf024`，26 checks 全绿）确认唯一缺陷候选 C1：旧
`test_bad_level_name_falls_back` 在 diagnostics 旧文件删除时未迁入新 config 套件，非法 level 名
回退失去测试锚定。责任人裁决本批补测，不登记技术债。

命令：

```text
uv run python -m pytest aixcoding/tests/config aixcoding/tests/diagnostics aixcoding/tests/auth/test_auth_environments.py aixcoding/tests/catalog/test_catalog_sync.py aixcoding/tests/mock -n0
uv run ruff check --no-fix aixcoding/tests/config/test_config.py
uv run ruff format --check aixcoding/tests/config/test_config.py
uv run python aixcoding/tools/check_docs.py
uv run python aixcoding/tools/gen_customizations.py --strict
git diff --check
```

结果：**218 passed, 2 skipped**；ruff All checks passed，1 file already formatted；check_docs OK；
strict **100 行 / 137 marker / retired=5 / 0 warning**；diff check 通过（仅既有 autocrlf 提示）。
