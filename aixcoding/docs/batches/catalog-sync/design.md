# 设计：模型目录同步 + 档案集中管理（catalog-sync）

<!--
签认规则（R10）：责任人本人编辑本头部（状态→已确认、填日期、确认意见可选），AI 不得代填。
签认后修改正文需重新签认。校验：uv run python aixcoding/tools/check_docs.py
-->

- 责任人：周彬
- 状态：已确认
- 确认日期：2026-11-09
- 确认意见：无特别意见

> 修订记录：2026-11-02（二）外部 AI 评审（证据核验 + 责任人裁决）后重写——UI 形态
> 裁决 A（照 0.28 final）、ACP 边界裁决 ②（仅承诺 TUI，限制登记）；采纳评审 blocker：
> 发布事务化（一-1）、registry 快照安装（三-1）、轮询单例生命周期防护（三-3）、
> 只读文案与 `_live_label` 补登（一-3）、当前会话语义钉死（一-4）。
> 2026-11-02（三）第二轮 AI 评审（env 分类专题）后定稿——三 A 组 env 更名确认
> （URL/TOKEN/SYNC_INTERVAL，含 SYNC_INTERVAL 归类局部更正）、废弃变量警告检测采纳、
> DIRECTORY.md §9 判据澄清（配置契约归属）随本批次落盘。
> 2026-11-02（四）Q2/Q3 两轮专题 AI 评审定稿——Q2 删除+marker（刀口原则化：功能
> 禁用、协议面不变；运行时开关/混合形态否决留档）；Q3 finally stop 保留（定位收紧
> 为"失效化通知"、Textual 8.2.7 退出窗口技术事实、挂点=上游既有 finally 块 +1 行）。
> 2026-11-02（五）上游测试连带面核查（picker 测试跟改漏项 + 三项排除：check_action
> 不查 BINDINGS / indicator 位置参数零跟改 / action_models_config 零引用）+ 外部 AI
> 评审两项决策——R2 追加 14 号（test_model_picker.py 跟改，参照 0.28 final 按 0.32.2
> 逐段复核）；manage 参数化行不删、改合成 OptionSelected 事件驱动（不直调
> _on_selected，协议路由覆盖保留）；登记预告补 C044/C045。
> 2026-11-02（六）第三轮外部 AI 评审（本轮修订专项）6 findings 处置——F-1 R2 合计
> 算术修正（+55/-19 系行数修订前遗留未重算，逐行实为 +78/-18 净 +60；12-13 号补
> -1）；F-2 R1 自查 13 行→14 行；F-3 回滚句精确化（12 号豁免 marker）；F-4 C045
> 归类改〔随组〕；F-5 验收口径补裁判基准（requirements 同步）；F-6 用户文档
> （docs/{en,zh-Hans}/guides/configuration/models.md 双语仍为上游本地管理指南；0.28
> final 曾有集中管理定制版——迁入映射遗漏的定制资产）经责任人裁决**不并入本批次，
> 留后续文档批次单独处理**（requirements 非目标登记；并入 ROADMAP §4 C 类"用户
> 指南文档定制化"工作项）。
> 2026-11-02（七）E 段落地后上游红面甄别（tests/app/tui/screens 10 failed，经三分类
> 全部为第一类——红因精确对应已签认行为差异）：R2 追加 15 号（wiring ×5，照 0.28
> 修构造不修断言）与 16 号（model_config ×1 文案断言）——实施前不可完全预判的红面
> 按验收口径随发现随登记；登记预告补 C046/C047；另 A 段 await 节点触发 wait
> manifest 生成产物机械刷新（+9，同 12 号豁免形态）。
> 2026-11-02（八）上游全套实施期红面终甄（6 failed：footer ×4 + bindings ×2，三分类
> 第一类——红因精确对应 R2 表 6 号已签认的 F4 binding 移除，含首失败点挡住的 zh
> 描述映射第二处）：R2 追加 17 号（删过期期望不放松锚定；locales 侧 `tui.binding.models`
> 翻译 key 保留——无主无害非本批射程）；登记预告补 C048/C049。经责任人批准执行。
> 2026-11-02（九）评审修复轮（外部 AI Kimi-K3.1-Pro 6 findings 全 confirm，责任人
> 批准处置）：F-1 R2 节标题改标准形态（原非标准标题致 check_r2_diff 解析失效）+
> 17 号拆 17/18 两行（一行一文件，解析器表规范；内容零变化）；F-2/3/4/6 产品修复
> 与 F-5 测试重写见 verification 修复节。
> 2026-11-02（十）第三轮评审（同评审 AI 增量复审后又全量一轮）4 findings 全 confirm，
> 责任人批准处置：原生 items/裸列表形态补 name/model_id 字符串校验（发布前拦截，
> 防 registry casefold 启动穿透）；interval 补非有限值回退（NaN 热循环/inf 溢出）；
> 退役 env 四名全覆盖 + 新 TOKEN 优先测试补齐；R1 自查行数随 15-18 号登记刷新为
> 18 行。修复明细与复跑见 verification 第三轮节。
> 2026-11-03（十一）PR #85 peer review（6 findings）处置：C1 发布前代次检查补测试
> 锚定；C2 tier 回退注释段与模块 docstring 对齐实现（"无源"形态澄清为凭据门控，
> 零行为改动）；C4 slug 消歧顺序语义注释澄清；C5 本条——R2 三个子合计（+78/-18、
> +10/-1、+23/-22）系**实施前预估**，评审 numstat 实测 1-11 号 +148/-48、12-13 号
> +19/-5、14-18 号 +37/-24（修复轮注释增量与 registry 重缩进整体计行所致），
> **实测以 verification.md §7（25 files +238/-84，逐文件 numstat 一致）为准**；
> C3 按 ADR-0018（内网部署明文 HTTP，2026-11-03 责任人裁决）口径排除；C6 R2 gate
> 池解析增强（一格多文件正确解析 + 真实路径校验）随本批落地，12 号格内 po/mo
> 路径改仓库根相对。
> 2026-11-03（十二）PR #85 第二份独立评审（候选 C1-C6/B1-B9/E1-E3，B4/B6 编号
> 空缺实为 16 项）分档处置：A 档两项行为级修复
> （同步链前置调用兜底——blocking 全链 try + 轮询两段 except Exception；
> `_positive_int` 排除 bool）+ 3 新用例；B 档六项文档/登记（version 无短路消费经
> 考古排除——0.28 同为无条件全量重写；非 ASCII 名整目录拒绝登记已知限制；worker
> 不触达 PEP 758；verification §3 实录化与 §5 manifest 逐行结论；state 窗口表述
> 精确化）；其余候选映射排除或维持签认边界。全部零上游改动，明细见 verification
> 第二份评审处置节。
> 2026-11-09（十三）environment-config 批次登记预告插入（ADR-0022 草案）：
> C032/C033/C039/C042/C043 现役行保留以维持严格对账；本次为草案联动，正文变更待责任人
> 随 environment-config 批次签认实施按 R10 一并重签。
> 2026-11-09（十四）environment-config 实施登记迁移：上述五个 id 的现役行删除，
> 退役记录由 environment-config design 持有；C034 等其余现役点不变，本文按 R10 待责任人重签。

## 方案概述

迁入旧分支（0.28.0）的"服务端模型目录同步 + 档案集中管理"能力，按 0.32.2 基线重新
落位与接线；**不原样照搬旧实现的两处已知缺陷**（非事务发布、锁内重扫磁盘）。核心
同步逻辑（payload 翻译 / 校验 / 抓取 / 应用 / 15 分钟轮询 / 登录联动）从旧
`src/chrys/service/profiles/models/catalog.py`（847 行，0.28 违规形态：定制文件居上游
树内）平移至定制包 **`src/chrys/service/aixcoding/catalog.py`**（service 档 `aixcoding/`
目录本批次首建，ADR-0011 档位判据见备选对比一），其中**发布流程重写为文件级两阶段**
（先写临时文件全集 → 同名 `os.replace` → 删除多余旧文件 → 写状态），**registry 更新
改为同版本快照安装**（`install_snapshot`，短临界区一次替换字典，锁内零磁盘 IO）；
两处机械改名（auth import 前缀、tier env 值）+ **A 组三 env 更名**（对比五定稿）；
挂接形态按新基线重构（启动挂点从
`bootstrap_runtime` 参数化收缩为 TUI `main()` 一处直调；轮询启停从 ChrysApp 方法收敛
为定制模块内进程级单例 + 代次防护，`login_actions.py` 两处占位符替换为实调——零上游
改动兑现 tui 批次 D3 预告）。UI 面为 **方案 A（照 0.28 final）**：模型配置屏恒只读
（含文案改集中管理措辞），F4 与 picker 管理条目隐藏，`/models` 命令 / 指示器
configure 态 / 未配置确认框保留为只读查看入口。ACP 边界为**裁决 ②**：本批次仅承诺
TUI，ACP 写删通道限制登记于 requirements 非目标。

```
~/.chrys/models/  ──(两阶段整替)──  catalog sync                  TUI
  *.yaml  *.yaml.tmp(暂存)     ┌────────────────────────────┐  ┌──────────────────────────┐
  .catalog-state.json  ◄───────│ src/chrys/service/         │  │ app/tui/app.py main()    │
                               │   aixcoding/catalog.py    │◄─│  启动阻塞同步(1处)        │
  目录唯一写者：TUI 同步线程      │  翻译/校验/抓取/两阶段发布   │  │  start + finally stop    │
  （ACP 写入=已登记已知限制）     │  + 轮询单例(代次防护)        │  └──────────────────────────┘
                               └────────┬───────────────────┘  ┌──────────────────────────┐
                                        │ lazy + 全吞错           │ app/aixcoding/tui/       │
                                        ▼                        │  login_actions.py       │
                               chrys.kernel.aixcoding.auth      │  登录:即时同步/登出:停     │
                               (token/userId，env 优先)          └──────────────────────────┘
                                        ▲
   registry: ModelProfileRegistry.install_snapshot(同版本 profiles)
   ——同步线程持 parse 校验后 items 直传，锁内零磁盘 IO
```

同步链：`catalog_url()`（显式 env URL > settings base > tier 基址表）→ `fetch_catalog()`
（httpx，`token` 头 + 固定 query + `userId`）→ `parse_catalog()`（服务端 `models` 形态
翻译 + 校验）→ `apply_catalog()`（两阶段发布，见备选对比八）→ `on_applied` 回调
（`install_snapshot` + UI 刷新）。**全链异常统一转换**：抓取（httpx/ValueError）与
发布（OSError 全族）都进入可观测降级路径（日志留痕 + 返回失败），旧目录在发布失败时
保持可用（见备选对比八的窗口分析）。

### 六项漂移复核结论（2026-11-02 对 0.32.2 逐项实测，不照抄盘点表）

| # | 盘点漂移点 | 本批次复核结论（证据） | 处置 |
|---|---|---|---|
| 1 | registry API：`replace_profiles` 缺失 | **实证**：新基线 `registry.py:98-100` 无 replace；`install_profiles` 逐项 register 只增不删 | **快照安装**（备选对比六）：新增 `install_snapshot(profiles)`，短临界区一次替换——非旧 `replace_profiles`（锁内重扫磁盘）的照搬 |
| 2 | registry 并发：RLock 缺失 | **实证**：7 方法全裸无锁；与 #1 同根（后台同步线程 vs UI 并发） | 恢复 RLock（`install_snapshot` 的短临界区依赖它；`install_profiles→register` 嵌套需可重入） |
| 3 | `ProcessSettings.model_catalog_base_url` 投影缺失 | **实证**：新基线 `process_settings.py:43-48` 无此字段；`settings.py` 亦无声明 | 声明（RESTART + ProjectMerge.DENY + label）+ 投影重新引入（R2 表 2/3 号） |
| 4 | 接线面（不止占位符） | **实证枚举**：settings 声明、TUI `main()` 启动同步、`login_actions.py` 占位符 ×2、F4（`screen.py:238`）、picker manage 条目（`picker.py:120-132`）、`config_actions.py` read_only、`layout.py:514` `DEFERRED_KEYS`、**只读文案**（新基线 `models/screen.py:73-76` fallback 仍为 "Agent is running"，旧分支 `:66-69` 已改集中管理措辞）、**`_live_label`**（`model_indicator.py` 0.28 定制，`compute_model_indicator_state(..., model_registry=)`） | 逐项入 R2 表（方案 A 形态） |
| 5 | 机械改名 | auth import ×2 lazy → `chrys.kernel.aixcoding.auth`；`ENVIRONMENT_ENV` 值 → `AIXCODING_AUTH_ENVIRONMENT`（drift test 锚 `environments.ENVIRONMENT_VARIABLE`） | 平移时改写 |
| 6 | 配置落点 | **A 组三变量更名定稿（责任人确认 2026-11-02）**：`CHRYS_MODEL_CATALOG_URL`/`TOKEN`/`SYNC_INTERVAL`（均模块直读、零上游挂靠）→ `AIXCODING_MODEL_CATALOG_URL`/`TOKEN`/`SYNC_INTERVAL`，legacy 名退役无兼容期 + 废弃名警告检测；`CHRYS_MODEL_CATALOG_BASE_URL` **不改**（settings 声明体系注册成员，例外条款覆盖；mock 批次 2026-10-25 预决策沿用，其中 SYNC_INTERVAL 归类经局部更正——见对比五） | 备选对比五 |

### 迁入映射表（旧 → 新）

| 旧资产（0.28.0） | 新落点 | 改写点 |
|---|---|---|
| `service/profiles/models/catalog.py`（847 行） | `src/chrys/service/aixcoding/catalog.py`（+ `__init__.py` 建 service 档） | ① auth import 改名 ×2（lazy 保持）；② `ENVIRONMENT_ENV` 值改 `AIXCODING_AUTH_ENVIRONMENT`；③ **A 组三 env 常量更名**（URL/TOKEN/SYNC_INTERVAL，对比五）+ legacy 名退役；③′ **废弃名警告检测**（四个退役名存在时 `logger.warning`，不记值，对比五）；④ **`apply_catalog` 重写为两阶段发布**（对比八）；⑤ **发布/抓取全异常统一转换**（OSError → 降级路径，评审一-1）；⑥ docstring 路径同步；⑦ 追加轮询运行时单例（`start_sync_runtime`/`stop_sync_runtime`/`sync_now`，~60 行：进程级 handle + registry/UI 回调注入 + **代次防护**，对比三）；⑧ `Design:` 指引行 |
| `registry.py` 定制（RLock + replace_profiles） | 原文件：RLock 恢复 + `install_snapshot` 替代 replace（**形态重审**，对比六） | ~+21 行 + marker ×2 |
| `settings.py` / `process_settings.py` 声明 + 投影 | 原文件恢复同构定制 | 照迁（spec 模式参照上游 `model_profile`；label 借壳，i18n 义务 R2 表 12 号） |
| `orchestration/startup.py` `_sync_model_catalog` + bootstrap 参数（~35 行） | **不再改 orchestration**——挂点迁 TUI `app/tui/app.py:main()`（R2 表 4 号） | 备选对比二（B2 方案） |
| `app/tui/app.py` ChrysApp 方法（~60 行） | **App 类零改动**——单例收敛；`main()` 内 start + finally stop（~6 行，stop 入既有 finally 块，对比三） | 备选对比三 |
| `screens/main/screen.py` 登录/登出联动 | `login_actions.py` 占位符 ×2 替换为实调（**定制文件，零上游改动**） | tui 批次 D3 预告兑现 |
| `screens/main/screen.py:228-229` F4 binding 注释 | 删除该行 + marker（对比四） | +1/-1 |
| `screens/models/picker.py` manage 条目段注释（~13 行） | 删除渲染段 + marker（对比四边界：保留枚举/msg 常量/协议分支） | +2/-15 |
| `screens/main/config_actions.py` read_only 恒 True（+2 注释） | 同构（方案 A 核心） | +3/-1 |
| `screens/models/screen.py` 只读文案 fallback | 同构（集中管理措辞；**连带 po `msgstr` 同步改写**——有翻译时翻译优先于 fallback） | +1/-1 + po 联动 |
| `screens/main/model_indicator.py` `_live_label` + `model_registry` 参数 | 同构恢复（~+14 行；调用方 `screen.py` 指示器刷新链传参 +1~2 行，实施时定位） | R2 表 7/11 号 |
| `screens/settings/layout.py` `DEFERRED_KEYS` + key（~5 行） | 同构（排除集追加） | +5 |
| `tests/service/profiles/models/test_catalog_sync.py`（203 行） | `aixcoding/tests/catalog/test_catalog_sync.py` | ① import 改 `chrys.service.aixcoding.catalog`；② `wait_until` 本地 conftest 复制（tui conftest 同款，ADR-0001）；③ drift test 锚新路径；④ **新增发布故障注入用例**（第 N 文件写失败/状态写失败/删除失败——评审一-1）；⑤ **新增废弃名检测用例**（旧名单独存在→不生效+有警告；新名优先——对比五） |
| `tests/app/tui/screens/main/test_config_actions.py` read_only 恒真断言 | `aixcoding/tests/catalog/test_read_only_gate.py` | import 改名；文案断言补集中管理措辞 |
| `tests/app/tui/screens/test_model_picker.py` manage 断言（4 处必红） | **原文件跟改**（R2 表 14 号）：参照 0.28 final 改写形态 + 0.32.2 逐段复核；manage 参数化行不删、改合成 `OptionSelected` 事件驱动（构造未注册 Option，路由断言保留） | marker ×2（C044/C045） |

## 备选方案对比（R12 迁移批次方案重审——旧实现只是内容索引）

**对比一：catalog 模块落位与形态**（结论不变）

| 方案 | 优点 | 缺点 | 升级冲突面 | 结论 |
|---|---|---|---|---|
| 照旧居上游树 `service/profiles/models/catalog.py` | import 最短 | 0.28 违规形态（上游树落定制文件，违 R4/ADR-0011） | 上游树内 +847 行 | 否决 |
| **定制包 service 档 `src/chrys/service/aixcoding/catalog.py` 单文件** | 依赖判据落位（`ModelProfile`/serializer 均 service 依赖 → 最低容纳档 service）；零上游文件冲突；旧实现单文件五段内聚 | service 档 aixcoding 首建（一次性 `__init__.py`） | 零（上游树零接触） | **是** |
| 拆 `catalog/` 子包 | 文件短 | ~900 行属可读范围；拆分徒增 import 面 | 零 | 否决 |

**对比二：启动同步挂点**（结论不变，评审三-2 同意）

| 方案 | 优点 | 缺点 | 结论 |
|---|---|---|---|
| A. 挂 `app/aixcoding/startup.py` `_TASKS` | 零上游改动 | **语义漂移**：`cli/app.py:main()` 对每个 icode 进程执行——`run`/`--version`/ACP 全同步（入口链实测）；argv 嗅探脆弱 | 否决 |
| B1. 照旧 `bootstrap_runtime` 加参数（2 上游文件 ~35 行） | 逐字对齐旧语义 | 多改 orchestration 一文件；签名再入 fork 定制 | 否决 |
| **B2. TUI `main()` 内直调**（bootstrap 后、`load_all()` 前，lazy + 统一降级） | 语义等价（先同步后加载，仅 TUI）；1 文件 ~3 行；settings 已加载 | 同步阻塞启动最长 3s（同旧形态） | **是** |
| B3. 挂 MainScreen `on_mount` + immediate 轮询 | app.py 零改动 | 启动序列变"先旧后刷"；tui 批次已留档"应用级职责须回迁 app 挂点" | 否决 |

> B2 的"全吞错"以**发布/抓取全异常统一转换**（对比八）为前提——评审一-1 指出旧实现
> `sync_catalog_blocking` 只捕 `ModelCatalogError`（`:695-699`），`unlink`/`write_text`/
> `chmod` 的 OSError 会穿透启动路径。本批次修复后 B2 成立。

**对比三：轮询单例生命周期（评审三-3 采纳重写；Q3 经第三轮 AI 评审定稿——保留 stop、不 join、不换挂点）**

| 方案 | 优点 | 缺点 | 结论 |
|---|---|---|---|
| 照旧 ChrysApp 方法 + `CatalogSyncHandle` 原样 | 与旧逐字同构 | app.py +60 行；`stop()` 只置标志不 join 不取消 in-flight——登出/账号切换后正在执行的 fetch+apply 仍会发布旧账号目录 | 否决 |
| **定制模块单例 + 代次防护**：`start_sync_runtime(registry, refresh_ui)` / `stop_sync_runtime()` / `sync_now()`；**generation 计数**（start/stop/sync_now 递增，apply 前校验代次，不匹配则丢弃结果不发布）；stop 递增代次 + 置停止标志 + 清理回调引用（App/screen 闭包失效化）；幂等（已运行时 start(immediate)=sync_now，旧语义保留）；不 join（daemon 线程，代次防护保证 in-flight 无害） | app.py 收敛 ~6 行（stop 入既有 finally 块）；登录/登出联动全落定制文件；in-flight 跨登出/账号切换安全 | 单例跨线程 UI 回调（`call_from_thread`）——回调由 main() 注入时绑定 app，生命周期由代次+清理双重防护 | **是** |

> **stop 的准确定位（第三轮评审口径收紧）**：`finally stop` 是**应用生命周期结束时的
> 失效化通知**——阻止后续工作调度、丢弃尚未 apply 的过期结果、兜底隔离已进入执行的
> 残余回调；**不是**"线程已终止"或"绝对不再发生回调"的保证。两条边界如实声明：
> ①清理回调引用不能撤回已被取出并进入执行的回调；②apply 前代次检查只丢弃尚未进入
> apply 的结果——检查之后、写盘过程中发生的 stop 不取消写盘（写盘本身幂等收敛）。
>
> **退出窗口的技术事实**（Textual 8.2.7 源码核验）：`call_from_thread` 在 `_loop`
> 清空后抛 `RuntimeError("App is not running")`（`textual/app.py:1818-1819`），但
> `_loop` 直到 `_shutdown()` 完成才清空——shutdown 期间回调仍可能成功调度，且
> `call_from_thread` 无超时等待 `future.result()`（极端竞态下同步线程可能挂起；
> daemon + 不 join 保证不阻塞进程退出）。吞错兜底保留为**最后一道隔离**，不作为
> 正常退出依赖路径。`finally` 位于 `app.run()` 之后、晚于 Textual 大部分 shutdown
> 过程——stop 的价值是**缩短并收敛退出竞态**，非绝对消除。
>
> **挂点与行数（核验修正）**：上游 `main()` 的 `app.run()` 本就在既有 `try/finally`
> 内（`app.py:1458-1469`，finally 做终端恢复）——`stop_sync_runtime()` 直接放入该
> **既有 finally 块**（+1 行调用），无需新建结构。
>
> **否决留档**：带超时 join（不能取消 fetch/写盘/`future.result()` 等待，确定性提升
> 有限，却使撞上 in-flight 的退出固定加延迟）；`atexit`（触发更晚，语义更差）；
> context manager（对现成 try/finally 加抽象无新增能力）；`on_unmount`（更早但覆盖
> 不全——挂载前异常路径不及 finally，未来若要求"shutdown 一开始即禁新回调"再评估）。
>
> **裁决原则**（第三轮评审）：不为假设性需求增加上游机制，但应为已经创建的资源支付
> 必要、局部且可验证的生命周期成本——stop 直接约束一个写目录、访问网络的线程及其
> UI 回调，置于既有 finally 仅 +1 行，属成比例的必要接线。

**对比四：picker manage 条目 / F4 binding 禁用形态**（删除+marker，两轮 AI 评审一致；原则与表述经第二轮评审提升）

| 方案 | 优点 | 缺点 | 结论 |
|---|---|---|---|
| 照旧注释保留（0.28 形态） | diff 可见"删了什么" | 死代码块驻留上游文件且不随上游有效演进（变量重命名/API 变化使其逐渐失真，恢复时仍需重核）；rebase 冲突面与删除**等价**（同为"此处有改动"）；注释副本可能过期，marker 回答"为何不同"应查上游 git 而非依赖注释副本 | 否决 |
| **删除代码 + marker** | 定制点表达清楚（marker 说明归属与去向）；无死代码漂移；删除内容直接可见于 diff（审批边界明确） | 恢复上游行为需从上游 git 找回 | **是** |
| 运行时开关（条件渲染/可配置启用） | 可逆 | 为假想需求（管理员重新开启本地管理？无此需求）加配置定义/默认值/读取路径 + 两套行为测试责任 + "禁用了但仍可能被配置打开"的治理歧义 | 否决（第二轮评审） |
| 混合形态（F4 注释 / picker 删除） | — | 同批次同类定制无理由异规 | 否决（同上） |

> **刀口原则**（第二轮评审原则化）：删除"**使入口出现或可触发**"的代码；保留上游协议、
> 处理器和文案定义——本批次目标是"**功能禁用，上游协议面保持不变**"，不是"尽可能
> 删除所有不可达代码"。
>
> **删**（picker）：manage 条目完整构造与添加段（`if selectable_profiles: ol.add_option(None)`
> 起至 `ol.add_option(Option(manage_content...))` 止 ~13 行，含分隔线与 hidden 计数提示
> ——它们只作为该条目的描述附注存在，不因段内含文案计算而整块保留）+ `content_width`
> 行 + 孤儿 import `wrap_description`（本改动自造的残留，R5 连带清理）；（F4）binding 一行。
>
> **留**（上游公共形状，非"死代码"）：`_MANAGE_MODELS`/`_HIDDEN_PROFILES` msg 常量
> （理由=**范围隔离**：本批次禁用 UI 入口，不收缩上游 i18n 消息目录——把入口禁用扩大
> 为翻译目录迁移（pot/po/mo/oracle 连锁）超出本批次语义；代价如实承认：文案保留一项
> 暂无 UI 消费者的条目）、`_MANAGE_OPTION_ID`、`ModelPickerAction.MANAGE` 枚举、
> `_on_selected` 的 manage 分支与 `config_actions` 的 `MANAGE` 消费分支（**仍有效的
> 协议处理/消费路径，只是当前 UI 不再产生该事件**——事件可被测试/合成驱动，非死代码；
> picker 协议路由经 R2 表 14 号跟改保留合成事件驱动锚定）、
> F4 的 `action_models_config`（上游 action 实现，删之无收益扩面；`/models` 命令链走
> `commands.py → open_model_config → config_actions`，不经该方法）。若将来做"移除本地
> 模型管理协议与实现"的清理批次，再完整删除这些对象及其 i18n 产物。

**对比五：catalog env 前缀归属（三变量更名定稿，责任人确认 2026-11-02；两轮 AI 评审一致）**

- **A 组三变量更名**：`CHRYS_MODEL_CATALOG_URL` / `CHRYS_MODEL_CATALOG_TOKEN` /
  `CHRYS_MODEL_CATALOG_SYNC_INTERVAL` → `AIXCODING_MODEL_CATALOG_URL` / `..._TOKEN` /
  `..._SYNC_INTERVAL`。判据=**配置契约归属**（变量的语义/解析/校验/默认值/生命周期由谁
  拥有）：三者均 catalog 模块自定义自读（`:53-59` 定义，`catalog_url()`/`_catalog_token()`/
  `sync_interval_seconds()` 直读 `os.environ`），settings.py 零字段声明——fork 私有配置
  契约，按 §9 应 `AIXCODING_`；顶 `CHRYS_` 是伪上游变量（后来者去上游查证查无此物），
  与 auth 更名（`AIXCODING_AUTH_*`，2026-10-25）同位同理。
- **`SYNC_INTERVAL` 归类局部更正**：mock design 决策表（`:131/:155-157`）曾将其与
  `BASE_URL` 并列圈入"不改"，归因"挂上游 settings 声明体系"——按代码事实该归因仅
  `BASE_URL` 成立（SYNC_INTERVAL 无 spec 声明、不经配置加载/快照、解析与默认值全由
  fork 自有），本批次**局部更正该归类，不影响同决策中 BASE_URL 的结论**；历史 mock
  design 为签认档不回改正文，以本节为更正凭据。
- **`BASE_URL` 不改名**（预决策沿用）：settings 声明体系注册成员（`spec(env=...)`
  声明、经加载/合并/面板/`ProcessSettings` 投影）——配置契约归属上游，例外条款覆盖。
  立论重心（第二轮评审修正）：**settings 配置面的一致性**（体系内所有 env 统一
  `CHRYS_`，异类前缀增加操作者/审阅者认知负担）；"上游将来同名便于语义合并"仅为
  次要推测性收益（同名≠语义等价，升级仍须逐点核对），不作核心论据。
- **无兼容期 + 废弃检测**：旧名（含 legacy `CHRYS_REMOTE_MODEL_CONFIG_URL` 共四个）
  不再读取、不做 alias（双倍维护面，内部产品无此必要；旧分支双轨期服役不受影响）。
  **"无兼容期"≠"无检测"**（第二轮评审采纳）：catalog 模块读取 env 前检测退役名，
  存在即 `logger.warning`（只记变量名、不记值）；测试锚定"旧名单独存在→不生效+有
  可观测提示；新名优先"。URL 旧名失效的回退风险详见风险节（端点静默变更，非简单
  失败）。
- **规范联动**：DIRECTORY.md §9 判据表述随本批次澄清为"配置契约归属"口径（机械
  代理判据=模块内定义直读；用户可见性/调试用途不影响分类——防调试变量转正式配置
  时二次改名），属澄清非政策修订；随本批次 PR 落盘。

**对比六：registry 重建语义（评审三-1 采纳重写——"旧实现是唯一形态"结论过强）**

| 方案 | 优点 | 缺点 | 结论 |
|---|---|---|---|
| R-a. 照旧 `replace_profiles()`（锁内 clear + 重扫磁盘 + 逐项安装） | 与 0.28 逐字同构 | 锁内含磁盘 IO；读盘异常时内存已清空；磁盘版本与 registry 版本可能错窗（其他进程写目录） | 否决 |
| **R-a'. `install_snapshot(profiles)`**：同步侧已持 `parse_catalog` 校验后的 items，短临界区 `with self._lock: self._profiles = {p.id: p for p in profiles}` 一次替换 | 整替语义保留；锁内零 IO；目录与 registry 同版本（同一 items 序列驱动发布与安装） | 新方法形态与旧不同（迁移批次允许：R12 旧实现只是内容索引） | **是** |
| R-b. `SettingsReload` 事件让 UI 自刷 | registry 零改动 | 该事件是 settings 重载 + engine rebuild 通道（`controls.py:449` 实测）；`install_profiles` 只增不删缺口仍在 | 否决 |
| R-c. 定制侧逐项 `remove()` | 每方法看似原子 | 差集计算与逐删之间无原子性；仍跨线程无锁写 | 否决 |

**对比七：模型配置 UI 形态（责任人裁决 A，2026-11-02；评审建议同序）**

| 方案 | 内容 | 评估 | 结论 |
|---|---|---|---|
| **A. 照 0.28 final** | 恒只读兜底 + F4/picker 管理入口隐藏；`/models` 命令、指示器 configure 态、未配置确认框（`screen.py:1441-1449`）保留为**只读查看**入口 | 改动最小（不删命令、不动 `/man` 索引测试）；空目录时用户仍可查看集中管理提示；上游新增入口受恒只读保护 | **是（裁决）** |
| B1. A 之上再砍全部查看入口 | 界面完全不出现配置屏 | 增加的是"禁止查看"而非编辑安全（编辑安全与 A 等价——都有恒只读兜底）；需删 `/models` + 跟改 `/man` 索引测试 + Protocol/适配连带 | 否决（无产品需求驱动不为"彻底"而加面） |
| B2. 仅断入口（无只读兜底） | 安全建立在入口穷举完备性上 | 未配置确认框入口（`screen.py:1441-1449`）证明枚举易漏；上游升级新增入口时可编辑屏静默复活 | 否决（双 AI 一致） |

**对比八：目录发布事务化（评审一-1 阻断项修复）**

旧形态（`:584-655`）：`_clear_directory()`（先删全部 YAML）→ 逐文件写 → 写状态。
写盘中途失败 → 空目录/新旧混合 + 状态未写 + **OSError 穿透启动路径**（只捕
`ModelCatalogError`）。新方案（文件级两阶段）：

1. **阶段一（全量暂存）**：逐 profile 写 `<id>.yaml.tmp`（权限位随行）；任一失败 →
   清理已写 tmp，旧目录未动，返回失败；
2. **阶段二（发布）**：逐个 `os.replace("<id>.yaml.tmp", "<id>.yaml")`（同卷文件级
   原子，Windows 可用——评审确认目录级 `os.replace` 在 Windows 不能覆盖非空目录，
   文件级无此限制）→ 删除不在新集合中的旧 `*.yaml` → 写 `.catalog-state.json`；
3. **窗口分析（如实声明，非目录级原子）**：replace 中途失败 → 新旧混合（无丢失——
   旧文件仍在或已被同名替换），状态未写（server-owned 标记未变），下轮同步重试收敛；
   删除阶段失败 → 多余旧文件残留，下轮幂等清理；状态写失败 → 目录已新但标记未更新
   ——降级路径日志留痕，下轮重写；
4. **全异常统一转换**：抓取（httpx/ValueError，旧已覆盖）与发布（OSError 全族）统一
   捕获转换进 `ModelCatalogError` 语义的失败返回 + 日志；启动路径（B2 挂点）全吞错
   降级——**"失败保留旧目录"成为可测试事实**（故障注入用例：第 N 文件写失败/状态
   写失败/删除失败）。

实施细节（tmp 命名/清理时机/失败恢复边界）随 test-plan 故障注入用例收敛，不在此
过度设计。

**对比九：ACP 边界（责任人裁决 ②，2026-11-02）**

| 方案 | 内容 | 结论 |
|---|---|---|
| ① 全前端禁写 | 目录管理态下 ACP `write_model_profile`/`delete_model_profile` 拒绝（`session_manager.py:451/486` 入口 +~4 行，0.28 预留 `require_catalog_writable()` 钩子接线） | 否决（本批次不做；后续批次可补） |
| **② 仅承诺 TUI** | ACP 写删通道保持现状；**已知限制登记**（requirements 非目标）：管理态下 ACP 写入被后续同步覆盖/删除、ACP 删除被"复活"、TUI 发布与 ACP 写入存在多进程并发写窗口（不设防）、ACP 进程模型视图为启动快照 | **是（裁决）** |

> 事实基础（评审一-2 核验属实）：ACP 具备档案新建/合并写盘（`session_manager.py:451-484`）、
> 删除（`:486-493`）、启动读目录（`cli/acp.py:134-137`）、reload 只增不删（`:1177`）。
> 0.28 的 `require_catalog_writable()` 全仓零调用（实测）——0.28 服役期间 ACP 通道
> 本就敞开，②为现状的正式文档化，非新增让步。

## 上游改动清单（R2）

> 节标题须保持机器可解析的标准形态（`check_r2_diff.py` 全等匹配本节标题）。
> 每行"归属结论"回答 R1 细则二之问："这行能挪进 `src/chrys/service/aixcoding/` 吗"。

| # | 文件 | 改什么 | 预估行数 | 标记注释位置 | 归属结论（逐行） |
|---|---|---|---|---|---|
| 1 | `src/chrys/service/profiles/models/registry.py` | `import threading` + `__init__` RLock + 六方法 `with self._lock:` + 新增 `install_snapshot(profiles)` 方法（短临界区一次替换字典） | ~+21 | 锁声明处、`install_snapshot` 定义处（marker ×2） | 线程契约与快照安装属 registry 自身状态一致性，挪进 catalog 包则须跨包改 `self._profiles`——不可能外置 |
| 2 | `src/chrys/foundation/config/settings.py` | `_LABEL_MODEL_CATALOG_BASE_URL`（msg 借壳）+ `model_catalog_base_url` 字段声明（RESTART/DENY/group=model/TEXT） | ~+21 | 字段声明处（marker ×1） | 声明体系单源（spec 元数据驱动面板/env/持久化），定制包无法注入字段 |
| 3 | `src/chrys/foundation/config/process_settings.py` | `ProcessSettings` 字段 + `from_settings` 投影行 | +2 | 字段处（marker ×1） | 快照类上游结构；`_snapshot_field_names()` 派生自动跟随 |
| 4 | `src/chrys/app/tui/app.py` | `main()`：bootstrap 后 `load_all()` 前 lazy 调 `sync_catalog_blocking()`（统一降级，marker）；`app.run()` 前 `start_sync_runtime(...)`；`stop_sync_runtime()` 放入既有 finally 块（`app.py:1464-1469`，+1 行，Q3 定稿） | ~+6 | 三处相邻接线（marker ×1 覆盖启动同步 + 启停注释指向单例） | 启动序列属 TUI main 结构；B2 最小形态（对比二/三） |
| 5 | `src/chrys/app/tui/screens/main/config_actions.py` | `ModelConfigScreen(...)` 实参恒 `True`（+理由注释 2 行） | +3/-1 | 实参处（marker ×1） | 上游已有 read_only 机制（34 处实证），定制仅改门限判定（方案 A 核心） |
| 6 | `src/chrys/app/tui/screens/main/screen.py` | 删除 F4 binding（:238） | +1/-1 | 删除处留 marker 行 | BINDINGS 表上游结构承载点；`action_models_config` 保留（binding 删除后不可达，删之无收益扩面；`/models` 命令链不经该方法） |
| 7 | `src/chrys/app/tui/screens/main/screen.py` | 指示器刷新链调用 `compute_model_indicator_state` 处传 `model_registry`（`_live_label` 调用方——已定位：`_refresh_model_indicator`（`:748-753`），+1~2 行） | +2 | 调用点（marker ×1） | 11 号的配套接线（调用点在上游 screen 结构内） |
| 8 | `src/chrys/app/tui/screens/models/picker.py` | 删除 manage 渲染段（~13 行）+ `content_width` 行 + 孤儿 import `wrap_description`；保留枚举/msg 常量/协议分支 | +2/-15 | 删除段原位（marker ×1） | `on_mount` 上游 compose 生命周期；对比四边界 |
| 9 | `src/chrys/app/tui/screens/settings/layout.py` | `DEFERRED_KEYS` 追加 `"model.catalog.base_url"` + 理由注释 4 行 | +5 | key 行处（marker ×1） | 排除集是上游面板渲染结构；与 2 号同批（声明入面板必须同步排除） |
| 10 | `src/chrys/app/tui/screens/models/screen.py` | `_READ_ONLY_NOTICE` fallback 改集中管理措辞（照旧分支 `:66-69` 文案） | +1/-1 | 常量处（marker ×1） | 同 key 异 fallback——恒只读后 "Agent is running" 文案误导（评审一-3）；文案属上游 msg 定义处 |
| 11 | `src/chrys/app/tui/screens/main/model_indicator.py` | `_live_label()` 函数 + `compute_model_indicator_state(..., model_registry=)` 参数（照 0.28 定制） | ~+14 | 函数定义处（marker ×1） | 会话中同步重写 profile 后指示器跟踪 registry 实时名——纯函数属上游模块，定制为参数化扩展 |
| 12 | `locales/chrys.pot` + `locales/zh-Hans/LC_MESSAGES/chrys.po` + `src/chrys/foundation/i18n/_catalogs/zh-Hans/LC_MESSAGES/chrys.mo` | i18n 产物：settings label 新 key（`settings.model.catalog.base_url.label`）extract/中文条目；`tui.model_config.read_only_notice` 的 po `msgstr` 同步改写（10 号 fallback 变更连带——有翻译时翻译优先）；格内路径为仓库根相对（PR #85 处置 C6：R2 gate 一格多文件正确解析 + 真实路径校验） | ~+8 | 标记豁免清单已有（tui 批次登记三产物） | 生成产物（extract/compile），随 msg 定义自动产生 |
| 13 | `tests/foundation/i18n/_catalog_oracle_ids.py` + `tests/foundation/i18n/test_catalog_oracle.py` | oracle 两动作：新 key 追加 `EXPECTED_MESSAGE_IDS` +1、size 断言 +1（C042/C043，同 tui 批次先例）；格内路径为仓库根相对（同 12 号，PR #85 处置 C6） | +2/-1 | 行尾 marker（文件级） | 上游 oracle 机制对借壳 key 的登记义务 |
| 14 | `tests/app/tui/screens/test_model_picker.py` | 8 号渲染段删除的测试连带（4 处必红，红因精确对应已签认行为差异）：①manage 正断言→`OptionDoesNotExist` 反断言；②计数 4/5→3/3 + 用例改名；③manage 参数化行**不删、改合成事件驱动**（构造未注册 manage Option + `post_message(OptionSelected)`，保留 `ModelPickerAction.MANAGE` 路由断言——协议覆盖不随 UI 入口消失）；④中文用例删 manage 段、保留标题断言 + 改名。参照 0.28 final，按 0.32.2 逐段复核（不机械照抄 diff） | ~+12/-10 | 反断言处、参数化改写处（marker ×2，C044/C045） | 渲染段删除的连带跟改；协议路由覆盖保留（对比四"留"清单的测试锚定落点） |
| 15 | `tests/app/tui/screens/test_model_indicator_wiring.py` | 7 号/11 号 live label 语义的测试连带（5 处红：标签跟 registry 而非运行时快照）：照 0.28 final **修构造不修断言**——registry 注册名对齐 runtime details 名 + 语义注释（registry 为活名真相源、registry 不识 id 回退快照）；测试意图（切换刷新/状态机标签保持/失败后收敛）完整保留 | ~+6/-5 | 语义注释处（marker ×1，C046） | 实施期红面发现（E 段落地后甄别，三分类第一类），随发现随登记 |
| 16 | `tests/app/tui/screens/test_model_config.py` | 10 号 notice 措辞的测试连带（1 处红）：断言期望串改集中管理新文案 | +1/-1 | 断言行（marker ×1，C047） | 同 15 号，实施期红面随发现随登记 |
| 17 | `tests/app/tui/behaviors/test_chrys_footer.py` | 6 号 F4 binding 移除的测试连带（全套 4 failed，三分类第一类）：footer 渲染/本地化/调和断言锚定已移除的 f4 `models_config`——删过期期望（action 集合 ×3、zh 描述映射、中文子集，共 4 处），保留行为的锚定强度不变 | ~+2/-4 | 语义注释处（marker ×1，C048） | 全套实施期红面发现，随发现随登记（同 15/16 号形态） |
| 18 | `tests/app/tui/i18n/test_bindings.py` | 6 号 F4 binding 移除的测试连带（全套 2 failed，三分类第一类）：bindings provenance/keymap 契约断言锚定已移除的 `tui.binding.models`——删过期期望（id 集合、契约行，共 2 处）；locales 侧该翻译 key 保留（无主无害，非本批射程） | ~+2/-2 | 语义注释处（marker ×1，C049） | 同 17 号 |

合计：源/配置文件（1-11 号）≈ **+78/-18（净 +60）**；i18n 流水线（12-13 号）≈ **+10/-1**；上游测试跟改（14-18 号）≈ **+23/-22**。
全部为接线/声明/登记/只读门限/文案五类结构性改动及对应测试跟改，**上游既有逻辑流零修改**。
（方案 A 相比 B1 形态：不删 `/models` 命令、不动 `/man` 索引测试与 suggestions 基建、
指示器 configure 路由不改——净省上游 delta ~20 行与两个高风险测试跟改点。）

R1 自查（必答）：
- 能否完全不改上游？——**否**：① 快照安装与线程安全属 registry 自身契约（对比六穷举）；② settings 声明体系单源；③ TUI 启动序列/BINDINGS 表/compose 生命周期/msg 文案定义处均为上游结构承载点。业务逻辑（翻译/校验/抓取/两阶段发布/轮询/单例）已 100% 外置 `src/chrys/service/aixcoding/`；接线点每文件仅一处职能（对比二/三将旧形态 4 文件收缩为 1 文件 + 定制文件直调）。
- 迁移批次细则：**基线重审**——六项漂移逐点实测 + 评审复核（含我方三处误判的修正：ACP 断言、文案定制、B1 论证方向）；**逐行归属**——R2 表末列 18 行 18 条结论（初稿 14 行，15-18 号随实施期红面随发现随登记）；**否决留档**——对比一至九（含责任人裁决否决项 B1/B2/ACP①）；**消费方视野**——catalog 单例成为第三个定制公共消费面（login_actions 占位符即首个消费者）；**硬约束与验收线**——上游对定制包 import 共 3 处（app.py 启动同步 lazy+统一降级；app.py 启停为结构性接线显式呈现；其余为纯上游内部改动无 import）。
- 上游假设已钉死（见下节）；高风险点（registry 锁、settings 声明、发布流程）各有回归/故障注入测试与回滚说明。

## 对上游隐式行为的假设

| # | 上游行为 | 我方依赖方式 | 假设失效时的表现 |
|---|---|---|---|
| 1 | `ModelConfigScreen.__init__(read_only=...)` 参数与整套只读机制存续（0.32.2 `screen.py:751` 等 34 处） | R2 表 5 号实参恒 True；测试断言 `screen._read_only is True` + 文案 | TypeError（参数被删）或只读失效——测试红 |
| 2 | `install_profiles` 逐项 register 只增不删；`load_profiles_from_dir` 扫 `*.yaml`/`*.yml`（前导点与 `.tmp` 后缀不入选——`.yaml.tmp` 结尾非 `.yaml`，**实施时以 loader glob 实测复核**） | `.catalog-state.json` 与 `*.yaml.tmp` 暂存文件靠命名避开扫描 | 暂存文件被当 profile 加载——发布测试红 |
| 3 | `ProcessSettings` 为 RESTART 快照，`_snapshot_field_names()` 从 dataclass 派生；bootstrap 前回退 fresh load | `catalog_base_url()` 读 `process_settings().model_catalog_base_url` | 快照字段集硬编码测试红（若有，实施时跟改——预期内，R2 追加项） |
| 4 | `ChrysApp._services.model_registry` 与 `_main_screen` 属性形态；`call_from_thread` 可从同步线程调度 UI 刷新 | `on_applied` 回调（main() 注入，代次防护 + stop 清理） | AttributeError/刷新失效——wiring 测试暴露 |
| 5 | auth `stored_token`/`stored_user_id`（`session.py:136/153`）、`environments.ENVIRONMENT_VARIABLE`、`Environment` 枚举、`_BASE_PATHS` host 前缀 | lazy import + 全吞错；`_CATALOG_BASES` 键/base 与 auth 对齐 | drift test 红（防线即测试） |
| 6 | TUI `main()` 序列：bootstrap → `load_all()` → `ChrysApp` → `run()`（`app/tui/app.py:1357-1421`） | R2 表 4 号同步调用插入点 | 上游重排启动序列时接线点重定位（挂点性质，runbook 常规） |
| 7 | **当前会话模型解析发生在 engine build 阶段**（`builder.py:357-362`），同步回调不触发 rebuild | **钉死旧语义**（评审一-4）：运行中 engine 保持 build 时配置，重启/设置重载后生效；被删 profile 下一 turn resolve 失败（与重启后一致）；`_live_label` 仅刷新显示名，不改变底层配置——此边界已写入 requirements 非目标 | 用户预期"同步即生效"落空——非缺陷，登记语义 |
| 8 | **ACP 写删通道存在且不受本批次约束**（`session_manager.py:451-493`；`cli/acp.py:134-137`；reload `:1177` 只增不删） | **已知限制**（裁决 ②）：ACP 写入被同步覆盖/删除复活/并发写窗口不设防/ACP 视图为启动快照 | 用户经 ACP 改动"静默丢失"——按登记限制解释（requirements 非目标） |
| 9 | picker `_on_selected` 的 `action:manage` 分支与 `ModelPickerAction.MANAGE` 为上游协议面 | 删除渲染段后**当前 UI 不可达，但可由合成事件驱动**（对比四边界；R2 表 14 号保留合成事件路由锚定） | 无（保守保留，协议路由有测试锚定） |
| 10 | settings 面板对未入 `DEFERRED_KEYS` 且带 label 的声明键默认渲染可编辑行 | R2 表 9 号排除 `"model.catalog.base_url"` | 遗漏时面板出现可编辑部署地址——面板快照测试可锚定 |
| 11 | `/models` 命令、指示器 configure 态（`on_model_tag_clicked` `case "configure"`）、未配置确认框（`screen.py:1441-1449`）为 ModelConfigScreen 的保留入口 | 方案 A：全部保留，打开即恒只读（5 号） | 入口被上游删除时只读屏可达性下降——升级核对项（登记 anchor） |

## 定制点登记预告

> 点级登记（ADR-0015）：匹配键=(落点, marker 锚)；id 自 C030 起（`--report` 实测当前
> 最大 C029）。实施后 `--strict` 对账为门禁。

| id | 落点 | marker 锚 | 预期计数 | anchor | risk | removal_condition |
|---|---|---|---|---|---|---|
| C030 | src/chrys/service/profiles/models/registry.py | AIxCoding catalog: background sync thread | 1 | `ModelProfileRegistry._profiles` 状态一致性（RLock 包覆） | 高（核心数据结构加锁） | 〔上游等价〕上游自带跨线程安全且整替语义有官方通道，经对比分析确认可迁移（提请裁决） |
| C031 | src/chrys/service/profiles/models/registry.py | AIxCoding catalog: install a same-version snapshot | 1 | `install_profiles` 只增不删语义（C030 孪生前提） | 高（删除语义核心） | 〔随组〕随 C030 一并裁决 |
| C034 | src/chrys/app/tui/app.py | AIxCoding catalog: startup sync + poll wiring | 1 | TUI `main()` 启动序列（假设 6）；catalog 单例 | 中（入口文件） | 〔上游等价〕上游提供启动期目录同步挂点，或部署形态取消本地目录同步 |
| C035 | src/chrys/app/tui/screens/main/config_actions.py | AIxCoding catalog: centrally managed, always read-only | 1 | `ModelConfigScreen(read_only=...)` 参数（假设 1） | 低 | 〔政策变更〕档案集中管理政策取消 |
| C036 | src/chrys/app/tui/screens/main/screen.py | AIxCoding catalog: F4 model-management entry disabled | 1 | `BINDINGS` 表 `localized_binding` | 低 | 〔政策变更〕随 C035 一并裁决 |
| C037 | src/chrys/app/tui/screens/main/screen.py | AIxCoding catalog: live label via registry | 1 | `compute_model_indicator_state` 调用链（C041 配套） | 低 | 〔随组〕随同步特性整体裁决 |
| C038 | src/chrys/app/tui/screens/models/picker.py | AIxCoding catalog: "Manage models…" entry disabled | 1 | `on_mount` 渲染段；`ModelPickerAction.MANAGE` 协议面（假设 9） | 低 | 〔政策变更〕随 C035 一并裁决 |
| C040 | src/chrys/app/tui/screens/models/screen.py | AIxCoding catalog: read-only notice wording | 1 | `_READ_ONLY_NOTICE` msg 定义（fallback + po msgstr） | 低 | 〔政策变更〕随 C035 一并裁决 |
| C041 | src/chrys/app/tui/screens/main/model_indicator.py | AIxCoding catalog: label tracks the live registry | 1 | `compute_model_indicator_state` 签名（`model_registry` 参数） | 低 | 〔随组〕随同步特性整体裁决 |
| C044 | tests/app/tui/screens/test_model_picker.py | AIxCoding catalog: manage entry disabled | 1 | manage 反断言（`OptionDoesNotExist`） | 低 | 〔政策变更〕随 C038（manage 入口恢复时反断言随之撤除） |
| C045 | tests/app/tui/screens/test_model_picker.py | AIxCoding catalog: manage routing via synthesized event | 1 | manage 参数化用例合成 `OptionSelected` 驱动（不依赖渲染选项） | 低 | 〔随组〕随 C038/C044；manage 入口恢复时还原为真实选项驱动，协议分支整体删除的清理批次再退役 |
| C046 | tests/app/tui/screens/test_model_indicator_wiring.py | AIxCoding catalog: registry is the live name source | 1 | wiring 测试 registry 名对齐构造（5 处）+ 语义注释 | 低 | 〔随组〕随 C037/C041；live label 语义恢复快照时回改 |
| C047 | tests/app/tui/screens/test_model_config.py | AIxCoding catalog: centrally managed notice assertion | 1 | notice 断言期望串（集中管理文案） | 低 | 〔随组〕随 C040；notice 文案恢复时回改 |
| C048 | tests/app/tui/behaviors/test_chrys_footer.py | AIxCoding catalog: F4 footer key removed | 1 | footer 渲染/本地化/调和断言（4 处过期期望删除） | 低 | 〔随组〕随 C036；F4 入口恢复时期望随之回补 |
| C049 | tests/app/tui/i18n/test_bindings.py | AIxCoding catalog: f4 binding retired | 1 | provenance id 集合 + keymap 契约表（2 处过期期望删除） | 低 | 〔随组〕随 C036/C048；F4 声明恢复时条目回补 |

> 退役登记（2026-11-09，environment-config 实施）：C032/C033/C039/C042/C043 已随模型目录
> base URL 退出上游 Settings 迁入 `aixcoding/docs/batches/environment-config/design.md` 的退役记录，
> 本节现役行同步删除；本文由责任人按 R10 重签后闭环。

（定制包 `src/chrys/service/aixcoding/` 与测试 `aixcoding/tests/catalog/` 为独立定制
模块/开发资产，无上游落点，不入登记表——ADR-0015 射程为上游文件定制点。）

## 风险与最坏情况

- **发布原子性窗口（如实声明）**：两阶段发布非目录级原子——replace 中途失败产生新旧
  混合（无丢失）、删除阶段失败残留多余旧文件（下轮幂等清理）、状态写失败目录已新
  标记未变（下轮重写）。窗口均小于旧形态（先全删后写 → 可能空目录）且可测试（故障
  注入用例锚定）；磁盘级灾难（写 tmp 与 replace 全失败）降级保留旧目录。
- **ACP 已知限制（裁决 ②，非缺陷登记）**：管理态下 ACP 写入被后续同步覆盖/删除、
  ACP 删除被复活、TUI 发布与 ACP 写入的多进程并发写窗口不设防、ACP 进程模型视图为
  启动快照。后续批次可经 `require_catalog_writable()` 钩子补齐全前端禁写。
- **RESTART 字段↔快照一致性**：若上游 `tests/foundation/config/` 存在硬编码快照字段
  列表断言，R2 表 2/3 号落地后红——属预期跟改（届时作为 R2 追加行报批，实施时验证）。
- **i18n 三方集合强一致门禁**（tui 批次实测教训）：label 新 key 必须 pot + po +
  oracle ids + size 四处同步；**read_only_notice 的 po `msgstr` 改写**是本批次特有
  联动（fallback 变更 + 翻译同步，漏改则界面显示旧文案；若上游存在断言该 key 翻译
  **内容**的测试，一并跟改——实施时验证）。全流程本地预跑。
- **`.yaml.tmp` 避开 loader 扫描**：假设 2 依赖 glob 语义，实施时以 loader 实测复核
  （`load_profiles_from_dir` 的 pattern 逐字核对），失败则暂存目录外置（发布时 rename
  跨目录）。
- **退役 env 名失效的回退风险（第二轮评审修正，此前估计偏乐观）**：URL 旧名失效后
  回退 tier 基址表——仅当旧显式 URL 恰等于该 tier 推导端点时行为不变；**非默认部署**
  （显式 URL 指向自建网关/不同路径/query）会**静默改从 tier 兜底端点拉取目录**（端点
  静默变更，非简单失败）；local/dev tier 下回退地址与旧显式 URL 更可能不同。TOKEN
  旧名失效回退登录 token（无凭据则取消同步，不发无认证请求）；SYNC_INTERVAL 回退
  15 分钟（联调表现为"缩短间隔不生效"，风险低）。**缓释=废弃名警告检测**（对比五：
  四退役名存在即警告）+ 发布前迁移核查（内部启动脚本/打包配置/联调文档检索旧名，
  核查范围含 `CHRYS_REMOTE_MODEL_CONFIG_URL`）——是否对 URL 旧名 fail-fast 未采纳
 （守住"启动不受影响"原则，警告即可观测）。
- **用户本地 profiles 首同步被整替**：server-owned 语义（0.28 同态）。**回滚提示**：
  revert 本 PR 前备份 `~/.chrys/models`；代码回滚不恢复已替换的目录内容。
- **RLock 短临界区**（快照安装后锁内零 IO）：UI 线程读 registry 等锁时间为字典替换
  级（微秒级）；目录 ≤128 条（`MAX_PROFILES`）。
- **轮询单例**：代次防护**缩短并收敛**登出/账号切换/退出的 in-flight 竞态（阻止后续
  调度、丢弃未 apply 的过期结果、兜底隔离残余回调）——非绝对消除（边界见对比三：
  已进入执行的回调不可撤回、apply 检查后的写盘不取消，写盘幂等收敛兜底）；不 join
  （daemon 线程，不阻塞退出）；Textual shutdown 期间回调可能成功调度（`_loop` 清空
  前的窗口），吞错兜底为最后隔离而非正常路径。
- **最坏情况**（同步线程异常/目录损坏）：统一降级路径（日志留痕 + 保留原目录）；
  回滚 = revert 本 PR——上游改动（1-14 号）按 R2 清单逐处回退（1-11、13-14 号带
  marker；12 号 i18n 产物按生成链回退），定制包与测试独立成目录整删。
- **对账义务**：本表（C030-C049）在实施落地 marker 后必须
  `uv run python aixcoding/tools/gen_customizations.py --strict` 绿；登记行任何后续
  改动属 R10 重签范围。

## ADR 判定（DEVELOPMENT.md §6）

1. 有被认真考虑但否决的备选方案？——有（对比一至九，含责任人裁决否决项 B1/B2/
   ACP① 与评审否决项 R-a/R-b/R-c/旧发布形态），均为**模块内实现选择与挂点形态**
   （同量级 tui 批次先例未出 ADR）；无架构级方向反转。
2. 撤销代价高？——service 档 aixcoding 首建属 ADR-0011 框架内应用；env 更名是
   DIRECTORY.md §9 既有规则应用（ADR-0009 体系延续）；发布两阶段与快照安装是本批次
   design 记录的实现决策；无数据格式/对外契约变更。
3. 约束未来工作模式？——ACP 限制登记（裁决 ②）与"当前会话不即时重建"（假设 7）是
   **显式边界声明**，随本 design 签认固化，后人以本文为凭——不构成需 ADR 的新机制。
4. 依赖上游非公开接口或行为？——依赖符号均上游公开模块成员，逐点入登记表 anchor
   与假设节（含 ACP 通道行为=已知限制的边界化处理）。

结论：**无需新 ADR**；DIRECTORY.md §9 例外表无需新行（`CHRYS_MODEL_CATALOG_BASE_URL`
沿 mock 批次先例；对比五更名反而消除三个 `CHRYS_` 前缀 fork 私有 env，§9 判据表述
澄清随本批次落盘——见对比五"规范联动"）。
