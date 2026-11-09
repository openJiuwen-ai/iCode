# Mock 专区（aixcoding/mock/）

`aixcoding/mock/` 是纯开发资产（不进 wheel，DIRECTORY.md §6），**按"一个外部服务一个子包"**
组织，每个子包独立模拟一个 AIxCoding 实际依赖的内部后端，让产品代码本地端到端跑通、
被契约测试覆盖，无需打真实环境。

## 注册表与端口

| 子包 | 模拟目标 | 默认端口 | 产品端指向（默认） |
|---|---|---|---|
| `auth/` | 设备码登录认证服务 | 7777 | `AIXCODING_ENVIRONMENT=local`（零配置） |
| `model_catalog/` | 服务端下发模型目录（**默认同端口内嵌 auth 路由** + LLM chat `/v1`） | 7777 | 同上（一站式）；目录基址可经 `aixcoding.toml` 的 `[model_catalog].base_url` 覆盖 |
| `update/` | TUI 版本更新检测（`--port` 可覆盖） | 7778 | 当前产品端未接入；仅用于契约测试，待 update 批次接线 |
| `telemetry/` | 遥测接收后端（**殿后**，随 telemetry 迁入） | 4321（占位） | config.py LOCAL 档默认 |

**合用端口说明**：catalog 与 auth 合用 :7777 并非技术上必须（auth 有
`AIXCODING_AUTH_SERVER_URL` 覆盖、catalog 有独立 base 配置），而是 LOCAL 档零配置联调的
刻意选择——单 server + 单 env 覆盖登录、目录、LLM chat。`--no-auth` + 独立 auth mock
的拆用形态保留。

## 组合启动器（一键联调）

```bash
uv run python -m aixcoding.mock            # 默认：model_catalog(:7777 含 auth) + update(:7778)
uv run python -m aixcoding.mock --list     # 列出可用 mock 与端口
uv run python -m aixcoding.mock --only auth           # 纯登录场景（独立 auth，:7777）
uv run python -m aixcoding.mock --only model_catalog  # 只要目录+登录
```

单服务调试（双形态）：

```bash
uv run python aixcoding/mock/auth/server.py            # 直跑（依赖 server.py 内 sys.path hack）
uv run python -m aixcoding.mock.auth.server            # 模块形态（注意：-m 须落到 server.py）
```

### 端口占用处理（fail-fast，不静默换端口）

固定端口是联调可预期的前提；静默换端口会让产品流量打不到 mock。被占时启动器/CLI 明确
报错（服务名 + 端口），人工处理：

```console
netstat -ano | findstr :7777        # 查占用 PID（Windows）
tasklist /fi "PID eq <pid>"         # 看进程名，杀掉残留 mock 后重试
```

逃生门（人工作出端口时）：auth 使用 mock 侧 `--port` + 产品端 `AIXCODING_AUTH_SERVER_URL`；
模型目录使用 mock 侧 `--port` + `aixcoding.toml` 的 `[model_catalog].base_url`。update 当前无产品端
消费方，仅通过契约测试或独立接口请求验证。

## 目录结构

```
aixcoding/mock/
├── __init__.py             # 命名空间锚点（本包说明）
├── __main__.py             # 组合启动器（python -m aixcoding.mock）
├── README.md               # 本文件：注册表 + 约定 + 新增 mock 步骤
├── auth/
│   ├── __init__.py
│   ├── server.py           # HTTP server、授权状态机、本地验证页
│   └── README.md           # 端点 / 模式 / CLI 用法
├── update/
│   ├── __init__.py
│   ├── server.py           # HTTP server、严格校验、四种模式、debug 端点
│   └── README.md
└── model_catalog/
    ├── __init__.py
    ├── server.py           # HTTP 装配、路由（含内嵌 auth）、CLI
    ├── source.py           # 数据源：config_new.json（缺省）或 --catalog，每请求读
    ├── modes.py            # 故障模式与 payload 构造
    ├── state.py            # 运行时 mode/revision 开关
    ├── config_new.json     # 参考服务端的目录数据
    └── README.md
```

契约测试统一在 `aixcoding/tests/mock/`（`test_<name>_server.py` + `conftest.py` 的
`direct_route` 夹具），显式运行：`uv run pytest aixcoding/tests`。

## 强约束（DIRECTORY.md §6）

- 只绑定 loopback（`127.0.0.1` / `::1`），启动时拒绝其它地址；
- stdlib-only，不 import 产品包（mock 保持黑盒；契约测试可以 import 产品解析器做
  消费者侧验证）；
- 按真实后端的 wire quirks 逐字节复刻；
- 测试直连避开系统代理；
- **每个 mock 必须有契约测试**，无契约测试的 mock 不得合入。

## 新增一个 mock 子包的步骤

1. **挑名字**：无前缀服务名（如 `telemetry`）；目录名描述职责域，不预设实现形态
   （形态由子包内文件表达，当前均为 HTTP server 即 `server.py`；进程内替身不进本专区
   ——测试 fake 归 `aixcoding/tests/`，产品内挂钩归 `src/chrys/{tier}/aixcoding/`（按落位规则，ADR-0011））；
2. **建子包**：
   - `__init__.py`：docstring 列端点与公开 API；
   - `server.py`：`create_server(...)` factory（测试进程内驱动）+ `main()` CLI（独立
     进程）；直跑形态需要时加 `sys.path` hack（参照 `model_catalog/server.py`，注意
     `parents[3]` 索引依赖目录深度）；
   - `README.md`：端点表、故障模式、CLI 用法；
3. **契约测试**：`aixcoding/tests/mock/test_<name>_server.py`，`create_server(port=0)`
   进程内起停、直连避代理；锚定 wire 复刻（信封、状态码语义、故障模式逐条对应）；
4. **登记**：本 README 注册表加行（端口选未占用值）+ launcher `_SERVICES` 表加条目
   （`aixcoding/mock/__main__.py`）+ 如与既有服务同端口互斥需在 `_resolve_selection`
   加检查；
5. **过门禁**：`pytest aixcoding/tests` 绿 + ruff check/format `aixcoding/` +
   `python aixcoding/tools/check_import_boundaries.py`。

## 命名约定速查

| 用途 | 命名 |
|---|---|
| 目录（子包） | 无前缀服务名：`auth` / `update` / `model_catalog` / `telemetry` |
| 主模块 | `server.py`（目录名已带 service 上下文） |
| 契约测试 | `aixcoding/tests/mock/test_<name>_server.py` |
| 配置/状态类 | `Mock*Config` / `Mock*State`，如 `MockAuthConfig` |
| 入口 | `create_server(...)`（测试）；`main()`（CLI）；组合启动 `python -m aixcoding.mock` |
