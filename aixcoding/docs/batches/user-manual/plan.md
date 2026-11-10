# 任务清单：定制版用户手册入库与安装版捆绑（user-manual）

> 活文档（AI 维护，不签认）。续接入口：当前状态、下一步、挂起事项。AI 每次接手先读本文（DEVELOPMENT.md §4 第 1 步）。

## 状态

**事后补录批次（2026-11-10）**：改动已实施并提交（fork/user-manual@ed8dd517，未 push），
本六件套按实际终态回写；design.md 待责任人签认，签认后进入收尾清单（R3 标记 / 登记 /
双 AI 评审 / 门禁总检）。当前分支：`fork/user-manual`。

## 待办

- [x] 实施主体（已随升级与手册两轮交付完成）：
  - [x] `aixcoding/manual/` 67 文件入库（0.35.1 同步 + 去链 + 模型措辞统一）
  - [x] `pyproject.toml` force-include 源切换（docs → aixcoding/manual）
  - [x] `scripts/check_wheel.py` 比对源切换（2 处）
  - [x] 本地启动器 `CHRYS_DOCS_ROOT` 指向（本地资产，不入库）
  - [x] wheel 重建 + 捆绑清单/哈希比对（67=67，零差异）
  - [x] docs 相关上游回归 92 passed；桌面 PDF 重出（139 页）
- [x] requirements.md（六件套，事后补录初稿）
- [x] design.md 起草（备选对比 A/B/C + R2 表 2 项 + 假设 4 条 + 定制点预告 C145/C146 + ADR 判定=出）
- [x] test-plan.md（覆盖矩阵对照验收 8 条）
- [x] verification.md（2026-11-09/10 两节真实验证证据）
- [x] plan.md（本文档）
- [x] ~~adr/0001~~（责任人 2026-11-10 裁决不出 ADR、已删除；决策内容留在 design 备选对比/
  风险节，见 design「ADR 判定」）
- [x] 双 AI 评审第一轮（HOWTO ①②）：评审方 Claude Code + GLM-5.3，3 blocker/2
  suggestion/1 nit → 责任人拍板全部处置（见 verification 评审处置节）
- [x] 评审修复：F1 契约测试跟改（C147）+ F2 重跑回填 + F3 manual 树去链 98 处 + F5 脚本落盘
- [x] 评审增量复审（HOWTO ③，同一评审 AI，2026-11-10）：F1-F6 六项修复经独立复现确认，
  新增 N1/N2/N3 已处置或登记 → 收敛（记录见 self-review 第二轮节，不重签）
- [x] **R3 标记补录**：pyproject.toml（C145/C148）/ check_wheel.py（C146）/
  test_documentation.py（C147）均带 `AIxCoding` 单行标记
- [x] **定制点登记**：design.md「定制点登记预告」C145-C148 随签认生效 →
  `gen_customizations.py --strict` → OK（105 行 / 142 标记 / 0 warning）
- [x] ruff 双门（check + format --check aixcoding/）本地绿（C148 exclude 落地后）
- [x] **责任人签认**：design.md / self-review.md（赵阔，2026-11-10）
- [x] 门禁总检：`check_docs.py` OK + `gen_customizations.py --strict` OK（证据见 verification）
- [x] INDEX.md 登记行与批次六件套已入库（评审 F4）
- [x] push：`fork/user-manual` 已推 origin（2026-11-10，远端 `42667097`；PR 未建，按需）
- [ ] 桌面 PDF 若手册后续再改：按 manual-version-update 技能链路重出（MD → HTML → Edge）

## 责任人裁决记录（2026-11-10，赵阔）

| # | 事项 | 裁决 | 落地 |
|---|---|---|---|
| 1 | 分支推送 | **推** | 已推 `origin/fork/user-manual`（`42667097`），未建 PR |
| 2 | 验收 8 条 | **全部通过** | requirements 验收清单全勾（含 F3 口径变更后的第 2 项） |
| 3 | C148（ruff extend-exclude） | **保留** | 按防御性策略口径留档，不撤销 |
| 4 | Linux 侧 check_wheel 正式门禁 | **不改 CI** | 等 `aixcoding-upstream-ci` 桥接自然覆盖，不新增上游 CI 改动（R2 免增） |
| 5 | 双轨同源对账（N3） | **下批次做** | 已登记「挂起与备忘」 |
| 6 | 定制版 wheel 交付 | **拷交付目录，不入库** | 已拷 `桌面\用户手册\20261110\icode_tui-0.35.1-定制手册版-py3-none-any.whl`（`dist/` 仍 gitignore） |
| 7 | 分支策略 | **长期分支** | `fork/user-manual` 存续，后续手册批次挂此分支 |

## 挂起与备忘

- **SAC 环境限制（本机）**：`uv build` 与 `check_wheel.py` 内部子进程 exe 被应用控制拦截
  （os error 4551）——构建用 `.venv` 的 `python -m hatchling build` 替代；`check_wheel.py`
  正式输出待 Linux/CI 侧补（aixcoding-upstream-ci 桥接的 build+wheel 探测 job）。
- **vendored rg.exe**：gitignore 资产，每台开发机首次需跑 `scripts/fetch_rg.ps1`，否则
  search_renderers 相关上游测试必挂（本机 2026-11-10 已修复，见 verification）。
- **手册对齐流程（每次升级固定步骤）**：`git diff <旧tag> <新tag> -- docs` 逐篇评估 →
  内容性定制重放（去链清单 + 措辞清单 + `check_links.py` 门禁）→ manual 提交 → 重出 wheel/PDF。
- **双轨同源对账（增量复审 N3 遗留，下个手册批次做）**：本批次"PDF 不需重出"依据是桌面版
  此前已单独去链 + chat-options 为 manual 树独有章节，但从未从 manual 树重出桌面版做全等
  比对——下个手册批次应从 `aixcoding/manual/` 重出桌面 MD/PDF 并与现行桌面版做字符级对账。
- **测试环境噪音（本机实测配方）**：全量并发跑前先确认无遗留 pytest 进程（曾出现 4 个重复
  后台进程挤兑导致 xdist worker 崩溃假失败）；`%TEMP%\pytest-of-*` 联接点偶发 WinError 448
  ——重跑即过，或 `--basetemp` 指到 C:\work 干净目录。
- **假设 2（捆绑路径三方一致）升级检查点**：追上游时核对 `BUNDLED_DOCS_PATH` /
  `BUNDLED_DOCS_PREFIX` / force-include 目标键三者未变，变则 C145/C146 跟改并重报 R2。
