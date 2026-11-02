# 任务清单：detail 仅文件日志子树

> 活文档（AI 维护，不签认）。续接入口：当前状态、下一步、挂起事项。AI 每次接手先读本文（DEVELOPMENT.md §4 第 1 步）。

## 状态

评审收敛（2026-11-02）：双 AI 评审完成（Kimi-K3.1-Pro 一轮：0 blocker/2 suggestion/3 nit → 责任人拍板全修 → 修复 → 复审 5/5 达标、零新问题，修复后回归 354 passed），self-review.md 草案待责任人签认；签认后进入 PR（git 写操作逐条授权）。

## 待办

- [x] design.md + ADR-0017（`standards/adr/0017-detail仅文件日志子树.md`）签认（责任人，2026-11-02）
- [x] test-plan.md（覆盖矩阵对照 requirements 验收标准逐条，含部分失败四态）
- [x] 实施：logging_support.py（DETAIL_LEVEL + addLevelName + setup_logging 独立 `_DetailMarker` sink + 六不变量重构"找到即 return" + docstring 修订）
- [x] 实施：session.py #1/#5/#8 双打改版（ehr 移 detail 通道）
- [x] 新增 aixcoding/tests/diagnostics/test_detail_channel.py（不串卷/四态/降级两形态/初始化顺序/常量防漂移）；改 test_session_logging.py 3 条 ehr 断言；扩展 aix_logger_state fixture（含两 sink）
- [x] 机器验证：pytest aixcoding/tests -n0 + tests/architecture -n0 + gen_customizations --strict
- [x] 人工 F6 目验（责任人 2026-11-02 通过：面板无 ehr；主卷/独立卷/上游 debug.log 三处文件证据由 AI 代查，见 verification.md §2）
- [x] verification.md（机器验证 + 人工目验 + 实施期注记三项 + 评审修复增量注记）
- [x] self-review.md（Kimi-K3.1-Pro 评审 + 复审收敛）——**待责任人签认**
- [ ] PR：建特性分支 → commit（message 规范预审）→ push → 责任人网页建 PR（git 写操作逐条授权）

## 挂起与备忘

- 三项责任人拍板已定（2026-11-02）：命名 `DETAIL=5`/`aixcoding.detail`；路线 A′；迁移范围 #1/#5/#8。
- 文件形态两轮评审定案（2026-11-02）：第一轮确立 A′ 隔离机制；第二轮推荐独立卷 `aixcoding.detail.log`（PII 单独成卷），责任人采纳——双 sink 六不变量与部分失败四态为实施硬约束（design 风险节）。
- 开放问题（实施期裁决，2026-11-02 已定）：DETAIL 常量共享位置——包根方案（`chrys.kernel.aixcoding` import）实测 `test_layering` 红（kernel→kernel 边违规，session.py/logging_support.py 两处），按 design 预授权 fallback 执行**各文件本地常量**（logging_support `DETAIL_LEVEL` 权威 + session `_DETAIL_LEVEL` 镜像 + 测试防漂移断言 `test_00_detail_level_constants_agree`）；包根 `__init__.py` 已回退纯 docstring。
- 方案 B 重启触发条件见 ADR-0017"后果"节；facade 变体（design 备选表 G 行）记档：调用点增多、双打漂移成痛点时重估。
- 本机 `%APPDATA%\chrys\aixcoding.toml` 现为 `panel=true`，人工目验可直接用。
