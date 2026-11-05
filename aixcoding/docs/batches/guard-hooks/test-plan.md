# 测试方案：安全护栏（guard hooks）迁入

> 活文档：随时更新、不签认；其质量由双 AI 自审（SELF-REVIEW-HOWTO）与行为锚定测试本身保障。

## 测试范围与策略

- **落点**：pytest 用例在 `aixcoding/tests/guard/`（定制测试唯一落点，R4）；规则自检
  `guard_pack_selftest.py` 与部署验证 `verify_guard_deployed.py` 同目录，显式运行；手工安装器
  在 `aixcoding/tools/guard_install_merge.py`。
- **层级**：
  - 单元——自装器算法（fresh / merge / flow-style YAML / 损坏备份 / 幂等 / 原子 / legacy 清理）；
  - 契约——规则包资产清单、hatch 自动打包边界、`hooks.yaml` 模板经上游 loader 真实加载；
  - 规则——70 条 selftest（每条规则正例 + 负例，含 T-MCP-01～03 与必需 ID 集断言）；
  - 端到端——CI `aixcoding-tests.yml` 三步（安装 → 规则自检 70 条 → 部署验证 64 例，三平台）；
    打包冒烟归 CI Build / wheel 检查。
- **行为锚定测试**（§5：真调上游符号断言行为，禁 mock 锚点本身）：
  - `get_platform()`（假设 2）：真调断言 `config_dir` 路径语义；
  - hooks 事件/配置契约（假设 3）：用上游 `service.hooks.loader` 真实加载包内 `hooks.yaml`；
  - `bootstrap_runtime()`（C136，假设 1）：临时环境隔离子进程真实调用，断言护栏落位；
  - `PYAPP_PROJECT_NAME`（假设 4）：扫描构建脚本；
  - hatch 包内资产（假设 5）：`test_packaging.py` 校验运行时文件完整、无仓库根副本、无
    force-include 映射；wheel 内容实证归 CI Build。
- **mock 限制**：仅允许 mock 锚点之外的协作对象；隔离用真实环境变量重定向。

## 覆盖矩阵

| 验收标准（requirements.md） | 测试入口 | 类型 |
|---|---|---|
| 1. 全新环境启动后 guard/ 与两条目就位 | `test_guard_install.py::test_fresh_install_writes_template_and_payload`；`test_anchors.py::test_bootstrap_installs_the_guard`；CI guard job | 单元 + 行为锚定 + 端到端 |
| 2. 已有 hooks.yaml：无关保留 / 按 id 替换 / 损坏备份重置 / flow-style 保留 | `test_guard_install.py` 的 merge、idempotent、flow hooks、flow settings 四组用例 | 单元 |
| 3. 每条规则一个正例且必需 ID 不缺 | `guard_pack_selftest.py` 70 条；`test_every_required_rule_id_is_present` | 规则自检 |
| 4. 部署验证三平台全绿（MANUAL_TESTS 64 例） | CI guard job 的 `verify_guard_deployed.py` | 端到端 |
| 5. wheel 内含全部运行时资产 | `test_packaging.py::test_runtime_pack_is_complete_and_fully_classified`、`::test_hatch_packages_the_pack_without_a_force_include_mapping`；CI Build / wheel 检查 | 契约 + 端到端 |
| 6. `gen_customizations --strict` 全绿 | `verify.py`（C136/C137 登记在位） | 契约 |
| 7. ruff / format / ty 全绿 | C137 最小豁免下跑三门禁；无 T201/noqa/ty override | 契约 |
| 8. 非法规则配置与决策写入故障不得静默放行 | `guard_pack_selftest.py::test_malformed_rules_are_rejected_instead_of_skipped`、`::test_bad_confirm_disposition_is_rejected`、`::test_prompt_hook_returns_nonzero_when_block_decision_cannot_be_written` | 规则 + 子进程 |
| 9. F7 写入范围无绕过 | selftest 的紧贴重定向 / `zip` / `Copy-Item`·`Move-Item` 用例；`test_guard_core.py` 的真实 junction/symlink 双入口用例 | 规则 + 真实文件系统 |

无覆盖空缺。

## 异常与降级路径

- **安装器任意异常 → fail-open**：`test_anchors.py::test_bootstrap_survives_install_failure`；
- **规则配置结构/枚举非法 → GuardError → fail-closed**：selftest 的 Engine/HookProcess 用例；
- **block 决策写不进去 → 非零退出**：selftest 的决策写入故障用例；
- **损坏 hooks.yaml**：备份 `.pre-guard` 后重置；
- **合法 flow-style YAML**：数据保留合并，不删除无关 hooks/settings；
- **legacy payload 清理**：安装后移除 0.31 前旧文件名；
- **并发首装竞态**：不自动化（与旧分支等同，风险自限），排障路径在 OPERATIONS 文档。

## 手工验证步骤（结果记入 verification.md）

1. 全新临时环境安装后：`uv run aixcoding-cli hook validate` → PASS；`hook list` 两条目；
2. 拦截行为抽测（Windows 真机）：提示词层 P01 拦、P11 放行；工具层 shell 拦、白名单写放行；
3. `uv run python aixcoding/tests/guard/verify_guard_deployed.py -v` 全绿；
4. CI：三平台 guard job 绿；上游 CI 与定制 CI 全绿后提 PR。
