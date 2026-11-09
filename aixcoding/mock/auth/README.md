# AIxCoding Auth Mock Server

`aixcoding/mock/auth/` —— AIxCoding 设备码登录认证服务的本地模拟。**不随产品
分发**，仅用于开发与验证 `aixcoding/auth/`（`AuthClient` / `LoginSession` / 登录对话框）。

内网认证服务（PROD `82.187.34.98` / DEV `81.89.182.150`）外网不可达，外网开发全靠
本 mock。标准库实现，零依赖，默认 `127.0.0.1:7777`（与 `aixcoding/auth/environments.py`
的 LOCAL 档对齐，无需额外配置）。

## 用法

CLI：

```bash
uv run python aixcoding/mock/auth/server.py                  # manual 模式，:7777
uv run python aixcoding/mock/auth/server.py --mode=auto      # 轮询交替 pending/authorized（冒烟）
uv run python aixcoding/mock/auth/server.py --deny           # 授权页一律拒绝（验证 access_denied）
uv run python aixcoding/mock/auth/server.py --slow-down 3    # 前 3 次轮询返回 slow_down（验证退避）
```

客户端指向它：

```bash
AIXCODING_ENVIRONMENT=local uv run aixcoding-cli
```

作为 Python 模块（测试桩）：

```python
from aixcoding.mock.auth.server import MockAuthConfig, create_server

server = create_server(MockAuthConfig(mode="auto"), host="127.0.0.1", port=0)
```

## 端点

| Method | Path | 用途 |
|---|---|---|
| POST | `/api/v1/auth/device/code` | 申请设备码（`result` 信封） |
| POST | `/api/v1/auth/device/token` | 轮询授权结果（`result` 信封；pending 时 `success` 仍为 `true`，结果看 `result.error`） |
| POST | `/api/v1/user/info` | 查用户信息（`data` 信封；未知 token 返回无 `success` 键的 `code: 400`，HTTP 仍 200） |
| GET | `/device/verify?user_code=...` | 本地授权确认页（manual 模式的人工确认入口） |
| POST | `/device/verify/confirm` | 确认页表单提交（allow / deny） |

## 两种模式

- `manual`（默认）：轮询保持 `authorization_pending`，直到有人在本地验证页点确认——
  如实模拟"等用户在浏览器完成登录"。
- `auto`：每个 `device_code` 的轮询奇偶交替 pending/authorized，适合冒烟测试，
  与参考实现 `mockServer/server.js` 行为一致。

## 测试位置

本子包不带 `test_server.py` / `conftest.py`：mock 的行为测试已由
`tests/app/aixcoding/`（`test_auth_client.py` 的 `TestLoopbackMockIntegration`、
`test_auth_session.py`、`test_login_dialog.py`、`test_probe_cancel.py`）通过真实
`AuthClient` 端到端覆盖，此处再写一份属重复建设。若日后需要脱离 `aixcoding`
单独测 mock 的协议怪癖（信封、HTTP 200 语义等），再按 `chrys_telemetry/` 的五件套
补齐，并把 `direct_route` 夹具上提到 `aixcoding/mock/shared/`。
