# 设计：catalog 只读行为正向测试覆盖盘点与补缺（catalog-readonly-coverage）

<!--
签认规则（R10）：责任人本人编辑本头部（状态→已确认、填日期，确认意见可选），AI 不得代填。
签认后修改正文需重新签认。校验：uv run python aixcoding/tools/check_docs.py
-->

- 责任人：周彬
- 状态：已确认
- 确认日期：2026-11-05
- 确认意见：无特别意见

> 修订记录：2026-11-05 独立评审（Kimi-K3.1-Pro / CodeBuddy，3 Blocker + 1 Suggestion）
> 修复——B1 G2 协程驱动改为"捕获协程 + await"；B2 V1 变异改为 `not _config_read_only()`
> 取反形态并修正文字；B3 新增"变异验证授权边界"节，"零上游改动"统一为最终交付 diff 口径；
> S1 G1 参数化扩为三信号四态（补 `submit.active`），等价接管表述随之成立。
> 2026-11-05 PR #92 peer review（C1）措辞精确化——"四态 = 判定全部输入面"更正为
> "三信号单真 + 全真四态，组合态不设（不增检出力）"；正文非 R2 内容，按 §4.1
> 签认分档不触发重签，git 留痕。

## 方案概述

纯测试补齐批次，**最终交付 diff 零产品代码、零上游文件改动**（变异验证的临时源码修改
属例外操作，授权边界见专节）。在 `aixcoding/tests/catalog/test_read_only_gate.py`
（我方定制测试，catalog-sync 批次落盘）内：① 提取既有 idle 用例的 controller stub 构造为
公共 helper；② 新增 G1 参数化用例（上游 `_config_read_only()` 三信号的四态恒只读锚定）；
③ 新增 G2 用例（指示器 configure 入口经真实路由链打开恒只读屏）。覆盖矩阵与"不补"决策
落盘本批次文档，作为后续升级/测试建设的对照资产。

```
RuntimeConfigController (config_actions.py)
  on_model_tag_clicked("configure")          ← G2：真实路由链（select/locked 分支不射）
      └→ open_model_config()  (L244 worker 包装、防重入)
              └→ _open_model_config()  (L260 单路径)
                      └→ push_screen(ModelConfigScreen(..., read_only=True))  ← G1：四态断言
                                 (marker C035, config_actions.py:282-285)

上游 _config_read_only() (L235-242) = agent_running or submit.active or execution_busy()
——G1 四态 = 三信号各自单独 True + 三者全 True；两两组合态不设——被锚语义为恒
  read_only=True（与信号取值无关），任一信号为真即足以使条件类变异（V1）暴露，
  组合态不增加检出力（PR #92 评审 C1 措辞精确化）。
  恒只读语义 ⊇ 条件只读语义，四态锚定使"C135 维度本体等价接管"成立
  （C135 时点 commands-running 由 execution_busy() 覆盖，其余两信号窗口一并锚定）。
```

既有锚定（不动）：idle 恒只读（T1，`test_read_only_gate.py:34`）、notice 常量（T2）、
F4 BINDINGS 静态缺失（T3）；渲染级只读全套（上游 `test_model_config.py:716/764`，fork 中
运行，C047 文案跟改）；picker/F4 契约（C044-C049）。上游 v0.33.0 被 skip 的两组
（C134/C135）不触碰。

## 备选方案对比

| 方案 | 优点 | 缺点 | 升级冲突面 | 结论 |
|---|---|---|---|---|
| **A. controller 层 stub harness 扩展**（复用 T1 形态：`RuntimeConfigController.__new__` + 属性注入，真实调用方法链） | 与既有 idle 用例同构；真实调用 `_open_model_config`/`on_model_tag_clicked`（不 mock 被测门）；构造轻、无真 app 依赖 | 断言到推屏参数为止，不到挂载后渲染 | 零（纯定制测试文件） | **是** |
| B. 真 TUI 挂载（Textual run_test 起 MainScreen/pilot 按键） | 端到端真实 | MainScreen 构造依赖重；C134/C135 的 engine 级形态已被外部评审否决混入；渲染级已有上游 C047 用例覆盖，B 属重复 | 零 | 否决（重复建设 + 成本失衡） |
| C. 仅盘点不补测试 | 零成本 | G1 维度真空有实际升级冲突风险（上游恰在此区活跃开发）；"不新增"依据不成立 | 零 | 否决（开工判定单已论证） |

R1 自查：最终交付零上游改动（R2 节删除；变异验证临时修改见授权边界节，不进入交付）；
测试落点为既有定制目录 `aixcoding/tests/catalog/`，复用其 conftest 隔离设施
（ADR-0001：不 import `tests/support/`）。

## 对上游隐式行为的假设

| # | 上游行为 | 我方依赖方式 | 假设失效时的表现 |
|---|---|---|---|
| 1 | `_open_model_config` 单路径构造 `ModelConfigScreen(..., read_only=...)`（`config_actions.py:278-288`；catalog-sync 假设 1 的延续） | G1 直调并断言推入屏 `_read_only is True` | 上游拆分构造/移动推屏点 → 推屏断言红 |
| 2 | `on_model_tag_clicked` 的 `"configure"` case 存在且调 `open_model_config()`（`config_actions.py:163-165`）；`open_model_config()` worker 包装形态（L244-258） | G2 从 `on_model_tag_clicked` 入口真实路由 | case 被删 → match 落空不推屏 → 断言红；方法签名变 → AttributeError 红 |
| 3 | `RuntimeConfigController` 实例属性形态 `_view/_state/_services/_callbacks/_model_config_opening`；`_state.run.agent_running` / `_state.run.agent_loading`；`_state.submit.active`（`config_actions.py:242` 上游判定输入）；`_services.execution_busy()`（T1 既有依赖 + 本批次扩展） | stub harness 属性注入 | 上游重构属性名 → 构造/断言红（预期防线行为） |
| 4 | `env_bridge.get_global_default_profile_id` 可 monkeypatch（T1 既有手法）；`open_model_config` 以协程对象传 `run_worker`（L252-254，captured-await 驱动的前提） | helper 沿用 + G2 捕获协程 | import 点/传参形态变化 → monkeypatch/捕获失效红 |

## 定制点登记预告

不适用（零上游接触）：本批次仅扩展定制测试文件 `aixcoding/tests/catalog/test_read_only_gate.py`
（纯开发资产，ADR-0015 射程为上游文件定制点），不新增/修改任何 marker。实施后
`gen_customizations.py --strict` 应维持 98 登记行 / 135 marker 不变（验收项）。

## 测试设计（与 test-plan 联动）

**公共 helper（从 T1 提取，T1 断言语义不变）**：
`_make_controller(agent_running=False, execution_busy=False, submit_active=False, registry=...) -> (controller, pushed, captured)`
——T1 的 `_View/_Run/_State/_Services` stub 原样上移并补 `submit.active` 注入点；
`_View.run_worker` 改为**捕获协程对象**（`captured.append(worker)`）而非驱动——驱动职责
移交用例显式 `await`（评审 B1：裸 `coroutine.send(None)` 只能推进到 `asyncio.to_thread`
首个挂起点且无人唤醒，无法到达 `push_screen`）。返回三元组固定暴露形态：`pushed` 为
推屏列表（T1/G1 消费，多余元素以 `_` 解包），`captured` 为 `run_worker` 捕获的协程列表
（G2 消费 `captured[0]`）。

**G1 `test_open_model_config_screen_is_read_only_while_busy`**（参数化 4 case：
`agent_running-only` / `execution_busy-only` / `submit-active-only` / `all-three`）：
helper 构造对应态 → `await controller._open_model_config()`（T1 同款直调，测试协程内
`to_thread` 正常完成）→ 断言 `pushed[0]` 为 `ModelConfigScreen` 且 `_read_only is True`。
四态锚定三信号的单真态与全真态；两两组合态不设（被锚语义与信号取值无关，任一信号
真即足以检出条件类变异，组合态不增检出力——PR #92 评审 C1）。不测 `agent_loading`（上游
"loading 拒绝打开"是入口拒绝行为，非只读维度，上游域）。

**G2 `test_model_indicator_configure_entry_opens_read_only`**（async）：helper 构造 idle
态 → `controller.on_model_tag_clicked("configure")` → `open_model_config()` 真实执行
（防重入/registry 检查真实走）并将 `_open_model_config()` 协程交给 `run_worker` →
stub 捕获 → 用例 `await captured[0]`（事件循环内完整驱动 `to_thread` 与推屏）→ 断言
推入 `ModelConfigScreen` 且 `_read_only is True`。同时锚定 catalog-sync 假设 11
（configure 入口保留）从"升级核对项"升级为运行时锚定。

**G2b（可选，默认不做，留档）**：picker `ModelPickerAction.MANAGE` 消费分支
（`config_actions.py:176-178` → `open_model_config()`）——C045 已锚定 picker 侧 dismiss，
消费侧接线无锚定。如纳入：stub `_View.push_screen` 捕获 `(ModelsScreen, callback)` →
`callback(ModelPickerAction.MANAGE)` → 捕获并 await `_open_model_config` 协程 → 断言
只读屏。构造多一层、价值与 G2 同量级，按"最小化补齐"默认不做。

**变异验证设计**（临时修改授权见下节；证据入 verification）：
- V1（G1 独有检出力）：`config_actions.py:285` `read_only=True` →
  `read_only=not self._config_read_only()`（上游判定的**取反**形态——idle 仍只读、任一
  busy 信号可编辑；比"恢复上游语义"更隐蔽，因 idle 用例 T1 保持绿而漏防）：idle 用例
  绿 + G1 四 case 全红；还原后全绿。
- V2（G2 检出力）：删除 `case "configure"` 分支（或改推可编辑屏）→ G2 用例红；还原后绿。
- 既有射程对照（本批次不重复执行）：`read_only=True → False` 裸变异由 idle T1 + G1
  同时检出；F4 恢复由 T3 + C048/C049 检出；删除按钮恢复由上游 C047 用例检出
  （`display is False` 断言）。

## 变异验证授权边界（评审 B3）

"零上游改动"指**最终交付 diff**；变异验证须临时编辑上游源码 `src/chrys/app/tui/screens/main/config_actions.py`，具体为：

| 变异 | 位置 | 形态 | 还原检查 |
|---|---|---|---|
| V1 | L285 `read_only=True` 实参 | 改为 `not self._config_read_only()` | `git diff --stat src/chrys/app/tui/screens/main/config_actions.py` 零输出 |
| V2 | L163-165 `case "configure"` 分支 | 删除分支（或整段改推非只读屏） | 同上 |

- 本文签认即批准上述**临时**修改（范围仅此两处、仅验证期间、不进入任何 commit）；
- 每次变异验证后：还原 → 复绿运行 → 上述 diff 检查零输出，三步证据一并记入 verification；
- 若验证过程中需要超出本清单的源码改动（如行号漂移需改别处），停下重新呈报。

## 风险与最坏情况

- **stub 依赖面**：对 controller 私有属性形态的依赖（假设 3）是 T1 既有形态延续——上游
  重构属性名时测试红属预期防线行为，跟改成本一行级。
- **覆盖边界如实声明**：G1/G2 断言到"推入屏的 `_read_only` 参数"为止；挂载后渲染全套
  （表单禁用/按钮隐藏/文案/不写盘）由上游 C047 用例在 fork 中持续覆盖，两者互补不重叠
  （这正是"不重复建设"的边界划分）。
- **G3 不补依据**（留档防重提）：screen.py handler 层守卫（`_on_delete`/`_do_delete` 等
  首行 return）击穿需"display 恢复 + 守卫删除"双变异；UI 单变异已由 C047 用例
  `display is False` 断言拦住；上游对 save press 已有断言（C047 用例 ⑩）。
- 最坏情况（测试写错/误锚）= revert 新增用例，零产品代码影响；回滚 = 本批次文件整删。

## ADR 判定

四问逐项：① 无被认真考虑且否决的架构级备选（B/C 否决均为成本/重复建设权衡，模块内实现
选择）；② 撤销代价低（测试文件增量，整删即回滚）；③ 不约束未来工作模式（覆盖矩阵是
对照资产非机制）；④ 依赖的上游符号均已登记于 catalog-sync 假设表/登记表（C035 等），
本批次不新增依赖类型。反面清单核对：属"唯一合理路径的照规范执行"（补测试的构造选择）。
结论：**无需 ADR**。
