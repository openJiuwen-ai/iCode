# 需求：catalog 只读行为正向测试覆盖盘点与补缺（catalog-readonly-coverage）

> 活文档：随时更新、不签认（git 留痕）；意图确认由 design 签认承载。

## 背景与问题

upstream-merge-v0.33.0 批次将上游两组与本方 catalog 只读管控语义互斥的测试做了 skip（C134
函数级 / C135 参数级，登记于该批次 design）。skip 后出现两个问题：

1. **维度真空**：上游 `test_prompt_shortcut_send.py:1203`（models 参数）是唯一覆盖
   "发送中/运行中打开模型对话框应为只读"的测试；models 侧 skip 后，该维度失去全部运行时
   覆盖。而我方语义下该维度仍真实可达——`/models` 命令 `allow_while_running=True`
   （`commands.py:1087-1093`）。定制侧仅有 idle 一例锚定
   （`test_read_only_gate.py::test_open_model_config_screen_is_always_read_only`）。
2. **盘点资产缺失**：现有正向覆盖分散在定制测试（3 锚定用例）与"仍在 fork 中运行的上游
   测试"（只读渲染全套 `test_model_config.py:716/764`、picker/F4 契约 C044-C049）两处，
   无统一矩阵；后续升级/新增测试时易误判为零覆盖而重复建设。

来源待办：`fork/temp/aixcoding-todos/2026-11-04-catalog-readonly-test-coverage.md`
（升级批次外部 AI 评审一致意见：留独立批次，不混入升级）。

## 目标与非目标

- 目标：
  - 建立"catalog 只读行为正向覆盖矩阵"（含**不补**决策及依据），落盘防重复建设；
  - 补齐缺口 G1：controller 层 busy 态（agent_running / execution_busy）恒只读的参数化锚定；
  - 补齐缺口 G2：状态栏指示器 configure 入口经真实路由链打开恒只读屏的锚定；
  - 对新增断言设计变异验证（改产品代码→测试红→还原→绿，证据入 verification）。
- 非目标：
  - 不修改/退役 C134/C135 的 skip（如需触碰另行呈报 R2）；
  - 不恢复上游本地编辑/删除场景（产品语义已签认，不可推翻）；
  - 不补 G3（screen.py handler 层 read_only 守卫单测——UI 路径已被 display=False +
    上游渲染断言挡死，击穿需双变异，补了属测实现，依据见 design 风险节）；
  - 不补 zh-Hans 只读文案断言（i18n 门禁覆盖，catalog-sync test-plan 已有决策在案）；
  - 不补 registry 整替 × 只读入口的端到端组合（各环节分别有锚定，端到端需真 app+engine，
    会话语义已由 catalog-sync 假设 7 钉死）；
  - 最终交付 diff 零上游文件改动（含上游测试文件）；变异验证的临时源码修改为例外操作，
    授权边界与还原检查见 design.md 专节。

## 用户场景

1. 会话发送中/运行中，用户经 `/models` 命令打开模型配置 → 界面恒只读（集中管理提示、
   表单禁用、删除按钮隐藏）；
2. 登录前/目录为空时，状态栏模型指示器呈 configure 态，点击 → 打开恒只读模型配置屏
   （方案 A 明确保留的只读查看入口，catalog-sync design 假设 11）。

## 验收标准

- [ ] busy 四态（上游 `_config_read_only()` 三信号 `agent_running` / `submit.active` /
      `execution_busy()` 各自单独 True + 三者全 True）下，
      `RuntimeConfigController._open_model_config()` 推入的 `ModelConfigScreen`
      均满足 `_read_only is True`（与既有 idle 用例互补，覆盖判定输入全集——
      C135 维度本体的等价接管）；
- [ ] `on_model_tag_clicked("configure")` 经 `open_model_config()` → 捕获并 await
      `_open_model_config()` 协程的真实路由链推入的屏 `_read_only is True`；
- [ ] 变异验证双证（临时源码修改按 design"变异验证授权边界"节，还原后零残留 diff）：
      ① `read_only=True` 改为 `not self._config_read_only()`（判定取反：idle 仍只读、
      任一 busy 信号可编辑）时 idle 用例保持绿且 4 个 busy case 全红；② configure 分支
      删除/改推可编辑屏时 G2 用例红；还原后全部转绿；
- [ ] `uv run pytest aixcoding/tests -n0 -q` 全绿（新增用例 + 既有 3 锚定用例不回归）；
- [ ] `gen_customizations.py --strict` 维持 98 登记行 / 135 marker / 0 error / 0 warning
      不变（本批次零 marker 改动）；
- [ ] 覆盖矩阵（维度 × 证据 × 不补依据）落盘于本批次 test-plan，后续批次可直接引用。

## 开放问题

- 无（G2b 可选增项——picker MANAGE 消费分支接线锚定——已在 design 备选留档，默认不做，
  如需纳入由责任人在 design 签认时批示）。
