# 设计：安全护栏运行期开关（guard switches）

<!--
签认规则（R10）：责任人本人编辑本头部（状态→已确认、填日期，确认意见可选），AI 不得代填。
签认后修改正文需重新签认。校验：uv run python aixcoding/tools/check_docs.py
-->

- 责任人：李海潮
- 状态：已确认
- 确认日期：2026-11-05 19:03:50
- 确认意见：将出厂使用同事(汤老师的方案(TRUE),其余进行合并，保证我的工能不丢失，他的工能不缺少)（可选；审阅中形成的判断、附加条件或保留）

## 方案概述

判定＝**逐块合成**：开关能力全部取自同事 v2 交付（0.28 分支），实现形态全部落在
0.33.2 骨架的定制包内。**不整文件覆盖**——同事那三份文件缺少本仓已落地的 10 项加固
（见 `merge-conflicts.md` §5），覆盖即回退。

```
rules.json（随包，每次启动重装）         guard_state.json（现场，重装保留）
   settings.enabled                         enabled              → 总开关
   settings.write_scope.allow_outside_…     rules{id:bool}       → 单条规则覆盖
   prompt_rules / tool_rules                write_scope{…}       → 写入范围覆盖
        │                                         │
        └──────> GuardConfig.__init__（严格校验）<┘
                            │
                     apply_state()（覆盖后按类型校验）
                            │
                  run_hook → master_enabled / *_evidence
                            │
                guardctl.py / guardctl.cmd（管理入口，装到 config_dir/hooks/guard/）
```

要点：

1. **状态读取**：`GuardConfig` 增加 `state` / `state_path` 两个字段与同名关键字参数；
   `load_config()` 先构造（走既有 `_validate_*` 严格校验）再 `apply_state()`。
   `apply_state()` 对覆盖值补布尔类型校验——同事原实现 `bool("false") == True`
   会把字符串误判为"允许"，属于静默反向，本批次修正。
2. **写入范围门闸**：`_write_scope_evidence()` 的门闸改为
   `if not config.write_scope_restricted: return []`（状态可覆盖出厂值），
   函数体内的路径比较保留我方 `_canonical_scope()` 的符号链接 / junction 解析。
3. **出厂值**：`rules.json` 的 `allow_outside_workspace` 改 `true`（裁决 C1，
   见 `adr/0001-guard-write-scope-factory-default.md`）。
4. **管理入口**：`pack/guardctl.py`（标准库、3.9+、零耦合）原样移植，改 4 处：
   模块路径文案、`# ty: ignore[unresolved-import]`、状态文件环境变量随
   `_guard_core.STATE_PATH_ENV`、文档命令形态。
   `pack/guardctl.cmd` 原样移植（ASCII、无 BOM）；`pack/_launcher.ps1` 换成 v2 版
   （`GUARD_SCRIPT` 支持绝对路径），否则 cmd 传入的绝对路径会被二次拼接。
5. **环境变量**：统一 `AIXCODING_GUARD_RULES` / `_AUDIT` / `_STATE`
   （同事用的是 `ICODE_*`，与 0.33.2 已统一的品牌前缀不一致）。
6. **打包**：三个新文件放在 `pack/`，随 `packages = ["src/chrys"]` 进 wheel，
   **无 force-include、无仓库根副本**；`GUARD_PAYLOAD_FILES` 与手工安装器
   `PAYLOAD_FILES` 同步为 7 个文件。

## 备选方案对比

| 方案 | 优点 | 缺点 | 升级冲突面（文件数×行数×耦合度，R1） | 结论 |
|---|---|---|---|---|
| A 整文件覆盖（取同事三份文件） | 落地最快，改动量最小 | 回退 10 项加固：严格校验、写决策非零退出、符号链接解析、`zip`/重定向/PowerShell 写目标、flow-style 合并、env 前缀；目录与打包形态不匹配（`chrys/app/guard_hooks/` 在本仓不存在） | 0（但质量回退） | 否 |
| **B 逐块合成（选定）** | 开关能力全取，加固全留，零上游业务代码 | 需处理 6 处真冲突 + 14 项半冲突（全部在定制包内）；`_guard_core.py` 约 60 行合成 | 定制包 12 文件；上游 1 行 | **是** |
| C 只带 `guardctl.cmd`，不带 `guard_state.json` 语义 | 改动更小 | 命令存在但读不到状态 → 关不掉、也放宽不了，"有命令不生效"比没命令更危险 | 同 B 减 3 文件 | 否 |
| D 自研开关（不取同事实现） | 无外部依赖 | 同一份 0.28 能力重写一遍，纯重复建设；两套口径后续必然分叉 | 更大 | 否 |

## 上游改动清单（R2）

| 文件 | 改什么 | 预估行数 | 标记注释位置 |
|---|---|---|---|
| `.gitattributes` | `# Shell` 段增一行 `*.cmd text eol=crlf`，保证 `guardctl.cmd` 检出为 CRLF（`* text=auto` 不会把 `.cmd` 规范成 LF） | +1 行（含 C138 标记注释共 +2） | 新增行上方单行标记（`AIxCoding guard: … (C138)`） |

**R1 自查（必答）**：业务逻辑是否已全部外置到定制模块？是——开关引擎在
`src/chrys/kernel/aixcoding/guard/pack/_guard_core.py`，管理入口在同目录
`guardctl.py` / `guardctl.cmd`，安装器在 `src/chrys/kernel/aixcoding/guard/guard_install.py`，
测试与工具按 R4 分流到 `aixcoding/tests/guard/`、`aixcoding/tools/`，文档在本目录。
接线点是否已最小化（能一处不两处）？是——**零新增接线点**：`bootstrap_runtime()`
的安装调用在 `batches/guard-hooks` 已就位，本批次不碰它。上游仅一处行尾声明，
无可外置逻辑。

## 对上游隐式行为的假设

1. **`.gitattributes` 的 `* text=auto` 统一规范化行尾** → 新增 `*.cmd text eol=crlf`
   后 Windows 检出的 `.cmd` 为 CRLF → 失效表现：检出为 LF；cmd.exe 一般仍能执行，
   但 `setlocal` / 标签解析在老系统上有隐患（自检只断言 ASCII 与无 BOM，不断言行尾）。
   anchor＝仓库根 `.gitattributes`。
2. **hatch 自动打包包内非 Python 资产**（沿用 `batches/guard-hooks` 假设 5）→
   `pack/` 下的 `.cmd` / `.ps1` / `.json` 一并进 wheel，无需 force-include →
   失效表现：wheel 缺 `guardctl.cmd`，现场无管理入口；`test_packaging.py` 与
   CI Build 侦测。
3. **hook 子进程按 `rules.json` 同目录读状态文件** → `guard_state.json` 落在
   `<config_dir>/hooks/guard/`，与 `rules.json` 同级 → 失效表现：状态读不到，
   开关静默失效（`guardctl status` 会打印它认为的路径，可核对）。
4. **`_launcher.ps1` 的 `GUARD_SCRIPT` 相对/绝对两态** → 我方 `hooks.yaml` 传
   `guard\_prompt_guard.py`（相对），`guardctl.cmd` 传绝对路径 → 失效表现：任一侧
   二次拼接得到坏路径，hook fail-closed（每句被拦，可观测）或 cmd 直接不可用。

## 定制点登记预告

| id | 落点 | marker 锚 | 预期计数 | anchor | risk | removal_condition |
|---|---|---|---|---|---|---|
| C138 | `.gitattributes` | AIxCoding guard: | 1 | （批次锚注） | 低：上游重排该文件段序时可能被覆盖 | 〔上游重排〕`.gitattributes` 不再需要单独行尾声明（如上游统一声明 `*.cmd`） |

## 风险与最坏情况

- **C1 放宽出厂写入范围**：写到 `.ssh`、启动项、计划任务目录不再被拦；
  `T-FS-02/03` 的**读**路径黑名单不受影响。回滚＝`rules.json` 一行 + 三处测试前置。
- **用户误关总开关后以为护栏还在**：`guardctl status` 会显示"总开关：关"；
  闭合状态下 `hook` 仍被调用但不写决策，审计留痕也停止记录命中（可观测）。
- **状态文件被手改坏**：解析失败按出厂默认处理（= 开 + 不限制写入）并告警，
  与 `fail_closed` 取向一致（不因一个坏文件把护栏静默关掉）。
- **`guardctl.py` 以两个模块身份各加载一次 `_guard_core`**：`guardctl` 通过
  `sys.path.insert` 加载同目录 `_guard_core`，包内又有一份。当前只读常量、
  无 `isinstance` 跨模块比较，风险可接受；若将来需要共享类型，改为绝对导入。
- **回滚路径**：单 commit 可 revert；开关关闭只需 `guardctl reset --master`，
  卸载护栏用 `aixcoding-cli uninstall`。
- **最坏情况**：`_launcher.ps1` 变更导致 Windows 上 hook 全部 fail-closed
  （每句被拦）。可观测且可恢复：`hook list` 会显示 run 形态，`guard_install_merge --check`
  会报健康状态。

## ADR 判定

- ① 有被认真考虑但否决的备选方案 → **是**（上表 A / C / D）。
- ② 撤销代价高（依赖方向/目录落点/数据格式/对外契约）→ 否：全部在定制包内，
  状态文件是新增数据格式但不构成对外契约（可整体删除回到文件级开关）。
- ③ 约束未来工作模式，后来者会问"为什么" → **是**：出厂不拦截写工作区外，
  后来者必然问"为什么规则包里有 `T-FS-01` 却不生效"。
- ④ 依赖上游非公开接口或行为 → 否（`bootstrap_runtime` / `get_platform` /
  hatch 打包均为公开或已在 `batches/guard-hooks` 登记）。

结论：①③ 成立 → 本批次出 `adr/0001-guard-write-scope-factory-default.md`
（功能级决策，落 `batches/guard-switches/adr/`）。
