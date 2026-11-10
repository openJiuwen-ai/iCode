# 需求：定制版用户手册入库与安装版捆绑（user-manual）

> 活文档：随时更新、不签认（git 留痕）；意图确认由 design 签认承载。
> 来源：0.35.1 升级后用户手册交付需求（2026-11-09/10 两轮：0.35.1 文档同步 → 定制手册入库与构建指向切换）。
> 修订记录：2026-11-10（一）初稿——并入"定制手册入库 + wheel 捆绑源切换 + F8 指向体系"三项决策；本批次为**事后补录**（改动已实施，管线文档按实际终态回写，见 plan.md 状态节）。

## 背景与问题

0.35.1 升级（upstream-merge-v0.35.1 批次）后，用户手册交付出现三个问题：

1. **升级冲突面**：此前（0.33 期）手册定制直接改上游 `docs/{en,zh-Hans}/` 树，每次追上游
   `docs/` 必然冲突；0.35.1 起手册更新频率上升，冲突面必须清零。
2. **安装版 F8 内容失真**：内网客户安装版（wheel）中 F8 用户手册只能读 wheel 捆绑副本
   （`chrys/app/tui/screens/guides/_docs/`），其捆绑源是 `pyproject.toml` force-include
   的仓库 `docs/`（上游项目版本）——我方定制的措辞修正、第三方外链去除、内网下载地址等
   改动**到不了客户手里**；安装版环境无 `CHRYS_DOCS_ROOT`、无源码 checkout 兜底。
3. **双轨交付**：桌面定稿 MD/PDF 与仓库内容需保持同源，避免两套真相。

## 目标与非目标

- 目标：
  - 定制版手册落位 fork 专属命名空间 `aixcoding/manual/`（双语树 + `index.yaml`，
    67 文件），仓库 `docs/` 保持上游项目版本**零定制**——上游更新 `docs/` 整树拉取零冲突；
  - wheel 捆绑源切换：`pyproject.toml` force-include `"docs"` → `"aixcoding/manual"`，
    安装版 F8 展示定制手册；`scripts/check_wheel.py` 捆绑清单比对同步切换；
  - 开发机 F8 指向体系：本地启动器设 `CHRYS_DOCS_ROOT`（解析最高优先级）指向
    `aixcoding/manual/`，开发态与安装态同源；
  - 手册内容质量基线：0.35.1 内容同步、内锚全有效、第三方外链清零、模型相关措辞统一；
  - 双轨同步：桌面定稿 MD/PDF 由本目录 MD 出品（MD → HTML → Edge 无头打印链路）。
- 非目标：
  - **不改 `src/chrys/foundation/documentation.py` 解析逻辑**——三级解析
    （env → 捆绑副本 → checkout）是上游既有扩展点，本批次只消费不改动；
  - `docs/`（上游）的长期演进——继续随上游整树拉取，不做任何定制改写；
  - 手册翻译质量与内容再创作——本批次只做同步、去链、措辞统一三类机械定制；
  - `aixcoding-launch.py` 入库——责任人裁定其为本地运行辅助（SAC 规避），永不提交 git，
    F8 指向能力不随分支分发（同事侧如需同能力，按 design"部署形态"节自行落位）。

## 用户场景

1. 内网客户安装 wheel 后按 F8：读捆绑副本（源 = `aixcoding/manual/`），看到含内网下载
   地址、修正措辞、无第三方外链的定制手册。
2. 开发机双击启动器运行：`CHRYS_DOCS_ROOT` 生效，F8 与客户所见同源；改
   `aixcoding/manual/` 任一 MD 后重开 F8 即见，无需重装。
3. 责任人跟上游 0.36.x：`git pull` 拉取上游 `docs/` 整树更新（项目版本），与本分支定制
   手册零交集、零冲突；手册对齐由本批次 plan.md 的"升级对齐流程"承接。

## 验收标准

- [x] `aixcoding/manual/` 入库 67 文件（`index.yaml` + `en/` + `zh-Hans/`，66 篇 MD），
      结构与上游 `docs/` 同构；仓库 `docs/` 保持上游 0.35.1 原状（零定制）；
- [x] 手册内容质量：内锚 237 个全有效（GitHub slug 规则校验）、第三方外链 0
      （2026-11-10 评审 F3 处置后复扫：manual 树围栏外外链 100→2，保留项为产品仓库发布页，
      wheel 内 0）、"模型配置 → 模型"措辞统一（24 文件清理已落主线 `9a6dca78`）；
- [x] wheel 捆绑源切换：构建产物捆绑文件清单与 `aixcoding/manual/` 逐文件一致（67=67，
      内容哈希零差异）；
- [x] `scripts/check_wheel.py` 捆绑完整性比对源为 `aixcoding/manual`（git ls-files）；
- [x] 开发机 F8 经 `CHRYS_DOCS_ROOT` 解析到 `aixcoding/manual/`（实测）；
- [x] 桌面定稿 PDF 与本目录 MD 同步（139 页，2026-11-09 15:42 版后随内容更新重出）；
- [ ] 本批次文档入库后 `check_docs.py` 门禁绿（26 design / 32 self-review）；
- [ ] R3 标记注释补录 + 定制点登记（C145/C146）+ `gen_customizations.py --strict` 对账绿
      （收尾项，见 plan.md）。

## 开放问题

（无——三项结构决策已定，见 design.md 备选方案对比与方案概述；遗留收尾项见 plan.md。）
