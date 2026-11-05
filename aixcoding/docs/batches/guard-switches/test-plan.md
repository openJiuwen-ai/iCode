# 测试方案：安全护栏运行期开关

> 活文档，随时更新、不签认。验证证据落 `verification.md`（命令 + 输出摘要）。

## 测试策略

三层，与 `batches/guard-hooks` 保持一致：

1. **规则自检**（纯标准库、不进 pytest 收集）：`aixcoding/tests/guard/guard_pack_selftest.py`
   ——每条规则一个正例 + 若干负例，外加开关语义与命令行行为；
2. **部署验证**：`aixcoding/tests/guard/verify_guard_deployed.py` 对**已安装**的规则包
   回放 MANUAL_TESTS 的 61 条机器用例（含写入范围）；
3. **pytest**：`aixcoding/tests/guard/test_guard_install.py`（安装器行为）、
   `test_packaging.py`（打包完整性）、`test_anchors.py`（上游锚点）。

## 覆盖矩阵

| 验收标准 | 测试 | 类型 |
|---|---|---|
| 三个新文件随包分发、`pack/` 文件集完整 | `test_packaging.py::test_runtime_pack_is_complete_and_fully_classified` | pytest |
| `_launcher.ps1` 支持绝对路径 | `guard_pack_selftest.py::test_launcher_accepts_an_absolute_script_path` | 自检（文本断言） |
| `guardctl.cmd` 随包、纯 ASCII、无 BOM、指向同目录启动器 | `test_cmd_entry_point_ships_with_the_pack` / `test_cmd_points_at_the_launcher_beside_itself` | 自检 |
| 无 uv 现场能跑通开关管理 | `test_cmd_runs_the_switch_manager_without_uv` | 自检（Windows-only，无解释器 skip） |
| 总开关 / 单规则 / 通配 / reset / show / workspace 全命令 | `GuardCtlTests` 12 条 | 自检 |
| 状态文件优先于出厂值、缺失/损坏回落 | `GuardStateTests` 8 条 | 自检 |
| 写入范围出厂不限制 | `test_factory_does_not_restrict_writes_to_the_workspace` | 自检 |
| 状态文件可把写入范围收紧 | `test_state_file_can_restore_the_workspace_restriction` | 自检 |
| 限制态下 `T-FS-01` / `T-SHELL-05` 正常拦截 | `test_tfs01_write_scope`、`test_ts05_*`（改 `config_with_write_scope(restricted=True)`） | 自检 |
| 状态值类型非法不静默反向 | `test_malformed_state_values_are_rejected`（新增） | 自检 |
| 装到现场含 guardctl、状态文件重装保留 | `test_guard_install.py` 新增 2 条 | pytest |
| 已装规则包 61 条用例全绿（含 F01 写入范围） | `verify_guard_deployed.py`（前置写限制态 `guard_state.json`） | 部署验证 |
| 三平台 + Windows 中文输出 | CI `guard` job（`PYTHONUTF8: "1"`） | CI |

## 异常与降级路径

| 场景 | 期望 |
|---|---|
| 状态文件不存在 | 出厂默认：总开关开、写入不限制；`guardctl status` 显示"未创建，用出厂默认" |
| 状态文件损坏（非法 JSON / 顶层非对象） | 记告警、按出厂默认处理（宁可多拦，不静默关掉护栏） |
| 状态值类型非法（如 `"enabled": "false"`） | 拒绝该覆盖并告警，不按 `bool()` 真值静默反转 |
| `rules.json` 配置非法（规则字段错、id 重复） | `GuardError` → fail-closed 拦截并提示（既有加固） |
| `guardctl` 找不到已安装副本 | 退回脚本同目录的规则文件；`--rules` / `--state` 可显式指定 |
| 启动器找不到解释器 | 非零退出 + 明确报错（hook 侧 fail-closed，可观测） |
| 决策文件写不出 | `return 1`，让 `on_error: block` 生效（既有加固） |

## 不做

- 流式输出层拦截（旧需求即不实现）；
- 状态文件并发写冲突（`guardctl` 用临时文件 + `os.replace` 原子替换，不做锁）；
- TUI `/hooks` 管理界面。
