<!--
Copyright (c) 2026 chana4afk & 四川爱赋康文化科技有限公司. All rights reserved.
原始方法学版权：iCode（openJiuwen / Huawei, Apache-2.0），Copyright (c) Huawei Technologies Co., Ltd. 2026；本文件在其方法学基础上脱敏准备，尊重并保留华为原始版权。
商标声明：iCode、Chrys、openJiuwen 为其各自权利人的商标，本示例未获其背书。
许可：本示例以 Apache License 2.0 贡献（与上游同许可）。
署名：示例贡献者 / 组织（非个人名）；保留上游 NOTICE 精神。
贡献声明（Contribution Notice）：本贡献由 chana4afk & 四川爱赋康文化科技有限公司 在 iCode（openJiuwen / Huawei, Apache-2.0）方法学基础上脱敏准备；尊重并保留华为原始版权，依 Apache-2.0 许可向上游提交，CLA 由贡献者签署。

-->

# 通用六阶段交付流水线 · iCode WorkflowBuilder DSL 示例

**许可**：Apache-2.0
**商标声明**：iCode、Chrys、openJiuwen 为其各自权利人的商标，本示例未获其背书。
**配套**：`../playground/six_stage_pipeline.py`

> 本目录**只新建文件**，未改动任何上游或内部文件。

---

## 0. 版本对齐与一句话结论

- 本示例基于 iCode（chrys）**v0.28.0** 编写；如你的版本不同，请以你方实际安装的运行时 API 为准。
- 流水线可被 iCode `WorkflowBuilder` 在内存构造成功（节点 / 边 / schema_version=1）。
- **关键事实**：iCode v0.28.0 的 workflow **只有 Python builder 这一种原生格式**，没有 YAML 格式。本示例因此用 `.py` builder，字段 100% 来自公开 API，未引入任何运行时未知字段。

---

## 1. iCode WorkflowBuilder 真实 API 发现结论

探查方式（只读，未复制 iCode 源码进本仓）：用 venv python 读 `chrys.workflows` → `chrys.service.workflows.sdk._builder` / `_values` / `_ask` → `discovery` → `admission`。

| 项 | 结论 | 证据 |
|---|---|---|
| 模块入口 | `from chrys.workflows import (WorkflowBuilder, Workflow, NodeHandle, WorkflowValue, Retry, BuilderScope, NodeContext, …)` | `chrys/workflows.py` 的 `__all__` |
| 构造器 | `WorkflowBuilder(title, *, description=None)`；`.build()` → `Workflow`（冻结图）；`Workflow.manifest()` 返回纯数据投影 | `sdk/_builder.py` |
| 节点种类 | `agent` / `python` / `join` / `loop` 四种 | `sdk/_builder.py` |
| agent 节点 | `wf.agent(name, *, profile, model=None, instructions_suffix=None, timeout=None, retry=None)` → `NodeHandle` | `sdk/_builder.py` |
| python 节点 | `wf.python(name, fn, *, timeout=300.0, retry=None)`；`fn` 形状 `(value)` 或 `(value, ctx)` | `sdk/_builder.py` |
| 边/汇聚 | `.edge(src,dst,when=None)` / `.switch(src,cases,default)` / `.join(sources,dst,combine=None)` / `.chain(*nodes)` | `sdk/_builder.py` |
| 循环 | `.loop(name, body, until, max_iterations, on_exhausted='continue'|'fail')` | `sdk/_builder.py` |
| 入口/出口 | `.start(node)`（唯一）/ `.output(node)`（≥1） | `sdk/_builder.py` |
| 重试 | `Retry(max_attempts, backoff=0.0)` | `sdk/_builder.py` |
| **外部 profile 引用** | **支持**。`profile=` 是任意字符串 selector（id / name / display_name），运行时由 `AgentProfileRegistry.resolve_selector` 解析；未知 → `REJECT_AGENT_PROFILE_MISSING` | `admission.py` |
| **gate 接入点** | SDK 无原生「gate」节点类型。安全红线由运行时 **hooks 子系统**在每次工具调用前硬拦截；workflow 侧只能以「python 节点读取并断言 hooks 清单存在/形态」作为 gate 声明，**不可重实现钩子逻辑** | `sdk/_builder.py` 无 gate 字段 |
| 文件发现 | workflow 文件是 `*.py`，文件名 stem = workflow id；扫描 `project(.chrys/workflows)` > `global(config/workflows)` > `builtin` | `discovery.py` |
| CLI | `chrys workflow` 子命令可用于列出/无头运行 workflow | `chrys workflow --help` |

**结论**：本示例用 `profile="<role-placeholder>"`（通用占位符，== 你方 `profiles/<x>.yaml` 的 `name` 字段）引用 agent profile；安全硬卡点以「前置 gate + 阶段 gate」两个 python 节点声明，仅「读取并断言」hooks 清单存在，不复制逻辑。

> 注：示例中所有 `<...>` 占位符（如 `<delivery-lead>`、`<security-agent>`）均为通用占位，**不代表任何专有团队或角色**，请替换为你方真实的 agent profile 选择器。

---

## 2. 通用六阶段交付流水线结构（以公开 API 事实为准）

六阶段闭环（通用交付方法论，来源：公开的交付工程实践）：

| 阶段 | 名称 | 参与 profile（通用占位） | 安全卡点落位 |
|---|---|---|---|
| 1 | 需求澄清 | `<legal-agent>`(数据分级/合规边界)、`<ip-agent>`、`<intent-agent>`、`<market-agent>`、`<delivery-lead>`(主持)、`<tax-agent>` | 需求阶段标注数据分级与合规边界 |
| 2 | 架构焊死 | `<architect>`(焊死分层架构+出决策)、`<agent-os-agent>`、`<db-agent>`、`<mw-agent>`、`<crypto-agent>`、`<techradar-agent>`、`<hardening-agent>` | 架构阶段内置控制项与威胁建模，出隔离等级决策 |
| 3 | 分域工程 | 各域工程角色并行（`<app-agent>`/`<backend-agent>`/`<frontend-agent>`/`<lowcode-agent>`/`<ui-agent>`/`<mobile-agent>`/`<distributed-agent>`/`<iot-agent>`/`<embodied-agent>`/`<model-agent>`/`<lang-agent>`/`<efficiency-agent>`/`<db-agent>`/`<mw-agent>`/`<hardening-agent>`） | 分域工程每域附『安全对照结论』 |
| 4 | 集成 | `<architect>`(跨域集成)、`<backend-agent>`(接口/数据契约)、`<qa-agent>`(集成测试)、`<quality-agent>`(供应链门禁)、`<repo-agent>`(投递) | 集成阶段核对攻击面闭合 |
| 5 | 质量门禁 | `<quality-agent>`(SBOM/SCA/双闸)、`<security-agent>`(红蓝对抗)、`<legal-agent>`(一票否决)、`<ip-agent>`(IP复核)、`<clerk-agent>`(留痕) | 门禁阶段红蓝对抗判定 |
| 6 | 双模交付 | `<quality-agent>`(交付门禁)、`<repo-agent>`(确投)、`<clerk-agent>`(回执)、`<oss-agent>`(开源)、`<strategic-agent>`(情报)、`<intent-agent>`(收口)、`<delivery-lead>`(收口) | 交付阶段随工程交付物附安全门禁结论 |

- **管线总定义**：六阶段闭环——澄清-架构焊死-分域工程-集成-门禁-交付；安全卡点以「前置 gate + 阶段 gate」声明，真正拦截由运行时 hooks 子系统完成。
- **安全卡点 owner**：`<security-agent>`（每个卡点共签 owner）。

---

## 3. 阶段 → iCode workflow step 映射

`six_stage_pipeline.py` 用「阶段内并行 fan-out（多个 agent 节点）+ join 汇总 → 下一阶段」表达串行六阶段。节点命名：`g1_security`（前置 gate，start）、`sN_<role>`（阶段 N 的 agent 节点）、`sN_merge`（阶段 N 的 join 目标）、`s5_security_gate`（阶段 5 安全 gate）。

| 阶段 | iCode step（节点） | 引用的 profile（通用占位） |
|---|---|---|
| 前置 gate | `g1_security`（python，断言 hooks） | —（读 `../hooks/security-l3-hooks.yaml`） |
| 1 需求澄清 | `s1_legal` `s1_ip` `s1_intent` `s1_market` `s1_lead` `s1_tax` → join `s1_merge` | `<legal-agent>` / `<ip-agent>` / `<intent-agent>` / `<market-agent>` / `<delivery-lead>` / `<tax-agent>` |
| 2 架构焊死 | `s2_architect` `s2_agentos` `s2_db` `s2_mw` `s2_crypto` `s2_techradar` `s2_harden` → join `s2_merge` | `<architect>` / `<agent-os-agent>` / `<db-agent>` / `<mw-agent>` / `<crypto-agent>` / `<techradar-agent>` / `<hardening-agent>` |
| 3 分域工程 | `s3_*` → join `s3_merge` | `<app-agent>` / `<backend-agent>` / `<frontend-agent>` / `<lowcode-agent>` / `<ui-agent>` / `<mobile-agent>` / `<distributed-agent>` / `<iot-agent>` / `<embodied-agent>` / `<model-agent>` / `<lang-agent>` / `<efficiency-agent>` / `<db-agent>` / `<mw-agent>` / `<hardening-agent>` |
| 4 集成 | `s4_architect` `s4_be` `s4_qa` `s4_quality` `s4_repo` → join `s4_merge` | `<architect>` / `<backend-agent>` / `<qa-agent>` / `<quality-agent>` / `<repo-agent>` |
| 5 质量门禁 | `s5_quality` `s5_legal` `s5_ip` `s5_clerk` `s5_security` `s5_security_gate` → join `s5_merge` | `<quality-agent>` / `<legal-agent>` / `<ip-agent>` / `<clerk-agent>` / `<security-agent>` / + gate 节点读 hooks |
| 6 双模交付 | `s6_quality` `s6_repo` `s6_clerk` `s6_oss` `s6_strategic` `s6_intent` `s6_lead` → join `s6_merge` | `<quality-agent>` / `<repo-agent>` / `<clerk-agent>` / `<oss-agent>` / `<strategic-agent>` / `<intent-agent>` / `<delivery-lead>` |
| 出口 | `wf.output(s5_merge)`, `wf.output(s6_merge)` | 门禁证据 + 最终交付物 |

- handoff：阶段间用 `wf.edge(sN_merge, sN+1_<agent>…)` 串行；阶段内 `wf.join([…], sN_merge)` 并行汇总。
- `instructions_suffix`：每个 agent 节点统一追加「受等保三级 hooks fail-closed 约束」提醒（仅 instructions 后缀，不重实现钩子逻辑）。

---

## 4. 引用的 profile 清单（通用占位，须替换为你方真实 profile）

> 下列为通用占位符，用于演示 DSL 结构与编排。**请全部替换为你方已注册、且 `name` 字段与选择器一致的真实 agent profile**。

`<legal-agent>`, `<ip-agent>`, `<intent-agent>`, `<market-agent>`, `<delivery-lead>`, `<tax-agent>`, `<architect>`, `<agent-os-agent>`, `<db-agent>`, `<mw-agent>`, `<crypto-agent>`, `<techradar-agent>`, `<hardening-agent>`, `<app-agent>`, `<backend-agent>`, `<frontend-agent>`, `<lowcode-agent>`, `<ui-agent>`, `<mobile-agent>`, `<distributed-agent>`, `<iot-agent>`, `<embodied-agent>`, `<model-agent>`, `<lang-agent>`, `<efficiency-agent>`, `<qa-agent>`, `<quality-agent>`, `<repo-agent>`, `<oss-agent>`, `<clerk-agent>`, `<strategic-agent>`, `<security-agent>`

**name 字段注意事项（重要）**：`profile=` selector 必须严格等于你方 YAML profile 的 `name` 字段才能被 `AgentProfileRegistry.resolve_selector` 命中。

---

## 5. 安全 gate 接入说明（不重实现钩子逻辑）

- **红线承载方**：5 个 `before_tool_call` 钩子（`hooks/security-l3-hooks.yaml`，每个 `execution: { mode: blocking, on_error: block }` = fail-closed 默认 deny）。任一工具调用命中红线即否决。
- **workflow 侧 gate 节点**：
  - `g1_security`（start）：运行开始前断言 hooks 清单存在且为 5×before_tool_call + 5×fail-closed；不符则抛错**阻断整条流水线**。
  - `s5_security_gate`（阶段 5 join 输入之一）：质量门禁阶段再次断言 hooks 就位。
  - 两节点体仅 `read_text` + 计数断言，**未复制任何钩子执行逻辑**。
- **运行时**：真正拦截发生在运行时加载 hooks 之后、每次工具调用之前——属 hooks 子系统职责，pipeline 仅声明其为本流水线的前置/阶段 gate。

---

## 6. 如何运行 / 验证

### 6.1 离线校验（结构校验）
```bash
# 在含 chrys 的 venv 下
python six_stage_pipeline.py   # 模块级 `workflow = wf.build()` 触发结构校验；无异常即 PASS
```
校验项：① import 触发 `WorkflowBuilder.build()` 结构校验成功；② `manifest.schema_version==1`；③ agent 引用的 profile 名与你的 profile 注册表一致；④ `g1_security`/`s5_security_gate` 存在且 hooks 形态断言可用。

### 6.2 在 iCode runtime 实际运行（需把你的 profile 注册为 iCode agent profile）
1. 把 `six_stage_pipeline.py` 放到 iCode workflow 目录（project：`<cwd>/.chrys/workflows/`；或 global config `workflows/`）。
2. 把你的 agent profile（对应上述占位符）注册为 iCode agent profile（`AgentProfileRegistry`，selector 用 `name`），开启安全 hooks。
3. 在 iCode TUI 的 Workflows 面板选 `delivery-pipeline` 运行；或 `chrys workflow run delivery-pipeline --input <需求文本>`。
4. **已知**：iCode 在 admission 阶段解析 `profile=`；若某 profile 未注册 → `REJECT_AGENT_PROFILE_MISSING`，workflow 被拒（这是集成依赖，非本 workflow 缺陷）。

---

## 7. 已知限制（如实报告，未假装全功能）

1. **无 YAML DSL**：iCode v0.28.0 只支持 Python builder。已用 `.py`，符合"优先用运行时原生支持的格式"。
2. **profile 未注册即可构造**：`build()` 只做结构校验（节点唯一/无环/可达/callable 形状），**不校验 profile 是否存在**；profile 在 admission 阶段由 `AgentProfileRegistry` 解析。故离线"内存构造成功"≠运行时一定能跑；真实运行需把你的 profile 注册为 iCode agent profile。
3. **gate 仅为存在性断言**：`g1_security`/`s5_security_gate` 断言 hooks 就位，并不在 workflow 内执行红线性拦截；拦截由运行时 hooks 子系统完成。
4. **未实跑 runtime 执行**：本环境若无 iCode worker runtime，仅做到"builder API 内存构造 + manifest 校验 + profile 名一致性 + hooks 形态校验"，未做端到端运行。属允许的降级表达。
