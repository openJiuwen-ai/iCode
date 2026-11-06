# aixcoding —— AIxCoding 定制专区（开发资产与分发物料）

> **基线**：2026-10-25 起按三阶段路线（规范 → 基础工程 → 功能迁入）在新分支 `main_aixcoding_cli`（上游 0.31.0 干净基线）重建，见 `docs/ROADMAP.md`；旧分支 `main_aixcoding_cli_0.28.0_final` 资产的迁入映射见 `docs/batches/restructure/plan.md`。

本目录承载 iCode-fork 的**全部定制开发资产**（文档、测试、mock、开发工具）与**随制品分发物料**（安装器脚本、用户安装手册，`release/`，ADR-0020），不进 wheel。
进 wheel 的定制产品代码按 tier 落位 `src/chrys/{tier}/aixcoding/`（ADR-0011）。

## 结构一图

```
aixcoding/
├── docs/          # standards/（规范族+ADR+模板）、INDEX.md（目录导航）、batches/<批次名>/（全部批次/专项目录，ADR-0016）
├── tests/         # 定制测试唯一落点（显式运行，见下）
├── mock/          # 自建 mock 专区（auth / update / model_catalog / telemetry）
├── release/       # 随制品分发物料（内网安装器脚本、用户安装手册；ADR-0020）
└── __init__.py    # 开发资产 import 命名空间锚点（不进 wheel）
```

## 速查

| 我要… | 去哪 / 怎么做 |
|---|---|
| 新增定制产品代码 | `src/chrys/{tier}/aixcoding/`（档位判据见规范 §3；先读 `docs/standards/DEVELOPMENT.md`） |
| 新增定制测试 | `aixcoding/tests/<特性>/` |
| 新增 mock | `aixcoding/mock/<服务名>/`（步骤见 `mock/README.md`；必须配契约测试） |
| 新增分发物料（安装器/用户手册） | `aixcoding/release/`（ADR-0020；随内网镜像分发，非开发过程消费） |
| 跑定制测试 | `uv run --extra all pytest aixcoding/tests -m "not integration and not gc_calibration"` |
| 查三阶段路线（规范→基础工程→功能迁入） | `docs/ROADMAP.md` |
| 查硬规则（R1–R13） | `docs/standards/DEVELOPMENT.md` |
| 判定本次改动走全管线还是微改动豁免 | `docs/standards/DEVELOPMENT.md` §6（三条件全满足即豁免） |
| 查人机工作流（责任人五个检查点 + AI 执行流） | `docs/standards/DEVELOPMENT.md` §4 |
| **团队成员必读（我要做什么·一页）** | `docs/standards/TEAM-GUIDE.md` |
| 查目录细则（落点/蓝图/tier/命名） | `docs/standards/DIRECTORY.md` |
| 查"为什么这样定" | `docs/standards/adr/`（含被否决的备选） |
| 自审怎么做（作者，提 PR 前照做） | `docs/standards/SELF-REVIEW-HOWTO.md` |
| 评审怎么做（评审人，打开 PR 照做） | `docs/standards/PEER-REVIEW-HOWTO.md` |
| 新建功能文档 | 从 `docs/standards/templates/` 复制骨架（requirements/design/test-plan/plan/verification/self-review/adr） |
| 签认文档（R10 责任人本人） | 编辑文档头部字段：状态→已确认、填日期；AI 不得代填 |
| 校验文档签认 | `uv run python aixcoding/tools/check_docs.py`（ADR + design + self-review 签认；design 含 R11 依赖审批字段联动） |
| 查定制点清单（升级地图） | `uv run python aixcoding/tools/gen_customizations.py --report`（登记源单一化对账，ADR-0015——CUSTOMIZATIONS.md 产物已退役） |

## 历史备注

本目录曾含 `auth/`、`tui/` 产品代码并以顶层包身份进 wheel（2026-10-08 `f4f90f5`），曾名 `telemetry-mock/`、`doc/` 等混杂结构。2026-10-21 目录重组（见 `docs/batches/restructure/plan.md` 与 ADR-0002/0003）后定位收敛为纯开发资产；2026-11-05 起增设 `release/` 分发物料维度（ADR-0020，internal-installer 批次）。
