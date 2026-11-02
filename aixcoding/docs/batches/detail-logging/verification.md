# 验证：detail 仅文件日志子树（独立明细卷）

## 1. 机器验证（2026-11-02）

| # | 命令 | 结果 |
|---|---|---|
| 1 | `set "PYTHONPATH=" && set "PYTHONUTF8=1" && uv run python -m pytest aixcoding/tests -n0 -q`（大写盘符仓库根） | **354 passed, 2 skipped**（45s）——新增 `test_detail_channel.py` 11 用例（隔离/不串卷/panel 双态/幂等/部分失败四态/降级两形态/levelname/常量防漂移）+ `test_session_logging.py` 3 条 ehr 断言迁移全过 |
| 2 | `uv run python -m pytest tests/architecture -n0 -q` | **1238 passed, 3 skipped**（135s）——import 边合规（详见 §3 注记 2） |
| 3 | `uv run python aixcoding/tools/gen_customizations.py` + `--strict` | **check OK**——25 登记行 / 29 marker / 0 warning；本批次零上游 marker 变更，对账幂等（连跑两次一致） |
| 4 | `uv run ruff check … && uv run ruff format --check …`（本批次源码与测试文件） | 通过（check 自动修复 1 处 import 排序；format 重排 1 处签名后全过） |

## 2. 责任人实测目验（2026-11-02 10:15-10:19）

环境：`uv run python -m aixcoding.mock`（model_catalog :7777 **含内嵌 auth auto** + update :7778）+ TUI `AIXCODING_AUTH_ENVIRONMENT=local`（`start` 独立窗口）；本机 `aixcoding.toml` 为 `panel=true`。责任人执行启动→登录→登出，面板目验 + 双卷文件检查（AI 代查证据如下）。

| 验收点 | 结果 | 证据 |
|---|---|---|
| F6 面板无 ehr | ✅ 责任人目验 | 登录/登出后面板记录均无 `ehr` 字样 |
| 主卷新版记录无 ehr | ✅ | `aixcoding.log` 今日三条：`10:15:46 未登录（首次使用或登录已过期）；env=local`（INFO）、`10:17:58 登录成功；env=local`（INFO，**无 ehr**）、`10:19:29 已登出；env=local`（INFO） |
| 独立卷含 ehr + DETAIL 级 | ✅ | `aixcoding.detail.log`（**本批次首次创建**）单行：`2026-11-02 10:17:58,016 DETAIL aixcoding.detail.auth: 登录成功；ehr=1234567 env=local`——levelname `DETAIL`、logger 名 `aixcoding.detail.auth`、含 ehr |
| 双打同刻对齐 | ✅ | 面板版与 detail 版时间戳**同毫秒**（10:17:58,016） |
| 上游 debug.log 无今日 ehr 扩散 | ✅ | `findstr "ehr=" debug.log | findstr "2026-11-02"` → 零匹配 |
| 历史记录不回改 | ✅ | 主卷 10-29～11-01 旧行（含 ehr 的旧版消息）原样保留 |

## 3. 注记（实施期事件与决策）

1. **pytest.exe 入口损坏（环境，非代码）**：`fork_2\.venv\Scripts\pytest.exe` 内嵌启动路径指向旧仓库 `iCode-fork\.venv` 的 Python（其 console-script 损坏，疑似 venv 手工迁移遗留）——`pytest.exe` 会用旧仓库 site-packages 与 `src` 解析 import（sys.path 实测全为旧仓库路径）。`.venv\Scripts\python.exe` 正常，故全验证用 `uv run python -m pytest` 绕行（语义等效）。建议将来 `uv pip install --force-reinstall pytest` 修复入口。另：`tests/architecture` traceback 显示旧仓库文件路径系拷贝 `.pyc` 的 `co_filename` 残留，`ROOT = REPO_ROOT`（自 `__file__` 推导）实证被扫描树为 fork_2，无实质影响。
2. **DETAIL 常量共享位置裁决（design 开放问题，按预授权 fallback 落地）**：包根方案（`from chrys.kernel.aixcoding import DETAIL_LEVEL`）实测 `test_layering` 红——`session.py:41: kernel (kernel) must not import 'chrys.kernel.aixcoding'` + `logging_support.py` 同族 2 处。按 design 签认文本预案（"红了则 fallback 各调用文件本地常量"）执行：`logging_support.DETAIL_LEVEL`（权威）+ `session._DETAIL_LEVEL`（本地镜像）+ `test_00_detail_level_constants_agree` 防漂移锚；包根 `__init__.py` 回退纯 docstring（净零改动）。
3. **实施期测试迭代（首跑 4 失败 → 修复收敛）**：①auth 用例 levelname 断言失败（`'Level 5' == 'DETAIL'`）——`detail_capture` fixture 补 `addLevelName`；②隔离用例主卷断言 FileNotFoundError——`_Marker` `delay=True` 下无主通道记录时主卷不落盘，断言改容不存在态；③#1/#5 detail 断言计数 2≠1——准备步骤（`login_once`）的 detail 记录被捕获（fixture 预置 level 使其越过门槛，与 caplog `at_level` 只护断言窗的机制不对称），断言前 `detail_capture.clear()`（与既有 `caplog.clear()` 对称）。三处均为测试侧口径修正，源码零返工。

## 4. 双 AI 评审与修复（2026-11-02，Kimi-K3.1-Pro / CodeBuddy）

一轮收敛：评审 0 blocker / 2 suggestion / 3 nit → 责任人拍板全修 → 修复 → 同 AI 复审 5/5 达标、零新问题。处置（全部测试侧，零源码改动）：S1 `detail_capture` fixture 补 `propagate=False` 生产预置镜像；S2 `aix_logger_state` teardown 对测试新增 handler 先 `close()`（防 Windows 临时文件占用）；N1 三处 detail 断言补 `exc_info is None` 红线；N2 `test_09` 显式重置未预置前提；N3 `test_00` 补 `DETAIL_LEVEL == 5` 字面值锚定。修复后回归：`uv run python -m pytest aixcoding/tests -n0 -q` → **354 passed, 2 skipped**（52.5s）+ ruff check/format 通过。详见 self-review.md。

## 5. CI 门禁本地全量预跑（2026-11-02，提交前自查，PR #70 教训执行）

按 `.github/workflows/aixcoding-tests.yml`（ADR-0004 三层 test/govern/gate）逐条预跑：

| CI 命令 | 本地结果 |
|---|---|
| `uv run ruff check aixcoding/` | **All checks passed!**（全目录） |
| `uv run ruff format --check aixcoding/` | **210 files already formatted** |
| `uv run python aixcoding/tools/check_docs.py` | **OK**——17 ADR / 21 design / 20 self-review 签认格式全合规（含本批次 design/ADR-0017/self-review 三份新签认） |
| `uv run python aixcoding/tools/check_import_boundaries.py` | **OK**（1131 文件） |
| `uv run python aixcoding/tools/gen_customizations.py --strict` | **check OK**（§1 已录，幂等复跑一致） |
| `check_r2_diff.py --base <fork point c54b60cf>` | **OK**——本批次零上游文件（工作区改动全部在 aixcoding/ 与定制层）；25 个已批上游文件全部在池内 |
| `check_r3_markers.py --base c54b60cf` | **OK**——marker_checked=17 / exempt=4，零新增/变更 marker |
| test job：`pytest aixcoding/tests -m "not integration and not gc_calibration"`（CI 原样，并发） | **354 passed, 2 skipped**（14.7s，`uv run python -m pytest` 绕损坏入口） |

上游全量 tests/ 不跑：本批次零上游源码改动，上游测试对 aixcoding 层零覆盖（ADR-0001 独立收集），层边界已由 tests/architecture（1238 passed）锚定——auth-logging 批次同口径先例。

## 6. 待人工项

无（面板 + 双卷 + debug.log 扩散面全部验收完毕，责任人 2026-11-02 目验通过）。

## 7. PR #84 peer review 处置（2026-11-02，head `090f3590`）

评审（独立 AI，worktree 副本 + GitHub API 交叉验证 + 2 组破坏性实验）：**0 blocker / 0 幻觉 API / 3 缺陷候选**；CI 双门禁实查双绿（AIxCoding Tests + AIxCoding Upstream CI，head `090f3590`）；数字对名全一致（`tests/architecture` 1237 vs §1 的 1238 差 1 = `test_network_egress` integration 标记被评审方过滤口径剔除，非失败）。责任人拍板：三条均**接受记档、零代码改动**——

- **C1**（`_attach_sink` 幂等重用只查 handler 类型、不校验重用 sink 落盘路径）→ 接受记档。设计前提：config 目录进程内稳定（`resolve_config_dir` 确定；生产唯一入口 `ensure_file_sink` 由 startup bootstrap 单次调用，无"同进程传不同 config_dir"的调用方）；失败方向为**明细缺失**（写旧位置、新位置无卷）而非泄漏。修路径校验需引入"同 marker 双实例双路径"新语义并重审六不变量④，代价大于收益。
- **C2**（双 sink ensure 的 check-then-attach 竞态，多线程首次并发可双挂）→ 接受记档。存量模式平移（原 `_file_sink` 同风险，本批仅量级 ×2）；生产接线单线程（`app/aixcoding/startup.py:26-34` bootstrap）；为零现实触发场景加锁违背简洁性原则；调用时序约定（单线程 bootstrap）记为设计前提。
- **C3**（双打两记录非原子：精简版落卷/面板与明细卷写入间存在中断窗）→ 接受记档。双打形态固有代价（design 备选表 G 行 facade 方案已隐含此权衡），失败方向为该时刻明细缺失而非泄漏，无廉价修法；显式记档避免后续批次误当已解决问题。§2 的"双打同毫秒"目验同时证明两调用相邻、窗口极窄。

评审记录本体存 `aixcoding/docs/pr/`（未跟踪工作文档区，不入库、不进签认/R2 体系）。
