# 需求：日志治理①——现存埋点路由修复

> 活文档：随时更新、不签认（git 留痕）；意图确认由 design 签认承载。
> 2026-11-05 修订：B 项撤销（调查结论修正，见 design"调查结论修正"节）；D 补充既有断言修订事实。

## 背景与问题

2026-11-05 日志体系调查（调查材料原存 `temp/2026-11-05-logging-design-discussion.md`，temp 目录后已清空——关键结论均已沉淀于本批 requirements/design 与 DEVELOPMENT.md §9 分层口径，本文档自包含）核实：双卷方案（ADR-0017 双打规范）在 `session.py` / `catalog.py` 已正确落地，但存在三处**路由与分类缺陷**——记录要么彻底无声、要么落在定制卷之外。责任人裁决日志治理拆三批次（①路由修复 → ②主通道冒泡解耦 → ③离线合并工具），本批次为①，范围仅限修复现存问题，不引入任何新机制。

缺陷清单（均已核实，行号为 2026-11-05 基线）：

- **A（子树外 logger，三处）**：`getLogger(__name__)` 得到 `chrys.kernel.aixcoding.auth` / `chrys.app.aixcoding.tui.login` 等名字，不在 `aixcoding` 顶树下——既不进 `aixcoding.log` 也不进 `aixcoding.detail.log`；其中两条 DEBUG 级另被 root `setLevel(INFO)`（`debug_log.py:222`）挡住，**彻底无声**：
  - A1 `kernel/aixcoding/auth/__init__.py:45,65`：`login_token_header_value` 失败记录（英文 debug + exc_info）——排查"LLM 请求为何未带 token"的关键信息完全丢失；
  - A2 `kernel/aixcoding/auth/crypto.py:32,257`：crypto 后端探测失败记录（英文 debug）无声，且**降级到 MemoryBackend 这个用户可感知事件本身（凭证重启即失）完全无日志**；
  - A3 `app/aixcoding/tui/login.py:49,134`：登录成功回调失败 `logger.exception`（英文 ERROR）——唯一真正冒泡进上游 `debug.log` 的一条（英文、不在定制卷）。
- ~~B（catalog.py:680/681 重复行）~~：**撤销**——design 起草阶段核实 `test_catalog_sync.py:973-1020` 钉死"完整 endpoint（含 userId）留在 detail 卷"为 catalog-auth-gating 批次签认行为；676 行注释约束对象是面向用户的错误文案，原判系误读（R8 留档于 design，防重提）。
- **C（死代码）**：`app/aixcoding/tui/login_actions.py:20,32` `import logging` 与 logger 定义后从未使用。
- **D（分类缺失）**：`catalog.py:230-235`"环境变量 %s 已停用且不生效"仅进 detail 卷——这是**用户配置错误**（用户可操作），用户在面板/主卷看不到提醒；属 catalog 批次签认的"catalog 全 detail-only"设计（2026-11-04 方案六）未覆盖的例外情形。注意：`test_catalog_sync.py:699` 现断言该警告"stays off the main volume"——本项将修订该既有断言。

## 目标与非目标

- 目标（五项修复，逐条路由定案）：
  - A1：改挂 `aixcoding.detail.auth`，中文化，保留 exc_info——每请求级调用，只进 detail 卷（任何公共级别都会刷屏）；
  - A2：`get_backend` 降级事件三打点（2026-11-05 peer review M1/M2 修订）：factory 抛异常→detail 技术原因；factory 往返校验不一致（不抛异常的静默路径）→detail；降级到 MemoryBackend→公共通道（`aixcoding.auth`）低频可操作中文 WARNING（凭证将仅保存于内存）**+ 同文案 detail 双写**（DEVELOPMENT.md §9 分层口径：用户功能事件双写两卷）；
  - A3：改挂 `aixcoding.detail.auth`，中文化（detail-only：登录已成功、对话框照常关闭，回调失败用户无感）；
  - C：删除未使用的 import 与 logger 定义；
  - D：停用环境变量警告升为公共通道 WARNING 双打（中文、含改用建议），catalog 其余记录仍 detail-only——"用户可操作的配置错误"成为 catalog 静默设计的明确例外；同步修订 `catalog.py:72` 定调注释与 `test_catalog_sync.py:699` 既有断言。
- 非目标：
  - 不动 `propagate`/`panel` 机制与 `aixcoding.toml [logging]` 配置语义（批次②）；
  - 不做日志合并工具（批次③）；不做显式开发摘要通道、独立用户日志视图（观察项/后续产品决策）；
  - 不触碰 detail 子树机制与 ADR-0017 六不变量；
  - 不为 Linux `PlaintextBackend` 明文落盘加提示（平台设计，与 reference 一致；作为开放问题留档）；
  - 不触碰任何上游文件（R2 清单为空：改动文件均在定制层）。

## 用户场景

1. 排查"LLM 请求未带 token"：`aixcoding.detail.log` 可见**每种异常类型首次失败**的完整原因（异常栈，中文消息）；同类型后续静默（C1 频控取舍：防刷屏优先于复发诊断，新异常类型会再记一条）；
2. crypto 后端降级（如 DPAPI 不可用）：用户在主卷/面板看到一条可操作 WARNING（重启后需重新登录）；开发在 detail 卷看到各后端探测失败原因；
3. 用户误配停用环境变量：主卷/面板出现 WARNING，指明改用 `AIXCODING_MODEL_CATALOG_*`。

## 验收标准

- [x] 三处子树外 logger 名（`chrys.kernel.aixcoding.*` / `chrys.app.aixcoding.*`）不再有定制埋点调用（测试断言目标卷收到记录且子树外名字零分发）；
- [x] A1：detail 卷含中文记录（"读取登录凭证失败"口径，M5 修正）+ exc_info；主卷/面板无记录（防刷屏）；
- [x] A1 持续故障频控：每异常类型一条带栈记录，同类型后续静默（PR #95 review C1 增补，进程级 once 守卫）；
- [x] A2：①主卷 WARNING 且不含技术细节（哨兵：后端名/异常文本不进公共卷）；②detail 同文案双写（M2）；③factory 异常路径与往返不一致路径（Garbage 形态）均有独立 detail 断言（M1）；④probe 成功路径两卷零记录；⑤健康候选经提前返回路径（`:255`）两卷零 WARNING——factory 失败≠降级，告警仅在候选耗尽的最终降级点（peer review2 M1 口径）；
- [x] A3：detail 卷有中文记录 + exc_info；主卷零记录；
- [x] C：`login_actions.py` 无未使用 import 与 logger；
- [x] D：配置停用变量时主卷出现 WARNING（既有断言 :699 已反转）；detail 卷同文案双写；其余 catalog 记录仍 detail-only（例外范围钉死为"用户可操作的配置错误"一类）；
- [x] `uv run python aixcoding/tools/verify.py --smart --paths <全部改动文件>` 通过；`uv run pytest aixcoding/tests -n0` 全绿；`uv run pytest tests/architecture -n0` 全绿；
- [x] `uv run python aixcoding/tools/gen_customizations.py`（含 `--strict`）0 error / 0 warning（D 修订 catalog.py:72 注释文本后对账义务）。

> 勾选说明（2026-11-05，随 PR #95 评审处置增量更新）：机器验证证据见 verification.md（含 C1 增量后全量：目标 59 passed、aixcoding/tests 467 passed / 4 skipped、architecture 1248 passed / 3 skipped、对账 0 warning）；verify.py --smart 统一入口经责任人裁决（选项 A）以拆分验证等效覆盖；PR #95 首轮评审对四项测试与全部门禁独立复核通过、增量评审两轮闭环（见 self-review.md）。

## 开放问题

- ~~A3 分类~~ 已定 detail-only（design 定案：登录成功、对话框照常关闭，回调失败用户无感）；
- D 与 catalog-sync 批次签认设计的修订关系：修订依据落本批次 design"调查结论修正/逐条定案"节，catalog-sync design 是否另加联动注记由责任人签认时裁决；
- A2 公共 WARNING 文案与可操作建议措辞（design 已给草案文案，签认时可改）；
- Linux `PlaintextBackend` 明文落盘是否需要一次性提示（平台设计，本批不做，留档）；
- A1/A2/A3 所在文件的模块级 logger 定义形态：已定直写字面名 + 本地 `_DETAIL_LEVEL` 常量（session.py/catalog.py 零依赖先例，design 定案）。
