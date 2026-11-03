# 需求：模型目录同步 + 档案集中管理（catalog-sync）

> 活文档：随时更新、不签认（git 留痕）；意图确认由 design 签认承载。
> 来源：ROADMAP §4 C2 行（2026-11-02 盘点，判定建议"有改动的迁"——本批次重做对比分析后呈报，见 design.md）。
> 修订记录：2026-11-02（二）外部 AI 评审处置后按责任人裁决重写——UI 形态选 A（照 0.28 final）、ACP 边界选 ②（仅承诺 TUI）。
> 2026-11-02（三）上游测试连带面核查后严格化"上游套件不回归"验收口径（仅精确对应已签认行为差异的失败可经 R2 登记跟改；配套 design R2 表 14 号）。
> 2026-11-02（四）第三轮评审处置：验收口径补裁判基准（F-5）；models.md 用户文档失真登记为非目标、留后续文档批次（F-6 责任人裁决）。

## 背景与问题

内部部署的模型清单由服务端统一管理（按登录用户下发）：本地 `~/.chrys/models/`
不再由用户手工维护，而是从模型目录服务端点拉取并整体替换；模型配置界面相应转为
只读（档案集中管理）。该能力在旧分支 `main_aixcoding_cli_0.28.0_final`（上游 0.28.0
基线）已服役，新分支（0.32.2 基线）尚未迁入——当前新分支的模型档案完全本地自治，
与内部部署形态不符。

配套资产已就位：`aixcoding/mock/model_catalog/`（M3，mock 批次 2026-10-26）、
auth 包（`chrys.kernel.aixcoding.auth`，tier 迁入完毕）、tui 批次在
`login_actions.py` 预埋的两处启停占位符注释。

## 目标与非目标

- 目标：
  - 迁入模型目录同步：启动阻塞同步（仅 TUI 入口）+ 15 分钟轮询 + 登录触发即时同步；
  - 迁入服务端 payload 翻译与校验（OpenRouter 形态 `models` 列表 → 本地 ModelProfile）；
  - 迁入目录整替语义：同步成功 = 目录整体替换（服务端删除可生效），失败 = 保留原列表
    ——**修正旧实现的非事务发布缺口**（写盘中途失败不再产生半新半旧/空目录，见
    design"目录发布"节）；
  - 迁入档案集中管理 UI 面（方案 A，照 0.28 final）：模型配置屏恒只读（含只读原因
    文案改为集中管理提示）、F4 入口隐藏、picker "Manage models…" 禁用；`/models`
    命令、模型指示器 configure 态、未配置确认框保留为**只读查看**入口；
  - 迁入配套定制测试（时钟/停止/即时同步/凭据门/tier 对齐 drift test/删除语义/
    发布故障注入）。
- 非目标：
  - **ACP 通道的集中管理（责任人裁决 ②，2026-11-02）**：本批次仅承诺 TUI。ACP 的
    `write_model_profile`/`delete_model_profile`（`session_manager.py:451-493`）继续
    可用，**已知限制登记**：目录管理态下经 ACP 的写入会在后续同步中被覆盖/删除、
    经 ACP 的删除会被同步"复活"、TUI 同步发布与 ACP 写入存在多进程并发写窗口（不设
    防位）；ACP 进程的模型视图为启动时快照（不参与同步）。后续批次可经
    `require_catalog_writable()` 钩子（0.28 预留未接线）补齐全前端禁写；
  - 当前会话的 engine 即时重建：同步落地后**沿用旧语义**——运行中的 engine 保持
    build 时解析的配置，重启/设置重载后生效；被服务端删除的 profile 在下一 turn
    resolve 失败（与重启后表现一致）；
  - 平台连接配置（旧 `config.py`）——ROADMAP §4 C3，拆解随消费批次；
  - telemetry / update 检测——各自独立批次（telemetry 殿后）；
  - mock 服务——M3 已就位，本批次只消费不改动；
  - **用户指南文档改写（责任人裁决 2026-11-02）**：`docs/{en,zh-Hans}/guides/
    configuration/models.md` 双语仍为上游"本地模型管理"指南（含 F4、创建/克隆/修改/
    删除、任务期只读），与本批次恒只读/F4 隐藏形态失真；0.28 final 曾有集中管理定制
    版（"不支持创建模型配置，仅支持切换；模型由管理员分配"）。该文档改写涉及双语全篇
    重写，**并入 ROADMAP §4 C 类"用户指南文档定制化"工作项，随后续文档批次处理**
    （0.28 final docs/ 双语树整树定制考古一并收敛于该工作项）。

## 用户场景

1. 内网用户启动 TUI（凭据已存）：启动路径先同步目录再加载 registry，模型列表即服务端
   最新清单；轮询使其长会话内自动跟进服务端变更；指示器标签经 `_live_label` 跟踪
   registry 实时名（服务端改名即时反映）。
2. 会话中执行 `/login`：登录成功回调触发一次即时同步，模型列表无需等待整轮间隔；
   `/logout` 停止轮询。
3. 管理员在服务端删除某模型：下一轮同步（最长 15 分钟）后，本地 profile 文件与内存
   registry 同步消失，picker 不再可选；当前会话中该模型仍在用的请求按"resolve 失败"
   处理（与重启后一致）。
4. 用户尝试本地编辑模型档案：F4 不在 footer；picker 无 "Manage models…" 条目（模型
   切换走 picker 的 profile 列表）；`/models` 命令与指示器 configure 态仍可打开模型
   配置屏——**恒只读**（idle 态亦然），页脚显示集中管理提示文案。

## 验收标准

- [ ] 已登录 + 配置了目录源（tier 默认或显式 base/env URL）时，TUI 启动完成一次同步：
      `~/.chrys/models` 被整替，`.catalog-state.json` 写入，registry 内容 = catalog；
- [ ] 同步删除语义：上一轮存在的 profile 在服务端删除后，下一轮同步使其从目录与
      registry 同时消失（快照安装行为有测试锚定）；
- [ ] **发布原子性（故障注入测试）**：抓取失败 / 解析失败 / 第 N 个 profile 文件写
      失败 / 状态文件写失败 / 旧文件删除失败——各情形下旧目录保持可用（或可恢复），
      启动路径不因未转换的 `OSError` 中断，降级路径有日志留痕；
- [ ] 无目录源 / 无凭据 / 同步失败三种情形：不启动轮询线程或保留原目录原列表，
      TUI 启动不受影响；
- [ ] 登录成功触发即时同步、登出停止轮询（占位符替换为实调，行为有测试；登出后
      in-flight 同步结果不再发布——代次防护）；
- [ ] UI 集中管理三面（方案 A）：模型配置屏 `read_only=True` 恒成立（含 idle 态，
      测试断言）且只读提示文案为集中管理措辞（非 "Agent is running"）；F4 binding
      从 footer 移除；picker 无 "Manage models…" 条目；`/models` 命令与指示器
      configure 态打开的屏为只读（保留入口不删）；
- [ ] tier 对齐 drift test 绿：catalog 的 tier 变量与 `chrys.kernel.aixcoding.auth`
      的 `AIXCODING_AUTH_ENVIRONMENT` / `Environment` 枚举 / PROD 回退一致；
- [ ] 废弃 env 名检测：四个退役名（`CHRYS_MODEL_CATALOG_URL` / `..._TOKEN` /
      `..._SYNC_INTERVAL` / `CHRYS_REMOTE_MODEL_CONFIG_URL`）任一存在时输出警告
      （只记变量名不记值）且不生效，新名优先（测试锚定）；
- [ ] 定制测试套件（`aixcoding/tests/catalog/`）全绿；上游套件不回归（口径：经 R2
      登记、且仅对应已签认行为差异的上游测试跟改（R2 表 14 号）完成后，上游全套测试
      通过；未登记或与本批次行为无关的失败均视为回归，不得通过删测、跳过或放宽无关
      断言消除失败。是否属于已签认行为差异，以 design R2 表 14-16 号为基准；超出
      该清单或归因存在争议时由责任人裁决，涉及新增上游测试跟改须先完成 R2 重签，
      实施 AI 不得自行认定）；`gen_customizations.py --strict` 对账绿；
- [ ] 离线打包冒烟：产物 TUI 可完成 mock 登录 + 目录同步 + picker 选择（ROADMAP
      阶段三 DoD 项）。

## 开放问题

（已收敛——2026-11-02 责任人裁决 + 双 AI 评审一致，随 design 草案呈报：）

- ~~UI 方案（0.28 final / B1 / B2）~~ → **A：照 0.28 final**；
- ~~ACP 边界~~ → **②：本批次仅承诺 TUI，限制登记于非目标**；
- ~~catalog env 前缀~~ → **已确认（2026-11-02）**：A 组三变量更名
  `AIXCODING_MODEL_CATALOG_URL` / `..._TOKEN` / `..._SYNC_INTERVAL`（含 SYNC_INTERVAL
  归类局部更正），legacy 名退役无兼容期 + 废弃名警告检测；
  `CHRYS_MODEL_CATALOG_BASE_URL` 不改（settings 声明体系例外，判据澄清随批次落
  DIRECTORY.md §9）；
- picker/F4 禁用形态：**删除代码 + marker**（双 AI 一致；理由=代码卫生与定制点可
  追踪）→ 随 design 签认一并确认；
- TUI `main()` 退出 `finally stop`：**保留（已定稿，第三轮 AI 评审）**——放入上游
  既有 finally 块（+1 行）；定位为"应用生命周期结束时的失效化通知"（阻止后续调度、
  丢弃未 apply 的过期结果、兜底隔离残余回调——非绝对消除竞态，边界见 design 对比三）；
  不 join、不用 atexit/context manager/on_unmount 替代。
