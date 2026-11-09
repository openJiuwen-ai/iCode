# 目录规范细则（iCode-fork 定制专区）

> 上位文档：`DEVELOPMENT.md`（硬规则）。本文是其目录与落点维度的细则层：蓝图、判定规则、机制与约定。
> 读者：全体定制开发成员与 AI 编码代理。修订本文视同修订规范。
> 决策理由与被否决的备选方案见 `adr/` 下对应记录，本文只写结论与规则。
> 升级流程（追上游版本）的操作展开见 `UPGRADE-RUNBOOK.md`（活文档，不签认）。
> **过渡期声明**：本文描述目录重组完成后的目标状态；执行进度与现状差距见 `../batches/restructure/plan.md`。

## 1. 总蓝图

```
iCode-fork/
├── src/chrys/                     # 定制产品代码按 tier 落位（ADR-0011：档位 = 能容纳全部依赖的最低档）
│   │                              #   四档 {kernel,service,orchestration,app} 各可有 aixcoding/ 子目录
│   │                              #   （按需建、foundation 档不设；下图仅示现有/预告档，非穷举）
│   ├── kernel/aixcoding/          #   kernel 档 = 默认最低档（零依赖与仅 foundation 依赖均落此）
│   │   ├── auth/                  #     仅 foundation 依赖（get_platform，lazy）（待回迁）
│   │   ├── diagnostics/           #     ≤kernel 依赖（launcher/crash/snapshot 已退役，ADR-0013）（待回迁）
│   │   └── telemetry/ 等          #     适配层模块（批次待迁，进度见 batches/restructure/plan.md）
│   └── app/aixcoding/             #   app 档（引用宿主 UI 设施的定制界面）
│       └── tui/                   #     产品 UI（被 app 接线点同层调用；批次待迁）
├── aixcoding/                    # 开发资产与分发物料，不进 wheel
│   ├── docs/                     #   standards/（规范族+全局 ADR+模板+评审清单）、INDEX.md（目录导航）、
│   │                             #   batches/<name>/（全部批次/专项目录平铺，见 DEVELOPMENT.md §6 与 ADR-0016）
│   ├── tests/                    #   【唯一】定制测试落点（显式运行）
│   ├── mock/                     #   【唯一】自建 mock 专区（纯实现，契约测试在 tests/mock/）
│   ├── tools/                    #   开发工具脚本（校验/脚手架等，如 check_docs.py）
│   ├── release/                  #   【唯一】随制品分发物料落点（安装器脚本、用户安装手册；ADR-0020）
│   ├── __init__.py               #   命名空间锚点（例外登记见 §8）
│   └── README.md                 #   专区导览
├── tests/                        # 纯上游测试，零定制文件
├── .github/workflows/
│   ├── ci.yml                    # 上游 CI（仅 PR 至 main 触发；发布链路 CD 经 workflow_call 复用）
│   ├── aixcoding-tests.yml       # 定制质量门（lint + 定制测试 + mock 契约测试 + 文档签认校验）
│   └── aixcoding-upstream-ci.yml # 上游 CI 桥接（PR 至 main_aixcoding_cli 经 workflow_call 跑上游全套，见 docs/batches/upstream-ci/）
└── pyproject.toml                # packages 仅 ["src/chrys"]（上游原状）
```

## 2. 落点判定

新增任何定制文件前，按此决策树判定落点：

| 问题 | 落点 |
|---|---|
| 会进发布包（被产品运行时用到）？ | `src/chrys/{tier}/aixcoding/`（按依赖落位，档位判据见 §3；ADR-0011） |
| 是测试？ | `aixcoding/tests/` |
| 是 mock（模拟外部后端服务）？ | `aixcoding/mock/` |
| 是随制品分发的物料（安装器脚本、用户安装手册等分发给最终用户的资产）？ | `aixcoding/release/`（先于"文档"判定——安装手册等分发文档按此行命中，ADR-0020） |
| 是文档？ | `aixcoding/docs/`（规范族进 `standards/`，其余一律 `batches/<name>/`，见 ADR-0016；非随制品分发） |
| 是开发工具脚本（校验、脚手架等）？ | `aixcoding/tools/` |
| 以上都不是 | 停下：先在方案中论证归属，必要时修订本文 |

**上游目录零定制文件**：`tests/`、`locales/`、`scripts/`、`docs/`（上游的）、`.github/workflows/ci.yml`、`pyproject.toml` 等 upstream 树内不落定制文件；对它们的任何改动都是 R2 报批项。当前例外清单：空。

## 3. `src/chrys/{tier}/aixcoding/`（产品代码，按 tier 落位）

定制产品代码按**实际依赖**落位各 tier 的 `aixcoding` 子目录（ADR-0011，取代 ADR-0002 的单点落位）：

| 档位 | tier | 现有/预告子包 | 判定标准 |
|---|---|---|---|
| `kernel/aixcoding/`（默认最低档） | 1 | `auth/`（仅 foundation 依赖：`get_platform` lazy）、`diagnostics/`（≤kernel；launcher/crash/snapshot 已退役见 ADR-0013）、`telemetry/` 等适配层（批次待迁） | 零依赖或仅依赖 foundation/kernel |
| `app/aixcoding/` | 4 | `tui/`（批次待迁） | 引用宿主 UI 基础设施，被 app 接线点同层调用 |

- **档位判据**：依赖做硬判据、语义做参考——落位档 = 四档（kernel/service/orchestration/app，按需建）中能容纳其全部依赖的**最低档**；kernel 档兜底零依赖与仅 foundation 依赖；foundation 档不设（考量见 ADR-0011 否决备选）。
- tier 的机器执行者是 `tests/architecture/test_layering.py` 的 DAG（`_source_top`/`_target_top` 按第一级包名归层——定制按 tier 落位后天然合法，无 fork 特判键）；kernel 档内互引必须**相对导入**（kernel 层硬约束；语义指**同包子模块** `.` 相对——kernel 内跨子包互引应避免：`..` 父级相对被 ruff TID252 禁止，包内绝对导入又被 test_layering 的 kernel↔kernel 必须相对规则禁止，二者皆死路，跨子包需求须解耦处理——auth-logging 批次实证 2026-10-29/30）。
- **例外（结构性挂接边）**：包内个别模块对宿主的向上挂接（进程入口、钩子注册，非业务库依赖）不改变整包档位——历史上经 `_DAG_EDGE_ALLOWLIST` 豁免（ADR-0011 决策 2/5），**该通道已随 launcher 退役整体移除（ADR-0013）；替代路径是内联接线（挂接模块按 ADR-0011 判据落 app 档，或入口直接调用——auth-logging 批次 `ensure_file_sink` 入口接线即先例），重建前任何向上 import 均为架构违规；豁免通道原则上不再重建——豁免机制是升级摩擦的永久来源，确需重建时属架构决策，须 ADR（含理由）+ 责任人签认，不得以任何形式先行登记**。
- 依赖上涨 → test_layering 红 → 挪档（反馈回路闭环）；`tui` 的依赖边界：仅限宿主 UI 基础设施（对话框基类、控件、绑定展示、主题），**不得**引用 service / orchestration 业务逻辑。

## 4. 根 `aixcoding/`（开发资产与分发物料）

- 目录构成：`docs/`、`tests/`、`mock/`、`tools/`、`release/` 与 README（例外登记见 §8）；**不放任何产品代码**。
- `release/` 承载**随制品分发物料**（安装器脚本、用户安装手册）——经内网镜像分发给最终用户的资产，非开发过程消费；不进 wheel 与本目录其余部分一致（设立决策与备选否决见 ADR-0020）。
- 不进 wheel 的机制是 `pyproject.toml` 的 `packages = ["src/chrys"]`——与目录内有无 `__init__.py` 无关。
- **import 机制**：`aixcoding/tests/conftest.py` 负责把仓库根加入 `sys.path`，此后开发资产以 `aixcoding.mock.<name>` 等包路径导入（mock 专区结构见 §6）。测试目录自身**不加** `__init__.py`（保持 pytest 的无包收集模式）。
- 产品代码（打包运行时）**不得** import `aixcoding.*`——根命名空间只存在于开发仓库内，wheel 里没有它。产品代码只 import `chrys.{tier}.aixcoding.*`。

## 5. `aixcoding/tests/`（定制测试唯一落点）

- **独立收集**：pytest `testpaths` 只含上游路径，本目录须显式运行。**本地统一入口**（local-verify 批次，2026-11-01 签认）：

  ```bash
  uv run python aixcoding/tools/verify.py --smart   # 委托上游 Smart Test + 无条件本套件（默认推荐）
  uv run python aixcoding/tools/verify.py --full    # fork-full：本套件 + 委托上游 --full，任一失败非零
  ```

  直接跑变体（与 CI 门同构）：`uv run pytest aixcoding/tests -m "not integration and not gc_calibration"`；纯上游快速反馈：`verify.py --upstream-smart`（显式跳过定制套件）。

- CI 执行点是独立的定制质量门 `.github/workflows/aixcoding-tests.yml`（见 §7），不寄生于上游 CI 分片。
- `conftest.py` 职责（实测口径，2026-11-01 校准）：顶层 = `sys.path` 注入仓库根 + 通用 fixtures（file_tree/git_repo）；`auth/conftest.py` 另有窄域 autouse（委托钳制 + 默认构造隔离，marker 豁免先例，见 auth-isolation-mock-ports 批次）。**不继承**上游 `tests/conftest.py` 的 autouse fixture——有意设计（检测器不与被检测对象共享故障域，理由见 ADR-0001）；config-dir/后端隔离以**测试内显式注入为主范式**（`config_dir=tmp_path` + `MemoryBackend()`），窄域 autouse 为防呆补充。
- 目录组织：按特性分子目录（如 `tests/auth/`、`tests/telemetry/`）；mock 的契约测试统一在 `tests/mock/`。测试**不镜像**产品代码的 tier 落位——tier 是依赖决定的技术落位（ADR-0011），测试不受 `test_layering` 分层守卫治理（其射程仅 `src/chrys`），组织跟随被测特性；文件内 import 路径则随产品落位联动（如 `chrys.kernel.aixcoding.auth`）。
- 上游测试设施（`tests/support/` 等）可以**阅读、复制**，但不得 import——上游测试内部接口无稳定性承诺。

## 6. `aixcoding/mock/`（mock 专区）

模拟定制所依赖的内部后端服务。**判断标准是"谁建谁维护"**——被测功能归上游还是归定制不影响资产归属（`model_catalog` 模拟的后端主要服务上游的模型目录功能，仍在此）。

**形态语义（2026-10-25 决策澄清）**：目录名描述职责域（mock 专区），实现形态由子包内文件表达——当前全部为 HTTP server 形态（即每子包 `server.py`），不因形态更名目录（曾议 `mock_server/`，维持 `mock/`，详见 `docs/batches/mock/design.md` 决策记录）。进程内替身不属本专区：测试专用 fake/stub 归 `aixcoding/tests/`（§5），产品内可注入测试挂钩归 `src/chrys/{tier}/aixcoding/`（按 §3 落位）；非 server 形态 mock 资产出现时按 §2 判定树重新论证落点。

**包根编排层（2026-10-25 登记）**：`__main__.py` 为组合启动器——`python -m aixcoding.mock`
一键起基础组合（默认 `model_catalog` :7777 含内嵌 auth + `update` :7778；`--all` /
`--only` / `--list`），仅编排各子包既有 `create_server(...)`，不承载 mock 实现；端口
fail-fast 不静默换。设计依据 `docs/batches/mock/design.md`。

每个子包统一结构：

```
aixcoding/mock/<name>/            # 命名：无前缀服务名（auth / update / model_catalog / telemetry）
├── __init__.py                   # docstring：端点与公开 API
├── server.py                     # create_server(...) factory（测试进程内驱动）+ main() CLI（独立进程）
└── README.md                     # 端点表、故障模式、CLI 用法
```

强约束（沿用原 `mock_server/README.md` 已验证的规范）：

- 只绑定 loopback（`127.0.0.1` / `::1`），启动时拒绝其它地址；
- stdlib-only，不 import 产品包；
- 按真实后端的 wire quirks 逐字节复刻；
- 测试直连，避开系统代理；
- **每个 mock 必须有契约测试**（`aixcoding/tests/mock/`），无契约测试的 mock 不得合入。

## 7. CI 质量门

> 过渡期：`aixcoding-tests.yml` 待建（`batches/restructure/plan.md` U3）；本节描述建成后状态。

| 代码 | 门禁 |
|---|---|
| 产品定制代码（`src/chrys/*/aixcoding/`） | 上游 `ci.yml` 全套（ruff / ty / test / compileall 随 `src/`、`src/chrys` 路径自动覆盖），经桥接 `aixcoding-upstream-ci.yml` 在 PR 至 main_aixcoding_cli 时实际生效（ci.yml 自身仅 PR 至 main 触发） |
| 定制测试 + mock + 文档（`aixcoding/`） | 独立 workflow `aixcoding-tests.yml`，三层结构（windows-matrix 批次，2026-11-01）：`test` 双 OS 矩阵跑 `pytest aixcoding/tests`（ubuntu + windows，DPAPI 平台真实用例 Windows 侧实际执行）；`govern` 单 Linux 跑 `ruff check/format aixcoding/` + `check_docs.py` + 治理三工具；`gate` 保名汇总 |

- 触发条件：`aixcoding-tests.yml` 为 PR + push 至 main_aixcoding_cli（Linux 侧实测 38 秒–5 分钟；Windows 侧首跑时长待录）；桥接 `aixcoding-upstream-ci.yml` **仅 PR 触发**（对齐上游 ci.yml 惯例，合并 push 不重复跑；ci.yml 自身仅 PR 至 main 触发），单次拉起上游全套 17 job（多 OS 分片 + architecture + build + wheel 探测）+ 汇总 gate（`AIxCoding upstream gate`，分支保护必过 check 的落点），实测约 11–12 分钟。定制门已扩 Windows 矩阵：`test`/`govern` 并行执行，`gate`（保名 `AIxCoding quality gate`，分支保护必过 check 的落点）fail-closed 聚合两者。
- ty 对齐上游惯例：只查 `src/chrys`，不查测试与 mock。

## 8. 例外登记（对应 DEVELOPMENT.md §10）

| 例外 | 原因 | 期限 |
|---|---|---|
| 根 `aixcoding/__init__.py` 保留 | 作为开发资产的 import 命名空间锚点（`aixcoding.mock.*`）；不进 wheel，无打包副作用 | 持续有效 |
| 定制面 i18n 中文单语（ADR-0010 总则，取代 ADR-0005 的 tui 条目） | 默认纯中文常量；上游强类型（`MessageRef` 等）处借壳 `msg(key, fallback="中文").bind()`，借壳条目随上游流水线登记（pot + **po 中文条目** + oracle，义务与边界见 ADR-0010 细则 2）；例外（必须双语/收益明显）须在批次 design 具名给理由。**已登记：`src/chrys/app/aixcoding/tui/*` 整包**（tui 批次已落地核销：对话框/徽标/toast 纯常量；命令注册处 description/man 借壳 + pot/po/oracle 三点登记，2026-10-28） | 产品需要英文界面时重评 |

## 9. 命名约定速查

| 对象 | 约定 |
|---|---|
| 定制目录/包名 | 全小写 `aixcoding`（上游标记注释用 `AIxCoding`，见 R3） |
| fork 新增环境变量 | 统一 `AIXCODING_` 前缀；例外：配置契约归属上游的沿用 `CHRYS_` 并在特性 design 登记——判据=**配置契约归属**（变量的语义/解析/校验/默认值/生命周期由谁拥有；由上游 settings `spec()` 声明并纳入其加载/合并/投影体系即归属上游），"模块内定义并直读"是 fork 私有契约的机械代理判据，用户可见性/调试用途不影响分类（首次决策 2026-10-25：`CHRYS_AUTH_*` 随 auth 迁入更名 `AIXCODING_AUTH_*`，详见 `docs/batches/mock/design.md` 决策记录；判据澄清与 `SYNC_INTERVAL` 归类更正 2026-11-02，catalog-sync 批次 design 对比五） |
| 统一部署环境 | `AIXCODING_ENVIRONMENT=local/dev/prod`；共享配置文件为数据目录下 `aixcoding.toml`，优先级 env > TOML > prod；旧变量零兼容（ADR-0022） |
| mock 子包 | 无前缀服务名：`auth`、`update`、`model_catalog`、`telemetry` |
| 测试文件 | `test_*.py`；契约测试随被测 mock 命名（`test_<name>_server.py`） |
| mock 公开 API | `create_server(...)`（factory）/ `main()`（CLI）；配置状态类 `Mock*Config` / `Mock*State` |
| 功能文档目录 | `batches/` 下小写中划线 `<feature-name>/`；文档文件名固定（requirements/design/test-plan/plan/verification.md，见 DEVELOPMENT.md §6） |
| 文档 | 中文（团队工作语言）；规范族与 ADR 在 `docs/standards/` |
