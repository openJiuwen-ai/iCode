# Chrys Model Catalog Mock

模拟"服务端下发模型目录"这一路后端，让 catalog 同步的整条链路
（拉取 → 校验 → 整表替换 → version 未变则跳过）能在本地跑通。

对应客户端：`src/chrys/service/profiles/models/catalog.py`。

## 运行

```bash
uv run python aixcoding/mock/model_catalog/server.py            # :7777
uv run python aixcoding/mock/model_catalog/server.py --port 0   # 随机端口
uv run python aixcoding/mock/model_catalog/server.py --catalog /tmp/catalog.json
uv run python aixcoding/mock/model_catalog/server.py --no-auth  # 只挂目录，不带登录
```

**一个端口同时是目录服务和登录服务**：客户端先登录才同步得了目录，所以本 mock
默认把 `aixcoding_auth` 的路由挂在同一个端口（`/api/v1/...`、`/device/...`），
而 `AIXCODING_ENVIRONMENT=local` 指向的正是 :7777 —— 不用再单独起登录 mock。
`--auth-mode manual` 时授权要人在 `http://127.0.0.1:7777/device/verify` 点确认，
默认 `auto` 自动放行。

然后让 iCode 指向它：

```bash
AIXCODING_ENVIRONMENT=local uv run aixcoding-cli
```

## 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/llm/api/v1/continue-config/dispatch` | 当前模式下的目录 payload（query 参数被忽略，客户端拼的 `scopeType`/`userId` 不影响响应） |
| POST | `/mock/control` | `{"mode": "error", "delay": 8}` 运行时切换故障模式；响应体带回 mode / revision / version / 请求数 |
| POST | `/v1/chat/completions` | LLM chat mock：请求行的统一日志只带出**认证头**（`token` / `Authorization` / 含 auth/key/secret 等字样的头，值打码，没有则显示 `(no credential header)`），不打印载荷；再按 `stream` 回最小合法 ChatCompletion 或 SSE 流。把 `config_new.json` 里模型的 `apiBase` 指到 `http://127.0.0.1:7777/v1` 即可走这条 |

只留这两条 —— 外加默认同端口挂载的登录 mock：`POST /api/v1/auth/device/code`、
`POST /api/v1/auth/device/token`、`POST /api/v1/user/info`、`GET /device/verify`。
原来还有 `/model-catalog`（和 dispatch 是同一 payload 的第二条路径，重复）
和 `/mock/state`（信息 `control` 的响应已经带回来了，冗余），都已删除。

payload 形状是**服务端自己的**，不是 iCode 的 `ModelProfile`：

```json
{
  "models": [
    {
      "title": " 智谱 GLM-5",
      "provider": "openrouter",
      "model": "GLM-5",
      "apiBase": "https://open.bigmodel.cn/api/paas/v4",
      "contextLength": 256000
    }
  ]
}
```

接口返回什么不由我们决定，所以转换放在客户端
（`catalog.translate_models_payload()`，规则见
`docs/model-catalog-sync-details.md` §1.1）：`title` → `name`（trim）、
`model` → `model_id` 兼派生 `id`、`apiBase` → `base_url`、
`contextLength` → `max_context_tokens`；`provider` / `api_style` 写死（下发模型
全部走 OpenRouter）；`apiKey` 与 `api_key` **都不使用** —— 服务端不下发凭据，
profile 里也不留，发消息时按 `base_url` 注入（`service/llm/clients.py`）。

## 故障模式

每一条都对应客户端的一条规则 —— **同步失败必须完全不动 `~/.chrys/models`**：

| mode | 行为 | 期望的客户端反应 |
|---|---|---|
| `ok` | 数据源原样返回 | 整表替换 |
| `empty` | `models: []` | 拒绝，保留现有列表 |
| `invalid` | 条目没有可用的 `model` | 拒绝（派生不出 id），保留现有列表 |
| `error` | HTTP 500 | 保留现有列表 |
| `slow` | 延迟 N 秒（默认 8 > 客户端 3s 启动超时） | 超时，保留现有列表 |
| `stale` | `title` 漂移、派生 id 不变 | 跳过写盘（指纹不变） |

```bash
curl -X POST localhost:7777/mock/control -d '{"mode":"error"}'          # 让它挂掉
curl -X POST localhost:7777/mock/control -d '{"mode":"slow","delay":9}' # 让它超时
curl -X POST localhost:7777/mock/control -d '{"mode":"ok"}'             # 恢复
```

服务端 payload 里没有 `version`，客户端因此退化为 **id 列表指纹**（§1）。
`stale` 正是利用这点：它让 `title` 漂移但不动 `model`，指纹不变 ——
专门用来验证 15 分钟轮询不会无脑重写。

## 数据源

- 不给 `--catalog`：读包内的 `config_new.json`（参考服务端那份）
- 给 `--catalog <file>`：**文件内容即响应**，每请求重读一次，改文件即改响应、不用重启
- 顶层必须是 JSON 对象；`models` 之外的字段（`aa` 之类）原样透传 —— 过滤与转换
  都是客户端的事（`translate_models_payload()`）

真实 payload 里没有 `testToken`，也没有 `apiKey`：凭据不由目录下发。

## 模块划分

| 模块 | 职责 |
|---|---|
| `server.py` | HTTP 装配、路由、`start()` / `create_server()` / CLI |
| `source.py` | 数据源：`config_new.json`（缺省）或 `--catalog` 文件，每请求读、原样返回 |
| `modes.py` | 模式定义 + payload 构造（只改 `models`，其余字段透传）+ 状态/延迟 |
| `state.py` | 运行时开关：mode / revision / version / 请求计数 |
| `test_server.py` | 集成测试（端口 0、`direct_route`、契约验证） |

## 关于契约

mock 实现**不 import 产品代码**（保持黑盒，见 `aixcoding/mock/README.md`）。
契约校验放在 `test_server.py`：把 mock 的输出喂给客户端真实的
`parse_catalog()` —— 它内部先经 `translate_models_payload()` 转换再校验，
所以 `models` 的取用、转换结果、以及 `empty` / `invalid` 两种拒绝路径都由
此守住。
