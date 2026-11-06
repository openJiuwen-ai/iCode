# 计划：安全护栏运行期开关

> 活文档，持续维护。AI 每次接手先读本文件的状态与挂起事项。冲突逐项明细见
> `merge-conflicts.md`，方案见 `design.md`（已签认 2026-11-05）。

## 状态

- 阶段：**已实施**（代码与文档随 `98b30006` 落地；PR #98 审阅中，POSIX cwd 修复
  `b7de350b`、peer review 处置 `bd9c9439`）
- 前置：`batches/guard-hooks` 已签认并已实施（commit `87934228`）
- 挂起：PR CI 上游 `ci.yml` 全量分片与 peer review F-R4/F-R5 裁决（见下）

## 任务清单

| # | 任务 | 落点 | 状态 |
|---|---|---|---|
| 1 | 文档管线（requirements / design / test-plan / plan / adr-0001）+ INDEX 登记 | `aixcoding/docs/batches/guard-switches/` | 完成（design 已签认） |
| 2 | `_guard_core.py` 回移状态三段并合成严格校验（C2/C3） | `src/chrys/kernel/aixcoding/guard/pack/_guard_core.py` | 完成（98b30006） |
| 3 | 移植 `guardctl.py`（改文案 / ty 忽略 / env） | `.../pack/guardctl.py` | 完成（98b30006） |
| 4 | 移植 `guardctl.cmd`、替换 `_launcher.ps1`（绝对路径） | `.../pack/` | 完成（98b30006） |
| 5 | `rules.json` 改 `allow_outside_workspace: true`（C1/ADR-0001） | `.../pack/rules.json` | 完成（98b30006） |
| 6 | `pack/.gitignore` 忽略 `guard_state.json` | `.../pack/.gitignore` | 完成（98b30006） |
| 7 | 安装器 payload 8 文件 | `.../guard/guard_install.py` | 完成（98b30006） |
| 8 | 手工安装器 payload 8 文件 | `aixcoding/tools/guard_install_merge.py` | 完成（98b30006） |
| 9 | 自检搬 25 条用例 + 4 个 helper 形参 + 3 个写范围方法前置 | `aixcoding/tests/guard/guard_pack_selftest.py` | 完成（98b30006） |
| 10 | 安装器测试 +2 条 | `aixcoding/tests/guard/test_guard_install.py` | 完成（98b30006） |
| 11 | 打包测试 +2 个文件名 | `aixcoding/tests/guard/test_packaging.py` | 完成（98b30006） |
| 12 | 部署验证写限制态状态文件 | `aixcoding/tests/guard/verify_guard_deployed.py` | 完成（98b30006） |
| 13 | `.gitattributes` +1 行（C142，R2）；CI guard job +`PYTHONUTF8` | 仓库根 / `.github/workflows/aixcoding-tests.yml` | 完成（98b30006） |
| 14 | 文档：`SWITCHES.md` 新增；`README`/`OPERATIONS`/`PORTING`/`MANUAL_TESTS` 增补 | `.../rule-pack/` | 完成（98b30006） |
| 15 | 验证：自检 + 部署验证 + `chrys_test --smart` + ruff/format/ty + `check_docs` + `gen_customizations --strict` | — | 完成（self-review/verification 留痕） |
| 16 | 双 AI 自审 → `self-review.md` → 责任人签认 | 本目录 | 完成（签认 2026-11-05） |

## 续接入口

- 设计要点：`design.md`「方案概述」1–6 条；
- 逐项冲突与选值影响：`merge-conflicts.md`（C1–C6 / S1–S14）；
- 同事交付来源：`D:\project\icode_new\guard_changes_20261105_v2`（v2，20 文件）；
- 不可回退清单：`merge-conflicts.md` §5（10 项加固）。

## 挂起事项

- `guardctl.py` 在包内被导入时会二次加载 `_guard_core`（第二个模块对象）。
  当前只读常量，风险可接受；若后续需要共享类型注解，改为绝对导入并复跑自检。
- 同事 v2 的 `util/verify_guard_deployed.py` 未交付，本批次以我方部署验证脚本为准；
  其 MANUAL_TESTS 用例数口径（105/106）与我方（64 自动 + 16 手工）不一致，不做对齐。
