# 验证：catalog-auth-gating

日期：2026-11-03。依据：已签认 design.md + test-plan.md（18 用例）。

## 一、实施清单（除生成物刷新外零上游改动）

| 文件 | 改动 |
|---|---|
| `src/chrys/kernel/aixcoding/auth/session.py` | 新增 `verify_live_credential()`（+21 行）：与 `check_silent()` 同走 `_active_delegated` 优先、同 `user/info` 验证，**零副作用**（不清 store、不置 delegated 标记、不写状态日志），裁决乙 |
| `src/chrys/service/aixcoding/catalog.py` | ① `_resolve_catalog_credential()` 单点解析 token+来源（D3），`_catalog_token()` 变薄包装；② `fetch_catalog` 预验证：store 来源先 `await verify_live_credential()`，None → WARNING（raise 前，B1 契约）+ `ModelCatalogError`；env 来源短路；③ 响应详版进 `aixcoding.detail.catalog`（version/default_profile_id/每模型四字段） |
| `aixcoding/mock/model_catalog/server.py` | `_serve_catalog` 前置 401：无 token/未知/过期拒绝（`grants()` 快照遍历，D4；`time.monotonic() >= expires_at` 判定），`--no-auth` 豁免 |
| `tests/architecture/trajectory_wait_manifest.json` | 生成物随同刷新（4 项 reopen：verify_live_credential:1、fetch_catalog:1-3）——架构测试生成链义务（上游文件、机械同步，实施后评审 F1 已补登记 design） |

> 关联前序改动（同日微改动豁免批次，已完成、未提交，实施后评审 F4 备注）：主卷错误日志
> `endpoint`→`log_endpoint` 脱敏 ×3 处、logger 改名 `aixcoding.catalog`、DEBUG 埋点、
> detail 请求详版——不属本批次范围，附注以免全量 diff 误读。
> 前序改动经责任人裁决（2026-11-03）与本批次**合并同一 PR 提交**，PR 描述分两节对应。
| 测试 ×3 | `test_verify_live_credential.py`（新，8 项）；`test_catalog_sync.py`（+7 用例；3 处 patch 点迁移 `_resolve`；双打用例 detail 计数改过滤式断言）；`conftest.py`（autouse 默认已验证）；`test_model_catalog_server.py`（+5 用例） |

## 二、自动化验证

- 三套测试 `aixcoding/tests/{catalog,auth,mock}`：**259 passed / 2 skipped**（新增 20 项：auth 8 + catalog 7 + mock 5；catalog 全套 51 项）。
- Smart Test（--paths 全部改动文件）：主测试 **10814 passed / 81 skipped**。
- architecture 全套（manifest 刷新后）：**1238 passed / 3 skipped**。
- `ruff check` / `ruff format --check`：通过（修复 PLW0108 ×1、末尾换行 ×1）。
- `gen_customizations.py --strict`：0 漂移；`check_docs.py`：OK。

## 三、实机重放（本机 mock + TUI，2026-11-03 23:06-23:09）

1. **mock 401**：无 token 直 GET → 401；未知 token → 401（curl 实证）。
2. **未登录启动**（local 凭证已清）：23:06:57 主卷 "No model catalog credential" + "sync not started"——**未发任何 catalog 请求**。
3. **登录 local 后**（23:09:25，device flow 真实走 mock）：
   - 预验证通过（`verify_live_credential` → mock `user/info` 有效）→ 请求携带有效 token（mock 401 校验放行）→ 发布成功；
   - 主卷：`sync started` + 请求摘要（**脱敏 endpoint，无 query/userId**）+ 响应摘要 + applied；
   - detail 卷：auth 登录详版（ehr）+ catalog 请求详版（完整 endpoint 含 `userId=1234567`）+ **响应详版**（version + `default_profile_id=openai-gpt-4o-mini` + 模型清单 id/name/model_id/base_url 四字段齐全）。

## 四、实施后评审（GLM-5.3，2026-11-03）处置

- **F1（blocker，R2）**：design 补登记生成物刷新例外（责任人已重签）；本文标题修正为
  "除生成物刷新外零上游改动"。
- **F2（责任人裁决 a）**：`token_owner` 补过期判定（`mock/auth/server.py`）——user/info 与
  catalog 的过期判定窗口期对齐；新增一致性用例（过期 token 双拒：业务 envelope + 401）。
  增量验证：三套 **260 passed / 2 skipped**（259 + F2 新用例 1）+ ruff 通过。
- **F3**：conftest 假 session 补 `stored_user_id="u"`（消除 `_catalog_user_id()` 降级
  WARNING 噪音，对齐真实链路）；catalog 51 项复跑全绿。
- **F4**：前序脱敏改动归属备注（见一节附注），非本批次范围。

## 五、验收对照（requirements.md）

全部勾选达成：无凭证不请求（既有保持）、失效/离线预验证拦截（用例 8/9/12 + auth 单测）、env token 短路（用例 10）、有效全链路（实机 3）、mock 401 三态 + 有效 + 豁免（用例 14-18 + 实机 1）、响应详版 detail 不冒泡（用例 13 + 实机 3）、零上游改动。

## 六、2026-11-04 增量实施与验证（裁决乙修订版 + 方案四/五）

### 实施清单（零上游源文件改动；manifest 未再触发——无新增 await 点）

| 文件 | 改动 |
|---|---|
| `src/chrys/kernel/aixcoding/auth/session.py` | ① verify 重写：rejected → 清账 + 主卷 WARNING（含服务端 message）+ detail + 回调；离线 → WARNING（异常链）+ detail、凭证保留；成功仅 detail；② `_clear_rejected_credential()` 共享清账点（check_silent 两分支迁入，F8）；③ `on_credential_rejected` 钩子 + `_notify_credential_rejected()` 异常吞（F7-3） |
| `src/chrys/app/aixcoding/tui/login_actions.py` | `_make_credential_rejected_callback()`：stop_sync_runtime + toast + 徽标刷新，`call_from_thread` hop + RuntimeError UI 线程 fallback；`silent_login_check` 覆盖式注册（F7-1/2）；`SilentCheckScreen` 协议 +app/notify |
| `aixcoding/mock/auth/server.py` | `token_owner` 摘除过期判定（方案五，推翻 F2 裁决 a，docstring 同步改写） |
| `aixcoding/mock/model_catalog/server.py` | catalog 401 摘除过期分支，与 user/info 同一真相源 |
| 测试 ×3 | `test_verify_live_credential.py` 重写 14 用例；`test_catalog_sync.py` +2 回调用例 + 端到端保底反转；`test_model_catalog_server.py` 改写 2（过期→存活 / 吊销→双拒） |

### 自动化验证

- `aixcoding/tests` 全套：**452 passed / 4 skipped**；
- Smart Test（--paths 7 改动文件）：tests **10586 passed / 64 skipped** + architecture **323 passed**
  （含 `test_wait_manifest_matches_source`，零漂移、无需刷新）；
- `ruff check` / `ruff format --check`：7 文件全过；`gen_customizations.py --strict`：0 漂移；
  `check_docs.py`：OK。

### 实机重放

已完成，结果见七节“实机重放”：mock 长会话轮询、人为吊销后的 detail 归因、徽标翻转、
面板 toast 与轮询终止均已验证；运行期失效主卷用户版的追加复验亦已闭环。

## 七、方案六实施与验证（2026-11-04，主卷用户化 + detail 全量）

### 实施

- `catalog.py`：**34 处**主卷 `logger` 调用全量迁移 `_detail_log`（info×10 / warning×15 /
  debug×4 / exception×5 显式 `exc_info=True` 保留堆栈）；孤儿主卷 logger 定义删除，注释更新；
- `session.py`：B1-B5 五条技术记录移 detail（check_silent 离线 ×2、verify rejected/offline、
  回调失败）；A 类 7 条新增双写（A1/A4/A5/A7/A8/A9/A10，失败/失效类 detail 附 `_reason` 链），
  A2/A3/A6 既有 ehr 双打保留；实机复审后 B3 再补主卷用户版：store/delegated 分别复用 A4/A5
  文案与 ERROR 级别，不含服务端 message/异常类名，detail 技术版保留；
- 测试：`test_verify_live_credential.py` 成功/离线归因用例反转为 detail + 主卷零技术记录；
  rejected 用例断言主卷恰好一条用户版 + detail 技术版，delegated 拒绝同步覆盖 A5 文案；
  `test_session_logging.py` test_03/07 反转为 detail-only；`test_catalog_sync.py` 9 用例反转
  （fault-injection/poll-parse/startup-degrades/retired-env×4/stale-generation/request-redact/
  unknown-fields/启动双条/轮询单条）。

### 验证

- `aixcoding/tests` 全套：**452 passed / 4 skipped**；
- Smart Test（--paths 9 文件）：tests **10814 passed / 81 skipped** + architecture **323 passed**；
- ruff check/format：通过（format 修复 replace_all 间距 ×2 文件）；`gen_customizations --strict`
  0 漂移；`check_docs.py` OK；
- 实施期发现：轮询 detail 卷同含 `sync started` 生命周期记录，断言由级别过滤改为消息过滤
  （级别过滤在 detail 卷不可用——全部 DETAIL 级）；
- B3 实机复审修正后增量复验：verify 定向 **13 passed**；`aixcoding/tests` **452 passed /
  4 skipped**；Smart Test（session.py + verify 测试）tests **1112 passed / 2 skipped** +
  architecture **323 passed**；ruff check/format、`gen_customizations --strict`、
  `check_docs.py` 均通过。

### 实机重放

- 启动后未登录等待 3 分钟：主卷仅"未登录"，无 catalog 轮询空转；执行 login 后主卷新增
  "登录成功"，detail 含预验证、请求、响应、applied 与常态轮询全链路；
- 重启 mock 吊销凭证后：detail 依次出现"用户不存在"、skip、`sync stopped`；徽标翻转为
  未登录并弹出 toast"登录已失效，请重新登录"（责任人确认）；主卷无技术事件；
- 上述重放同时暴露 B3 缺口：运行期失效已有 toast 却无主卷用户版。已按责任人反馈补 A4/A5
  同文案 ERROR 记录；
- B3 修正后实机复验（11:07-11:08，仅以进程 env `AIXCODING_MODEL_CATALOG_SYNC_INTERVAL=10`
  缩短轮询，零代码/配置文件改动）：11:08:24 mock 重启间隙先判离线，凭证保留且主卷零记录；
  11:08:37 预验证明确拒绝，主卷新增恰好一条 ERROR"本机保存的登录已失效，已自动清除，
  请重新登录"；detail 同刻保留服务端"用户不存在"、`AuthServerError`、skip 与 `sync stopped`；
  主卷无预验证、服务端 message 或异常类名，B3 实机缺口闭环。

## 八、方案七：catalog 文案中文化（2026-11-04）

### 实施

- `catalog.py` 既有 34 处迁移日志（加 2 处既有详版）及同文件全部 `ModelCatalogError`
  文案整体中文化；保留 HTTP/GET/JSON、环境变量名、字段名、模型 ID 与外部原始异常；
- 为保留全角中文标点，`pyproject.toml` 仅新增 `src/chrys/service/aixcoding/**` 的
  RUF001 目录级豁免；注释含理由与批准指向，C132 点级登记对账通过，无文件级 noqa；
- `session.py::verify_live_credential` docstring 的 B3 旧口径已修正（独立评审 P2）；
- `test_catalog_sync.py` 英文断言改中文，并新增无凭证外层吞错 + 内层错误的组合中文断言。

### 验证

- catalog 定向：**51 passed**；`aixcoding/tests`：**453 passed / 4 skipped**；
- `verify.py --full`（pyproject 变更触发）：上游 **33262 passed / 623 skipped**；
  architecture **1237 passed / 3 skipped / 1 deselected**，Summary PASSED；
- ruff check/format、`gen_customizations --strict`（95 登记行 / 132 marker / 0 warning）、
  `check_docs.py` 均通过；范围内英文叙述模板与中文旁 ASCII 标点残留检索均为零；
- 实机 12:34:02 无凭证启动：detail 为"同步模型目录失败，保留旧模型列表：没有模型目录
  凭证：请登录，或设置 AIXCODING_MODEL_CATALOG_TOKEN。本次同步已跳过，保留旧模型列表。"；
  下一条"模型目录同步未启动：暂无凭证，登录后将启动。"；主卷两条旧英文组合文案均零命中。

## 九、方案八：登录失败 UI 与日志分离（2026-11-04）

### 实施

- 获取登录码与授权轮询失败的 UI 均为固定中文指引，不再拼接异常；
- 主卷分别记录"登录失败：无法获取登录码，请稍后重试。"与"登录失败：登录未完成，
  请稍后重试；env=..."；complete_login 主卷原有 `_reason` 技术链已移除；
- detail 仅记录异常类型/message 摘要，不传 `exc_info`，无代码异常栈；session 既有原因摘要保留；
- 实机发现主卷 sink 仅挂 `aixcoding` 根 logger，UI 的 `chrys.app...` logger 会漏记；获取登录码
  主卷记录改用 `aixcoding.auth`，消除 caplog 通过而实机无记录的盲区。

### 验证

- 登录 UI/session 定向：**16 passed**；定制全套：**454 passed / 4 skipped**；
- 实机通过后 Smart Test：**4284 passed / 10 skipped**；architecture：**324 passed**；
- 实机 14:33:13：主卷新增恰好一条 `aixcoding.auth` ERROR"登录失败：无法获取登录码，
  请稍后重试。"；detail 同刻仅一行"获取登录码失败：AuthNetworkError: request to ..."，
  包含 URL 与连接失败摘要，未新增 Traceback；界面固定中文由责任人确认。

## 十、独立评审 N1 收尾（2026-11-04）

- 按责任人裁决合并 verify 离线 B4 与"离线详情"重复行；detail 恰好一条，仍含
  `AuthNetworkError` / `_reason`，主卷零离线记录；N2/N3 不修理由见 self-review 十一节；
- 定向：**13 passed**；定制全套：**454 passed / 4 skipped**；ruff/format、
  `gen_customizations --strict`、`check_docs.py` 均通过；
- **Smart Test 按责任人明确指示跳过**（仅本次 N1 收尾），不得据此声称 Smart Test 通过；
  方案八此前 4284 passed 是前序证据，不冒充本次 N1 验证。

## 十一、提交前审计（2026-11-04，忽略 Smart Test）

- 文档残留清理：catalog collect 由历史 53 回填为实测 **54 项**；清除历史“待补”，更新
  方案七/八实施状态、最终增量分界、test-plan 版本与 C132 验证口径；无剩余功能增量；
- 定制全套复跑：**454 passed / 4 skipped**；11 个改动 Python 文件 ruff check/format 全过，
  IDE error/warning 为 0；
- `gen_customizations --strict`：**95 登记行 / 132 marker / 0 warning**；`check_docs.py` OK；
  `git diff --check` 通过；工作区为预期的 18 个本批文件；
- 本次审计按责任人要求未运行 Smart Test；代码、文档事实记录与所要求测试均无未完成项，
  剩余仅 design 正文事实更新及本自审追加后的责任人签认。
