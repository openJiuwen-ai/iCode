# 测试方案：定制版用户手册入库与安装版捆绑（user-manual）

> 活文档：随时更新、不签认；其质量由双 AI 自审（SELF-REVIEW-HOWTO）与行为锚定测试本身保障。

## 测试范围与策略

本批次为"数据源 + 构建声明"改动，无新增定制代码模块，测试分四层：

1. **上游回归（行为锚定）**：`tests/foundation/test_documentation.py`（`resolve_docs_root`
   三级解析：env 覆盖 / 坏覆盖降级 / checkout 回退 / 捆绑优先）+ `tests/app/tui/screens/guides/`
   （GuideDialog/index/reader 全链路）+ `tests/service|orchestration/**/test_documentation.py`
   （agent 运行时文档路由）——上游测试仅 **1 处跟改**（C147：契约测试
   `test_wheel_bundles_docs_where_the_resolver_looks` 断言键随捆绑源切换，
   `design.md` R2 表第 3 行，2026-11-10 评审 F1 增补），其余零跟改。
2. **构建完整性门禁**：`scripts/check_wheel.py`（安装 wheel 后 `resolve_docs_root()` 必须解析到
   捆绑副本 + 捆绑清单与 `git ls-files aixcoding/manual` 逐文件比对）——本批次的自动化主门禁。
3. **内容门禁**：`check_links.py`（`manual-version-update` 技能脚本）：GitHub slug 内锚校验 +
   第三方外链扫描 + 裸 URL 清点，PASS/FAIL 退出码。
4. **人工冒烟**：开发机 F8 指向实测 + 安装版 wheel 内捆绑内容抽验（哈希逐文件）。

涉及上游 anchor 时：`resolve_docs_root` / `BUNDLED_DOCS_PATH` / `BUNDLED_DOCS_PREFIX` 为
本批次依赖的上游符号，行为锚定由第 1 层上游既有测试承担（真调符号、无 mock 锚点）。

## 覆盖矩阵

| 验收标准（对应 requirements.md） | 测试入口（文件::用例/命令） | 类型 |
|---|---|---|
| manual/ 入库 67 文件、结构同构 docs/、docs/ 零定制 | `git ls-files aixcoding/manual`（67）+ `git diff <上游tag> -- docs`（空） | 结构检查 |
| 内锚 237 全有效 / 外链 0 | `check_links.py <手册MD>` → `内锚 237 \| 失效 0 \| 外链 0 \| PASS` | 内容门禁 |
| wheel 捆绑源 = manual，逐文件一致 | `python -m hatchling build` + zipfile 清单/哈希比对（verification 2026-11-10 节）；正式门禁 `python scripts/check_wheel.py dist/<wheel>`（SAC 受限机器以一次性等价校验替代：脚本用毕即删，输出与复现方式留 verification） | 构建/契约 |
| check_wheel 比对源切换 | `git grep -n "aixcoding/manual" scripts/check_wheel.py`（2 处）+ 构建比对绿 | 静态 + 契约 |
| 开发机 F8 指向 manual | `CHRYS_DOCS_ROOT=<repo>/aixcoding/manual` 下调 `resolve_docs_root()` 断言路径（verification 实测） | 行为锚定（上游符号） |
| 上游解析/手册 UI 不回归 | `pytest tests/foundation/test_documentation.py tests/app/tui/screens/guides tests/service/context/providers/test_documentation.py tests/orchestration/{workflows,invoker}/test_documentation.py -q`（含契约测试 C147 跟改后断言） | 上游回归 |
| 桌面 PDF 同步 | Edge 无头打印页数核验（139 页）+ HTML 锚点数复核 | 人工/交付 |
| check_docs 门禁绿（文档入库后） | `uv run python aixcoding/tools/check_docs.py` → exit 0 | 文档门禁 |

覆盖缺口与理由：无新增定制代码模块（§5"新增独立定制代码模块须有测试"不触发）；构建比对在
SAC 受限机器上无法直接跑 `check_wheel.py`（其子进程 exe 被拦，os error 4551），已用一次性
等价校验取得证据（命令与输出留 verification.md，脚本不保留）——正式 CI（Linux）环境无此
限制，`aixcoding-upstream-ci` 桥接跑 build+wheel 探测时自然覆盖，此为**本批次唯一遗留缺口**。

## 异常与降级路径

- `CHRYS_DOCS_ROOT` 指向不存在路径 → `resolve_docs_root()` 返回 None → F8 显示"文档缺失"
  态（上游 `tests/.../test_documentation.py` env 覆盖用例锚定，本批次不改动）；
- manual 缺 `index.yaml` → 捆绑副本不完整 → `check_wheel.py` 清单比对红（防漏打包）；
- rg.exe 缺失（vendor 未下载）→ 依赖真实 ripgrep 的上游测试失败——**环境前提**，非本批次
  缺陷；修复方式 = `scripts/fetch_rg.ps1`（已实测，verification 2026-11-10 节）。

## 手工验证步骤（结果记入 verification.md）

```
1. 构建：uv pip install hatchling（清华镜像，临时）→ python -m hatchling build
2. 捆绑比对：zipfile 列清单 ∪ 哈希逐文件 vs aixcoding/manual/（等价脚本，见 verification）
3. F8 解析：CHRYS_DOCS_ROOT 设定后 resolve_docs_root() 打印路径
4. 内容门禁：check_links.py 桌面定稿 MD → PASS
5. 门禁总检：uv run python aixcoding/tools/check_docs.py → exit 0
```
