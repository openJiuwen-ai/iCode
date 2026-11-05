# 日志体系全景与路由对照（logging-routing-fixes 批次辅助说明）

> 活文档：随时更新、不签认（git 留痕）。本文件是批次辅助说明——前半为**超出本批范围的体系全景**（供导航与后续批次②③复用），后半为本批目标态对照。
> 依据：ADR-0017（detail 仅文件日志子树）、DEVELOPMENT.md §9"日志分层口径"（2026-11-04 定稿）、批次 design.md（已签认 2026-11-05）。
> 状态口径（2026-11-05 更新）：design 已签认，批次①实施与机器验证已完成（见 plan.md / verification.md），当前处于双 AI 自审→PR 阶段——第三节"批次①"列已是**落地后实态**，其余两列"现状"指批次②③未实施前的主干。

## 一、通道与卷的物理结构

```
aixcoding logger 树（定制层）
│
├── aixcoding ────────── 主通道（"用户功能事件"通道）
│   ├── handler① _Marker → ~/.chrys/aixcoding-logs/aixcoding.log（1MB×3 轮转）
│   └── propagate = aixcoding.toml [logging] panel（默认 false）
│       └─ true 时冒泡 root → F6 面板 "aixcoding" tab + 上游 debug.log（root level=INFO，INFO+ 记录）
│
└── aixcoding.detail ─── detail 子树（"技术事件+全量"通道，propagate=False 永不冒泡）
    └── handler② _DetailMarker → ~/.chrys/aixcoding-logs/aixcoding.detail.log（1MB×3 独立轮转）
```

- 域 logger：`aixcoding.auth`（登录域）、`aixcoding.catalog`（目录域，批次① D 项首次启用其公共通道）；
- detail 域 logger：`aixcoding.detail.auth`、`aixcoding.detail.catalog`；
- 级别：主通道默认 INFO（`[logging] level` 可调 DEBUG/INFO/WARNING/ERROR/CRITICAL，重启生效）；detail 固定 DETAIL=5（低于 DEBUG，F6 面板 handler 的 DEBUG 门槛天然拦截）；
- 面板显示过滤：F6 面板内按 Space 循环 DEBUG→INFO→WARNING→ERROR（仅显示过滤，不改文件）。

## 二、两大分类口径（DEVELOPMENT.md §9:221）

| 分类 | 判定 | 去向 |
|---|---|---|
| **用户功能事件** | 人话信息，用户应知（登录状态流、降级警告、配置错误警告） | 主卷 ✅ + 面板（panel=true）✅ + **detail 卷同文案双写 ✅**（使 detail 自包含） |
| **技术事件** | 开发分析用（请求/响应/发布细节、异常栈、探测失败） | **只进 detail 卷**；主卷/面板永不出现 |

硬约束：用户功能事件不含 ehr 等 PII、不含后端名/异常链/完整 URL 等技术细节；detail 卷**允许且刻意保留**全量（ehr、含 userId 的完整 URL、异常栈）；秘密（token 等）任何卷不记（埋点红线）。

## 三、事件明细与路由对照

| 事件 | 主卷+面板（用户） | detail 卷（开发） | debug.log | 现状 → 批次① |
|---|---|---|---|---|
| 登录成功/已登录 | ✅ INFO「登录成功；env=…」 | ✅ 全量版含 ehr | panel=true 时冒泡 | 既有（A1-A10 双打） |
| 登录失败/取消/登出 | ✅ INFO/ERROR 精简版 | ✅ 全量版+异常类链 | 同上 | 既有 |
| 凭证被服务端拒绝并清除 | ✅ ERROR「已自动清除，请重新登录」 | ✅ 服务端消息+异常链 | 同上 | 既有 |
| **crypto 降级**（系统凭据库不可用） | ✅ WARNING「凭证将仅保存在内存中，重启后需重新登录」 | ✅ 同文案双写 + 探测失败/往返不一致技术原因 | 同上 | 现状**完全无日志** → A2 新增 |
| **停用环境变量误配** | ✅ WARNING「已停用且不生效；请改用 AIXCODING_MODEL_CATALOG_*」 | ✅ 同文案双写 | 同上 | 现状仅 detail（用户不可见）→ D 升公共 |
| catalog 请求/响应/发布/轮询 | ❌ | ✅ path 版 + userId 全量版（签认设计：全技术事件） | ❌ | 既有，不变 |
| **token 请求头读取失败** | ❌（每请求级防刷屏） | ✅「读取登录凭证失败」+异常栈 | ❌ | 现状英文+挂错子树**彻底无声** → A1 修复 |
| **登录成功回调失败** | ❌（登录已成功、对话框照常关闭，用户无感） | ✅ +异常栈 | ❌ | 现状英文 ERROR 泄入 debug.log → A3 修复 |

## 四、用户可见情况（按人群）

- **终端用户**（默认 panel=false）：`aixcoding.log` 文件与（配置 panel=true 后的）F6 "aixcoding" tab 呈现同一份用户功能事件流——全中文人话、无 PII、无技术噪音。注意 F6 其他 tab（chrys/httpx 等）为上游技术日志，面板整体**不是**用户安全面板。
- **开发人员排障**：首选 `aixcoding.detail.log`（单卷自包含：用户事件全量版+全部技术事件）；`debug.log` 有全部上游时间线，aixcoding 摘要现状仅 panel=true 时进入（批次②解耦）；跨卷时间戳对齐由批次③合并工具解决。
- **PII/外发边界**：ehr、完整 URL、异常栈只在 detail 卷——单独清理（停进程删文件即净）、诊断包导出默认排除。

## 五、治理路线（2026-11-05 责任人裁决：拆三批次）

| 批次 | 内容 | 状态 |
|---|---|---|
| ① logging-routing-fixes（本目录） | 现存路由修复：A1/A2/A3 子树外 logger 归位、C 死代码、D 停用 env 警告升公共；B（catalog 680/681）调查后撤销留档 | design 已签认（2026-11-05）；实施与机器验证完成，双 AI 自审进行中 |
| ② 机制类（未立项） | 主通道恒冒泡：用户功能事件摘要进 debug.log 与 panel 开关解耦 | 方向已裁决 |
| ③ 工具类（未立项） | 诊断合并命令：三卷+轮转文件按时间戳归并、来源标签、公共记录去重；默认产物不含 detail | 方向已裁决 |
| 观察项（暂不做） | 显式开发摘要通道（白名单外 logger 进 debug.log 不进面板）、独立用户日志视图、Linux 明文落盘一次性提示 | 留档，实证痛点再启动 |

## 修订记录

- 2026-11-05：初版（全景 + 批次①目标态对照；B 项撤销与两轮评审结论见 design.md）。
