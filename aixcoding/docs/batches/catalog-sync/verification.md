# 验证记录：模型目录同步 + 档案集中管理（catalog-sync）

> 追加不删旧（R7：验证即证据——命令 + 输出摘要）。本地验证落盘于此；CI 证据归属 PR checks 本身（§7）。
> 状态：实施验证完成（上游全套三分类收敛 + 离线打包冒烟 + 责任人目验均落盘）；双 AI 评审三轮收敛见文末两节。

## 2026-11-02 实施验证（Windows 11，本地）

环境口径（DEVELOPMENT.md 本地配方）：`PYTHONPATH=` 清空、`PYTHONUTF8=1`、大写盘符 `D:\`（Python 3.14.8，本地已与 .python-version 对齐，无需 `UV_PYTHON` override）。

### 1. 定制测试套（含 catalog 新套件）

```
set "PYTHONPATH=" && set "PYTHONUTF8=1" && uv run pytest aixcoding/tests -n0 -q
381 passed
```

新增 `aixcoding/tests/catalog/`：`test_catalog_sync.py`（平移 10 + 新增 12 = 22 用例）+ `test_read_only_gate.py`（3 用例）+ `conftest.py`（单例隔离 autouse fixture：每用例后 `stop_sync_runtime()` + 重置 `_runtime`/`_last_wiring`/`_warned_retired_env`；`wait_until` 本地复制，ADR-0001 失败域隔离）。

### 2. 上游测试

tui screens 目录（E 段红面origin处，R2 14/15 号跟改后复跑）：

```
uv run pytest tests/app/tui/screens -q
2769 passed   （红面清零：E 段落地时甄别的 10 failed 全部收敛）
```

上游全套（三分类口径：仅 R2 14-17 号签认跟改的行为差异允许变化，其余零失败）：

```
uv run pytest tests -n 8 -m "not integration and not gc_calibration" --deselect tests/app/test_install_scripts.py -q
6 failed, 34471 passed, 591 skipped in 921.53s (0:15:21)
```

三分类终甄：6 failed 全部第一类——红因精确对应 R2 表 6 号已签认的 F4 binding 移除
（footer 渲染/本地化/调和断言 ×4 + bindings provenance/keymap 契约断言 ×2，均硬编码
锚定已移除的 f4 `models_config`）。R2 追加 17 号（2026-11-02 责任人批准）跟改
`test_chrys_footer.py` 4 处 + `test_bindings.py` 2 处过期期望后，两目录复跑：

```
uv run pytest tests/app/tui/behaviors tests/app/tui/i18n -q
402 passed in 114.03s (0:01:54)
```

全套整体结论：除 6 处签认跟改红面外零失败（34471 passed / 591 skipped 与既有基线
一致）；跟改为纯测试期望删改，射程仅两文件自身用例，全套终态由 PR CI 全量复跑把关。

### 3. 门禁：gen_customizations / ruff / format / ty

```
uv run python aixcoding/tools/gen_customizations.py --strict
check OK — 43 lines / 47 markers / 0 errors / 0 warnings
```

（首跑曾报 18 errors：twin 计数超 2 处 + 未登记 marker 9 处 + 孤儿登记 8 处——全部按"登记锚为签认基准、代码注释向锚对齐"原则修正，见 §6 波折④。17 号跟改落地 C048/C049 后复跑：**45 registration rows / 49 markers / 0 error / 0 warning**。）

```
uv run ruff check <21 改动文件>            → All checks passed
uv run ruff format --check <21 改动文件>   → already formatted
uv run ty check --error-on-warning --python-platform linux src/chrys   → 零 error
uv run ty check --error-on-warning --python-platform darwin src/chrys → 零 error
uv run ty check --error-on-warning --python-platform win32 src/chrys  → 零 error
```

### 4. i18n 流水线

- 新 key `settings.model.catalog.base_url.label` 四处同步（pot/po/mo/_catalog_oracle_ids.py）；
- zh-Hans 译文：`模型目录服务地址`；`read_only_notice` 去 fuzzy 改译 `• 模型配置由系统统一管理，可选用切换，不可编辑。`（照 0.28 final 口径）；
- oracle 断言主集合计数 2503 → 2504；

```
uv run pytest tests/foundation/i18n -q
285 passed
```

（mo 编译产物 `src/chrys/foundation/i18n/_catalogs/zh-Hans/LC_MESSAGES/chrys.mo` 随同更新 213534 → 213637 bytes——i18n 编译落点在包内嵌 `_catalogs/` 而非 `locales/`，断言 localedir 须指包内路径。）

### 5. 架构生成物再生成

`tests/architecture/trajectory_wait_manifest.json` +9 行：新模块 `chrys.service.aixcoding.catalog` 的 awaitable 登记项（`fetch_catalog` ×2 / `sync_catalog` ×1，均 unresolved）——架构测试生成物随同改动刷新（"Refresh generated artifacts in the same change"义务）。

### 6. 实施波折记录

- ① catalog conftest 首版 `from .conftest import wait_until` 相对导入失败——改绝对导入 `from aixcoding.tests.catalog.conftest import wait_until`（tui 批次同款惯例）；
- ② `test_stale_generation_result_is_dropped` 首版观测器用 `asyncio.Event` 跨 loop 无效（fetch 在 sync 线程独立 `asyncio.run` loop 中运行）——改 `threading.Event` + while 轮询（`# noqa: ASYNC110` 附原因注释）；又因线程先睡 interval 才首轮 fetch，改 `start_sync_runtime(..., immediate=True)`；
- ③ `test_open_model_config_screen_is_always_read_only` 首版 monkeypatch 打错命名空间（打了调用方 config_actions 的属性，实际函数内 from-import 取源模块符号）——改打 `chrys.service.profiles.models.env_bridge.get_global_default_profile_id`；
- ④ `gen_customizations --strict` 首跑 18 errors（twin-group 计数超 2 处 + 未登记 marker 9 处 + 孤儿登记 8 处）——按"登记锚为基准、代码注释向锚对齐"修正 11 处代码注释（不反向改登记表），复跑 0/0；
- ⑤ ruff 拦截 PLW0108（`lambda: _Session()` 内联为 `_Session`）/ SIM105（try-except-pass 改 `contextlib.suppress(StopIteration)`）；
- ⑥ E 段落地后甄别计划外红面 10 failed（wiring ×5 + model_config ×1 相关）——三分类判定均为第一类（已签认行为差异连带），R2 追加 15/16 号呈报责任人裁决（2026-11-02 "按你的建议处理"），design 修订落盘 + 修测试构造不修断言（registry 名对齐 runtime 名）；
- ⑦ 上游全套暴露 6 failed（footer ×4 + bindings ×2，`tests/app/tui/screens` 之外目录当时未跑）——三分类终甄全部第一类（R2 6 号 F4 移除连带）；逐点枚举发现 `localizes` 用例 :349 的 zh 描述映射第二处红点为首失败点 :282 挡住的**隐藏红**（逐点枚举避免"改一处复跑一次"的半红收敛）；R2 追加 17 号（删过期期望不放松锚定，locales 翻译 key 保留）呈报责任人批准后 6 处跟改 + C048/C049 marker，两目录 402 passed 收敛。

### 7. 工作区改动面

```
git status --short
 M aixcoding/docs/INDEX.md
 M aixcoding/docs/ROADMAP.md
 M aixcoding/docs/standards/DIRECTORY.md
 M locales/chrys.pot
 M locales/zh-Hans/LC_MESSAGES/chrys.po
 M src/chrys/app/aixcoding/tui/login_actions.py            （D 段：占位符实调）
 M src/chrys/app/tui/app.py                                 （D 段：接线，R2 唯一上游行之一）
 M src/chrys/app/tui/screens/main/config_actions.py         （E 段：read_only 恒真）
 M src/chrys/app/tui/screens/main/model_indicator.py        （E 段：live label）
 M src/chrys/app/tui/screens/main/screen.py                 （E 段：F4 禁用 + live label）
 M src/chrys/app/tui/screens/models/picker.py               （E 段：manage 入口禁用）
 M src/chrys/app/tui/screens/models/screen.py               （E 段：read-only 文案）
 M src/chrys/app/tui/screens/settings/layout.py             （C 段：settings 三件套）
 M src/chrys/foundation/config/process_settings.py          （C 段）
 M src/chrys/foundation/config/settings.py                  （C 段）
 M src/chrys/foundation/i18n/_catalogs/zh-Hans/LC_MESSAGES/chrys.mo   （F 段：编译产物）
 M src/chrys/service/profiles/models/registry.py            （B 段：RLock + snapshot）
 M tests/app/tui/screens/test_model_config.py               （R2 16 号跟改）
 M tests/app/tui/screens/test_model_indicator_wiring.py     （R2 15 号跟改）
 M tests/app/tui/screens/test_model_picker.py               （R2 14 号跟改，4 处）
 M tests/app/tui/behaviors/test_chrys_footer.py             （R2 17 号跟改，4 处 + C048）
 M tests/app/tui/i18n/test_bindings.py                      （R2 17 号跟改，2 处 + C049）
 M tests/architecture/trajectory_wait_manifest.json         （生成物再生成，§5）
 M tests/foundation/i18n/_catalog_oracle_ids.py             （F 段：oracle key）
 M tests/foundation/i18n/test_catalog_oracle.py             （F 段：计数断言）
?? aixcoding/docs/batches/catalog-sync/                      （六文档管线产物）
?? aixcoding/tests/catalog/                                  （新测试套件）
?? src/chrys/service/aixcoding/                             （A 段：catalog.py 定制包）
```

统计：25 files changed, 238 insertions(+), 84 deletions(-) + 三处新增目录。
（17/18 号跟改两文件计入后为 25；含第三轮评审修复的 catalog.py/测试改动属定制包与
定制测试，在 untracked 目录内不占此统计。）

## 2026-11-02 离线打包冒烟（DoD 项）

### 1. 离线缓存版本核对与更新

上游 0.32.2 升级后两缓存过期，按配方"缓存版本随上游升级需核对更新"更新
（curl 直连，代理调整后 cpython 22.15MB 约 5s + rg 1.70MB 约 1s）：

```
dist\downloads\cpython-3.14.8+20261003-x86_64-pc-windows-msvc-install_only_stripped.tar.gz  （新增；旧 3.14.0 留存）
dist\downloads\ripgrep-15.2.0-x86_64-pc-windows-msvc.zip                                  （新增；旧 15.1.0 留存）
```

（核对依据：`.python-version`=3.14.8；build.ps1 的 PYTHON_BUILD_STANDALONE_RELEASE=20261003、
fetch_rg.ps1 默认 RG_VERSION=15.2.0。）

### 2. 构建波折：CRLF 检出 vs PyApp LF 源码（0.28 时代已知问题复现）

直接跑 `scripts\build.ps1 -Offline` 在 "Patching PyApp runtime aliases" 步 throw
`failed to patch PyApp runtime aliases`（:441）：本机 git autocrlf 把 build.ps1 检出为
CRLF（469/469 行），here-string 精确文本匹配 LF 的 PyApp 源码静默失配。复刻 0.28 时代
build_lf.ps1 方案（该脚本未入仓）：字节级 CRLF→LF 生成临时副本执行，上游文件零改动：

```
uv run python -c "...read_bytes().replace(b'\r\n', b'\n')..."   → scripts\build_lf_tmp.ps1（跑后删除）
set PYAPP_SOURCE / PYTHON_DIST / RG_ARCHIVE（三下载物绝对路径） && powershell -File scripts\build_lf_tmp.ps1 -Offline
==> Building chrys v0.32.2 (PyApp v0.29.0) → cargo release 58.25s → dist\icode.exe (85.9 MB)
```

build_offline_dist.ps1 的 rg 去重 SyntaxError（PS 5.1 传参破坏引号）复现，已如配方
预判为非致命噪音（构建继续：94 locked distributions、archive 85MB、验证 bundled
chrys 0.32.2 + rg + ca 通过）。

### 3. 产物冒烟

```
dist\icode.exe --version    → 0.32.2（首跑解包内嵌分发成功）
```

目录同步链路（mock 联调服务 :7777 就位，dispatch payload = GPT-4o mini 单模型；
env `AIXCODING_AUTH_ENVIRONMENT=local` + `AIXCODING_MODEL_CATALOG_TOKEN` 触发凭据门，
一次性 Python subprocess 脚本驱动 TUI 30s 后 taskkill /T）：

```
BEFORE（首次）: 7af6dae239ae.yaml + deepseek-v4-flash.yaml + deepseek-v4-pro.yaml（联调残留 3 profile）
AFTER:          openai-gpt-4o-mini.yaml + .catalog-state.json（目录整替 + state 写入）
STATE:          {"default_profile_id": "openai-gpt-4o-mini", "profile_ids": ["openai-gpt-4o-mini"],
                 "source": "remote", "version": "0bb346deb...52d73"}
二次运行:        mtime 刷新、集合不变（幂等重写收敛）
```

- **启动同步整替语义端到端实证**：旧 profile 集合被服务端目录整替、快照 state
  （`source: remote`）写入——A/B 段（发布事务 + 快照安装）签认行为的产物级验证；
- **F4 不在 footer（渲染帧实证）**：TUI 输出尾帧 footer 键位行为
  `f1 会话 f2 智能体 f6 日志 f8 帮助 f9 主题 f10 设置`——无"模型"键（F4）；
- TUI 中文渲染正常（输入框提示、agent 状态栏、模型标签）。

### 4. 责任人实测目验（2026-11-02，产物 + mock 联调）

运行形态：新窗口 mock（`uv run python -m aixcoding.mock`，model_catalog :7777 含内嵌
auth）+ 新窗口产物 `dist\icode.exe`（`AIXCODING_AUTH_ENVIRONMENT=local`）。

- **F4 不在 footer** ✅（责任人确认；与 §3 渲染帧实证一致）；
- **`/models` 保留且打开恒只读** ✅（责任人确认"打开的模型配置界面是只读的"——符合
  design 方案 A / R2 表 11 号：`/models` 命令是**有意保留**的只读屏入口，非缺陷）；
- banner 观察项（责任人提出"看到 Chat 而非 AIxCoding"）：核查归类为**非本批射程**——
  "Chat" 是 `app_header.py` 的 chat/workflow **模式徽章**（`MODE_CHAT`，上游组件，与
  0.28 final 同构未动）；品牌名差异属 **C4 品牌定制**（ROADMAP §4 C 表，待产品决策）
  未实施——当前分支 branding 为上游默认 `iCode`（header 标题实测 `iCode v0.32.2`），
  0.28 final 的 "AIxCoding" 来自其 branding 定制（`APP_DISPLAY_NAME`）。本批次零改动
  `app_header.py` / `branding.py` / `workflow_text`；
- picker 无 "Manage models…" 入口 ✅（责任人 2026-11-02 确认"列表底部没有'管理模型…'/
  "Manage models…" 动作行"——C038 落地实证，与 `test_model_picker.py` 跟改锚定一致）。

## 2026-11-02 双 AI 评审修复轮（HOWTO ①②③）

评审 AI：**Kimi-K3.1-Pro / CodeBuddy**（独立会话，只读评审）；6 findings（4 blocker +
2 suggestion）+ 1 轮复审（F-3 范围扩展）——逐条独立核实**全部 confirm**（零 reject），
责任人批准"按建议处理 + F-3 扩展口径"。修复明细：

- **F-1（blocker）R2 节标题致 check_r2_diff 解析失效**：原标题
  `上游改动清单（R2；本表即 R2 报批清单）` 不在解析器白名单（`_common.py:377-379`），
  本批 14 个上游文件判 unauthorized（本仓实施期漏跑该门禁——gen_customizations 过
  不代表 R2 门禁过，两道门禁独立）。修复：标题改标准 `上游改动清单（R2）` + 17 号
  拆 17/18 两行（一行一文件表规范）；design 修订记录（九）；
- **F-2（blocker）mkdir OSError 穿透**：`directory.mkdir` 移入发布 try 块（实现与
  docstring "every OSError converted" 契约对齐）；新增 mkdir 故障用例（apply 抛
  `ModelCatalogError` + `sync_catalog_blocking` 返回 None + caplog）；
- **F-3（blocker，按复审扩展口径）payload 类型异常穿透**：① `_positive_int` 补
  `OverflowError`（JSON `1e999` → `int(inf)`）；② parse_catalog 的 dataclass 构造 +
  字段校验并入统一 `except (TypeError, ValueError)` 转换（复审指出非 str id 过构造后
  在 regex match 抛 TypeError、不可哈希 provider 在 set 判断抛 TypeError——dataclass
  构造不做运行时类型校验）；新用例：inf 直测 + models 形态端到端 + 两类 TypeError
  payload 断言 `ModelCatalogError`；
- **F-4（blocker）轮询解析失败静默**：fetch_catalog 内 parse 调用补日志（warning +
  re-raise，`_sync_once` "already logged" 注释重新为真）；新用例**驱动真实轮询**
  （start_sync_runtime immediate + httpx 假 client + parse 注入，`await wait_until`
  断言 caplog）——非只测启动降级；
- **F-5（suggestion）阶段一清理空断言**：用例重写为首个 profile 真写落盘、第二个抛
  （清理断言非 vacuous——删实现清理块必红）+ `written == ["b.yaml.tmp", "c.yaml.tmp"]`
  精确文件名锚定（连带 F-6）；
- **F-6（suggestion）tmp 双后缀**：`f"{id}.yaml{SUFFIX}"` → `f"{id}{SUFFIX}"`（`<id>.yaml.tmp`
  与签认设计一致；orphan 清理 glob 天然兼容历史双后缀残留）。

实施期自纠两处（修复中暴露，如实记录）：① `wait_until` 为 async 函数，同步直调返回
coroutine 恒 truthy——假通过，改 `@pytest.mark.asyncio` + `await`（评审教的 vacuous
教训即时生效）；② `written` 断言首版漏计第二个 append 项。另 mkdir 用例发现
`_catalog_token` 未 wire（fetch 在 token 检查处提前 raise），补 token stub。

复跑（全部绿）：

```
uv run pytest aixcoding/tests/catalog -n0 -q          → 31 passed（27 + 4 新用例）
uv run python aixcoding/tools/check_r2_diff.py --base HEAD → check_r2_diff: OK
  （approved_pool 41，upstream_changed 21 全部 approved）
uv run python aixcoding/tools/gen_customizations.py --strict → 45 rows / 49 markers / 0 warning
uv run ruff check + format --check（两文件）          → All checks passed / already formatted
uv run ty check --error-on-warning --python-platform {win32,linux,darwin} src/chrys → 三平台 All checks passed
uv run python aixcoding/tools/check_docs.py           → OK
```

## 2026-11-02 第三轮评审修复（全量复审后 4 findings）

评审 AI：Kimi-K3.1-Pro（第三轮，以当前代码为准不沿用旧结论；定向复跑新增套件 +
5 个跟改测试文件 148 passed）；4 findings 逐条独立核实**全部 confirm**，责任人批准
"按此执行"。

- **F-1（blocker）非法 name 类型发布后启动穿透**：原生 `items`/裸列表形态只校验
  id/provider/api_style，`name=["bad"]` 可发布落盘，下次启动 `load_all()` 的
  `profile.name.casefold()` 抛 `AttributeError`（启动链不捕获）。修复：parse_catalog
  字段校验区补 `name`/`model_id` 字符串校验——**发布前拦截**（坏 payload 不落盘），
  五个核心 str 字段（id/name/provider/model_id/api_style）全覆盖；新用例断言两类
  非法 payload 抛 `ModelCatalogError`；
- **F-2（suggestion）NaN/inf interval 未降级**：`float("nan")` 过 `<= 0` 检查
  （`Condition.wait(nan)` 立即返回→热循环）、`inf` 抛 `OverflowError` 杀轮询线程。
  修复：`math.isfinite(value) and value > 0`（三异常值统一警告 + 回退 15min）；
  nan/inf/-inf 参数化用例；
- **F-3（suggestion）退役 env 测试缺口**：原仅覆盖退役 URL/INTERVAL 与新 URL/INTERVAL。
  修复：四退役名参数化全覆盖（URL/TOKEN/SYNC_INTERVAL/`CHRYS_REMOTE_MODEL_CONFIG_URL`
  各自单独存在→三读取函数全回退默认 + 警告含名不含值）；新 TOKEN 优先断言（退役
  TOKEN 名输给登录存储 token；新名 env 覆盖登录）——登录侧 monkeypatch 打源模块
  `chrys.kernel.aixcoding.auth.get_login_session`；
- **F-4（nit）文档状态未同步**：verification 状态行（"两节待补"已过期）刷新；
  统计 23→25 files / +238/-84（17/18 号两跟改文件计入）；design R1 自查"14 行"刷新
  为 18 行；self-review 同步。

复跑（全绿）：

```
uv run pytest aixcoding/tests/catalog -n0 -q          → 38 passed（31 + 7 新参数化/用例）
uv run ruff check + format（两文件）                  → All checks passed
uv run ty check --error-on-warning --python-platform win32 src/chrys → All checks passed
```

## 2026-11-02 第三轮修复后责任人复验（源码态）

运行形态：mock（:7777）+ 源码态 `uv run icode`（`AIXCODING_AUTH_ENVIRONMENT=local`，
**含第三轮全部修复**——旧 dist 产物不含，故用源码态）。责任人目验结论：**没有发现
问题**——mock 登录 → 即时同步 GPT-4o mini（新 parse 校验不误拦正常 payload）、footer
无 F4、picker 无 manage 入口、`/models` 恒只读集中管理文案，正常路径零回归。

## 待补项

- 无——批次验证收官，待 self-review.md 签认后进入提交流程。
