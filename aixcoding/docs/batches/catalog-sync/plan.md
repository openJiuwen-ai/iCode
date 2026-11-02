# 任务清单：模型目录同步 + 档案集中管理（catalog-sync）

> 活文档（AI 维护，不签认）。续接入口：当前状态、下一步、挂起事项。AI 每次接手先读本文（DEVELOPMENT.md §4 第 1 步）。

## 状态

管线推进中（2026-11-02）：design.md 已签认（含五轮外部 AI 评审收敛：评审一/二/三 + Q2/Q3 专题 + 连带面核查两轮）；test-plan.md 已起草（平移 10 + 新增 12 + UI 门 3 + 跟改 4 处）；本文 plan 起草后进入**实施阶段**。实施全程逐文件对照 R2 表 1-14 号与登记预告 C030-C045（marker 锚文本逐字落，`--strict` 对账为门禁）。

## 待办

- [x] requirements.md（活文档，四轮修订）
- [x] design.md 签认（责任人，2026-11-02；修订一~六轮含 F-6 裁决）
- [x] test-plan.md（覆盖矩阵对照 requirements 验收 10 条；故障注入细节收敛）
- [x] plan.md（本文档）
- [ ] **实施 A——定制包主体（零上游接触）**：`src/chrys/service/aixcoding/`（`__init__.py` 建档 + `catalog.py` 自旧 847 行平移改写：①auth import 改名×2 lazy；②`ENVIRONMENT_ENV` 值改 `AIXCODING_AUTH_ENVIRONMENT`；③A 组三 env 更名 + 四退役名警告检测（只记名不记值）；④`apply_catalog` 两阶段发布（tmp 命名/孤儿清理/全量重写按 test-plan 细节节）；⑤全异常统一转换；⑥轮询单例 + 代次防护 ~60 行）
- [ ] **实施 B——registry（R2 1 号，C030/C031）**：`import threading` + RLock + 六方法加锁 + `install_snapshot`（短临界区一次替换，锁内零 IO）
- [ ] **实施 C——settings 声明（R2 2/3/9 号，C032/C033/C039）**：`settings.py` label 借壳 + 字段声明（RESTART/DENY/group=model/TEXT）；`process_settings.py` 投影 +2；`layout.py` `DEFERRED_KEYS` 排除（与 2 号同批，防面板出现可编辑行）
- [ ] **实施 D——TUI 接线（R2 4 号，C034；login_actions 零上游）**：`app.py` main() 启动同步（bootstrap 后 load_all 前，lazy+统一降级）+ `start_sync_runtime` + finally `stop_sync_runtime()`（入既有 finally 块 +1 行）；`login_actions.py` 两处占位符替换实调（登录即时同步/登出停止——D3 兑现）
- [ ] **实施 E——UI 禁用面（R2 5/6/8/10/11/7 号，C035-C041）**：`config_actions.py` read_only 恒 True；`screen.py` F4 binding 删除（:238）；`picker.py` manage 渲染段删除（~13 行 + `content_width` + 孤儿 import `wrap_description`）；`models/screen.py` 文案改集中管理措辞；`model_indicator.py` `_live_label` + `compute_model_indicator_state(..., model_registry=)` 尾部默认参数 + `screen.py` `_refresh_model_indicator`（:748-753）调用方传参
- [ ] **实施 F——i18n 流水线（R2 12/13 号，C042/C043）**：`scripts/i18n.py` extract → update → translate → compile（新 key 四处同步）；oracle ids +1 / size +1；read_only_notice po msgstr 改写（翻译优先于 fallback——漏改则界面旧文案）
- [ ] **实施 G——测试落地**：`aixcoding/tests/catalog/`（conftest：wait_until 复制 + 单例隔离 autouse fixture；test_catalog_sync.py 平移 10 + 新增 12；test_read_only_gate.py 3）；上游 `test_model_picker.py` 跟改 4 处（R2 14 号，C044/C045）
- [ ] **机器验证**：①定制套件 `pytest aixcoding/tests -n0` 全绿；②上游全套（三分类口径：仅 14 号①—④行为差异允许变化）；③`gen_customizations.py --strict` 0 error/0 warning（C030-C045 对账）；④ruff check/format + ty 三平台；⑤`i18n.py check`；⑥Smart Test 产品+测试双传
- [ ] **离线打包冒烟（DoD 项）**：build_lf.ps1 -Offline（三下载物缓存）→ 产物 TUI mock 登录 + 启动同步 + picker 只 profile + `/models` 恒只读 + F4 不在 footer
- [ ] verification.md（机器验证 + 冒烟证据 + 实施期注记）
- [ ] self-review.md（双 AI 自审，SELF-REVIEW-HOWTO 流程）
- [ ] PR：特性分支 feat/catalog-sync（已建）→ add（清单含新路径目录，status 无 RM 残留）→ commit（message 规范五要素预审）→ push → 责任人网页建 PR（git 写操作逐条授权，逐条停等）

## 挂起与备忘

- **裁决已定**（design 签认承载）：UI 形态 A（照 0.28 final）；ACP 边界②（仅承诺 TUI，限制登记）；env 三变量更名 + 无兼容期 + 废弃检测（`CHRYS_MODEL_CATALOG_BASE_URL` 不改）；Q2 删除+marker（刀口原则）；Q3 finally stop 保留（既有 finally 块 +1 行、不 join）；F-6 models.md 文档并入 ROADMAP §4 C 类文档定制化工作项（后续文档批次）。
- **R2 合计**：源/配置（1-11 号）+78/-18（净 +60）；i18n（12-13 号）+10/-1；测试跟改（14 号）+12/-10。行数显著超估须停下重报（§4.2），不自行吸收。
- **实施顺序依赖**：B（install_snapshot）先于 A 的 on_applied 回调联调；C 的 settings 声明先于 A 的 `catalog_base_url()` 投影读取；E 各面相互独立可并行；F 的 i18n 在 E 文案/msg 常量落地后跑；G 的定制测试可随各实施段同步写（测试文件最终一次成型）。
- **假设 2 实测复核**：`.yaml.tmp` 避开 loader glob——test-plan 用例 11 已升格为测试锚定（比 design 的"实施时人工复核"更强）；失效则暂存目录外置（design 回退路径）。
- **风险在案**（design 风险节）：RESTART 字段↔快照一致性硬编码测试若存在属预期跟改（届时 R2 追加行报批）；i18n 翻译内容断言测试若存在一并跟改；test_gc_freeze_calibration 满载偶发 flaky 与 install_scripts deselect 属环境噪音（记忆配方）。
- **本地测试配方**：大写盘符 + `set "PYTHONPATH=" && set "PYTHONUTF8=1"`；Python 3.14.8 已就位无需 UV_PYTHON。
- **B 段锁形态专题评审（2026-11-02，外部 AI，结论=保留 A）**：三形态对比（A 六方法+RLock / B 仅 install_snapshot 无锁 / C 只锁写）——B 为 copy-on-write 快照发布，在"发布后不原地修改"的调用纪律下安全（当前运行图成立），但该不变量属全局时序而非类内机制；A 把安全证明缩小为"类内经同一把锁访问"，9 行实质成本换自包含保证；C 无完整读写互斥不推荐。口径修正留档：锁消除的是**字典结构竞态**，不消除丢更新/旧版本后发布/profile 字段竞态（语义层靠代次防护 + 目录唯一真相源下轮收敛）。
