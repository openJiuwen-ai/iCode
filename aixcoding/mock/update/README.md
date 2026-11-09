# AIxCoding Update-Detection Mock Server

`aixcoding/mock/update/` —— AIxCoding TUI 版本更新检测服务
（`GET /csas/api/v2/releases/check`）的本地模拟。**不随产品分发**，仅用于开发与
验证 TUI 更新检测功能。
真实后端尚未就绪，本 mock 冻结 wire 契约（四个必填查询参数严格校验、v2
manifest、`ETag`/`304`/`204` 语义），让客户端实现、手工联调、集成测试可以先行。

标准库实现，零依赖，默认 `127.0.0.1:7778`，仅绑定 loopback。

## 用法

启动无任何参数（默认 update 模式、`127.0.0.1:7778`），所有行为调整都走运行期
`POST /debug/config`：

```bash
uv run python aixcoding/mock/update/server.py   # 启动：update 模式、:7778
curl -X POST http://127.0.0.1:7778/debug/config -d '{"mode": "no_update"}'   # 有效请求一律 204
curl -X POST http://127.0.0.1:7778/debug/config -d '{"mode": "error"}'       # 有效请求一律 500（验证客户端吞错）
curl -X POST http://127.0.0.1:7778/debug/config -d '{"mode": "garbage"}'     # 200 但 body 非 JSON（验证解析容错）
curl -X POST http://127.0.0.1:7778/debug/config -d '{"version": "0.29.0", "slow_ms": 8000}'
curl -X POST http://127.0.0.1:7778/debug/config -d '{"version": "0.27.0", "force_update": true}'  # 脏数据演练：跳过比较一律 200
curl -X POST http://127.0.0.1:7778/debug/config -d '{"force_upgrade": true}'  # manifest 标记 forceUpgrade=true（强更演练）
curl -X POST http://127.0.0.1:7778/debug/config -d '{"drop_fields": ["version", "artifact.url"]}'  # 响应格式错误演练：manifest 缺字段
curl -X POST http://127.0.0.1:7778/debug/config -d '{"raw_body": "{\"version\": \"0.29."}'         # 响应格式错误演练：任意坏 body（如截断应答）
curl -X POST http://127.0.0.1:7778/debug/config -d '{"slow_ms": 20000}'  # 切到超时场景
curl -X POST http://127.0.0.1:7778/debug/config -d '{"mode": "error", "slow_ms": 0}'  # 切到失败场景
curl -X POST http://127.0.0.1:7778/debug/reset  # 一键恢复默认配置并清空请求记录
curl http://127.0.0.1:7778/debug/requests  # 查看每次检测请求的参数与状态码
```

产品端接线状态：当前版本尚未迁入内部 update 消费方，本 mock 仅用于契约测试与独立接口演练；
不得继续使用 0.28 的全局扩展地址环境变量。产品端配置由后续 update 批次按 ADR-0022 接线。

作为 Python 模块（测试桩）：

```python
from aixcoding.mock.update.server import MockUpdateConfig, create_server

server = create_server(MockUpdateConfig(mode="no_update"), host="127.0.0.1", port=0)
```

## 端点

| Method | Path | 用途 |
|---|---|---|
| GET | `/csas/api/v2/releases/check` | 版本检测（严格模式：参数未知/缺失/非法 → `400 {"error":"invalid_app_release_query"}`；有效请求按 mode 回答） |
| GET | `/debug/requests` | 检测请求记录（时间、参数、状态码、当时模式） |
| POST | `/debug/config` | 运行期切换配置（`mode` / `version` / `download_url` / `slow_ms` / `force_update` / `force_upgrade` / `raw_body` / `drop_fields`），返回快照 |
| POST | `/debug/reset` | 全部恢复默认值（mode / version / 延迟 / 演练开关等）并清空请求记录，返回重置后快照 |

## 检测端点行为

有效请求（`clientType` / `platform` / `architecture` /
`currentVersion` 四参数齐全且合法）按 `mode` 回答：

| mode | 回答 | 覆盖的客户端路径 |
|---|---|---|
| `update`（默认） | 候选版本**严格大于** `currentVersion`（SemVer 2.0.0 优先级）→ `200` manifest + `ETag`（body SHA-256），`If-None-Match` 命中 → `304`；否则 `204` | 有新版本提示 / 已最新 |
| `no_update` | `204` 空 | 已最新 |
| `error` | `500` | 网络/服务错误静默 |
| `garbage` | `200` 但 body 非 JSON | 解析失败静默 |

非法请求在任何模式下都回契约的 `400`。manifest（v2 结构）的 `kind` 反射
`clientType`、artifact 的 `platform`/`architecture` 反射请求参数
（`artifact.platform` 由 platform 推导友好命名：darwin→macos、win32→windows），
`version`、下载地址与 `forceUpgrade` 可配置，任意客户端形态发起都能拿到自洽的应答。

响应格式错误演练参数组（仅作用于 200 应答，经 `/debug/config` 切换）：

| 参数 | 效果 | 覆盖的客户端路径 |
|---|---|---|
| `drop_fields` | 从 manifest 移除指定字段（支持 `artifact.url` 点路径，未知路径为 no-op） | 结构不符 → 静默失败 |
| `raw_body` | 用任意字面量替换 200 应答体（如截断的 JSON） | 解析失败 → 静默失败 |

## 模拟三种版本场景

`update` 模式自带 SemVer 2.0.0 比较（prerelease 优先级也实现了，含
`0.28.0` > `0.28.0-dev-abc1234` 这类 dev 构建拼写），三种场景只改
`version` 一个参数，不用切 mode：

| 场景 | `POST /debug/config` 请求体 | 对 `currentVersion=0.28.0` 的回答 |
|---|---|---|
| 有新版本 | `{"version": "0.29.0"}`（或用默认 `999.0.0`） | `200` |
| 没有新版本（相等） | `{"version": "0.28.0"}` | `204` |
| 版本比本地小 | `{"version": "0.27.0"}` | `204` |

`force_update: true` 跳过比较，强制对任意请求回 `200`——用于演练"服务端脏数据推了个
更小的版本号"，验证客户端不做本地版本比较的约定（`204`/`200` 之外不做判断）。

## 测试位置

`test_server.py`（本子包内）用 stdlib `urllib` + 空 `ProxyHandler` 直连（与
`chrys_model_catalog` 的 `direct_route` 夹具等效，免夹具依赖），覆盖严格校验、
四模式、304、请求记录与 loopback 约束。客户端实现后，端到端测试放
`tests/app/aixcoding/`。
