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
