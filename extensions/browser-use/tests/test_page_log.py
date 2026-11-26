# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from icode_browser_use.actions import logs
from icode_browser_use.config import BrowserConfig
from icode_browser_use.page_log import MAX_ENTRIES, PageLog, scrub
from icode_browser_use.session import BrowserSession


def _console(kind, *args):
    return {"type": kind, "args": list(args)}


def test_console_messages_are_levelled_formatted_and_redacted():
    log = PageLog()
    log.console("t", _console("log", {"type": "string", "value": "hello"}, {"type": "number", "value": 3}))
    log.console("t", _console("warning", {"type": "object", "description": "Object"}))
    log.console("t", _console("error", {"type": "string", "value": "token=abc123 failed\nnext line"}))
    entries = log.entries("t")
    assert [(e.level, scrub(e.text)) for e in entries] == [
        ("info", "console.log: hello 3"),
        ("warning", "console.warning: Object"),
        ("error", "console.error: token=*** failed next line"),
    ]


def test_urls_and_compound_secret_keys_in_text_are_scrubbed():
    page = PageLog()
    for value in (
        "callback failed https://idp.example/cb?code=S1&state=s2#frag",
        "auth access_token=S2 refresh_token: S3 X-Amz-Signature=S4",
        "Authorization: Bearer S5",
        "status code: 404 for user 7",
    ):
        page.console("t", _console("error", {"type": "string", "value": value}))
    page.exception(
        "t",
        {"exceptionDetails": {"exception": {"description": "Error: x\n at f (https://cdn.example/a.js?sig=S6:1:2)"}}},
    )
    texts = [scrub(entry.text) for entry in page.entries("t")]
    assert not any(secret in text for text in texts for secret in ("S1", "S2", "S3", "S4", "S5", "S6"))
    assert texts[0] == "console.error: callback failed https://idp.example/cb?…"
    assert texts[1] == "console.error: auth access_token=*** refresh_token=*** X-Amz-Signature=***"
    assert texts[3] == "console.error: status code: 404 for user 7"
    assert "https://cdn.example/a.js?…" in texts[4]


def test_uncaught_exception_keeps_message_and_top_of_stack():
    page = PageLog()
    stack = "TypeError: x is undefined\n    at a (app.js:1)\n    at b (app.js:2)\n    at c\n    at d\n    at e"
    page.exception("t", {"exceptionDetails": {"text": "Uncaught", "exception": {"description": stack}}})
    (entry,) = page.entries("t")
    assert entry.level == "error"
    assert scrub(entry.text).startswith("uncaught: TypeError: x is undefined | at a (app.js:1)")
    assert "at d" not in scrub(entry.text)


def test_network_keeps_api_calls_http_errors_and_real_failures():
    log = PageLog()
    for request_id, method, url in (
        ("1", "GET", "https://example.com/app.css"),
        ("2", "POST", "https://example.com/api/items?token=x"),
        ("3", "GET", "https://example.com/missing.png"),
        ("4", "GET", "https://example.com/slow"),
        ("5", "GET", "https://ads.test/x.js"),
    ):
        log.request("t", {"requestId": request_id, "request": {"method": method, "url": url}})
    log.response(
        "t", {"requestId": "1", "type": "Stylesheet", "response": {"status": 200, "url": "https://example.com/app.css"}}
    )
    log.response(
        "t",
        {
            "requestId": "2",
            "type": "Fetch",
            "response": {"status": 201, "url": "https://example.com/api/items?token=x"},
        },
    )
    log.response(
        "t", {"requestId": "3", "type": "Image", "response": {"status": 404, "url": "https://example.com/missing.png"}}
    )
    log.failed("t", {"requestId": "4", "errorText": "net::ERR_ABORTED", "canceled": True})
    log.failed("t", {"requestId": "5", "errorText": "net::ERR_FAILED", "blockedReason": "inspector"})
    assert [(e.level, e.text, e.url) for e in log.entries("t")] == [
        ("info", "POST 201", "https://example.com/api/items"),
        ("warning", "GET 404", "https://example.com/missing.png"),
        (
            "error",
            "GET FAILED blocked by the allow-list (ICODE_BROWSER_ALLOWED_DOMAINS/ORIGINS)",
            "https://ads.test/x.js",
        ),
    ]
    assert [e.text for e in log.entries("t", errors_only=True)] == [
        "GET 404",
        "GET FAILED blocked by the allow-list (ICODE_BROWSER_ALLOWED_DOMAINS/ORIGINS)",
    ]
    assert [e.kind for e in log.entries("t", kind="console")] == []


def test_entries_are_per_tab_cursor_based_and_bounded():
    log = PageLog()
    log.console("a", _console("log", {"type": "string", "value": "first"}))
    log.console("b", _console("log", {"type": "string", "value": "other tab"}))
    cursor = log.seq
    log.console("a", _console("log", {"type": "string", "value": "second"}))
    assert [e.text for e in log.entries("a")] == ["console.log: first", "console.log: second"]
    assert [e.text for e in log.entries("a", since=cursor)] == ["console.log: second"]
    for index in range(MAX_ENTRIES + 5):
        log.console("a", _console("log", {"type": "string", "value": str(index)}))
    assert len(log.entries("a")) == MAX_ENTRIES
    assert log.entries("a")[-1].seq == log.seq


def test_session_records_owned_pages_only():
    async def run():
        session = BrowserSession(BrowserConfig(allowed_domains=("example.com",)))
        session._record_page("tab-1", "s-1", opener_id=None)
        session.register_oopif("frame-s", "target-if", page_target_id="tab-1")
        handler = session._log_handler(session.page_log.console)
        message = _console("error", {"type": "string", "value": "boom"})
        await handler(message, "s-1")
        await handler(message, "frame-s")
        await handler(message, "foreign-session")
        await handler(message, None)
        return session.page_log.entries("tab-1"), session.page_log.seq

    entries, seq = asyncio.run(run())
    assert len(entries) == 2 and seq == 2


def test_tool_renders_cursor_quoted_text_and_trimmed_urls():
    log = PageLog()
    log.console("tab", _console("error", {"type": "string", "value": 'say "hi"'}))
    log.request("tab", {"requestId": "9", "request": {"method": "GET", "url": "https://example.com/a?secret=1"}})
    log.response(
        "tab", {"requestId": "9", "type": "XHR", "response": {"status": 500, "url": "https://example.com/a?secret=1"}}
    )
    session = SimpleNamespace(page_log=log, target_id="tab")
    arguments = {"kind": "all", "errors_only": False, "since": 0, "limit": 1}
    text = asyncio.run(logs.run(session, arguments))[0]["text"]
    assert "cursor 2" in text and "since=2" in text
    assert "更早的 1 条未显示" in text
    assert '#2 network.error https://example.com/a "GET 500"' in text
    assert "secret" not in text

    text = asyncio.run(logs.run(session, {**arguments, "limit": 30, "kind": "console"}))[0]["text"]
    assert '#1 console.error "console.error: say \\"hi\\""' in text

    text = asyncio.run(logs.run(session, {**arguments, "since": 2}))[0]["text"]
    assert "没有匹配的日志" in text


def test_hostile_page_text_costs_little_per_event_and_to_read():
    import time

    hostile = (
        "ws://" * 800,
        "a." * 2000,
        "a-" * 2000,
        ("a." * 19 + "token ") * 90,
        "token" * 20000,
        "x" * 4000,
        "https://a:" * 400,
        "?a=" * 1000,
        "Bearer " * 600,
        '"' * 4000,
    )
    log = PageLog()
    started = time.perf_counter()
    for text in hostile * 20:
        log.console("t", _console("error", {"type": "string", "value": text}))
    per_event = (time.perf_counter() - started) / (len(hostile) * 20)
    assert per_event < 0.0005, f"{per_event * 1000:.2f} ms per event"
    for entry in log.entries("t")[-len(hostile) :]:
        started = time.perf_counter()
        scrub(entry.text)
        assert time.perf_counter() - started < 0.005, entry.text[:20]


def test_urls_are_stored_short_without_credentials_or_payloads():
    log = PageLog()
    cases = {
        "1": "data:image/png;base64," + "A" * 1_000_000,
        "2": "https://user:pass@example.com:8443/a/b?x=1#f",
        "3": "https://example.com/" + "p" * 5000,
    }
    for request_id, url in cases.items():
        log.request("t", {"requestId": request_id, "request": {"method": "GET", "url": url}})
    assert all(len(url) <= 300 for _method, url in log._requests.values())
    for request_id in cases:
        log.failed("t", {"requestId": request_id, "errorText": "net::ERR_FAILED"})
    urls = [entry.url for entry in log.entries("t")]
    assert urls[0] == "data:"
    assert urls[1] == "https://example.com:8443/a/b"
    assert len(urls[2]) <= 300 and urls[2].startswith("https://example.com/ppp")


def test_logs_hint_is_truthful_and_a_cursor_from_a_reset_log_is_not_silent():
    log = PageLog()
    for index in range(3):
        log.console("tab", _console("log", {"type": "string", "value": f"m{index}"}))
    session = SimpleNamespace(page_log=log, target_id="tab")
    arguments = {"kind": "all", "errors_only": False, "since": 0, "limit": 1}
    text = asyncio.run(logs.run(session, arguments))[0]["text"]
    assert "更早的 2 条未显示" in text and "since 缩小" not in text

    text = asyncio.run(logs.run(session, {**arguments, "since": 99, "limit": 30}))[0]["text"]
    assert "since=99 大于当前 cursor 3" in text
    assert "m0" in text and "m2" in text


def test_json_secrets_and_relative_url_queries_are_masked():
    cases = {
        '{"token":"abc123secret"}': "abc123secret",
        '{"access_token": "S2", "x":1}': "S2",
        '"password":"hunter2"': "hunter2",
        "fetch /api/cb?code=SECRETCODE failed": "SECRETCODE",
        "#access_token=S9&expires=1": "S9",
    }
    for text, secret in cases.items():
        assert secret not in scrub(text), text
    for benign in ("status code: 404 for user 7", "3 tokens left", "is it ok? yes"):
        assert scrub(benign) == benign


def test_finished_requests_do_not_push_out_a_slow_one():
    log = PageLog()
    log.request("t", {"requestId": "slow", "request": {"method": "POST", "url": "https://example.com/api/slow"}})
    for index in range(600):
        log.request(
            "t", {"requestId": str(index), "request": {"method": "GET", "url": f"https://example.com/i{index}.png"}}
        )
        log.response("t", {"requestId": str(index), "type": "Image", "response": {"status": 200}})
    log.response(
        "t", {"requestId": "slow", "type": "Fetch", "response": {"status": 200, "url": "https://example.com/api/slow"}}
    )
    assert [entry.text for entry in log.entries("t")] == ["POST 200"]


def test_more_credential_shapes_are_masked_and_stack_positions_survive():
    leaks = {
        "GET https://u:pw@api.example.com/x?flag&code=AUTHCODE": ("pw@", "AUTHCODE"),
        "session_id=ABCDEF sid=QQQ jwt=eyJx": ("ABCDEF", "QQQ", "eyJx"),
        "retry with Bearer eyJhbGciOi.eyJzdWIi.sig": ("eyJhbGciOi",),
        '{"password": "my secret pass"}': ("my", "secret pass"),
        '{\\"token\\":\\"esc\\"}': ("esc",),
    }
    for text, secrets in leaks.items():
        masked = scrub(text)
        assert not any(secret in masked for secret in secrets), (text, masked)
    assert scrub("at f (https://cdn.example/a.js?v=1:10:5)") == "at f (https://cdn.example/a.js?…:10:5)"
    assert scrub("https://u:pw@api.example.com/x") == "https://api.example.com/x"
    for benign in ("is it ok? yes", "consider: inside=3 outside=4", "visit https://example.com/a?b"):
        assert scrub(benign) == benign


def test_short_urls_drop_path_parameters_and_keep_ipv6_hosts():
    from icode_browser_use.page_log import _short_url

    assert _short_url("https://h/p;jsessionid=ABC?x=1") == "https://h/p"
    assert _short_url("http://[::1]:8080/a") == "http://[::1]:8080/a"
    assert _short_url("http://[zz/a") == "http:"
    assert _short_url("https://user:pw@example.com:8443/a/b?x=1") == "https://example.com:8443/a/b"


def test_a_restarted_browser_does_not_show_the_previous_run_s_logs():
    from icode_browser_use.config import BrowserConfig
    from icode_browser_use.session import BrowserSession

    session = BrowserSession(BrowserConfig(allowed_domains=("example.com",)))
    session.page_log.console("7", _console("error", {"type": "string", "value": "old run"}))
    cursor = session.page_log.seq
    asyncio.run(session._teardown_transport())
    assert session.page_log.entries("7") == []  # a new Chrome may hand out tab id 7 again
    session.page_log.console("7", _console("error", {"type": "string", "value": "new run"}))
    assert [entry.seq for entry in session.page_log.entries("7", since=cursor)] == [cursor + 1]


def test_a_cursor_older_than_the_buffer_reports_the_gap():
    log = PageLog()
    log.console("tab", _console("log", {"type": "string", "value": "first"}))
    cursor = log.seq
    for index in range(MAX_ENTRIES + 5):
        log.console("other", _console("log", {"type": "string", "value": f"noise {index}"}))
    session = SimpleNamespace(page_log=log, target_id="tab")
    text = asyncio.run(logs.run(session, {"kind": "all", "errors_only": False, "since": cursor, "limit": 30}))[0][
        "text"
    ]
    assert "已不在缓冲区" in text and f"#{log.seq - MAX_ENTRIES + 1}" in text


def test_a_reduced_document_response_is_left_to_the_full_one():
    log = PageLog()
    log.request("t", {"requestId": "9", "request": {"method": "GET", "url": "https://example.com/missing"}})
    log.response("t", {"requestId": "9", "type": "Document", "response": {"status": 404}})  # bridge status copy
    assert log.entries("t") == []
    log.response(
        "t", {"requestId": "9", "type": "Document", "response": {"status": 404, "url": "https://example.com/missing"}}
    )
    assert [(entry.text, entry.url) for entry in log.entries("t")] == [("GET 404", "https://example.com/missing")]


def _session_with_client(capabilities):
    from icode_browser_use.config import BrowserConfig
    from icode_browser_use.session import BrowserSession

    session = BrowserSession(BrowserConfig(allowed_domains=("example.com",)))
    methods = []
    client = SimpleNamespace(on=lambda method, handler: methods.append(method), closed=False)
    if capabilities is not None:
        client.capabilities = frozenset(capabilities)
    session._client = client
    session._register_handlers()
    return session, methods


def test_an_old_bridge_extension_gets_no_page_log_and_is_told_why():
    session, methods = _session_with_client({"ping"})  # an extension that does not gate page logs
    assert "Runtime.consoleAPICalled" not in methods and "Network.loadingFailed" not in methods
    assert "Network.responseReceived" in methods  # the navigation status still works
    session._target_id = "t"
    text = asyncio.run(logs.run(session, {"kind": "all", "errors_only": False, "since": 0, "limit": 30}))[0]["text"]
    assert "icode-browser-use update-bridge" in text


def test_a_gating_extension_and_direct_cdp_collect_page_logs():
    for capabilities in ({"ping", "page_log_gated"}, None):
        _session, methods = _session_with_client(capabilities)
        assert "Runtime.consoleAPICalled" in methods


def test_a_page_built_method_name_is_stored_bounded():
    log = PageLog()
    log.request("t", {"requestId": "1", "request": {"method": "M" * 100_000, "url": "https://example.com/a"}})
    assert all(len(method) <= 32 for method, _url in log._requests.values())


def test_usernames_as_tokens_and_short_secret_keys_are_masked():
    for text, secret in {
        "https://ghp_TOKEN123@github.com/x": "ghp_TOKEN123",
        "pwd=hunter2": "hunter2",
        "auth=xyz123": "xyz123",
        "Cookie: theme=dark; auth=SECRETAUTH": "SECRETAUTH",
        "session=abc123": "abc123",
    }.items():
        assert secret not in scrub(text), text
    for benign in ("author=Ann sessions=3", "https://example.com/a@b", "mail me at a@b.c"):
        assert scrub(benign) == benign


def test_tabs_downloads_and_page_log_share_url_sanitization():
    from icode_browser_use.actions._shared import display_url as tab_display_url
    from icode_browser_use.downloads import display_url as download_display_url
    from icode_browser_use.page_log import _short_url

    raw = "https://alice:topsecret@example.com:8443/files/report.pdf;jsessionid=S1?token=T1#frag"
    expected = "https://example.com:8443/files/report.pdf"
    for fn in (tab_display_url, download_display_url, _short_url):
        rendered = fn(raw)
        assert rendered == expected
        assert "alice" not in rendered and "topsecret" not in rendered
        assert "S1" not in rendered and "T1" not in rendered and "frag" not in rendered
    for fn in (tab_display_url, download_display_url, _short_url):
        assert fn("data:text/html;base64,PHNjcmlwdD5zZWNyZXQ8L3NjcmlwdD4=") == "data:"


def test_untitled_url_fallback_title_strips_query_secrets():
    from icode_browser_use.policy import sanitize_display_title

    url = "http://127.0.0.1:8080/callback?access_token=SUPERSECRET&code=OAUTH777#frag=1"
    # Chrome sets document.title to host:port/path?query on untitled pages
    chrome_title = "127.0.0.1:8080/callback?access_token=SUPERSECRET&code=OAUTH777"
    cleaned = sanitize_display_title(chrome_title, url)
    assert cleaned == "127.0.0.1:8080/callback"
    assert "SUPERSECRET" not in cleaned and "OAUTH777" not in cleaned

    assert sanitize_display_title("https://u:pw@example.com/cb?token=S1") == "https://example.com/cb"
    assert sanitize_display_title("OAuth Callback?code=S2") == "OAuth Callback?…"
    assert sanitize_display_title("Is it ok? yes") == "Is it ok? yes"
    assert sanitize_display_title("Q4 采购明细表", url) == "Q4 采购明细表"
