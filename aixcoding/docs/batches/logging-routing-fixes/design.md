# 设计：日志治理①——现存埋点路由修复

<!--
签认规则（R10）：责任人本人编辑本头部（状态→已确认、填日期，确认意见可选），AI 不得代填。
签认后修改正文需重新签认。校验：uv run python aixcoding/tools/check_docs.py
-->

- 责任人：周彬
- 状态：已确认
- 确认日期：2026-11-05
- 确认意见：无特别意见；重签追认 PR #95 C1 修订节（2026-11-05）

## 方案概述

五处修复（B 项已撤销，见下），全部位于定制层文件、零上游改动、零新依赖：三处子树外 logger（`getLogger(__name__)`）归位到 ADR-0017 双卷体系的正确通道并中文化，crypto 降级补上缺失的公共 WARNING，catalog 停用环境变量警告按"用户可操作的配置错误"例外升为公共通道双打。不引入任何新机制——propagate/panel 解耦（批次②）、合并工具（批次③）均不在本批。

改动文件与行数（2026-11-05 基线，行号随实施可能微移）：

| 文件 | 改动 | 行数 |
|---|---|---|
| `src/chrys/kernel/aixcoding/auth/__init__.py` | A1：`_log`（`__name__`）→ `_detail_log`（`aixcoding.detail.auth`）+ 本地 `_DETAIL_LEVEL`；消息中文化，保留 `exc_info` | ~6 行 |
| `src/chrys/kernel/aixcoding/auth/crypto.py` | A2：`_LOGGER`（`__name__`）→ `_log`（`aixcoding.auth`）+ `_detail_log`；factory 异常与往返不一致各记 detail；`return MemoryBackend()` 前新增公共 WARNING **+ 同文案 detail 双写** | ~12 行 |
| `src/chrys/app/aixcoding/tui/login.py` | A3：删 `logger`（`__name__`）定义；134 行 `logger.exception` → `_detail_log` 中文 + `exc_info`（复用同文件既有 `_detail_log`/`_DETAIL_LEVEL`） | ~3 行 |
| `src/chrys/service/aixcoding/catalog.py` | D：新增 `_log`（`aixcoding.catalog`）；`_warn_retired_env` 双打（公共 WARNING + detail 原文）；修订 :72 定调注释文本 | ~6 行 |
| `src/chrys/app/aixcoding/tui/login_actions.py` | C：删未使用的 `import logging` + `logger` 定义 | −2 行 |

### 调查结论修正（R8 留档，防重提）

requirements 草案曾列 B 项（删 `catalog.py:681`"重复行破坏 path-only 口径"）。design 起草阶段核实 `aixcoding/tests/catalog/test_catalog_sync.py:973-1020`：**"完整 endpoint（含 userId）留在 detail 卷"是 catalog-auth-gating 批次的签认行为**（`assert "userId=1234567" in detail_record.getMessage(), "the full endpoint stays on detail"`），ADR-0017 允许 detail 卷含 ehr 全量。676 行注释（"logs and error text keep path only"）的约束对象是**面向用户的错误文案**（`ModelCatalogError` 消息与其后的错误记录均用 `log_endpoint`），不含 detail 技术记录。原判系误读。B 项撤销；680（path 版）与 681（全量版）并存属可读性取舍，非缺陷。

### 逐条定案

**A1 `auth/__init__.py`（login_token_header_value）**：每请求级调用（匿名期每次 LLM 请求触发），任何公共级别都会刷屏——只进 detail 卷。改动：

```python
# 替换 :45 的 _log = logging.getLogger(__name__)
_detail_log = logging.getLogger("aixcoding.detail.auth")  # 直写字面名，session.py 零依赖先例
_DETAIL_LEVEL = 5  # 本地常量，session.py/catalog.py 同款
# :65 改为（文案语义修正：stored_token 返回 None 的正常未登录不进此分支，
# 真正到达这里的是会话构造/凭证读取失败——peer review M5，原英文亦有此歧义）
_detail_log.log(_DETAIL_LEVEL, "读取登录凭证失败，token 请求头未附加。", exc_info=True)
```

`exc_info` 决策：沿用 catalog 意外异常先例（`catalog.py:574` 等 `exc_info=True`），非 session.py 12 条状态转换的"no traceback"红线——该红线针对预期路径的状态记录；A1 是意外异常兜底，栈是排障刚需，且 `get_login_session()` 失败链（存储读取）不携带 token 值。

**A2 `crypto.py`（get_backend）**：三个打点（peer review M1/M2 修订版——原草案漏"往返不一致"非异常路径的 detail 记录，且公共 WARNING 未双写，违反 DEVELOPMENT.md §9 日志分层口径"用户功能事件双写两卷，使 detail 卷自包含"）：

- factory **抛异常**（:256-257）→ detail（技术原因）：`_detail_log.log(_DETAIL_LEVEL, "加密后端 %s 探测失败：%s", getattr(factory, "__name__", factory), exc)`
- factory **往返校验不一致**（`if ... == probe` 不成立、不抛异常的静默跳过路径，`test_auth_crypto.py:113-124` Garbage 即此形态）→ detail：`_detail_log.log(_DETAIL_LEVEL, "加密后端 %s 往返校验不一致。", getattr(factory, "__name__", factory))`
- **`return MemoryBackend()`（:259）前公共 WARNING + 同文案 detail 双写**——当前完全无日志的用户可感知降级点（凭证重启即失）：
  - `_log.warning("系统凭据库不可用，登录凭证将仅保存在内存中，重启后需重新登录。")`
  - `_detail_log.log(_DETAIL_LEVEL, "系统凭据库不可用，登录凭证将仅保存在内存中，重启后需重新登录。")`

低频性：`get_backend` 经 login session 进程级 singleton 实际每进程一次。detail 卷对降级事件自包含：异常路径、不一致路径与最终降级均有记录。

Linux `PlaintextBackend`（显式明文，probe 通过）**不**打 WARNING——与 reference `safeStorage` 行为一致的平台设计，非意外降级；是否为明文落盘给一次性提示，作为开放问题留档（非本批）。

**A3 `login.py`（登录成功回调失败）**：detail-only 定案——登录已成功、`dismiss_when_topmost` 照常执行，回调失败（消费方 UI 刷新类）用户无感或低感；若回调失败造成用户可见故障，那是回调实现方的缺陷，不应由公共日志通道承担。:134 改 `_detail_log.log(_DETAIL_LEVEL, "登录成功回调失败，继续关闭登录对话框。", exc_info=True)`，删除 :49 的 `logger` 定义。

**C `login_actions.py`**：`import logging`（:20）与 `logger`（:32）全文件无其他使用，两行同删。

**D `catalog.py`（停用环境变量警告）**：

```python
_log = logging.getLogger("aixcoding.catalog")  # catalog 域首个公共通道记录
# _warn_retired_env 内（:230-236）改为双打：
_log.warning("环境变量 %s 已停用且不生效；请改用 AIXCODING_MODEL_CATALOG_* 系列变量。", name)
_detail_log.log(_DETAIL_LEVEL, "环境变量 %s 已停用且不生效；请改用 AIXCODING_MODEL_CATALOG_* 系列变量。", name)
```

`_warned_retired_env` 集合保证每名字仅一次，无刷屏风险。**同步修订两处既有签认口径**（修订依据：2026-11-05 日志治理讨论 + 外部 AI 评审 + 责任人裁决"用户可操作的配置错误"例外）：

1. `catalog.py:72-75` 定调注释：`every catalog record is a technical event and lives only on detail` → 补充例外（user-actionable configuration errors additionally carry a public WARNING）；
2. `test_catalog_sync.py:699` 断言 `"the retired-name warning stays off the main volume"` → 反转为进主卷 WARNING 断言。

## 备选方案对比

| 方案 | 优点 | 缺点 | 升级冲突面 | 结论 |
|---|---|---|---|---|
| A1 公共 WARNING（非 detail） | 用户可见 | 每请求刷屏（匿名期高频） | 零 | 拒绝 |
| A1 不修（维持无声） | 零改动 | 排查"请求未带 token"永久盲区 | 零 | 拒绝 |
| A2 仅 detail（无公共 WARNING） | 改动最小 | 用户不知道"重启要重登"，与外部评审"低频可操作公共 WARNING"结论相悖 | 零 | 拒绝 |
| A2 公共 WARNING 不双写 detail | 少一条 | 违反 DEVELOPMENT.md §9 分层口径"用户功能事件双写两卷"（peer review M2）；detail 卷不自包含 | 零 | 拒绝 |
| A2 在 factory 失败处打公共（非 MemoryBackend 返回点） | 位置就近 | 语义错位：factory 失败≠降级（可能还有下一个候选）；公共文案含后端名违反公共卷精简原则 | 零 | 拒绝 |
| A3 公共+detail 双打 | 用户可见 | 对话框照常关闭，用户无感；公共版徒增噪音 | 零 | 拒绝 |
| D 仅公共（删 detail 版） | 少一条 | 违反"用户可见信息在 detail 双写"的既定方案 | 零 | 拒绝 |
| D 维持 detail-only | 零改动、不动既有断言 | 用户配置错误用户看不见（本批立项动机） | 零 | 拒绝 |
| B 删 681 | 减一行 | 推翻 catalog-auth-gating 签认设计与钉死测试（见调查结论修正） | 零 | **撤销** |
| 通道归属：域 logger（`aixcoding.auth`/`aixcoding.catalog`） vs 顶 logger（`aixcoding`） | 前者 name 可辨识归属 | 后者来源不可分 | 零 | 采用域 logger |

## 上游改动清单（R2）

**无上游改动**（五个文件均为定制层文件；R2 清单为空）。R1 自查：无接线点、无上游行为变更，业务逻辑零外溢问题不适用。

## 对上游隐式行为的假设

无新增假设。本批不改变任何 root/上游 handler 相关行为；detail 子树依赖 `setup_logging` 预置（既有机制，ADR-0017 六不变量覆盖，非本批引入）。

## R13 豁免申请（peer review M3）

A1/A2 的中文日志文案沿用既有全角标点风格（session.py 冻结文案先例），而 `pyproject.toml` per-file-ignores 的 RUF001 豁免范围不含 `src/chrys/kernel/aixcoding/auth/`（现有：`app/aixcoding/tui/*`、`service/aixcoding/**`、`aixcoding/tests/**`）。申请：

- **形态**：文件级 noqa——`auth/__init__.py` 与 `crypto.py` 头部各加两行（session.py `:1-2` 同款格式与理由文案，批准指向本批次 design）；不动 `pyproject.toml`（避免触碰上游文件，R2 清单保持为空）
- **理由**：中文日志消息的全角标点是本仓定制层既定设计（auth-logging 批次 2026-10-29 签认），与规范结构性冲突（RUF001 无法区分 TUI 文案与日志文案的中文标点意图）；文件级 noqa 是 session.py 已确立的先例形态
- **已尝试的正当路径**：改用半角标点（被拒：与 session.py/catalog.py 冻结文案风格不一致，日志阅读体验割裂）；pyproject per-file-ignores（被拒：改上游文件引入 R2 项，收益为零）
- A3（`login.py`，`app/aixcoding/tui/*` 已豁免）与 D（`catalog.py`，`service/aixcoding/**` 已豁免）无需新增

## 定制点登记预告

不适用新增登记：本批改动全部位于定制层文件，无上游文件 marker 变更。注意：D 修订 `catalog.py:72` 的 `# AIxCoding` 注释文本——按登记义务（ADR-0015），实施后提交前重跑 `uv run python aixcoding/tools/gen_customizations.py --strict` 确认 0 error / 0 warning（对账幂等）；若对账指出该注释为登记锚且需更新登记行，属 R10 重签范围，届时呈报。

## 测试计划锚点（test-plan 详化）

复用 `test_session_logging.py` 的 `_aix_logger_isolation`（`aixcoding` 子树钉扎）与 `detail_capture`（detail 子树捕获）fixture 模式，新增路由测试（落点 `aixcoding/tests/auth/` 与 `aixcoding/tests/catalog/`）：

- A1：构造 `get_login_session` 失败（monkeypatch 抛异常）→ detail 捕获含中文消息"读取登录凭证失败"（哨兵：不含"无登录会话"误导表述）；caplog（主卷代理）零记录；`chrys.kernel.aixcoding.auth` logger 零分发（哨兵：该名字无 handler 收到记录）；
- A2（peer review2 M1 修订——打点位置与测试锚点口径统一：公共 WARNING + detail 双写位于最终 `return MemoryBackend()`（:259）前，仅在**候选全部耗尽**时触发；`_platform_factories`（:262-268）真实实现每平台单候选，故真实降级路径必然到达该点；`:255` 健康候选提前返回路径**不告警**——"factory 失败≠降级"的既定决策）：
  - ①factory 抛异常且候选耗尽：`[Exploding]` 单候选形态（复用 Exploding 类，**工厂列表由既有测试的 `[Exploding, MemoryBackend]` 收窄**）→ detail 含后端名与异常 + 公共 WARNING + detail 同文案双写，三断言一并覆盖；
  - ②factory 往返不一致且候选耗尽：`[Garbage]`（`test_auth_crypto.py:113-124` 同款形态）→ detail"往返校验不一致" + 公共 WARNING + detail 双写——非异常路径独立断言（M1）；
  - ③混合形态 `[Exploding, MemoryBackend]`（健康候选经 `:255` 提前返回）→ detail 含 Exploding 记录但**两卷零 WARNING**——把评审指出的既有形态转化为"factory 失败≠降级不告警"决策的锚定断言；既有 `test_exploding_backend_degrades_to_memory`（走 `:255` 路径，断言返回类型）不受本批影响、不改动，日志面断言由本批新测试承担；
  - ④probe 成功（MemoryBackend/PlaintextBackend 候选）→ 两卷零记录；
  - 主卷 WARNING 断言**哨兵：不含后端名/异常文本**；
- A2 补充：`crypto.py` 文件级 `noqa: RUF001` 落位后 `uv run ruff check` 该文件零告警；
- A3：登录成功回调抛异常 → detail 捕获中文消息 + exc_info；主卷零记录；
- C：静态断言（ruff F401 兜底）；
- D：既有 `test_retired_env_names_warn_and_do_not_apply` 修订——:699 反转为"进主卷 WARNING"；新增 detail 双写断言（保留 :697/:698 值不泄漏断言不变）；
- 行为锚定：全部经真实 logger 分发断言，不 mock logging 本身。

## 验证

1. `uv run python aixcoding/tools/verify.py --smart --paths <五个改动文件 + 测试文件>`；
2. `uv run pytest aixcoding/tests -n0` 全绿；
3. `uv run pytest tests/architecture -n0` 全绿（字面名直写不引入跨子包 import）；
4. `uv run python aixcoding/tools/gen_customizations.py --strict` 0 error / 0 warning。

## 风险与最坏情况

- **未来多候选工厂列表的告警缝隙（peer review2 M1 留档）**：当前 `_platform_factories` 每平台单候选，最终降级必然走 `:259` 双写点；若未来工厂列表加入多候选（尤其显式 `MemoryBackend` 兜底候选），健康候选经 `:255` 提前返回路径不告警——届时"选中内存后端但用户未被告知重启将丢凭证"成为可能，需重评告警覆盖（候选链耗尽 or 返回实例 degraded 二判据），由当时的批次 design 处理；
- 文案冻结后断言同步负担：中文消息进入测试断言（沿 session.py 先例，全角标点 noqa 已有模式）；
- D 修订既有断言若与 catalog-sync 批次 CI 记忆冲突：本批 design 签认即修订依据，verification 留痕；catalog-sync design 是否加联动注记为责任人签认时裁决项；
- 最坏情况：某条路由决策不合适（如 A2 WARNING 文案误导）——纯日志行改动，git revert 单文件即可回滚，无状态、无迁移。

## ADR 判定

四问逐项：①有被否决备选？——各修复点备选见对比表，均为批次内实现选择，不构成架构级"被认真考虑并否决"的决策纪录（B 撤销属调查纠错，R8 留档于本文即可）；②撤销代价高？——低（纯日志行，git revert 即回滚）；③约束未来工作模式？——否，是执行既有 ADR-0017/埋点规范，不新增规则；④依赖上游非公开接口？——否。**结论：不出 ADR。**

## 签认后修订（PR #95 review C1，2026-11-05）

**修订**：A1 定案增补**进程级频控**——`auth/__init__.py` 模块级 `_token_header_failures_logged: set[type[BaseException]]` 按异常类型去重，每类型仅首条带栈落 detail 卷，同类型后续静默；新异常类型视为新故障再记一条。

**依据**：PR #95 peer review C1——原稿 A1 定案（"逐条定案"A1 节）仅论证"公共级别每请求刷屏 → 只进 detail 卷"，未评估 detail 卷自身容量：持续型会话构造故障下"每 LLM 请求一条带栈记录"（约 1KB × 每秒 1-2 请求）可在 1 小时内碾过 1MB*4 全部轮转空间，侵蚀 ADR-0017"detail 单卷自包含"立论。

**取舍口径**（增量评审确认）：防刷屏优先于复发诊断——同类型后续静默意味着"同类型新故障点"与"恢复后同类故障再现"不再补记（栈可能不同），以容量保护换取；实现注释与 requirements 用户场景均按此表述（不再声称"无诊断损失"）。

**批准**：责任人 2026-11-05 批准扩范围（聊天批准，本节落盘追认，R10 重签生效）。实现与验证证据：`verification.md` PR #95 处置增量节、`self-review.md` PR #95 记录、`test_routing_fixes.py::test_a1_persistent_failure_logs_once_per_exception_type`。

**其余定案不受影响**：A2/A3/C/D 及测试锚点与实施一致，无偏差。
