# 设计：定制版用户手册入库与安装版捆绑（user-manual）

<!--
签认规则（R10）：责任人本人编辑本头部（状态→已确认、填日期，确认意见可选），AI 不得代填。
签认后修改正文需重新签认。校验：uv run python aixcoding/tools/check_docs.py
-->

- 责任人：赵阔
- 状态：已确认
- 确认日期：2026-11-10
- 确认意见：同意方案 C（数据源切换：fork 命名空间落位 + force-include 换源 + env 指向）；R2 表所列 pyproject.toml / scripts/check_wheel.py / tests/foundation/test_documentation.py 三处上游改动予以批准（C145-C148）；评审第一轮三个 blocker 按处置结论修复。
- 审批人：赵阔（R11：无新增第三方依赖，本节结论批准）
- 审批日期：2026-11-10

## 方案概述

定制手册整体落位 fork 专属命名空间 `aixcoding/manual/`（与 `aixcoding/docs`、`aixcoding/tools`
平级的开发资产目录），内容为上游 `docs/` 双语树的定制副本（0.35.1 同步 + 去链 + 措辞统一）。
手册进入产品的通道**沿用上游既有机制、只换数据源**：

```
安装版（内网客户）F8
  → resolve_docs_root()（src/chrys/foundation/documentation.py，上游代码零改动）
  → 捆绑副本 chrys/app/tui/screens/guides/_docs/
  ← pyproject.toml force-include："aixcoding/manual" = "chrys/app/tui/screens/guides/_docs"（上游行为改动①）

开发机 F8（经本地启动器）
  → CHRYS_DOCS_ROOT=aixcoding/manual（解析第一优先级，上游扩展点）
  → 直接读工作树 aixcoding/manual/（改 MD 即见，免重装）

源码直跑（无启动器）F8
  → 兜底仓库 docs/（上游项目版本，保持零定制）
```

`scripts/check_wheel.py` 的捆绑完整性门禁（`git ls-files` 比对）同步切换比对源（上游行为
改动②）。仓库 `docs/` 不删除、不定制——继续随上游整树演进，作为升级对齐的对照基准。

### 落点判定（DIRECTORY.md §2 决策树）

`aixcoding/manual/` 会进发布包（F8 运行时消费）→ 按树应落 `src/chrys/{tier}/aixcoding/`；
但该落位判据针对**代码**（tier=依赖容纳档位，ADR-0011），本资产是纯数据资源，经上游已有的
`force-include` 构建通道进包（上游 `docs/` 原本就是同通道进包的先例）。故落 `aixcoding/manual/`
（开发资产 + 构建进包），不新建 `src/chrys/*/aixcoding/manual/` 数据目录、不违反 R4
（新文件均在 aixcoding 命名空间内）。

## 备选方案对比

| 方案 | 优点 | 缺点 | 升级冲突面（R1） | 结论 |
|---|---|---|---|---|
| A. 延续 0.33 做法：直接定制上游 `docs/{en,zh-Hans}/` | 零新机制；安装版自动带定制 | 每次追上游 `docs/` 整树必冲突；定制定位失焦（改的是"上游的文件"） | 高：docs/ 双语树每次升级 50+ 文件逐个手排 | **否决**（0.33→0.35.1 实际发生过） |
| B. 改 `src/chrys/foundation/documentation.py` 默认解析指向 `aixcoding/manual` | 一处改动全形态生效（含 `python -m` 直跑） | 动上游核心模块：R2 报批重 + 升级冲突面上移到代码层 | 中高：1 文件但属解析核心，上游重构解析即冲突 | **否决**（上游已有 env 扩展点） |
| C. 数据源切换（本方案）：fork 命名空间落位 + force-include 换源 + env 本地指向 | 上游文件仅碰 2 个配置/脚本点；手册定制全部收敛进 fork 命名空间；三形态指向体系成立 | `python -m` 直跑（无启动器）只能看 `docs/` 项目版本（已知限制，登记于 requirements 非目标） | 低：2 文件 × ~5 行 × 声明式配置/比对清单，上游低频改动区 | **采用** |

## 上游改动清单（R2）

| 文件 | 改什么 | 预估行数 | 标记注释位置 |
|---|---|---|---|
| `pyproject.toml` | ① `[tool.hatch.build.targets.wheel.force-include]` 源键 `"docs"` → `"aixcoding/manual"`；② `[tool.ruff] extend-exclude = ["aixcoding/manual"]`（**评审后增补的防御性策略**：ruff 0.17.1 会格式化 md 内 ```python 围栏、重排手册有意对齐的注释〔显式路径可复现：`ruff format --check aixcoding/manual` 报 6 个 md unformatted〕——交付内容不允许机器改写；CI 现用目录遍历 `aixcoding/` 尚未覆盖 md，故此项为预防未来行为变化/显式路径调用的策略声明，非为修当前红灯。理由口径已按增量复审 N1 更正，2026-11-10） | +8 / −1 | ① 键行上方 `# AIxCoding manual bundle: wheel docs source key (user-manual C145)`；② extend-exclude 行上方 `…(user-manual C148)` |
| `scripts/check_wheel.py` | `check_archive()` 捆绑清单比对源 `git ls-files -z docs` → `-z aixcoding/manual`；`removeprefix("docs/")` → `removeprefix("aixcoding/manual/")` | +3 / −2 | 比对源行上方 `# AIxCoding manual bundle: compare against the fork-owned manual tree (user-manual C146)` |
| `tests/foundation/test_documentation.py` | 契约测试 `test_wheel_bundles_docs_where_the_resolver_looks` 断言键 `force_include["docs"]` → `["aixcoding/manual"]`（**评审 F1 增补**，2026-11-10：改键名必然连带，跟改使契约测试继续守护新契约） | +2 / −1 | 断言上一行 `# AIxCoding manual bundle: …`（C147） |

> 修订记录：2026-11-10 独立评审（Claude Code + GLM-5.3）F1——上表第 3 行为评审后增补；
> 原稿遗漏该连带跟改，系"上游测试零跟改"误判（该契约测试本机不跳过，旧 92 passed 证据
> 对应 pyproject 切换前的树况）。

- R1 自查：业务逻辑零外置（本批次无新增定制代码模块）；接线点最小化——上游行为改动仅
  "数据源替换"一处语义（pyproject 与 check_wheel 是同一语义的声明侧与校验侧，两处都必须
  改，否则构建与门禁口径分裂）。
- 本批次**不属迁移批次**（无旧实现平移），R1 迁移细则不适用。

## 第三方依赖申请（R11）

无新依赖（PDF 生成链路的 `markdown` 包为桌面交付侧临时工具，用毕即卸、不入 `uv.lock`，
不属产品依赖）。

## 对上游隐式行为的假设

1. **三级解析优先级**：`resolve_docs_root()` 中 `CHRYS_DOCS_ROOT` 环境变量 > wheel 捆绑副本
   > 源码 checkout（`documentation.py:22-36`）——我方依赖"env 恒最高"。假设失效表现：开发机
   F8 回退到捆绑副本/`docs/`，显示项目版本内容（不致错，但与客户不同源）。
2. **捆绑目标路径契约**：force-include 目标 `chrys/app/tui/screens/guides/_docs` 必须与
   `documentation.py` 的 `BUNDLED_DOCS_PATH = Path("app/tui/screens/guides/_docs")` 及
   `check_wheel.py` 的 `BUNDLED_DOCS_PREFIX` 三方一致——上游若改捆绑路径，本批次两处改动需
   跟随（升级检查项，见风险节）。
3. **force-include 支持子目录源键**：`"aixcoding/manual"` 作为源键（相对仓库根的子目录）在
   hatch 构建语义下合法——已实测（本批次重建 wheel 验证捆绑清单一致）。
4. **`git ls-files` 比对前提**：`check_wheel.py` 以 `git ls-files aixcoding/manual` 为期望集，
   要求手册文件**已入库**——手册未提交时该门禁退化为空集比对（不报错但失去防护），
   因此手册入库与脚本切换必须同笔提交（已按此执行，ed8dd517）。

## 定制点登记预告

（ADR-0015 点级登记；本批次 2 个定制点，编号接现有高水位 C144 之后的 C145/C146。）

| id | 落点 | marker 锚 | 预期计数 | anchor | risk | removal_condition |
|---|---|---|---|---|---|---|
| C145 | pyproject.toml | AIxCoding manual bundle: wheel docs source key | 1 | `[tool.hatch.build.targets.wheel.force-include]` | 低——声明式配置，上游改捆绑数据源策略时冲突 | 〔机制替代〕上游提供手册数据源配置化能力，或手册定制淘汰回归纯上游版本 |
| C146 | scripts/check_wheel.py | AIxCoding manual bundle: compare against the fork-owned manual tree | 1 | `check_archive()` 捆绑清单比对 | 低——校验脚本低频改动 | 〔机制替代〕同 C145（两点同生共死） |
| C147 | tests/foundation/test_documentation.py | AIxCoding manual bundle | 1 | `test_wheel_bundles_docs_where_the_resolver_looks` 断言 | 低——契约测试随 C145 键名联动 | 〔机制替代〕同 C145（契约随捆绑源同生共死） |
| C148 | pyproject.toml | AIxCoding manual bundle: the manual is delivered user-facing content | 1 | `[tool.ruff] extend-exclude` | 低——格式化配置，上游改 lint 版式策略时冲突 | 〔机制替代〕上游把交付文档纳入格式化治理并提供正式排除通道，或手册定制淘汰 |

> R3 正式单行标记与 `customization-registry` 登记为收尾项（plan.md 待办），登记完成后
> `gen_customizations.py --strict` 对账作为门禁。

## 风险与最坏情况

- **内容漂移**：`aixcoding/manual/` 与上游 `docs/` 是两份树，上游 0.36+ 更新手册后若不对齐，
  客户手册逐渐落后。缓解：每次升级批次（UPGRADE-RUNBOOK 流程）将"manual 对齐上游 docs/
  变更"列为固定步骤（对账基准 = `git diff <旧tag> <新tag> -- docs`）；内容性定制（去链、
  措辞）按 `check_links.py` 门禁 + 措辞清单重放。
- **上游改捆绑机制**（假设 2 失效）：force-include 目标路径或 `BUNDLED_DOCS_PATH` 变更 →
  构建后 F8 报"找不到手册"或捆绑校验红——升级语义层检查项，2 处跟随改动即修复。
- **最坏情况回滚**：`pyproject.toml` 与 `check_wheel.py` 各回退一处键名/前缀即恢复上游
  原状（捆绑 `docs/`）；`aixcoding/manual/` 目录整体删除即完全回到升级前形态——回滚不涉及
  `src/`，无代码层纠缠。

## ADR 判定（DEVELOPMENT.md §6）

- ①有被认真考虑但否决的备选方案？**是**（方案 A 直接定制 docs/、方案 B 改解析代码）；
- ②撤销代价高（目录落点/构建契约）？**是**（手册资产落位与 wheel 捆绑源是结构契约）；
- ③约束未来工作模式？**是**（此后所有手册定制必须走 `aixcoding/manual/`，不得回改 `docs/`）；
- ④依赖上游非公开接口？否（env 变量与 force-include 均为公开声明式机制）。

→ 按四问 ①②③ 成立应出 ADR；**责任人 2026-11-10 裁决不出 ADR**（决策内容已由本文
「备选方案对比」「方案概述」三形态指向体系、「风险与最坏情况」节完整承载，撤销代价低
——回退两处键名前缀并删目录即还原）；原草案 `adr/0001-手册定制落位与捆绑源切换.md`
已按该裁决删除，删除不改变决策内容。
