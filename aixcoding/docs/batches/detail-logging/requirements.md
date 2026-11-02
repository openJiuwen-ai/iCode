# 需求：detail 仅文件日志子树（敏感明细分流）

> 活文档：随时更新、不签认（git 留痕）；意图确认由 design 签认承载。

## 背景与问题

F6 日志面板（上游 `LogViewerScreen` 的 "aixcoding" tab）的受众包含终端用户与演示/报障场景。当前 aixcoding 管道是"全有或全无"：`panel=true` 时所有 `aixcoding.*` 记录都同时进文件与面板，无法按记录区分——部分信息（如 `ehr` 等 PII 字段）不宜在 UI 上呈现，但排查又需要文件里留完整信息。外部评审（2026-11-02，责任人委托）另确认：上游 `debug.log` 的两级 handler 均未设级别，任何冒泡到 root 的记录（含低于 DEBUG 的自定义级别）都会被写入，敏感信息存在第二个扩散面。

## 目标与非目标

- 目标：
  - 建立 `aixcoding.detail` 仅文件子树：记录**只**写入独立明细卷 `aixcoding-logs/aixcoding.detail.log`（1MB×3 独立轮转），不进 F6 面板、不进上游 `debug.log`、不进主卷；
  - 该通道零上游文件改动、零上游行为假设（不依赖上游 handler 级别设置）；
  - auth 埋点中含 `ehr` 的 3 条记录（#1/#5/#8）迁移为"面板精简版 + 文件全量版"双打形态；
  - 既有普通 `aixcoding.*` 日志行为（`panel` 开关、`level` 配置语义、冒泡路径）回归不变。
- 非目标：
  - 不做面板中心化脱敏/改写（方案 B，评审记录在案：出现"禁止调用点自行决定"的信息安全边界需求时再重估）；
  - 不改动 `aixcoding.toml` 的 `[logging] level / panel` 配置语义；
  - 不触碰任何上游文件（R2 清单为空）；
  - telemetry 等后续埋点批次不在本次范围（它们只是未来的 detail 消费者）。

## 用户场景

1. 终端用户/演示中按 F6 打开日志面板：看到"登录成功；env=prod"等精简记录，**看不到** `ehr` 等敏感字段；
2. 排查者查看主卷 `aixcoding-logs/aixcoding.log`（无 ehr）与独立明细卷 `aixcoding-logs/aixcoding.detail.log`（同一条记录为全量版 `登录成功；ehr=1234567 env=prod`，级别显示 `DETAIL`）——完整排查按时间戳关联两卷；
3. 上游 `debug.log`（16MB 滚动、可能被上游诊断流程导出）与主卷中均不出现 aixcoding 敏感明细；敏感历史需要清理时，停止进程后删除 `aixcoding.detail.log` 及其全部轮转备份即可，不牵连普通日志。

## 验收标准

- [ ] 经真实 logger 分发的 `DETAIL` 级记录：写入独立卷 `aixcoding.detail.log`（levelname 渲染为 `DETAIL`，1MB×3 独立轮转），F6 面板 handler（`_TuiLogHandler` 挂 root 实测）缓冲中无，上游 debug 队列 handler（模拟 root 分发）中无，主卷 sink 中无（不串卷）；
- [ ] 普通 `INFO` 记录行为回归：面板可见性与主卷落盘与改造前一致（`panel=true`/`panel=false` 两态）；
- [ ] auth 埋点 #1/#5/#8：面板与主卷消息不含 `ehr`，独立卷消息含 `ehr`；其余 9 条消息全文与级别不变；
- [ ] `setup_logging` 双 sink 幂等与部分失败六不变量（两 sink 分别 ensure、单失败不牵连、缺失可重试补建、共存不重复挂载、detail 失败保持隔离丢弃+stderr、propagate 先于 level 设置）；部分失败四态（主成 detail 败/主败 detail 成/双成/重试补建）有测试覆盖；
- [ ] `uv run pytest aixcoding/tests -n0` 全绿；`uv run pytest tests/architecture -n0` 全绿（auth 直写模式不被破坏）；
- [ ] `uv run python aixcoding/tools/gen_customizations.py` 对账幂等（本批次零上游 marker，产物 diff 为空）。

## 开放问题

- `DETAIL = 5` 常量的共享位置：`chrys.kernel.aixcoding` 包根（子包→包根 import 是否过 `test_layering` 未验证）vs 各调用文件本地常量（auth-logging 已确立 session.py 直写零依赖先例）——design 给出优先级与 fallback，实施期以架构测试实跑裁决；
- 责任人三项拍板已定（2026-11-02）：命名 `DETAIL`/`aixcoding.detail`；路线 A′；ehr 迁移范围 #1/#5/#8。
