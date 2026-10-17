# Configure web tools

The web tools let an agent look things up on the internet: `web_search` finds pages and returns their URLs and summaries, and `web_fetch` reads the text of a page at a known URL. This guide explains how to turn them on for an agent, where their requests go, and how to choose a search provider and limit network access.

**Web tools are off by default.** No agent shipped with iCode (Code, QA, Explore, or General) includes them, so iCode sends no search queries and fetches no pages until you enable the tools for an agent.

`web_search` is separate from the "File search" tools (`grep` and `glob`), which only search files on your machine.

## Turn on web tools

### Add the tools to an agent

Enter `/agents tools` in the input field, select the agent, check **Web search**, **Web fetch**, or both, and click "Save". Both are available straight away. Web search sends its queries to Exa (exa.ai) unless you choose another provider, so read [Where web requests go](#where-web-requests-go) before you turn it on.

To do the same in an agent profile, add the tool kinds to `tools.builtins`:

```yaml
tools:
  builtins: [filesystem.read, search, web_search, web_fetch]
```

Web tools belong to each agent separately. A sub-agent such as General gets them only if its own profile includes them. To add them to a built-in agent, edit and save it in the TUI; iCode then saves your own copy of that agent (see [Override built-in agents](../../reference/agent-profile.md#override-built-in-agents)).

### Turn a web tool off

To turn a tool off without removing it from your agents, press **F10** to open **Settings**, select the **Tools** tab, and set **Web search mode** or **Web fetch mode** to `off` in the **Web search** section. **Web search mode** defaults to `auto` and **Web fetch mode** to `on`, so an agent that includes a tool can use it.

These settings apply only to agents whose profile does not set a mode. A mode set in an agent profile takes precedence, so a profile can turn a tool off for that agent alone, and a profile that sets `on` keeps the tool even when the setting is `off`:

```yaml
tools:
  web_fetch:
    mode: off
```

Changes in **Settings** apply when you close the dialog. Changes to an agent's tools apply from the next request.

## Where web requests go

With `web_search` turned on and no provider configured, the default mode `auto` sends your search queries to **Exa's public, anonymous search endpoint at `https://mcp.exa.ai/mcp`**. Exa is a third-party search service. No API key or account is needed. iCode sends only what the agent searches for and how many results it wants, not your files or the conversation itself; the agent writes the queries, so they can include details from your task. The service has free-tier rate limits and can be unavailable. To send queries somewhere else, [choose a search provider](#choose-a-search-provider); to stop searching, set the mode to `off`.

`web_fetch` requests each URL directly from the website that hosts it, or through the [web proxy](#control-network-access) if you set one. Requests identify themselves with the user agent `Chrys-Web/1.0` and never send cookies or stored credentials.

Search results and fetched pages are returned to the agent as tool results, so they are also sent to the model provider along with the rest of the conversation. Requests are made only when the agent calls a tool, never at startup.

## Approve web tool calls

The `web_search` and `web_fetch` kinds require approval by default. This default applies after any explicit `approval.overrides` rules and before the profile's `approval.default`, so an agent's `default: auto` does not remove it. The current approval mode decides how the request is handled, as for any other tool:

- In manual mode, each web tool call opens the **Approval Required** dialog.
- In automatic mode, the approval judge model may approve the call.
- In bypass mode, including the headless CLI `icode run`, web tool calls run without asking.

To stop being asked about searches, set an override in the agent profile, for example `approval.overrides: {web_search: auto}`. See [Configure approval modes](./approval.md).

## Choose a search provider

Configure search providers in the agent profile under `tools.web_search`. The fields are listed in the [agent profile reference](../../reference/agent-profile.md#toolsweb_search). Each provider has an ID of your choice in `providers`, and the mode decides how providers are used:

| Mode | Behavior |
| --- | --- |
| `auto` | With no `provider`, `fallback_chain`, or `providers`, uses anonymous Exa. With a `providers` map, you must also set `fallback_chain` to the provider IDs to try, in order. |
| `provider` | Uses exactly one provider, named in `provider`. `fallback_chain` is not allowed. |
| `off` | Turns web search off for this agent. |

`true` and `false` are also accepted: `true` means `auto` (`on` for `web_fetch`) and `false` means `off`.

If the combination is invalid, such as `auto` with a `providers` map but no `fallback_chain`, the agent starts without its web tools and iCode shows a warning with the reason (see [When web tools do not appear](#when-web-tools-do-not-appear)). An unknown mode or a malformed provider entry makes the whole profile invalid instead.

### Search services that need no key

| Type | Service |
| --- | --- |
| `exa_mcp` | Exa's public endpoint, the same service `auto` uses by default |
| `bing_html` | Bing search result pages |
| `duckduckgo_html` | DuckDuckGo's HTML search result pages |

These types accept only `type`: the endpoints are fixed and take no credentials. For example, to use DuckDuckGo instead of Exa:

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

`bing_html` and `duckduckgo_html` read ordinary search result pages rather than an official API, so availability and result quality depend on the service and your network. Advertising and generated answers are left out. A captcha, consent page, or unfamiliar page layout is reported as an error, never as an empty result.

### Search APIs with a key

| Type | Service | Typical key variable |
| --- | --- | --- |
| `tavily` | Tavily Search API | `TAVILY_API_KEY` |
| `brave` | Brave Search API | `BRAVE_SEARCH_API_KEY` |
| `exa` | Exa Search API (authenticated) | `EXA_API_KEY` |

Set `api_key_env` to the name of the environment variable that holds the key:

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

iCode reads the key from the environment that started iCode or from your user `.env` file (macOS / Linux: `~/.chrys/.env`; Windows: `%APPDATA%\chrys\.env`). A variable set in the starting environment takes precedence, even when it is empty. A `.env` file in the working directory cannot provide web credentials. The user `.env` file is read each time the agent is built, so after changing it, save the agent again or restart iCode. Only the keys of the providers the agent actually uses are read.

### Fall back to another provider

In `auto` mode, iCode tries the providers in `fallback_chain` in order and returns the first answer, including an empty one. It moves on to the next provider when:

- The provider runs out of its share of the time limit (see [Time limits and retries](#time-limits-and-retries)).
- A connection error, HTTP 5xx, 408, or 429 persists after one retry.
- The provider cannot be reached: its name cannot be resolved, it resolves to a private address, or the connection is refused in a way a retry cannot fix (for example, a certificate rejected by a proxy).
- The request is too large for the provider, such as a long query that no longer fits in its URL.
- The provider redirects, returns a captcha or consent page, or sends a response that is too large, uses an unsupported compression, or cannot be read.
- `exa_mcp`, `bing_html`, or `duckduckgo_html` returns any HTTP 4xx error, or `exa_mcp` reports that the search failed (for example, its free rate limit was reached). An empty result from `exa_mcp` is an answer, so the chain stops there.

If you see `Error: exa_rate_limited`, Exa has reported that its anonymous search limit was reached. iCode does not immediately retry this error; it tries the next provider in your fallback chain, if any. When Exa is the last provider, including the default setup, the error suggests trying again later or configuring a [search API with a key](#search-apis-with-a-key).

For keyed and custom providers, an HTTP 4xx error usually means a configuration problem, such as a wrong key, so the chain stops and the agent receives the error with its status, for example `Error: http_4xx (HTTP 403)`. Errors caused by the request itself, such as an empty query, also stop the chain. There is no hidden fallback: iCode tries only the providers you list.

### Use your own search endpoint

The `custom_http` type connects to any HTTP search service that returns JSON. Map the request and response in the profile:

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

A custom endpoint also needs a grant in your user settings. The grant names the exact endpoint URL and every environment variable the provider may read. Add it in **Settings** under **Custom search endpoints**, or in the user `settings.yaml`:

```yaml
tools:
  web_search:
    custom_endpoints:
      - url: https://search.example.com/v1/search
        credential_env_names: [CORP_SEARCH_API_KEY]
```

Mapping rules:

- `{$input: NAME}` inserts a value from the search: `query`, `limit`, `allowed_domains`, or `blocked_domains`. `{$env: NAME}` inserts an environment variable, which must also be listed in the grant. Each marker replaces a whole value and keeps its type.
- `method` is `GET` or `POST`. `GET` sends `request.query_params`, with lists becoming repeated parameters. `POST` can also send a `request.json` body.
- `auth.location` is `header`, `query`, or `none`. Put keys only in `auth` or `$env` markers, never directly in headers.
- The `*_pointer` fields are JSON Pointers, such as `/data/items`. `results_pointer` and `url_pointer` are required. If the results container is missing, the search fails instead of returning nothing.

Three presets fill in the mapping for common services:

- SearXNG: `type: custom_http`, `preset: searxng`, and your instance's `endpoint`, such as `https://search.example.com/search`. JSON output must be enabled on the SearXNG server.
- SerpAPI: `type: custom_http` and `preset: serpapi`. Grant `https://serpapi.com/search` with `credential_env_names: [SERPAPI_API_KEY]`.
- [Serply](https://serply.io): `type: custom_http` and `preset: serply`. Grant `https://api.serply.io/v1/search` with `credential_env_names: [SERPLY_API_KEY]`. Serply returns at most 10 results per search ([docs](https://serply.io/docs)).

## Control network access

Network access for web tools is set only in your user settings. Project settings and agent profiles cannot change it, so a repository you open cannot widen what the tools may reach. The common search options are on the **Tools** tab of **Settings**; the fetch lists are set in the user `settings.yaml`. For every key, see the [settings file reference](../../reference/settings.md#web-tools).

```yaml
tools:
  web_egress:
    proxy_url: ""        # an explicit HTTP(S) proxy for web tools only
    proxy_dns: local     # remote: the proxy resolves host names
  web_search:
    private_origins: []
    http_origins: []
    custom_endpoints: []
  web_fetch:
    denied_origins: []   # never fetch these; this always wins
    allowed_origins: []  # when not empty, fetch only these
    private_origins: []  # allow these even if they resolve to a private address
    http_origins: []     # allow plain http:// for these
```

List entries are origins: a scheme, host, and optional port, such as `https://docs.example.com` or `http://127.0.0.1:8080`. They cannot include paths, queries, credentials, or wildcards. Default ports and upper-case host names are normalized. In **Settings**, lists are entered as JSON arrays, such as `["https://docs.example.com"]`, and are checked with the same rules the tools use before they are saved.

The rules work as follows:

- **HTTPS only**: plain `http://` is refused unless the origin is in `http_origins`.
- **Public addresses only**: before connecting, iCode resolves the host name and refuses private, loopback, link-local, and other non-public addresses unless the origin is in `private_origins`. iCode then connects to the address it checked, while keeping the original host name for the request and TLS certificate checks. With **Web proxy DNS** set to `remote`, the proxy resolves the name instead; see [Use a fake-IP proxy](#use-a-fake-ip-proxy).
- **Fetch lists**: `denied_origins` always wins. When `allowed_origins` is not empty, `web_fetch` reads only from those origins.
- **Search and fetch are separate**: an exemption for search never lets `web_fetch` reach that origin, and the reverse.
- **Proxy**: web tools ignore proxy variables such as `HTTP_PROXY` and `HTTPS_PROXY`. To use a proxy, set **Web proxy URL** (`tools.web_egress.proxy_url`) to an `http://` or `https://` proxy address without credentials or a path. It applies only to web tools. With **Web proxy DNS** at its default `local`, iCode reaches a site through the proxy at its IPv4 addresses only, so a site that has only IPv6 addresses fails with `proxy_ipv6_unsupported`. With `remote`, the proxy resolves the name and may reach such a site over IPv6. In either mode, a URL that names an IPv6 address directly, such as `https://[2001:db8::1]/`, cannot go through the proxy. Certificate settings from the environment, such as `SSL_CERT_FILE`, are still honored.
- Host names may contain underscores, such as `http://search_box:8080`, in tool URLs, origin lists, and the proxy URL.

For example, to use a SearXNG instance at `http://127.0.0.1:8080/search`, grant that endpoint in `custom_endpoints` and add `http://127.0.0.1:8080` to both `tools.web_search.private_origins` and `tools.web_search.http_origins`.

### Use a fake-IP proxy

Some proxies, such as Clash or Surge in TUN or fake-IP mode, answer DNS lookups with placeholder addresses in `198.18.0.0/15` and connect to the real site themselves. Those addresses are not public, so every web request fails with `private_address_blocked`, and the error adds a hint that names this setting. To use such a proxy:

1. Set **Web proxy URL** (`tools.web_egress.proxy_url`) to the proxy's HTTP port, such as `http://127.0.0.1:7890`.
2. Set **Web proxy DNS** (`tools.web_egress.proxy_dns`) to `remote`.

```yaml
tools:
  web_egress:
    proxy_url: http://127.0.0.1:7890
    proxy_dns: remote
```

With `remote`, iCode sends the host name to the proxy, and the proxy decides which address it finally connects to. Blocking private and other non-public destinations for a name is then the proxy's job, not iCode's. iCode still applies every other rule: HTTPS only, the origin lists, redirect checks, and TLS certificate checks against the host name. It also still refuses URLs that name a non-public address directly, such as `https://127.0.0.1` or `https://localhost`, unless the origin is in `private_origins`. An IPv6 address written in a URL still cannot go through a proxy and fails with `proxy_ipv6_unsupported`.

iCode never switches to `remote` on its own. Adding the fake-IP range to `private_origins` is not a substitute: the lists hold single origins, and exempting the range would also let names that resolve to your own network through.

## Filter search results by domain

The agent can limit a search to `allowed_domains` or exclude `blocked_domains`, but not both at once. A domain also covers its subdomains. iCode always checks the results itself, and how much the provider helps depends on the provider:

- Tavily and the Exa API filter by domain themselves.
- Exa's public endpoint cannot filter, so iCode asks it for more results and keeps only the matching ones.
- Other providers receive the filter as `site:` and `-site:` terms in the query.

When the filter removes results, the search output reports how many.

## Time limits and retries

| Setting | Default | Meaning |
| --- | --- | --- |
| Search `timeout_seconds` | 30 seconds | Deadline for the whole search call, including time spent waiting for a free connection slot |
| Fetch `timeout_seconds` | 60 seconds | Deadline for the whole fetch call |

Within a search, each remaining provider gets an equal share of the time left. A transient failure (a connection error, or HTTP 5xx, 408, or 429) is retried once within that share. A provider that runs out of time passes the search to the next provider in `fallback_chain`.

Set these limits in the user `settings.yaml` (`tools.web_search.timeout_seconds` and `tools.web_fetch.timeout_seconds`, 1 to 120 seconds), or per agent under `tools.web_search` and `tools.web_fetch` in the profile.

## Limits of fetched pages and search results

- `web_fetch` reads one URL with GET. It accepts text, HTML, JSON, and XML. HTML is converted to Markdown after scripts, styles, forms, and hidden templates are removed. PDFs, images, and other binary files are refused, and the body of an error response is never returned.
- The `prompt` the agent passes to `web_fetch` is shown to you and included with the page as a focus hint. No second model reads the page.
- Same-site redirects (same scheme and port, with or without a `www.` prefix) are followed for up to 2 hops, each checked against the network rules. A redirect to another site is reported to the agent with the new URL, which it must fetch explicitly.
- Responses are limited to 2 MiB, both as sent and after decompression. Web fetch and the Bing and DuckDuckGo adapters accept only gzip or uncompressed responses.
- A fetched page returns at most 16,000 tokens by default (`tools.web_fetch.max_tokens`, up to 64,000). The agent can ask for less, but not more. Search output is limited to about 8,000 tokens.
- Fetched pages are reused for 15 minutes (up to 64 pages and 16 MiB), and URLs that differ only in the `#fragment` share one copy.
- Web search and web fetch share 3 simultaneous connections per agent, and an agent can make at most 20 searches in one turn.
- iCode does not run JavaScript, does not read `robots.txt`, and does not summarize pages. Use `denied_origins` to keep `web_fetch` away from specific origins. Each entry matches exactly one origin: it does not cover subdomains or the `www.` variant, so list each one you mean.

Search results and page text come from outside sources. iCode marks them as untrusted data for the agent; check the source links before relying on important claims.

## Provider-run search

Some model services can run web search themselves. When a model profile declares such a tool in its `chat_options`, for example `{"tools": [{"type": "web_search"}]}`, the provider's tool takes over that name. If the agent also includes the matching iCode tool, the local tool is turned off for that run and iCode shows a notice once. This works per name: a provider-run `web_search` leaves a local `web_fetch` in place.

Provider-run search happens on the model provider's side, so iCode's approval rules and network settings do not apply to it. To use iCode's own tools instead, remove the declaration from the model's `chat_options`.

## When web tools do not appear

If iCode cannot set up an agent's web tools, for example because a key variable is missing or a custom endpoint is not granted, the agent still starts without them and iCode shows a warning that names the agent and the reason. Fix the configuration, then save the agent again or restart iCode.

Also check that:

- The agent includes `web_search` or `web_fetch` in `tools.builtins`.
- The tool's mode is not `off`: the profile's `tools.web_search.mode` or `tools.web_fetch.mode`, or, when the profile sets none, **Web search mode** or **Web fetch mode** in **Settings**.
- The model profile does not declare a provider-run web tool with the same name.

To see which tools are loaded, enter `/runtime` in the input field; see [Tool kinds and names](../../reference/tool-kinds-and-names.md).
