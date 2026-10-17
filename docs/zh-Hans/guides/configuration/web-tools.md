# 配置网络工具

网络工具让智能体可以在互联网上查找资料：`web_search` 搜索网页，返回链接和摘要；`web_fetch` 按已知 URL 读取页面文本。本指南介绍如何为智能体启用这两个工具、它们的请求发往何处，以及如何选择搜索提供方和限制网络访问。

**网络工具默认关闭。** iCode 自带的智能体（Code、QA、Explore 和 General）都不包含网络工具；在你为某个智能体启用之前，iCode 不会发送任何搜索请求，也不会读取任何网页。

`web_search` 与“文件搜索”工具（`grep` 和 `glob`）不同，后者只搜索本机文件。

## 启用网络工具

### 为智能体添加工具

在输入栏中输入 `/agents tools`，选择智能体，勾选“网络搜索”“网页读取”或两者，然后点击“保存”。两者都立即可用。除非你选择了其他提供方，网络搜索会把搜索词发送给 Exa（exa.ai），启用前请先阅读[网络请求的去向](#网络请求的去向)。

在智能体配置文件中，也可以把这两个工具类别加入 `tools.builtins`：

```yaml
tools:
  builtins: [filesystem.read, search, web_search, web_fetch]
```

网络工具按智能体分别配置。子智能体（例如 General）只有在自己的配置中包含网络工具时才能使用。如需为内置智能体添加网络工具，在 TUI 中编辑并保存该智能体，iCode 会为其保存一份你自己的配置（参阅[覆盖内置智能体](../../reference/agent-profile.md#覆盖内置智能体)）。

### 关闭网络工具

如需在不从智能体中移除工具的情况下关闭某个工具，按 **F10** 打开“设置”窗口，选择“工具”标签页，在“网络搜索”区域将“网络搜索模式”或“网页读取模式”设为 `off`。“网络搜索模式”默认为 `auto`，“网页读取模式”默认为 `on`，因此包含该工具的智能体可以直接使用。

这两项设置只作用于配置文件未指定模式的智能体。智能体配置文件中的模式优先，因此可以只为某个智能体关闭工具；配置文件设为 `on` 时，即使上述设置为 `off`，该智能体仍保留该工具：

```yaml
tools:
  web_fetch:
    mode: off
```

“设置”中的修改在关闭窗口后生效；智能体工具配置的修改从后续请求开始生效。

## 网络请求的去向

启用 `web_search` 且未配置搜索提供方时，默认的 `auto` 模式会将搜索请求发送到 **Exa 的公开匿名搜索端点 `https://mcp.exa.ai/mcp`**。Exa 是第三方搜索服务，无需 API 密钥或账号。iCode 只发送智能体要搜索的内容和所需结果数量，不会发送你的文件或对话本身；但搜索词由智能体撰写，可能包含任务中的细节。该服务有免费额度的速率限制，也可能无法访问。如需将搜索请求发往其他服务，参阅[选择搜索提供方](#选择搜索提供方)；如需停止搜索，将模式设为 `off`。

`web_fetch` 直接向托管该 URL 的网站发起请求；如果设置了[网络代理](#控制网络访问)，则经由该代理。请求使用 `Chrys-Web/1.0` 作为 User-Agent，不会发送 Cookie 或已保存的凭据。

搜索结果和读取的页面会作为工具结果返回给智能体，因此也会随对话的其余内容一起发送给模型提供方。只有智能体调用工具时才会发出网络请求，启动时不会发送。

## 审批网络工具调用

`web_search` 和 `web_fetch` 两个类别默认需要审批。这条默认规则在所有显式的 `approval.overrides` 规则之后、配置文件的 `approval.default` 之前生效，因此智能体设置 `default: auto` 并不会取消它。与其他工具一样，当前审批模式决定如何处理请求：

- “手动”模式下，每次网络工具调用都会弹出“工具审批”对话框。
- “自动”模式下，审批裁判模型可以批准调用。
- “绕过”模式下（包括无界面命令行 `icode run`），网络工具调用不经询问直接执行。

如果不希望每次搜索都询问，可以在智能体配置文件中设置覆盖项，例如 `approval.overrides: {web_search: auto}`。参阅[配置审批模式](./approval.md)。

## 选择搜索提供方

在智能体配置文件的 `tools.web_search` 下配置搜索提供方，字段说明见[智能体配置文件参考](../../reference/agent-profile.md#toolsweb_search)。在 `providers` 中为每个提供方取一个 ID，模式决定如何使用这些提供方：

| 模式 | 行为 |
| --- | --- |
| `auto` | 未配置 `provider`、`fallback_chain` 和 `providers` 时，使用匿名 Exa。配置了 `providers` 时，必须同时用 `fallback_chain` 按顺序列出要尝试的提供方 ID。 |
| `provider` | 只使用 `provider` 指定的一个提供方，不能设置 `fallback_chain`。 |
| `off` | 为该智能体关闭网络搜索。 |

也可以写 `true` 或 `false`：`true` 表示 `auto`（对 `web_fetch` 表示 `on`），`false` 表示 `off`。

如果组合无效，例如 `auto` 模式配置了 `providers` 却没有 `fallback_chain`，智能体仍会启动，但不包含网络工具，iCode 会显示一条写明原因的警告（参阅[网络工具没有出现时](#网络工具没有出现时)）。未知的模式或格式错误的提供方条目则会使整个配置文件无效。

### 无需密钥的搜索服务

| 类型 | 服务 |
| --- | --- |
| `exa_mcp` | Exa 公开端点，即 `auto` 默认使用的服务 |
| `bing_html` | Bing 搜索结果页 |
| `duckduckgo_html` | DuckDuckGo 的 HTML 搜索结果页 |

这些类型只接受 `type` 字段：端点固定，不接受凭据。例如，改用 DuckDuckGo 代替 Exa：

```yaml
tools:
  builtins: [filesystem.read, search, web_search, web_fetch]
  web_search:
    mode: provider
    provider: ddg
    providers:
      ddg:
        type: duckduckgo_html
```

`bing_html` 和 `duckduckgo_html` 读取的是普通搜索结果页，而不是官方 API，可用性和结果质量取决于该服务和本地网络。结果不包含广告和生成式回答。遇到验证码、同意页面或无法识别的页面布局时，会报告错误，而不会当作空结果返回。

### 需要密钥的搜索 API

| 类型 | 服务 | 常用密钥变量 |
| --- | --- | --- |
| `tavily` | Tavily Search API | `TAVILY_API_KEY` |
| `brave` | Brave Search API | `BRAVE_SEARCH_API_KEY` |
| `exa` | Exa Search API（需认证） | `EXA_API_KEY` |

将 `api_key_env` 设为保存密钥的环境变量名：

```yaml
tools:
  web_search:
    mode: auto
    fallback_chain: [primary, backup]
    providers:
      primary:
        type: tavily
        api_key_env: TAVILY_API_KEY
      backup:
        type: exa
        api_key_env: EXA_API_KEY
```

iCode 从启动 iCode 时的环境变量或用户 `.env` 文件（macOS / Linux：`~/.chrys/.env`；Windows：`%APPDATA%\chrys\.env`）读取密钥。启动环境中已设置的变量优先，即使其值为空。工作目录中的 `.env` 文件不能提供网络工具凭据。用户 `.env` 文件在每次构建智能体时读取，修改后需重新保存智能体或重启 iCode。iCode 只读取智能体实际使用的提供方所需的密钥。

### 回退到其他提供方

`auto` 模式下，iCode 按 `fallback_chain` 的顺序尝试各提供方，并返回第一个得到的结果，空结果也算。出现以下情况时，iCode 会改用下一个提供方：

- 该提供方用完了分给它的时间（参阅[时间限制与重试](#时间限制与重试)）。
- 连接错误、HTTP 5xx、408 或 429 在重试一次后仍然出现。
- 无法连接提供方：其域名无法解析、解析到私有地址，或者连接因重试也无法解决的原因被拒绝（例如证书被代理拒绝）。
- 请求对该提供方来说过大，例如搜索词过长，放进 URL 后超出长度限制。
- 提供方返回重定向、验证码或同意页面，或者其响应过大、使用不支持的压缩方式或无法读取。
- `exa_mcp`、`bing_html` 或 `duckduckgo_html` 返回任意 HTTP 4xx 错误，或 `exa_mcp` 报告搜索失败（例如触发了免费版的频率限制）。`exa_mcp` 返回空结果属于正常回答，回退链会就此停止。

如果看到 `Error: exa_rate_limited`，说明 Exa 报告匿名搜索已触发限流。iCode 不会立即重试这类错误；如果回退链中还有提供方，会继续尝试下一个。当 Exa 是最后一个提供方时（默认配置也属于这种情况），错误提示会建议稍后再试，或配置[需要密钥的搜索 API](#需要密钥的搜索-api)。

对于需要密钥的提供方和自定义提供方，HTTP 4xx 错误通常说明配置有误（例如密钥错误），因此回退链会停止，智能体收到带状态码的错误，例如 `Error: http_4xx (HTTP 403)`。由请求本身引起的错误（例如搜索词为空）也会停止回退链。iCode 不会暗中回退：只尝试你列出的提供方。

### 使用自己的搜索端点

`custom_http` 类型可以连接任何返回 JSON 的 HTTP 搜索服务。在配置文件中映射请求和响应：

```yaml
tools:
  builtins: [web_search]
  web_search:
    mode: provider
    provider: corporate
    providers:
      corporate:
        type: custom_http
        endpoint: https://search.example.com/v1/search
        method: POST
        auth:
          location: header
          name: Authorization
          prefix: "Bearer "
          key_env: CORP_SEARCH_API_KEY
        request:
          json:
            query: {$input: query}
            limit: {$input: limit}
        response:
          results_pointer: /data/items
          url_pointer: /link
          title_pointer: /title
          snippet_pointer: /summary
```

自定义端点还需要在用户设置中授权。授权项写明确切的端点 URL，以及该提供方可以读取的每个环境变量。可以在“设置”的“自定义搜索端点”中添加，也可以写在用户 `settings.yaml` 中：

```yaml
tools:
  web_search:
    custom_endpoints:
      - url: https://search.example.com/v1/search
        credential_env_names: [CORP_SEARCH_API_KEY]
```

映射规则：

- `{$input: NAME}` 插入搜索参数：`query`、`limit`、`allowed_domains` 或 `blocked_domains`。`{$env: NAME}` 插入环境变量，该变量也必须列在授权项中。每个标记替换一个完整的值，并保留其类型。
- `method` 为 `GET` 或 `POST`。`GET` 发送 `request.query_params`，列表会展开为重复的参数；`POST` 还可以发送 `request.json` 请求体。
- `auth.location` 为 `header`、`query` 或 `none`。密钥只能放在 `auth` 或 `$env` 标记中，不能直接写在请求头里。
- `*_pointer` 字段是 JSON Pointer，例如 `/data/items`。`results_pointer` 和 `url_pointer` 必填。找不到结果容器时，搜索会失败，而不是返回空结果。

以下三个预设可以为常见服务自动填写映射：

- SearXNG：`type: custom_http`、`preset: searxng`，并将 `endpoint` 设为你的实例地址，例如 `https://search.example.com/search`。SearXNG 服务器需要启用 JSON 输出。
- SerpAPI：`type: custom_http` 和 `preset: serpapi`。授权 `https://serpapi.com/search`，并设置 `credential_env_names: [SERPAPI_API_KEY]`。
- [Serply](https://serply.io)：`type: custom_http` 和 `preset: serply`。授权 `https://api.serply.io/v1/search`，并设置 `credential_env_names: [SERPLY_API_KEY]`。Serply 每次搜索最多返回 10 条结果（[文档](https://serply.io/docs)）。

## 控制网络访问

网络工具的网络访问只能在用户设置中配置。项目设置和智能体配置文件都不能修改它，因此打开的代码仓库无法扩大工具可访问的范围。常用的搜索选项位于“设置”的“工具”标签页；网页读取相关的列表需在用户 `settings.yaml` 中设置。各设置项的说明参阅[设置文件参考](../../reference/settings.md#网络工具)。

```yaml
tools:
  web_egress:
    proxy_url: ""        # 仅供网络工具使用的 HTTP(S) 代理
    proxy_dns: local     # remote：由代理解析主机名
  web_search:
    private_origins: []
    http_origins: []
    custom_endpoints: []
  web_fetch:
    denied_origins: []   # 从不读取这些来源，始终优先
    allowed_origins: []  # 非空时，只读取这些来源
    private_origins: []  # 即使解析到私有地址也允许
    http_origins: []     # 允许对这些来源使用明文 http://
```

列表条目是来源（origin），即协议、主机和可选端口，例如 `https://docs.example.com` 或 `http://127.0.0.1:8080`。条目不能包含路径、查询参数、凭据或通配符。默认端口和主机名大小写会被规范化。在“设置”窗口中，列表以 JSON 数组形式输入，例如 `["https://docs.example.com"]`，保存前会按工具使用的同一套规则检查。

规则如下：

- **仅限 HTTPS**：除非来源列在 `http_origins` 中，否则拒绝明文 `http://`。
- **仅限公网地址**：连接前，iCode 解析主机名，并拒绝私有、回环、链路本地等非公网地址，除非来源列在 `private_origins` 中。随后 iCode 连接已检查过的地址，同时在请求和 TLS 证书校验中保留原始主机名。“网络代理 DNS”设为 `remote` 时改由代理解析主机名，参阅[使用 fake-IP 代理](#使用-fake-ip-代理)。
- **网页读取列表**：`denied_origins` 始终优先。`allowed_origins` 非空时，`web_fetch` 只从这些来源读取。
- **搜索和读取分开授权**：为搜索设置的例外不会让 `web_fetch` 访问该来源，反之亦然。
- **代理**：网络工具忽略 `HTTP_PROXY`、`HTTPS_PROXY` 等代理变量。如需使用代理，将“网络代理地址”（`tools.web_egress.proxy_url`）设为 `http://` 或 `https://` 代理地址，不含凭据和路径。该代理只用于网络工具。“网络代理 DNS”为默认的 `local` 时，iCode 经代理只连接网站的 IPv4 地址，因此只有 IPv6 地址的网站会返回 `proxy_ipv6_unsupported` 错误。设为 `remote` 时由代理解析主机名，代理可以通过 IPv6 访问这类网站。无论哪种模式，直接写出 IPv6 地址的 URL（例如 `https://[2001:db8::1]/`）都无法通过代理访问。环境中的证书设置（例如 `SSL_CERT_FILE`）仍然有效。
- 工具 URL、来源列表和代理地址中的主机名都可以包含下划线，例如 `http://search_box:8080`。

例如，要使用位于 `http://127.0.0.1:8080/search` 的 SearXNG 实例，需要在 `custom_endpoints` 中授权该端点，并将 `http://127.0.0.1:8080` 同时加入 `tools.web_search.private_origins` 和 `tools.web_search.http_origins`。

### 使用 fake-IP 代理

部分代理（例如处于 TUN 或 fake-IP 模式的 Clash、Surge）会用 `198.18.0.0/15` 中的占位地址回答 DNS 查询，再由代理自己连接真实网站。这些地址不是公网地址，因此所有网络请求都会返回 `private_address_blocked` 错误，错误中附带指向本设置的提示。要使用这类代理：

1. 将“网络代理地址”（`tools.web_egress.proxy_url`）设为代理的 HTTP 端口，例如 `http://127.0.0.1:7890`。
2. 将“网络代理 DNS”（`tools.web_egress.proxy_dns`）设为 `remote`。

```yaml
tools:
  web_egress:
    proxy_url: http://127.0.0.1:7890
    proxy_dns: remote
```

设为 `remote` 后，iCode 把主机名交给代理，最终连接哪个地址由代理决定。对主机名拦截私有地址和其他非公网目标因此成为代理的职责，而不再由 iCode 负责。其他规则仍然有效：仅限 HTTPS、来源列表、重定向检查，以及按主机名进行的 TLS 证书校验。直接写成非公网地址的 URL（例如 `https://127.0.0.1` 或 `https://localhost`）仍会被拒绝，除非来源列在 `private_origins` 中。URL 中直接写出的 IPv6 地址仍然无法通过代理访问，会返回 `proxy_ipv6_unsupported` 错误。

iCode 不会自行切换到 `remote`。把 fake-IP 地址段加入 `private_origins` 也不能替代这一设置：列表只接受单个来源，而放行整个地址段还会让解析到你自己网络的主机名一并通过。

## 按域名筛选搜索结果

智能体可以用 `allowed_domains` 限定搜索范围，或用 `blocked_domains` 排除域名，但两者不能同时使用。域名同时涵盖其子域名。iCode 始终自行检查结果，提供方能提供多少帮助则因提供方而异：

- Tavily 和 Exa API 自行按域名筛选。
- Exa 公开端点不支持筛选，因此 iCode 向其请求更多结果，只保留匹配的结果。
- 其他提供方会在搜索词中收到 `site:` 和 `-site:` 条件。

筛选移除了结果时，搜索输出会报告移除的数量。

## 时间限制与重试

| 设置 | 默认值 | 含义 |
| --- | --- | --- |
| 搜索 `timeout_seconds` | 30 秒 | 整个搜索调用的截止时间，包括等待空闲连接名额的时间 |
| 读取 `timeout_seconds` | 60 秒 | 整个网页读取调用的截止时间 |

在一次搜索中，剩余的每个提供方平分剩下的时间。临时故障（连接错误，或 HTTP 5xx、408、429）会在该份时间内重试一次。用完时间的提供方会把搜索交给 `fallback_chain` 中的下一个提供方。

可以在用户 `settings.yaml` 中设置这些限制（`tools.web_search.timeout_seconds` 和 `tools.web_fetch.timeout_seconds`，1～120 秒），也可以在配置文件的 `tools.web_search` 和 `tools.web_fetch` 下为单个智能体设置。

## 网页读取与搜索结果的限制

- `web_fetch` 用 GET 读取一个 URL，接受文本、HTML、JSON 和 XML。HTML 在移除脚本、样式、表单和隐藏模板后转换为 Markdown。PDF、图片及其他二进制文件会被拒绝，错误响应的正文不会返回。
- 智能体传给 `web_fetch` 的 `prompt` 会显示给你，并作为关注重点与页面一起提供给智能体。不会有第二个模型读取页面。
- 同站重定向（协议和端口相同，仅多或少 `www.` 前缀）最多跟随 2 次，每次都按网络规则检查。重定向到其他网站时，iCode 会把新 URL 报告给智能体，由智能体显式读取。
- 响应大小限制为 2 MiB，传输大小和解压后大小都受此限制。网页读取以及 Bing 和 DuckDuckGo 适配器只接受 gzip 压缩或未压缩的响应。
- 读取的页面默认最多返回 16,000 个词元（`tools.web_fetch.max_tokens`，最多 64,000）。智能体可以要求更少，但不能更多。搜索输出限制在约 8,000 个词元。
- 读取过的页面在 15 分钟内会被复用（最多 64 个页面、16 MiB）；仅 `#fragment` 不同的 URL 共用一份。
- 网络搜索和网页读取在每个智能体中共享 3 个并发连接；一轮对话中，智能体最多搜索 20 次。
- iCode 不执行 JavaScript，不读取 `robots.txt`，也不对页面做摘要。可以用 `denied_origins` 阻止 `web_fetch` 访问特定来源。每个条目只精确匹配一个来源，不包括子域名或 `www.` 变体，需要的每一个都要单独列出。

搜索结果和页面文本来自外部来源，iCode 会向智能体标明它们是不可信的数据；使用重要信息前，应核对来源链接。

## 由提供方运行的搜索

部分模型服务可以自行运行网络搜索。模型配置在 `chat_options` 中声明此类工具时（例如 `{"tools": [{"type": "web_search"}]}`），提供方的工具会占用该名称。如果智能体也包含对应的 iCode 工具，本地工具会在该次运行中停用，iCode 会显示一次提示。该规则按名称生效：由提供方运行的 `web_search` 不影响本地的 `web_fetch`。

由提供方运行的搜索在模型提供方一侧执行，因此不受 iCode 审批规则和网络设置约束。如需改用 iCode 自己的工具，请从模型的 `chat_options` 中删除该声明。

## 网络工具没有出现时

如果 iCode 无法为某个智能体准备网络工具（例如缺少密钥变量，或自定义端点未授权），该智能体仍会启动，只是不包含网络工具，iCode 会显示一条警告，写明智能体名称和原因。修正配置后，重新保存智能体或重启 iCode。

另外请检查：

- 智能体的 `tools.builtins` 包含 `web_search` 或 `web_fetch`。
- 工具的模式不是 `off`：即配置文件中的 `tools.web_search.mode` 或 `tools.web_fetch.mode`；配置文件未设置时，为“设置”中的“网络搜索模式”或“网页读取模式”。
- 模型配置没有声明同名的、由提供方运行的网络工具。

如需查看已加载的工具，在输入栏中输入 `/runtime`；参阅[工具类别和名称](../../reference/tool-kinds-and-names.md)。
