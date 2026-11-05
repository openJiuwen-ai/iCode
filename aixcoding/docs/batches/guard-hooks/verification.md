# 验证：安全护栏（guard hooks）迁入

> 活文档：命令 + 输出摘要（R7 落盘形态），追加不删旧。
>
> **阅读提示**：下方既有节保留方案 X（仓库根 + force-include）阶段的历史证据，已不代表
> 最终实现；以文末「2026-11-05 第二 AI 自审修复后（最终）」节为准。

## 2026-11-05 实施验证（方案 X 最终形态）

环境：Windows 本地，`.venv` 直调（pyproject 变更后 `uv run` 会尝试联网重解析，内网不可达；
锁定版本的 ruff/ty/python 二进制等价）。

| # | 命令 | 结果 |
|---|---|---|
| 1 | `.venv\Scripts\ruff.exe check guard_hooks/` | `All checks passed!`（guard_hooks/ruff.toml 自带配置，含 BLE001 noqa） |
| 2 | `.venv\Scripts\ruff.exe format --check guard_hooks/` | `9 files already formatted`（py39 target，PEP 758 括号 except 不被改写） |
| 3 | `.venv\Scripts\ty.exe check --error-on-warning --python-platform {linux,darwin,win32} src/chrys` | 三平台 `All checks passed!` |
| 4 | `.venv\Scripts\python.exe guard_hooks\selftest.py` | `Ran 63 tests ... OK`（规则正负例自检） |
| 5 | 临时部署（`install_guard_hooks(hooks_dir=%TEMP%/guard_deploy/hooks)`）+ `.venv\Scripts\python.exe guard_hooks\verify_guard_deployed.py`（`AIXCODING_GUARD_HOOKS_DIR` 指向临时部署） | `共 64 条，失败 0 条：[]`（MANUAL_TESTS 全量，Windows；移入规则包资产族后复跑同结果） |
| 6 | `.venv\Scripts\python.exe -m pytest aixcoding/tests/guard -n0 -q` | `19 passed`（自装器 11 + 打包 3 + 锚定 5） |
| 7 | `.venv\Scripts\python.exe -m pytest tests/orchestration/test_startup.py tests/app/cli/test_run.py tests/app/cli/test_acp.py tests/architecture/test_entrypoint_bootstrap.py tests/architecture/test_test_hygiene.py -n0 -q` | `193 passed`（含 guard 19；接线未破坏入口引导/卫生守卫；get_platform monkeypatch 隔离对安装器生效） |
| 8 | `.venv\Scripts\python.exe -m pytest aixcoding/tests -m "not integration and not gc_calibration" -q` | `478 passed, 4 skipped`（定制测试全套） |
| 9 | `.venv\Scripts\ruff.exe check --no-fix src/ tests/ aixcoding/` | `All checks passed!` |
| 10 | `.venv\Scripts\ruff.exe format --check src/ tests/ aixcoding/` | `2878 files already formatted` |
| 11 | `.venv\Scripts\python.exe aixcoding/tools/check_import_boundaries.py` | `OK`（1175 文件） |
| 12 | `.venv\Scripts\python.exe aixcoding/tools/gen_customizations.py --strict` | `check OK — 100 registration row(s) / 137 marker(s) across 11 design(s), 0 warning(s)` |
| 13 | `.venv\Scripts\python.exe aixcoding/tools/check_docs.py` | `OK`（19 ADR / 27 design / 25 self-review） |
| 14 | `check_r2_diff.py --base 8d5a3208`（merge-base origin/main_iCode） | 本批次两文件 `approved`：`pyproject.toml (changed)`、`src/chrys/orchestration/startup.py (changed)`；ERROR 全为本地未跟踪目录（`.codebuddy/`、`制品/`、`util/`），CI checkout 不存在——非批次问题 |
| 15 | `check_r3_markers.py --base 8d5a3208` | 同上（噪声为未跟踪目录）；改动处 marker 均在位（C136/C137） |

### 实施期发现与处置（留档）

- **PEP 758 × py39**：`ruff format`（py314 target）会把 payload 带括号 except 改写为
  PE 758 无括号形态，py<3.14 解释器上为语法错误 → hook fail-closed 全拦。方案 X（仓库根
  + 自带 `ruff.toml` 的 `target-version = "py39"`）消解，payload 字节原样。
- **BLE001**：ruff 0.17 默认规则集含 BLE001；`guard_hooks/ruff.toml` 只写 `extend-select`
  （基础=默认集）故 BLE001 启用，`_guard_core.py` 原 `# noqa: BLE001` 有效（曾误删后恢复）。
- **get_platform 隔离**：`guard_install` 由 `from ... import get_platform` 改为
  `platform_api.get_platform()` 延迟属性访问，使上游 `tests/conftest.py` /
  `test_startup.py` 的 monkeypatch 隔离对安装器生效（否则测试会写真实 config dir）。
- **测试断言适配**：`tests/conftest.py` 会话级 pin `config_dir` 至 `chrys-session-config-*`；
  锚定测试对用户视角路径改用 `conventional_config_dir()`。
- **R13 新增豁免归零**（责任人指示 2026-11-05：无特殊意外不新增豁免）：首轮曾拟对
  `test_guard_install.py`（`RUF003`）与 `verify_guard_deployed.py`（`RUF002/003/T201`）
  加文件级 `# ruff: noqa`——实测前者**本体零命中**（唯一违规是 noqa 注释行自身的全角标点，
  已删）；后者移入 `guard_hooks/`（规则包资产族，与 `selftest.py` 同族）——中文文案与
  print 由 `guard_hooks/ruff.toml` 自带配置承载（`ignore` 含 RUF001/002/003，`extend-select`
  未启用 T20）。**最终零新增豁免、零代码改造**；`ruff check guard_hooks/ aixcoding/tests/guard/`
  全绿、`--ignore-noqa` 复核验证无隐藏命中（除 `_guard_core.py` 原有且有效的 BLE001 noqa）。

### 未在本地执行（归 CI）

- 打包冒烟（`uv build` / `check_wheel.py`：wheel 含 force-include 产物）——CI Build job；
- guard 三平台 job（安装 → selftest → 部署验证）——`aixcoding-tests.yml` 新增；
- 上游全量 CI 分片——PR 门禁。

## 2026-11-05 第二 AI 自审修复后（最终）

环境：Windows，`.venv` 直调，Python 3.14.8；仅本地验证，未执行 Git 写操作。

| # | 命令 | 结果 |
|---|---|---|
| 1 | `.venv\Scripts\ruff.exe check --no-fix src/ tests/ aixcoding/ scripts/...` | `All checks passed!` |
| 2 | `.venv\Scripts\ruff.exe format --check src/ tests/ aixcoding/ scripts/...` | `2895 files already formatted` |
| 3 | `.venv\Scripts\ty.exe check --error-on-warning --python-platform {linux,darwin,win32} src/chrys` | 三平台均 `All checks passed!` |
| 4 | `.venv\Scripts\python.exe -m pytest aixcoding/tests/guard -n0 -q` | `24 passed`（含 flow YAML、MCP 三组、决策写入故障、严格配置、F7 junction/symlink 双入口） |
| 5 | `.venv\Scripts\python.exe -m aixcoding.tests.guard.guard_pack_selftest` | `Ran 70 tests ... OK` |
| 6 | 临时部署 + `verify_guard_deployed.py`（`AIXCODING_GUARD_HOOKS_DIR`） | `共 64 条，失败 0 条：[]` |
| 7 | `.venv\Scripts\python.exe -m pytest aixcoding/tests -m "not integration and not gc_calibration" -q` | `483 passed, 4 skipped` |
| 8 | 上游入口/卫生相关 5 文件 pytest | `175 passed` |
| 9 | 架构守卫（hygiene / layering / bootstrap / action pins） | `270 passed` |
| 10 | `check_import_boundaries.py` | `OK`（1182 文件） |
| 11 | `gen_customizations.py --strict` | `check OK — 100 row(s) / 137 marker(s) / 0 warning(s)` |
| 12 | `check_docs.py` | `OK`（19 ADR / 27 design / 25 self-review） |
| 13 | Python 3.9 语法级 `ast.parse`（payload） | `py39 syntax: OK` |
| 14 | `uv build --wheel --offline` | `Successfully built dist\icode_tui-0.33.0-py3-none-any.whl` |
| 15 | 解包 wheel 到隔离目录并执行 `install_guard_hooks()` | `WHEEL_GUARD_SMOKE_OK`；6 个运行时资产齐全 |
| 16 | F7 定向：紧贴重定向 / `zip` 归档 / `Copy-Item`·`Move-Item` 目的地 / workspace junction | 均命中 `T-SHELL-05`；junction 文件系统入口命中 `T-FS-01` |

### 全量套件环境说明（非本批通过证据）

`verify.py --smart --paths ...` 因包含 `pyproject.toml` 自动升级为完整本地套件；运行到 99%
后由责任人取消，未生成 Summary。失败缓存复跑为 `27 failed / 30 passed`，其中：

- 2 条在 `PYTHONUTF8=1` 下通过（Windows 默认 GBK 读取）；
- 其余集中在 Windows 长路径用例；本机 `LongPathsEnabled=0`，测试主动构造 280 字符路径，
  失败为 `WinError 206/3`，与 guard 文件无交集。

因此本批次**不声明全量本地套件通过**；构建与护栏定向门禁均已独立通过。

### F7 写入范围增强

责任人 2026-11-05 决定从“保持旧逻辑”改为增强拦截。已覆盖并通过：

- 紧贴参数的重定向（`x>file` / `2>file` / `x>>file`）被拆成独立操作符；
- `zip` 检查第一个位置参数（归档目标）；
- `Copy-Item` / `Move-Item` 优先 `-Destination`，否则检查第二个位置参数；
- 范围判定前对 native 路径做 `resolve`，workspace 内 junction/symlink 指向外部时拦截；
- 引号内的 `>` 不误判，workspace 内正常写入仍放行。
