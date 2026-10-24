# Copyright (c) 2026 chana4afk & 四川爱赋康文化科技有限公司. All rights reserved.
# 原始方法学版权：iCode（openJiuwen / Huawei, Apache-2.0），Copyright (c) Huawei Technologies Co., Ltd. 2026；本文件在其方法学基础上脱敏准备，尊重并保留华为原始版权。
#
# 商标声明：iCode、Chrys、openJiuwen 为其各自权利人的商标，本示例未获其背书。
#
# 许可：本示例以 Apache License 2.0 贡献（与上游同许可）。
# 署名：示例贡献者 / 组织（非个人名）；保留上游 NOTICE 精神。
# 贡献声明（Contribution Notice）：本贡献由 chana4afk & 四川爱赋康文化科技有限公司 在 iCode（openJiuwen / Huawei, Apache-2.0）方法学基础上脱敏准备；尊重并保留华为原始版权，依 Apache-2.0 许可向上游提交，CLA 由贡献者签署。

#
# ============================================================================
# 通用六阶段交付流水线（iCode WorkflowBuilder DSL 示例）
# ============================================================================
# 本示例演示：如何用 iCode v0.28.0 的 WorkflowBuilder 表达一个「六阶段闭环」
# 的交付流水线（澄清 -> 架构 -> 分域工程 -> 集成 -> 质量门禁 -> 交付）。
#
# 重要：本文件中的 agent 节点 profile 均为「通用占位符」(如 <delivery-lead>、
# <security-agent>)。它们不代表任何专有团队或角色，仅用于演示 DSL 结构与编排
# 方式。实际使用时，请把每个 profile= 替换为你方真实的 agent profile 选择器
# （即你方已在 iCode 注册、且 name 字段与选择器一致的 profile）。
#
# 等保三级（网络安全等级保护）硬卡点以「前置 gate + 阶段 gate」节点声明，
# 真正的红线拦截由 iCode 的 before_tool_call hooks 子系统在每次工具调用前完成；
# 本 workflow 仅「读取并断言」hooks 清单就位，不重实现任何钩子逻辑。
#
# 本文件只依赖 iCode 的公开 WorkflowBuilder API，不依赖任何外部专有模块。
# ============================================================================

from pathlib import Path
from typing import Any

from chrys.workflows import (
    BuilderScope,
    NodeContext,
    NodeHandle,
    Retry,
    Workflow,
    WorkflowBuilder,
    WorkflowValue,
)

# --------------------------------------------------------------------------
# 常量与定位
# --------------------------------------------------------------------------
ICODE_VERSION = "0.28.0"
# 若你方把等保三级 hooks 放在本 workflow 的同级 ../hooks 目录，可在此定位；
# 否则请改为你方实际路径（相对或绝对均可）。
_HOOKS_FILE = (Path(__file__).resolve().parent / ".." / "hooks" / "security-l3-hooks.yaml").resolve()

# 等保三级前置 gate 期望：5 个 before_tool_call 钩子，且全部 fail-closed。
EXPECTED_HOOK_EVENTS = 5
EXPECTED_FAIL_CLOSED = 5

# 每个 agent 节点统一追加的「等保三级卡点」提醒（仅作 instructions 后缀，不重实现钩子逻辑）。
SECURITY_SUFFIX = (
    " 本节点受等保三级 hooks(before_tool_call) fail-closed 硬卡点约束：涉及写/删/外发/提权/"
    "跨租户动作须先定 severity，高危动作由 <security-agent> 共签卡点放行；红线由 hooks 在工具"
    "调用前硬拦截，本段仅作提醒。"
)


def _assert_security_harness() -> None:
    """读取 hooks 清单（只读），断言 5 个 before_tool_call + 全 fail-closed。

    这是 gate 的『存在性/形态』校验，不是钩子执行逻辑本身。钩子执行由 iCode runtime 负责。
    """
    if not _HOOKS_FILE.is_file():
        raise RuntimeError(
            f"等保三级 gate 失败：找不到 hooks 清单 {_HOOKS_FILE}。"
            "本流水线要求 5 个 before_tool_call fail-closed 钩子已就位。"
        )
    text = _HOOKS_FILE.read_text(encoding="utf-8")
    n_events = text.count("event: before_tool_call")
    n_failclosed = text.count("execution: { mode: blocking")
    if n_events != EXPECTED_HOOK_EVENTS:
        raise RuntimeError(
            f"等保三级 gate 失败：before_tool_call 钩子数={n_events}，期望 {EXPECTED_HOOK_EVENTS}。"
        )
    if n_failclosed != EXPECTED_FAIL_CLOSED:
        raise RuntimeError(
            f"等保三级 gate 失败：fail-closed 钩子数={n_failclosed}，期望 {EXPECTED_FAIL_CLOSED}。"
        )


# --------------------------------------------------------------------------
# 通用节点体（仅 gate / join；agent 节点无体，靠 profile + instructions_suffix）
# --------------------------------------------------------------------------
def _security_gate(value: WorkflowValue) -> str:
    """流水线前置 gate（start 节点）：断言等保三级钩子已就位，再放行。"""
    _assert_security_harness()
    return value.text


def _stage5_security_gate(value: WorkflowValue) -> str:
    """阶段 5（质量门禁）的等保三级 gate：再次断言等保三级钩子就位。"""
    _assert_security_harness()
    return value.text


def _stage_merge(value: WorkflowValue) -> str:
    """各阶段 join 的目标节点：汇总上游并行 agent 的文本产出，向下一阶段 handoff。"""
    return value.text


# --------------------------------------------------------------------------
# 构建 workflow
# --------------------------------------------------------------------------
wf = WorkflowBuilder(
    "delivery-pipeline · 通用六阶段交付流水线",
    description=(
        "六阶段交付闭环：澄清->架构->分域工程->集成->质量门禁->交付；"
        "等保三级 hooks 作为前置 gate。iCode v0.28.0。"
        "所有 agent 节点 profile 为通用占位符，请替换为你方真实 profile。"
    ),
)

# 前置 gate（start）。
g1_security: NodeHandle = wf.python("g1_security", _security_gate)

# --------------------------------------------------------------------------
# 阶段 1 · 需求澄清（Requirement Clarification）
#   由人类或 <delivery-lead> 主持；各参与 profile 标注合规边界与意图理解。
# --------------------------------------------------------------------------
s1_legal: NodeHandle = wf.agent(
    "s1_legal", profile="<legal-agent>",
    instructions_suffix="需求澄清：标注数据分级与合规边界、PIPL/关基/数据出境一票否决口径。" + SECURITY_SUFFIX,
)
s1_ip: NodeHandle = wf.agent(
    "s1_ip", profile="<ip-agent>",
    instructions_suffix="需求澄清：标注 IP/敏感代码边界，外发与开源前置。" + SECURITY_SUFFIX,
)
s1_intent: NodeHandle = wf.agent(
    "s1_intent", profile="<intent-agent>",
    instructions_suffix="需求澄清：精确理解需求方意图，做最短路径编排与资源边界判定。" + SECURITY_SUFFIX,
)
s1_market: NodeHandle = wf.agent(
    "s1_market", profile="<market-agent>",
    instructions_suffix="需求澄清：市场/竞品/需求可行性调研，输出可证伪结论。" + SECURITY_SUFFIX,
)
s1_lead: NodeHandle = wf.agent(
    "s1_lead", profile="<delivery-lead>",
    instructions_suffix="需求澄清 host：主持需求澄清回合、仲裁跨角色冲突，须澄清后再并行。" + SECURITY_SUFFIX,
)
s1_tax: NodeHandle = wf.agent(
    "s1_tax", profile="<tax-agent>",
    instructions_suffix="需求澄清：标注跨境税务/外汇合规边界。" + SECURITY_SUFFIX,
)
s1_merge: NodeHandle = wf.python("s1_merge", _stage_merge)
wf.join([s1_legal, s1_ip, s1_intent, s1_market, s1_lead, s1_tax], s1_merge)

# --------------------------------------------------------------------------
# 阶段 2 · 架构焊死（Architecture Freeze）
#   由 <architect> 焊死分层架构并出架构决策；参与：OS 底座 / 数据底座 / 中间件与隔离 /
#   密码工程 / 技术雷达 / 主机加固。
# --------------------------------------------------------------------------
s2_architect: NodeHandle = wf.agent(
    "s2_architect", profile="<architect>",
    instructions_suffix="架构焊死：焊死分层架构（OS + 数据底座 + 前端框架），输出架构决策与隔离等级。" + SECURITY_SUFFIX,
)
s2_agentos: NodeHandle = wf.agent(
    "s2_agentos", profile="<agent-os-agent>",
    instructions_suffix="架构焊死：智能体 OS 底座与多租户中间件对齐。" + SECURITY_SUFFIX,
)
s2_db: NodeHandle = wf.agent(
    "s2_db", profile="<db-agent>",
    instructions_suffix="架构焊死：数据底座与向量库维度对齐门禁。" + SECURITY_SUFFIX,
)
s2_mw: NodeHandle = wf.agent(
    "s2_mw", profile="<mw-agent>",
    instructions_suffix="架构焊死：中间件/租户隔离等级落位（L0-L3）。" + SECURITY_SUFFIX,
)
s2_crypto: NodeHandle = wf.agent(
    "s2_crypto", profile="<crypto-agent>",
    instructions_suffix="架构焊死：密码工程（国密/TLS/PQC）转化为等保三级与密评合规要点。" + SECURITY_SUFFIX,
)
s2_techradar: NodeHandle = wf.agent(
    "s2_techradar", profile="<techradar-agent>",
    instructions_suffix="架构焊死：技术版本雷达与栈选型事实源。" + SECURITY_SUFFIX,
)
s2_harden: NodeHandle = wf.agent(
    "s2_harden", profile="<hardening-agent>",
    instructions_suffix="架构焊死：OS/主机加固与底层安全基线对齐。" + SECURITY_SUFFIX,
)
s2_merge: NodeHandle = wf.python("s2_merge", _stage_merge)
wf.join([s2_architect, s2_agentos, s2_db, s2_mw, s2_crypto, s2_techradar, s2_harden], s2_merge)

# --------------------------------------------------------------------------
# 阶段 3 · 分域工程（Domain Engineering）——并行 fan-out
#   各域工程角色并行；每域附『等保三级对照结论』。
# --------------------------------------------------------------------------
s3_app: NodeHandle = wf.agent(
    "s3_app", profile="<app-agent>",
    instructions_suffix="分域工程：应用/客户端全谱代码实现，内置安全默认。" + SECURITY_SUFFIX,
)
s3_be: NodeHandle = wf.agent(
    "s3_be", profile="<backend-agent>",
    instructions_suffix="分域工程：应用层服务端实现（API/微服务），后端安全默认 + 租户隔离 fail-closed。" + SECURITY_SUFFIX,
)
s3_fe: NodeHandle = wf.agent(
    "s3_fe", profile="<frontend-agent>",
    instructions_suffix="分域工程：非移动 Web 前端。" + SECURITY_SUFFIX,
)
s3_lowcode: NodeHandle = wf.agent(
    "s3_lowcode", profile="<lowcode-agent>",
    instructions_suffix="分域工程：低代码/智能前端。" + SECURITY_SUFFIX,
)
s3_ui: NodeHandle = wf.agent(
    "s3_ui", profile="<ui-agent>",
    instructions_suffix="分域工程：UI 风格与组件方案 + 等保三级 UI 安全对照结论。" + SECURITY_SUFFIX,
)
s3_mobile: NodeHandle = wf.agent(
    "s3_mobile", profile="<mobile-agent>",
    instructions_suffix="分域工程：移动端技术栈决策 + 小程序/App 交付 + 等保三级移动端对照结论。" + SECURITY_SUFFIX,
)
s3_dist: NodeHandle = wf.agent(
    "s3_dist", profile="<distributed-agent>",
    instructions_suffix="分域工程：分布式平台与编排。" + SECURITY_SUFFIX,
)
s3_iot: NodeHandle = wf.agent(
    "s3_iot", profile="<iot-agent>",
    instructions_suffix="分域工程：物联网域工程 + 等保三级对照结论。" + SECURITY_SUFFIX,
)
s3_embodied: NodeHandle = wf.agent(
    "s3_embodied", profile="<embodied-agent>",
    instructions_suffix="分域工程：具身 AI 工程。" + SECURITY_SUFFIX,
)
s3_model: NodeHandle = wf.agent(
    "s3_model", profile="<model-agent>",
    instructions_suffix="分域工程：模型优化与跨模型记忆底座对齐。" + SECURITY_SUFFIX,
)
s3_lang: NodeHandle = wf.agent(
    "s3_lang", profile="<lang-agent>",
    instructions_suffix="分域工程：语言版本库依据，禁止跳过版本库直定版本。" + SECURITY_SUFFIX,
)
s3_eff: NodeHandle = wf.agent(
    "s3_eff", profile="<efficiency-agent>",
    instructions_suffix="分域工程：协同效率督导。" + SECURITY_SUFFIX,
)
s3_db_impl: NodeHandle = wf.agent(
    "s3_db_impl", profile="<db-agent>",
    instructions_suffix="分域工程：存储/数据契约实现。" + SECURITY_SUFFIX,
)
s3_mw_impl: NodeHandle = wf.agent(
    "s3_mw_impl", profile="<mw-agent>",
    instructions_suffix="分域工程：中间件/缓存/消息实现与隔离落位。" + SECURITY_SUFFIX,
)
s3_harden: NodeHandle = wf.agent(
    "s3_harden", profile="<hardening-agent>",
    instructions_suffix="分域工程：OS/主机加固与底层安全基线对齐。" + SECURITY_SUFFIX,
)
s3_merge: NodeHandle = wf.python("s3_merge", _stage_merge)
wf.join(
    [
        s3_app, s3_be, s3_fe, s3_lowcode, s3_ui, s3_mobile, s3_dist, s3_iot,
        s3_embodied, s3_model, s3_lang, s3_eff, s3_db_impl, s3_mw_impl, s3_harden,
    ],
    s3_merge,
)

# --------------------------------------------------------------------------
# 阶段 4 · 集成（Integration）
#   由 <architect> 跨域集成；<backend-agent> 接口与数据契约；测试 / 供应链门禁 / 投递。
# --------------------------------------------------------------------------
s4_architect: NodeHandle = wf.agent(
    "s4_architect", profile="<architect>",
    instructions_suffix="集成：跨域集成裁决，接口与数据契约一致性。" + SECURITY_SUFFIX,
)
s4_be: NodeHandle = wf.agent(
    "s4_be", profile="<backend-agent>",
    instructions_suffix="集成：接口与数据契约、租户隔离串联零容忍验证。" + SECURITY_SUFFIX,
)
s4_qa: NodeHandle = wf.agent(
    "s4_qa", profile="<qa-agent>",
    instructions_suffix="集成：集成/E2E 测试即代码，验收测试与交付验收证据。" + SECURITY_SUFFIX,
)
s4_quality: NodeHandle = wf.agent(
    "s4_quality", profile="<quality-agent>",
    instructions_suffix="集成：供应链门禁（SBOM/SCA/EPSS/KEV/VEX）协同。" + SECURITY_SUFFIX,
)
s4_repo: NodeHandle = wf.agent(
    "s4_repo", profile="<repo-agent>",
    instructions_suffix="集成：已定稿代码的精准投递。" + SECURITY_SUFFIX,
)
s4_merge: NodeHandle = wf.python("s4_merge", _stage_merge)
wf.join([s4_architect, s4_be, s4_qa, s4_quality, s4_repo], s4_merge)

# --------------------------------------------------------------------------
# 阶段 5 · 质量门禁（Quality Gate）——等保三级 gate 在此
#   由 <quality-agent>（双闸）+ <security-agent>（红蓝对抗）+ <legal-agent>（一票否决）
#   + <ip-agent>（IP 复核）+ <clerk-agent>（留痕）协同。
# --------------------------------------------------------------------------
s5_quality: NodeHandle = wf.agent(
    "s5_quality", profile="<quality-agent>",
    instructions_suffix="质量门禁：SBOM/SCA/EPSS/KEV/VEX + 产物双闸；交付脚本门禁。" + SECURITY_SUFFIX,
)
s5_legal: NodeHandle = wf.agent(
    "s5_legal", profile="<legal-agent>",
    instructions_suffix="质量门禁：合规判定一票否决（等保/密评/数据出境/关基/PIPL）。" + SECURITY_SUFFIX,
)
s5_ip: NodeHandle = wf.agent(
    "s5_ip", profile="<ip-agent>",
    instructions_suffix="质量门禁：IP 四维复核。" + SECURITY_SUFFIX,
)
s5_clerk: NodeHandle = wf.agent(
    "s5_clerk", profile="<clerk-agent>",
    instructions_suffix="质量门禁：全量记录与督导留痕。" + SECURITY_SUFFIX,
)
s5_security: NodeHandle = wf.agent(
    "s5_security", profile="<security-agent>",
    instructions_suffix="质量门禁：等保三级卡点 owner，红蓝对抗『不可被攻破/可自愈』判定与对照结论共签放行。" + SECURITY_SUFFIX,
)
s5_security_gate: NodeHandle = wf.python("s5_security_gate", _stage5_security_gate)
s5_merge: NodeHandle = wf.python("s5_merge", _stage_merge)
wf.join([s5_quality, s5_legal, s5_ip, s5_clerk, s5_security, s5_security_gate], s5_merge)

# --------------------------------------------------------------------------
# 阶段 6 · 双模交付（Dual-mode Delivery）
#   由 <quality-agent> 交付门禁；<repo-agent> 确投；<clerk-agent> 回执；
#   <oss-agent> 开源贡献；<strategic-agent> 情报分析；<intent-agent> 收口。
# --------------------------------------------------------------------------
s6_quality: NodeHandle = wf.agent(
    "s6_quality", profile="<quality-agent>",
    instructions_suffix="双模交付：交付门禁（等保三级全环节卡点 + SBOM/SCA + 产物双闸）终检。" + SECURITY_SUFFIX,
)
s6_repo: NodeHandle = wf.agent(
    "s6_repo", profile="<repo-agent>",
    instructions_suffix="双模交付：确投（仅投递通过合规联合定稿确认书的产物）。" + SECURITY_SUFFIX,
)
s6_clerk: NodeHandle = wf.agent(
    "s6_clerk", profile="<clerk-agent>",
    instructions_suffix="双模交付：交付验收留痕 + 可复算命令回执。" + SECURITY_SUFFIX,
)
s6_oss: NodeHandle = wf.agent(
    "s6_oss", profile="<oss-agent>",
    instructions_suffix="双模交付：开源贡献/社区协同（受开源贡献合规门禁约束）。" + SECURITY_SUFFIX,
)
s6_strategic: NodeHandle = wf.agent(
    "s6_strategic", profile="<strategic-agent>",
    instructions_suffix="双模交付：情报分析；情报仅走公开渠道。" + SECURITY_SUFFIX,
)
s6_intent: NodeHandle = wf.agent(
    "s6_intent", profile="<intent-agent>",
    instructions_suffix="双模交付：收口协同与资源编排，冲突上抛协调人。" + SECURITY_SUFFIX,
)
s6_lead: NodeHandle = wf.agent(
    "s6_lead", profile="<delivery-lead>",
    instructions_suffix="双模交付收口：交付验收四步与上线 checklist 收口，须过质量门禁方可交付。" + SECURITY_SUFFIX,
)
s6_merge: NodeHandle = wf.python("s6_merge", _stage_merge)
wf.join([s6_quality, s6_repo, s6_clerk, s6_oss, s6_strategic, s6_intent, s6_lead], s6_merge)

# --------------------------------------------------------------------------
# 串联：前置 gate -> 各阶段 merge 依次 handoff（阶段内并行 fan-out，阶段间串行）
# --------------------------------------------------------------------------
wf.start(g1_security)
wf.edge(g1_security, s1_legal)
wf.edge(g1_security, s1_ip)
wf.edge(g1_security, s1_intent)
wf.edge(g1_security, s1_market)
wf.edge(g1_security, s1_lead)
wf.edge(g1_security, s1_tax)

wf.edge(s1_merge, s2_architect)
wf.edge(s1_merge, s2_agentos)
wf.edge(s1_merge, s2_db)
wf.edge(s1_merge, s2_mw)
wf.edge(s1_merge, s2_crypto)
wf.edge(s1_merge, s2_techradar)
wf.edge(s1_merge, s2_harden)

wf.edge(s2_merge, s3_app)
wf.edge(s2_merge, s3_be)
wf.edge(s2_merge, s3_fe)
wf.edge(s2_merge, s3_lowcode)
wf.edge(s2_merge, s3_ui)
wf.edge(s2_merge, s3_mobile)
wf.edge(s2_merge, s3_dist)
wf.edge(s2_merge, s3_iot)
wf.edge(s2_merge, s3_embodied)
wf.edge(s2_merge, s3_model)
wf.edge(s2_merge, s3_lang)
wf.edge(s2_merge, s3_eff)
wf.edge(s2_merge, s3_db_impl)
wf.edge(s2_merge, s3_mw_impl)
wf.edge(s2_merge, s3_harden)

wf.edge(s3_merge, s4_architect)
wf.edge(s3_merge, s4_be)
wf.edge(s3_merge, s4_qa)
wf.edge(s3_merge, s4_quality)
wf.edge(s3_merge, s4_repo)

wf.edge(s4_merge, s5_quality)
wf.edge(s4_merge, s5_legal)
wf.edge(s4_merge, s5_ip)
wf.edge(s4_merge, s5_clerk)
wf.edge(s4_merge, s5_security)
wf.edge(s4_merge, s5_security_gate)

wf.edge(s5_merge, s6_quality)
wf.edge(s5_merge, s6_repo)
wf.edge(s5_merge, s6_clerk)
wf.edge(s5_merge, s6_oss)
wf.edge(s5_merge, s6_strategic)
wf.edge(s5_merge, s6_intent)
wf.edge(s5_merge, s6_lead)

# 输出：门禁证据 + 最终交付物
wf.output(s5_merge)
wf.output(s6_merge)

# iCode 加载模块级变量 `workflow`。build() 校验整图：节点名唯一、无环、可达、callable 形状
# （不校验 profile 是否存在——profile 在 admission 阶段由 AgentProfileRegistry 解析）。
workflow: Workflow = wf.build()
