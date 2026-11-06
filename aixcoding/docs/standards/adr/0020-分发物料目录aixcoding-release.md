# ADR-0020：分发物料目录 aixcoding/release/

<!--
签认规则（R10）：责任人本人编辑本头部（状态→已确认、填日期），AI 不得代填。
签认后修改正文需重新签认；被取代时由新 ADR 回指，本文状态行改为：被取代（被 ADR-00NN 取代）。
校验：uv run python aixcoding/tools/check_docs.py

命名与编号：NNNN-标题.md，取目录现有最大编号+1；编号被引用后不改号；
并行撞号罕见且必然被 git 同名冲突暴露，暴露早期重新编号即可。

ADR 判定 checklist（出 ADR 的门槛，任一"是"；粒度是决策不是功能）：
① 有被认真考虑但否决的备选方案（tools/ 落点、批次目录落点、恢复 scripts/ 上游树——均被否决，见"理由"）
③ 约束未来工作模式，后来者会问"为什么有这个目录"（✔）

分池依据（DEVELOPMENT.md §6）：全局/架构级决策入本目录（standards/adr/，不随特性消亡）；
本决策确立"随制品分发物料"这一资产类别的唯一落点规则并修订根 aixcoding/ 的定性
（纯开发资产 → 开发资产与分发物料），约束此后所有涉及分发物料的批次（internal-installer
为首个消费方，C1 更新链内部化、文档批次后续均可能涉及），属全局级。
出处：internal-installer 批次 design.md §8（第一轮评审 B1 落点违规 → R-1a 裁决新建目录；
第二轮复审 B2 指出规范闭环不完整 → 责任人裁决 B2a 完整闭环 + 本 ADR，2026-11-05）。
-->

- 责任人：周彬
- 状态：已确认
- 确认日期：2026-11-05
- 确认意见：无特别意见

## 背景

internal-installer 批次（内网安装器与发布约定迁移）需新增三类分发物料：内网版
安装器脚本×2（`install-internal-mirror.sh/ps1`，从 0.28 旧分支迁入并按 0.33/C4 契约适配）
与用户安装手册（`INSTALL.md`）。考古事实：这三类物料在 0.28 系 fork 纯新增
（上游 v0.28.0 的 `scripts/` 无 `install.*`），上游 0.29.1 起才在 `scripts/`
同名路径引入自己的公网版脚本——新基线上该路径已是上游活跃文件（带 C4 定制
marker C111-C114 与上游测试套 C119-C124）。

落点约束：R4 要求所有新增文件一律入 `aixcoding` 目录；DIRECTORY §2 要求上游
目录（含 `scripts/`）零定制文件。既有判定树（mock？文档？开发工具脚本？）无
"分发物料"类别——它不是开发工具（用户消费，非开发过程消费）、不是 mock、
不是批次过程文档（长期分发资产）。

## 决策

**设立 `aixcoding/release/` 作为随制品分发物料的唯一落点**（ADR-0016 的
"文档一律 batches/" 不适用于分发物料——batches 是过程记录语义）。同步修订
`DEVELOPMENT.md` §2 根目录定性（上位总纲，第三轮复审 B1 补）、`DIRECTORY.md`
§1 总蓝图、§2 判定树（分发行前置于"文档"行，避免安装手册双匹配）、§4 根目录
定性与本专区 README："纯开发资产"→"**开发资产与分发物料**"。首个消费方为
internal-installer 批次（`install-internal-mirror.sh/ps1` + `INSTALL.md`）。

## 理由

- **否决 `aixcoding/tools/`**：tools 定义为"开发工具脚本（校验/脚手架等）"，
  安装器的消费者是内网最终用户（经镜像分发），非开发者——语义错位会长期误导
  落点判定；
- **否决批次文档目录（batches/<name>/）**：批次目录是过程记录与批次资产载体，
  用户手册与安装器是跨批次长期分发资产，随批次目录沉淀语义不符；
- **否决恢复 `scripts/` / 根级 `release/`（0.28 落法）**：违反 R4 与 DIRECTORY §2
  （上游目录零定制文件）；且 `scripts/install.*` 已与上游撞名——并存新增会污染
  上游路径，覆盖替换会丢失 C4 marker 并与上游测试套/演进冲突；
- **不进 wheel**：分发物料不是产品代码，与根 `aixcoding/` 其余部分一致
  （`packages = ["src/chrys"]` 机制天然覆盖）；
- **D5 关联**：镜像地址统一 IP 直连与本 ADR 同源于内网分发形态（ADR-0018
  明文 HTTP + IP 直连口径）。

## 后果

- 此后所有"随制品分发的物料"（安装器、用户手册、发布说明等）一律落
  `aixcoding/release/`，经 DIRECTORY §2 判定树该行判定；
- `release/` 下文件为纯 aixcoding 树资产：不进 marker 扫描
  （`gen_customizations.iter_upstream_files` 以 `is_custom` 跳过）、不登记 C id、
  `--strict` 对其零预期——批次以"strict 零新增登记零漂移"为验收项；
- 根 `aixcoding/` 定性表述以"开发资产与分发物料"为准（`DEVELOPMENT.md` §2、
  `DIRECTORY.md` §1/§4、专区 README 已同步；判定树分发行先于文档行匹配）；
- 内网分发体系废弃时，本目录连同其资产随退役批次处置（re-评估触发条件）。
