# 验证记录：catalog 只读行为正向测试覆盖盘点与补缺（catalog-readonly-coverage）

> 活文档：追加不删旧；命令真实执行，输出摘要为证。

## 2026-11-05 实施验证（Windows 11，本地）

> 配方：PowerShell 会话 `$env:PYTHONPATH=$null; $env:PYTHONUTF8='1'`（清 genie shim 注入 +
> UTF-8），仓库大写盘符路径；命令经 `uv run`。

### 1. 定向：`aixcoding/tests/catalog/test_read_only_gate.py`

```
8 passed in 1.66s
```

= 既有 3（idle 恒只读 / notice 常量 / F4 BINDINGS）+ G1 参数化 4（agent-running /
execution-busy / submit-active / all-three）+ G2（指示器 configure 入口）。

实施波折记录：首跑 6 failed——`class _Run: agent_running = agent_running` 触发
`NameError`（Python 类体不构成闭包，类作用域无法解析外层函数局部变量；既有 T1 用字面量
`False` 故未踩过）。修复：类体字面量占位 + 类定义后赋值（`_Run.agent_running = ...` /
`_Submit.active = ...`）。修复后全绿。

### 2. 全定制套件：`aixcoding/tests -n0 -q`

```
459 passed, 4 skipped in 57.26s
```

4 skipped 归因（实施轮评审 S1 修正，原记"mock 7778 端口族"有误）：
`branding/test_command_migration.py:15/:32` ×2（`skipif(sys.platform == "win32")`——
POSIX symlink 语义）；`mock/test_model_catalog_server.py:268/:281` ×2（consumer 契约
skipif——条件锚定旧模块路径 `chrys.service.profiles.models.catalog`，而 consumer 实际
已由 catalog-sync 迁至 `chrys.service/aixcoding/catalog.py`，条件未随迁更新属 mock
批次存量遗留，见 self-review 附加观察）。较批次前基线净增 5 用例
（459 = 454 + G1×4 + G2，idle/常量/F4 三用例为重构非新增）。

### 3. ruff

```
ruff check  aixcoding/tests/catalog/test_read_only_gate.py  → All checks passed!
ruff format --check 同文件                                   → 1 file already formatted
```

### 4. 定制点对账：`gen_customizations.py --strict`

```
check OK — 98 registration row(s) / 135 marker(s) across 10 design(s), 0 retired id(s), 0 warning(s)
```

与批次前基线完全一致（零 marker 改动，符合 design 登记预告"不适用"结论）。

### 5. `check_docs.py`

```
scanned 19 ADR(s), 26 design(s), 24 self-review(s) — OK
```

## 2026-11-05 变异验证（design"变异验证授权边界"节执行，临时修改已全部还原）

### V1：`read_only=True` → `read_only=not self._config_read_only()`（判定取反形态）

| 步骤 | 证据 |
|---|---|
| 变异应用 | `config_actions.py:285` 实参单行替换（marker 注释行未动） |
| 红面 | `4 failed, 4 passed`——FAILED 恰为 G1 四个 busy case（`assert False is True` 于 `_read_only`）；T1 idle / G2 / T2 / T3 保持绿（取反形态下 idle 仍只读，证明 busy 用例**独有**检出力） |
| 还原 | 实参改回 `read_only=True` |
| 复绿 | `8 passed in 1.12s` |
| 零残留 | `git diff --stat src/chrys/app/tui/screens/main/config_actions.py` 无输出；`git status --short` 同文件无输出 |

### V2：删除 `case "configure": self.open_model_config()` 分支

| 步骤 | 证据 |
|---|---|
| 变异应用 | `config_actions.py:163-165` 三行中删两行（match/case 结构保留） |
| 红面 | `1 failed, 7 passed`——FAILED 恰为 G2（`assert captured` 失败于空列表：路由断链后 `open_model_config` 未被调用）；其余 7 用例不受影响 |
| 还原 | 分支两行原样恢复 |
| 复绿 | `8 passed in 1.13s` |
| 零残留 | 同 V1 双检查均无输出 |

结论：V1/V2 均按预期检出且互不误伤；变异操作未超出授权清单（仅 config_actions.py 两处），
全部还原并有零残留证明。

## 2026-11-05 交付前自查：CI govern 门禁本地全量预跑（用户指令补齐）

> 自查发现 test-plan 验证清单漏列 CI govern job 的三项门禁（教训同 auth-logging 批次
> RUF001 事件：CI 各门禁命令须本地逐条全量预跑），补齐如下（与 `.github/workflows/
> aixcoding-tests.yml` govern job 逐条同命令）：

| CI 门禁命令 | 本地结果 |
|---|---|
| `uv run ruff check aixcoding/` | All checks passed! |
| `uv run ruff format --check aixcoding/` | 247 files already formatted |
| `uv run python aixcoding/tools/check_docs.py` | OK（另见上文第 5 节） |
| `uv run python aixcoding/tools/check_import_boundaries.py` | OK（scanned 1170 files） |
| `uv run python aixcoding/tools/gen_customizations.py --strict` | 98 行 / 135 marker / 0 warning（另见上文第 4 节） |
| `check_r2_diff.py --base 8d5a3208`（= `merge-base origin/main_iCode HEAD`，CI 同款） | OK——upstream_changed=80 全为存量已批（approved_pool=83），本批次零新增上游改动；branding 批次 R2 表头解析 warn 为存量在案噪音 |
| `check_r3_markers.py --base 8d5a3208` | OK——marker_checked=76 / exempt=4，与升级批次基线一致 |

test job 双 OS 矩阵说明：本地已跑 Windows 全量（459 passed / 4 skipped，含 CI `-m` 过滤
的超集）；ubuntu 腿——新增 G1/G2 为纯 stub + asyncio 构造（ModelConfigScreen 仅构造不
挂载），无平台依赖，预期双绿；余 2 项 skip 中 POSIX symlink 族属 branding 既有行为。

其他自查项：ROADMAP 无本批次关联条目（治理批次不强制登记，INDEX 已登记）；交付物清单
核对无缺（六件套文档 + 测试 + INDEX 行）；CI test job 的 `-m "not integration and not
gc_calibration"` 过滤子集 ⊆ 本地已跑全量。
