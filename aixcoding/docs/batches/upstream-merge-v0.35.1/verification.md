# 验证记录：追上游 v0.35.1（upstream-merge-v0.35.1）

## 一、验证矩阵结果总表（2026-11-09）

| 验证项 | 结果 | 证据 |
|---|---|---|
| 升级前基线：定制套件 | **562 passed, 51 skipped, 2 failed**（98.85s；2 failed = guard symlink 环境性在案） | 基线 HEAD a7efff2e |
| merge 后定制全套 | **562 passed, 51 skipped, 2 failed**（111s；同 2 环境性，零回归） | 同口径复跑 |
| `gen_customizations.py --strict`（merge+C028 退役后） | 99 行/136 marker，0 error 0 warning | 登记迁移后实测 |
| `gen_customizations.py --strict`（C143/C144 增补+重签后） | **101 行/138 marker/6 退役，0 error 0 warning**（与预告精确一致） | 终态实测 |
| 行为锚定 `test_panel_anchor.py` | 4 passed（含于工具/锚定 38 passed 复跑） | 机检 |
| R2 `check_r2_diff.py --base 7c423b06` | OK（commit 终态 upstream_changed=82 全落 approved_pool=90；81/88 为 C143/C144 登记前时点，评审 F1 订正） | 新 merge-base |
| R3 `check_r3_markers.py --base 7c423b06` | **OK**（upstream_changed=82 / marker_checked=79 / exempt=3；mo UD 中间态经 `git rm --cached` 后收敛） | 新 merge-base |
| ruff check（--no-fix）+ format（全目录含 aixcoding/） | 全绿（3046 files already formatted） | CI 同口径 |
| ty 三平台（linux/darwin/win32） | 全绿 | — |
| i18n 管线 extract→update→compile→check ×3 轮 | 全绿；**pot/po hash 三轮逐位一致**（pot 9d82c63b… / po f35406be…） | 三轮实测 |
| C143/C144 适配测试 | 两文件 **54 passed** | 适配后复跑 |
| fork-full `verify.py --full`（首轮） | FAILED：custom 562+2 在案 / upstream **34 failed**, 35818 passed / architecture 未跑 | 归因见第二节 |
| fork-full `verify.py --full`（复跑） | upstream **32 failed, 35820 passed**（+2 = 品牌适配生效；32 = 31 环境性 448 + 1 mo UD 中间态）；custom 562+2 在案 | 真实语义冲突清零 |
| architecture 套件（fork-full 两轮均因 tests 失败未随跑，**单独补跑**） | **1248 passed, 3 skipped**（223s） | 复查缺口闭环 |
| 打包 `uv build` + `check_wheel.py`（merge 带入 hatch hook/mo 入 wheel 新链路） | **通过**（wheel 构建 + 安装探测 + mo 随包与安装态翻译校验） | 复查补强 |
| 依赖对齐 `uv sync --frozen` | 已对齐（CPython 3.14.10 / icode-tui 0.35.1 / babel 2.18.0；uv 升 0.13.7） | — |
| TUI 冒烟 | **通过**：headless `--version`=0.35.1；责任人双窗口人工验证启动/F6 aixcoding tab/登录徽标与命令/F4 禁用无问题；discard 入口因模型同步不可用改自动化验证——上游 rollback/discard 相关 8 个 TUI 测试文件 **153 passed**（中断卡片 Discard 按钮、消息头入口、范围预览与确认链） | test-plan 手工步骤 + rollback_smoke 复跑 |
| PR 双 CI | 待 PR | — |

## 二、冲突点全清单 + 逐点处理意见（runbook §5 交付物 1）

### merge 文本/二进制冲突（4 处，与干跑预判逐字一致）

| # | 文件 | 处置 | 验证 |
|---|---|---|---|
| 1 | `AGENTS.md`（C001/C130/C131） | 两冲突块均保留我方段：①branding 段 + AIxCoding customization 三段 marker 完整保留；②aixcoding-cli 命令示例段保留。上游侧为 trim 后单行版，无新增正文丢失 | strict 138 marker 对账 + 逐段人工比对 |
| 2 | `locales/zh-Hans/LC_MESSAGES/chrys.po`（豁免产物） | 唯一冲突块在 obsolete 区：我方 `settings.model.catalog.base_url.label` 与上游 `settings.model.role.buddy_model_id.label` **两侧都保留** | 管线三轮绿 + hash 幂等 |
| 3 | `src/chrys/foundation/i18n/_catalogs/zh-Hans/LC_MESSAGES/chrys.mo` | modify/delete：**接受上游删除**（改构建时编译 + gitignored）；豁免表 mo 行同步移除（_common.py） | strict 收敛 + 上游 `test_the_generated_catalog_is_never_tracked` 机检（add 后生效） |
| 4 | `tests/foundation/i18n/test_catalog_oracle.py`（C028） | **接受上游去计数化**（删 `== 2704` 计数行）；C028 登记行迁入本批次退役记录，tui design 现役行删除 + 指向注记（R10 重签） | strict 101/138/6 绿 |

### auto-merge 通过但人工语义核对（design 预判点）

- **`main/screen.py`（+101 行 turn 转向/discard/变更预览）**：C013–C017 marker 全在位，
  登录接线未被重构绕过；auth 定制测试绿（strict + 定制全套双重机检）；
- **`diff/screen.py`（244 行变化）**：C006 无徽标特例 marker 在（strict 对账）；
- **`commands.py` / `test_command_suggestions_i18n.py`（C024/C086）**：/login /logout 孪生组
  marker 在；/man 断言融合随 fork-full 通过（该文件未入失败清单）；
- **`orchestration/startup.py`（+57 行）**：C140 guard 安装接线行在，guard 定制测试绿
  （仅 2 个 symlink 环境性在案）；
- **`config_actions.py`（C035）**：恒只读 marker 在，catalog 定制测试绿；
- **`chrys.tcss`（C005）/ `skills/provider.py` / `approval.py`**：branding/徽标 marker 在；
- **固定关注文件（上批次留痕）**：test_manual_reload / test_reload_profiles /
  test_models_dialog_deletion / test_prompt_shortcut_send 的 skip marker 与 reason 前缀
  逐点在（strict 对账）；test_reload_profiles 唯一失败 = WinError 448 环境性（单跑 f2 日志），
  非语义回归；
- **`test_agent_builder_rollback.py`（C102）**：fork-full 通过，未触发新适配。

### merge 后验证暴露的新冲突（2 处，R2 增补已批准 2026-11-09）

fork-full 首轮：upstream **34 failed / 35818 passed**，对账 **32 + 2 = 34** 完整：

| # | 测试 | 根因 | 处置 | 登记 |
|---|---|---|---|---|
| A1 | `test_extension_tool_hooks.py::test_a_failing_after_hook_withholds_the_result_and_later_hooks_see_the_note` | 上游新扩展机制失败提示断言硬编码 `so iCode withheld this result` vs 我方 AIxCoding | 断言品牌适配 + marker（同 C139 先例） | C143 |
| A2 | `test_startup.py::test_bootstrap_runtime_drops_a_proxy_address_httpx_cannot_use` | 代理警告文案断言 `iCode can use` + `display_message.args` 的 `app: "iCode"` 绑定 | 两处断言品牌适配 + marker ×1（计数 1 保持，v0.34.0 C102 教训） | C144 |

其余 32 个 = 环境性/中间态（见下）。

### 环境性失败族（31 个，WinError 448，非代码回归）

`OSError WinError 448 无法遍历该路径，因为它包含不受信任的装入点`——本机 Windows 安全策略
禁止遍历 junction（nvm `.nodejs` 等装入点）。**OS 级对照实验坐实**：系统 Python 直接
`ntpath.realpath(nvm junction)` 即 448，与仓库代码无关；同日 uv 亦被 448 击中
（python minor 链接）。覆盖 tool_metadata_alignment ×9、injection ×6、background/
sub_agents/workflows、junction 参数化用例等；升级前 0.34.0 基线同机全绿 → 本机策略
2026-11-09 新发变化，非本 merge 引入。**实跑验证归 PR 双 CI**（Unix/Windows runner 无此策略）。

另 1 个 `test_the_generated_catalog_is_never_tracked` = mo UD 未暂存中间态（merge add 后消失）。

## 三、定制化改变清单（runbook §5 交付物 2）

- **需新增**：无（C143/C144 为上游测试断言适配，非新功能）；
- **需适配**：① i18n 机制适配（mo 接受删除 + 豁免表 mo 行移除 + oracle 去计数化接受 +
  我方 report 渲染测试期望同步 chrys.mo→chrys.po）；② C143/C144 品牌断言适配（已批准）；
  ③ 环境适配（uv 0.13.7 + Python 3.14.10 + uv sync --frozen）；
- **建议删除（淘汰候选）**：C028 退役（上游侧载体消失，已批准并入退役记录）；
  C004/C140 对比分析见第五节——**AI 建议不命中/保留，待责任人裁决**。

## 四、登记矩阵逐组存活核对结论（runbook §3 结构层）

| 定制组 | 结论 |
|---|---|
| 文档入口（C001/C130/C131） | 存活：三段 marker 完整（冲突 #1 人工逐段比对） |
| 日志面板（C007） | 存活：上游区间零改动 |
| 启动接线（C004） | 存活：上游 app.py 零改动；淘汰对比见第五节 |
| TUI 登录/徽标/命令（C005–C029） | 存活：C013–C017/C006/C024/C086 逐点核对；tui 定制测试绿；C028 退役（裁决） |
| LLM 认证（C021/C022） | 存活：上游 wire_client.py 零改动 |
| catalog-sync（C030–C049） | 存活：registry.py 上游零改动（v0.34.0 融合成果无暴露）；C035 恒只读在；catalog 测试绿 |
| branding-and-command（C050–C131） | 存活：strict 全对账；C102 无新适配 |
| guard（C140/C141/C142） | 存活：startup.py 接线行在；pyproject lint profile 与 .gitattributes 行在；guard 测试仅 2 环境性在案 |
| i18n（C002/C027/C028/C132+豁免表） | 存活+机制适配：C027 及 4 借壳 key 在 id 集合；C028 退役；豁免表 4→3 项（mo 移除）；管线三轮幂等 |
| ruff 豁免（C003） | 存活：pyproject per-file-ignores 行在（ruff 全绿佐证） |
| 新增（C143/C144） | 已登记：品牌断言适配，54 passed |
| 机检兜底 | test_panel_anchor 4 passed + strict 101/138/6 = 0/0 |

## 五、淘汰检查逐组结论（runbook §5 交付物 3）

以 `--report` 100 行登记全景（升级前时点）为输入，逐组 hit/not-hit：

| 定制组 | 结论与依据 |
|---|---|
| 文档入口 / ruff 豁免 | **未命中**（〔常驻〕/政策未变） |
| 日志面板 C007 | **未命中**：上游 logs/screen.py 区间零改动，无第三方模块分层原生机制出现 |
| **启动接线 C004** | **命中候选 → 对比分析如下（AI 建议：不命中、保留）** |
| TUI 登录域（C005–C029） | **未命中**：上游无账号登录/徽标/命令功能（turn 转向/discard 不涉登录语义）；C028 为上游侧载体消失退役（非淘汰类，已单列裁决） |
| LLM 认证 C021/C022 | **未命中**：wire_client.py 零改动；`{{!command}}` 信任链路（v0.33.0 选 A 裁决）区间未触碰（command_credentials.py / 我方 catalog.py 零改动，clients.py 改动 = async 化 + observer 连接事实，无凭据链路交互） |
| catalog-sync C030–C049 | **未命中**：registry.py 零改动；F4 禁用/恒只读〔政策变更〕条件未触发 |
| branding C050–C131 | **未命中**：品牌政策未变（新增 2 断言适配属存续维护非退役） |
| **guard C140** | **命中候选 → 对比分析如下（AI 建议：不命中、保留）** |
| i18n 豁免/C002/C027/C132 | **未命中**（机制演进由上游承载，适配完成；借壳机制存续） |

### C004 对比分析（〔消费者退役〕命中判定）

removal_condition 原文："接线消费者全部退役，或上游提供官方插件/hook 机制可承载"。

| 维度 | 上游 v0.35.1 扩展机制（ff22b004） | 我方 C004 需求 |
|---|---|---|
| 时机 | per-turn context（turn 开始）+ before/after_tool hooks——**turn/工具调用级** | **进程启动级**：每次 CLI/TUI 调用即执行 startup hooks（auth 静默登录检查、guard 安装等启动期初始化） |
| 承载形态 | 用户安装的 Python 扩展包内声明（`Extension.contexts`/`before_tool_hooks`/`after_tool_hooks`/`tools`，extensions.py 实证**无 startup 类 hook**） | `app.py main()` 起始接线，覆盖所有入口（含无 turn 场景） |
| 消费者 | auth/guard 等仍全部在服役 | — |

**AI 建议：不命中、保留 C004**——上游扩展机制无进程启动时机承载（startup ≠ per-turn），
消费者全部在役。**最终裁决待责任人。**

### C140 对比分析（〔上游机制替代〕命中判定）

removal_condition 原文："上游自带 hook 规则包分发/安装机制，或安全护栏特性整体退役"。

| 维度 | 上游 v0.35.1 扩展 tool hooks | 我方 C140 需求 |
|---|---|---|
| 拦截能力 | before_tool 可检查/拒绝/改参（单点规则逻辑**可表达**） | 工具层阻塞拦截（能力重叠） |
| 分发安装 | **无**：扩展 = 用户自行安装的 Python 包，无"内置规则包随产品分发 + 启动自装"通道 | bundled 规则包随产品分发 + `orchestration/startup.py` 启动自装（guard-hooks 批次） |
| 开关治理 | 无规则级开关机制 | guardctl 总开关/单规则/写入范围（guard-switches 批次） |

**AI 建议：不命中、保留 C140**——名似≠等价：上游提供的是扩展作者编程接口，非规则包
分发/安装机制；拦截能力可表达 ≠ 分发通道存在。**最终裁决待责任人。**

## 六、裁决记录（追加不删旧）

- **2026-11-09 开工裁决**：按完整升级管线开工；merge 目标 ref = **release tag v0.35.1**
  （非镜像头）；本批次优先级高于 ROADMAP 其他候选批次。
- **2026-11-09 镜像同步方式**：责任人 GitHub 网页同步 main_iCode 至上游 main 最新头
  （30df994a，领先 v0.35.1 若干提交）→ 本地 ff（05679bec→30df994a）；镜像领先不影响
  merge 目标与 R2/R3 base（= 7c423b06）。
- **2026-11-09 design 签认**：初稿经外部评审（CodeBuddy/BM:GLM-5.3，blocker 0，F1–F5）
  处置后签认；R2 实证增补 C143/C144 与 C028 退役表后**重签**（含 tui/design.md 联动重签）。
- **2026-11-09 R2 增补批准**：fork-full 暴露 2 处上游新测试品牌断言冲突（C143/C144）
  经责任人批准；C144 同点第二处 app 绑定断言适配不新增 marker（计数 1 保持）。
- **2026-11-09 C028 退役批准**：随 design 重签生效——上游 v0.35.1 去计数化载体消失，
  登记行迁入本批次退役记录（id 永不复用），借壳机制存续（C027）。
- **2026-11-09 环境性失败认定**：31 个 WinError 448 族经 OS 级对照实验坐实为本机
  junction 策略（非代码回归），实跑归 PR 双 CI；guard symlink ×2 为升级前既有在案。
- **2026-11-09 C004/C140 淘汰候选**：**责任人裁决保留**（第五节对比分析成立——C004：
  上游扩展机制无进程启动时机承载，startup ≠ per-turn，不构成〔消费者退役〕命中；
  C140：上游 tool hooks 为扩展编程接口，非规则包分发/安装机制，拦截能力可表达 ≠
  分发通道存在，不构成〔上游机制替代〕命中；两定制维持现状）。

## 七、时点快照（gen_customizations，2026-11-09 C143/C144 适配+重签后）

```
gen_customizations: check OK — 101 registration row(s) / 138 marker(s) across 14 design(s), 6 retired id(s), 0 warning(s)
```

（升级前基线 100 行 → 升级后 101 行：+2 登记（C143/C144）−1 退役（C028）；
marker 138 = 升级前 137 − 1（C028）+ 2（C143/C144）；退役累计 6。在册旧定制点零丢失。）

## 八、遗留项（收尾跟踪）

- PR 双 CI 待 push 后跑（aixcoding-tests + 上游 ci.yml 桥接）；
- ~~TUI 人工冒烟~~ **已闭环**（2026-11-09：人工四项无问题；discard 入口自动化 153 passed）；
- **R3 + mo 禁跟踪测试复跑**：merge add/rm 后中间态信号（mo UD）收敛确认，随收尾执行；
- 本机 31 个 WinError 448 环境族：若需本地全绿，由责任人评估本机 junction 策略/nvm
  链接修复（属机器环境，不属本批次代码范围）；
- 离线打包：`dist\downloads\` PBS 缓存更新至 20261026（配套 3.14.10）下次打包前执行；
  系统级 Python 3.14.8 可选升 3.14.10（uv managed 已满足本仓）；
- 旧 `.venv_old_pre_0351/` 残留已入 `.git/info/exclude`（本地排除不入 PR），磁盘清理留手动；
- tui 批次 design 的 R2 表 9 号（mo 重编提交）/11 号（size 断言同步）口径自本版起过时：
  mo 由构建自动生成、oracle 去计数化——后续若修订 tui design 正文按 R10 处理，
  本批次已在 design「i18n 机制适配」第 4 点留痕。
