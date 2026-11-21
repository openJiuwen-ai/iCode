# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from icode_browser_use.config import BrowserConfig, is_valid_domain, parse_domains
from icode_browser_use.policy import (
    _AUTO_ATTACH_PARAMS,
    NetworkPolicy,
    decide,
    host_allowed,
    is_private_ip,
    scheme_allowed,
)

PUBLIC_IP = "93.184.216.34"


def test_domains_reject_ip_literals() -> None:
    assert is_valid_domain("example.com")
    assert is_valid_domain(".example.com")
    assert not is_valid_domain("127.0.0.1")
    assert not is_valid_domain("https://example.com")
    assert not is_valid_domain("example.com/path")


def test_parse_domains_splits_accepted_and_rejected() -> None:
    accepted, rejected = parse_domains("example.com, 127.0.0.1, .foo.com")
    assert accepted == ("example.com", ".foo.com")
    assert rejected == ["127.0.0.1"]


def test_host_allowed_is_dot_boundary() -> None:
    assert host_allowed(("example.com",), "www.example.com")
    assert not host_allowed(("example.com",), "badexample.com")
    assert host_allowed((".example.com",), "a.example.com")
    assert not host_allowed((".example.com",), "example.com")


def test_decide_fails_closed() -> None:
    allow = ("example.com",)
    assert decide("https://example.com/x", allow, lambda _: [PUBLIC_IP])
    assert not decide("https://example.com/x", allow, lambda _: ["127.0.0.1"])
    assert not decide("file:///etc/passwd", allow, lambda _: [PUBLIC_IP])
    assert not decide("https://evil.com/x", allow, lambda _: [PUBLIC_IP])


def test_network_policy_blocks_unresolvable_host() -> None:
    import asyncio

    from icode_browser_use.policy import NetworkPolicy

    async def empty(_host: str) -> list[str]:
        return []

    policy = NetworkPolicy(("example.com",), resolver=empty)
    assert asyncio.run(policy.evaluate("https://example.com/x")) is False


def test_scheme_and_private_ip() -> None:
    assert scheme_allowed("HTTP")
    assert not scheme_allowed("file")
    assert is_private_ip("10.0.0.1")
    assert is_private_ip("127.0.0.1")
    assert not is_private_ip(PUBLIC_IP)


def test_public_web_still_requires_public_dns_addresses() -> None:
    assert decide("https://example.com/x", (), lambda _: [PUBLIC_IP])
    assert not decide("https://example.com/x", (), lambda _: [])
    assert not decide("https://example.com/x", (), lambda _: [PUBLIC_IP, "10.0.0.1"])
    assert not decide("http://127.0.0.1:8080/x", (), lambda _: [PUBLIC_IP])
    assert not decide("http://localhost:8080/x", (), lambda _: [PUBLIC_IP])
    assert not decide("http://100.64.0.1/x", (), lambda _: [PUBLIC_IP])


def test_config_defaults_to_headed() -> None:
    config = BrowserConfig.from_env({"ICODE_BROWSER_ALLOWED_DOMAINS": "example.com"})
    assert config.allowed_domains == ("example.com",)
    assert config.headless is False
    assert config.evaluate_enabled is False


def test_config_reads_cdp_url() -> None:
    config = BrowserConfig.from_env(
        {
            "ICODE_BROWSER_ALLOWED_DOMAINS": "example.com",
            "ICODE_BROWSER_CDP_URL": "ws://127.0.0.1:9222/devtools/browser/abc",
        }
    )
    assert config.cdp_url == "ws://127.0.0.1:9222/devtools/browser/abc"


def test_config_reads_bridge_wait() -> None:
    config = BrowserConfig.from_env(
        {
            "ICODE_BROWSER_ALLOWED_DOMAINS": "example.com",
            "ICODE_BROWSER_BRIDGE": "1",
            "ICODE_BROWSER_BRIDGE_WAIT": "45",
            "ICODE_BROWSER_PREWARM": "1",
            "ICODE_BROWSER_IDLE_CLOSE_SECONDS": "0",
        }
    )
    assert config.bridge is True
    assert config.bridge_wait == 45.0
    assert config.prewarm is True
    assert config.idle_close_seconds == 0.0


class FakeClient:
    def __init__(self, *, fail: bool = False, fail_times: int | None = None) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.session_ids: list[str | None] = []
        self.fail = fail
        self.fail_times = fail_times
        self.closed = False

    def on(self, method: str, handler) -> None:
        self.calls.append(("on", method))

    async def send(self, method: str, params=None, session_id=None, timeout=None):
        self.calls.append((method, params))
        self.session_ids.append(session_id)
        if self.fail:
            raise RuntimeError("boom")
        if self.fail_times is not None and self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("boom once")
        return {}


async def _public(_host: str) -> list[str]:
    return [PUBLIC_IP]


def test_attach_registers_three_handlers() -> None:
    policy = NetworkPolicy(("example.com",), resolver=_public)
    client = FakeClient()
    policy.attach(client)
    assert [call[1] for call in client.calls] == [
        "Fetch.requestPaused",
        "Target.attachedToTarget",
        "Target.detachedFromTarget",
    ]


def test_request_paused_allows_listed_host() -> None:
    import asyncio

    policy = NetworkPolicy(("example.com",), resolver=_public)
    client = FakeClient()
    policy.attach(client)
    asyncio.run(policy._on_request_paused({"requestId": "1", "request": {"url": "https://example.com/x"}}, None))
    assert ("Fetch.continueRequest", {"requestId": "1"}) in client.calls


def test_request_paused_blocks_unlisted_host() -> None:
    import asyncio

    policy = NetworkPolicy(("example.com",), resolver=_public)
    client = FakeClient()
    policy.attach(client)
    asyncio.run(policy._on_request_paused({"requestId": "2", "request": {"url": "https://evil.com/x"}}, None))
    assert ("Fetch.failRequest", {"requestId": "2", "errorReason": "BlockedByClient"}) in client.calls
    assert not any(call[0] == "Fetch.continueRequest" for call in client.calls)


def test_arm_child_failure_does_not_release_it() -> None:
    import asyncio

    policy = NetworkPolicy(("example.com",), resolver=_public)
    client = FakeClient(fail=True)
    policy.attach(client)
    asyncio.run(policy._arm_child("child-session"))
    assert not any(call[0] == "Runtime.runIfWaitingForDebugger" for call in client.calls)
    assert policy.arm_failures == 1
    # One retry before the frame is written off: eight install commands in total.
    assert sum(1 for call in client.calls if call[0] == "Fetch.enable") == 2


def test_arm_child_retries_once_when_the_connection_is_alive() -> None:
    import asyncio

    policy = NetworkPolicy(("example.com",), resolver=_public)
    # The first install stops at its first failure (fail-closed); the retry then succeeds.
    client = FakeClient(fail_times=1)
    policy.attach(client)
    asyncio.run(policy._arm_child("child-session"))
    methods = [call[0] for call in client.calls if call[0] != "on"]
    assert methods.count("Fetch.enable") == 2
    assert methods.count("Runtime.runIfWaitingForDebugger") == 1
    assert policy.arm_failures == 0


def test_arm_child_does_not_retry_on_a_closed_connection() -> None:
    import asyncio

    from icode_browser_use.errors import ConnectionClosed

    class ClosedClient(FakeClient):
        async def send(self, method, params=None, session_id=None, timeout=None):
            self.calls.append((method, params))
            raise ConnectionClosed("cdp gone")

    policy = NetworkPolicy(("example.com",), resolver=_public)
    client = ClosedClient()
    client.closed = True
    policy.attach(client)
    asyncio.run(policy._arm_child("child-session"))
    assert sum(1 for call in client.calls if call[0] == "Fetch.enable") == 1
    assert not any(call[0] == "Runtime.runIfWaitingForDebugger" for call in client.calls)
    assert policy.arm_failures == 1


def test_arm_child_success_order_and_scope() -> None:
    import asyncio

    policy = NetworkPolicy(("example.com",), resolver=_public)
    client = FakeClient()
    policy.attach(client)
    asyncio.run(policy._arm_child("child-session"))
    methods = [call[0] for call in client.calls if call[0] != "on"]
    assert methods == [
        "Fetch.enable",
        "Network.enable",
        "Network.setBlockedURLs",
        "Target.setAutoAttach",
        "Runtime.runIfWaitingForDebugger",
    ]
    assert all(session_id == "child-session" for session_id in client.session_ids)


def test_arm_page_resumes_before_secondary_commands() -> None:
    import asyncio

    policy = NetworkPolicy(("example.com",), resolver=_public)
    client = FakeClient()
    policy.attach(client)
    asyncio.run(policy._arm_child("page-session", page=True, target_id="t-page"))
    methods = [call[0] for call in client.calls if call[0] != "on"]
    assert methods == [
        "Fetch.enable",
        "Target.setAutoAttach",
        "Runtime.runIfWaitingForDebugger",
        "Network.enable",
        "Network.setBlockedURLs",
    ]
    assert policy.arm_failures == 0 and policy.arm_degraded == 0


def test_an_unarmable_duplicate_page_session_is_detached() -> None:
    import asyncio

    policy = NetworkPolicy(("example.com",), resolver=_public)
    client = FakeClient(fail=True)
    policy.attach(client)
    asyncio.run(policy._arm_child("page-session", page=True, target_id="t-page"))
    methods = [call[0] for call in client.calls if call[0] != "on"]
    assert methods.count("Fetch.enable") == 2
    assert "Runtime.runIfWaitingForDebugger" not in methods
    assert "Target.detachFromTarget" in methods
    assert "Target.closeTarget" not in methods
    assert policy.detached_pages == 1 and policy.arm_degraded == 0 and policy.arm_failures == 0


def test_an_unarmable_popup_is_closed_instead_of_running_unguarded() -> None:
    import asyncio

    def owns(target: str, session: str) -> bool:
        return (target, session) == ("t-popup", "popup-session")

    policy = NetworkPolicy(("example.com",), resolver=_public, owns_session=owns)
    client = FakeClient(fail=True)
    policy.attach(client)
    asyncio.run(policy._arm_child("popup-session", page=True, target_id="t-popup"))
    methods = [call[0] for call in client.calls if call[0] != "on"]
    assert "Target.closeTarget" in methods
    assert "Target.detachFromTarget" not in methods
    assert policy.arm_failed_pages == 1


def test_an_unarmable_foreign_page_is_detached_but_never_closed() -> None:
    import asyncio

    policy = NetworkPolicy(("example.com",), resolver=_public)
    client = FakeClient(fail=True)
    policy.attach(client)
    asyncio.run(policy._arm_child("user-session", page=True, target_id="t-user"))
    methods = [call[0] for call in client.calls if call[0] != "on"]
    assert "Target.detachFromTarget" in methods
    assert "Target.closeTarget" not in methods
    assert policy.detached_pages == 1


def test_page_secondary_hardening_failure_keeps_the_guarded_page_running() -> None:
    import asyncio

    class NetworkFails(FakeClient):
        async def send(self, method, params=None, session_id=None, timeout=None):
            if method == "Network.enable":
                self.calls.append((method, params))
                self.session_ids.append(session_id)
                raise RuntimeError("boom")
            return await super().send(method, params, session_id=session_id, timeout=timeout)

    policy = NetworkPolicy(("example.com",), resolver=_public)
    client = NetworkFails()
    policy.attach(client)
    asyncio.run(policy._arm_child("page-session", page=True, target_id="t-page"))
    methods = [call[0] for call in client.calls if call[0] != "on"]
    assert methods.count("Runtime.runIfWaitingForDebugger") == 1
    assert policy.arm_failures == 0 and policy.arm_degraded == 1


def test_a_page_without_frame_auto_attach_is_closed() -> None:
    import asyncio

    class AutoAttachFails(FakeClient):
        async def send(self, method, params=None, session_id=None, timeout=None):
            if method == "Target.setAutoAttach":
                self.calls.append((method, params))
                self.session_ids.append(session_id)
                raise RuntimeError("boom")
            return await super().send(method, params, session_id=session_id, timeout=timeout)

    def owns(target: str, session: str) -> bool:
        return (target, session) == ("t-popup", "popup-session")

    policy = NetworkPolicy(("example.com",), resolver=_public, owns_session=owns)
    client = AutoAttachFails()
    policy.attach(client)
    asyncio.run(policy._arm_child("popup-session", page=True, target_id="t-popup"))
    methods = [call[0] for call in client.calls if call[0] != "on"]
    assert "Target.closeTarget" in methods
    assert policy.arm_failed_pages == 1


def test_bootstrap_denies_downloads() -> None:
    import asyncio

    policy = NetworkPolicy(("example.com",), resolver=_public)
    client = FakeClient()
    asyncio.run(policy.bootstrap(client, session_id="s1"))
    assert ("Browser.setDownloadBehavior", {"behavior": "deny"}) in client.calls


def test_bootstrap_allows_controlled_downloads_only_for_an_owned_browser(tmp_path) -> None:
    import asyncio

    policy = NetworkPolicy(("example.com",), resolver=_public)
    client = FakeClient()
    asyncio.run(policy.bootstrap(client, session_id="s1", download_dir=str(tmp_path)))
    assert (
        "Browser.setDownloadBehavior",
        {"behavior": "allowAndName", "downloadPath": str(tmp_path), "eventsEnabled": True},
    ) in client.calls
    assert ("Browser.setDownloadBehavior", {"behavior": "deny"}) not in client.calls
    # Scoped (Attach/Bridge) bootstrap keeps the page-scoped denial: the user's
    # browser-wide download settings must not change.
    scoped = FakeClient()
    asyncio.run(policy.bootstrap(scoped, session_id="s1", scoped=True, download_dir=str(tmp_path)))
    assert ("Page.setDownloadBehavior", {"behavior": "deny"}) in scoped.calls
    assert not any(method == "Browser.setDownloadBehavior" for method, _ in scoped.calls)


def test_auto_attach_filter_covers_pages_and_iframes() -> None:
    types = {entry["type"] for entry in _AUTO_ATTACH_PARAMS["filter"]}
    assert types == {"page", "iframe"}


def test_bootstrap_with_page_discovery_adds_a_page_only_browser_attach() -> None:
    import asyncio

    policy = NetworkPolicy(("example.com",), resolver=_public)
    client = FakeClient()
    asyncio.run(policy.bootstrap(client, session_id="s1", scoped=True, discover_pages=True))
    attaches = [params for method, params in client.calls if method == "Target.setAutoAttach"]
    assert len(attaches) == 2
    assert {entry["type"] for entry in attaches[0]["filter"]} == {"page", "iframe"}
    assert {entry["type"] for entry in attaches[1]["filter"]} == {"page"}
    assert attaches[1]["waitForDebuggerOnStart"] is True
    assert ("Browser.setDownloadBehavior", {"behavior": "deny"}) in client.calls
    # Scoped-only bootstrap keeps the page-scoped download denial instead.
    scoped_client = FakeClient()
    asyncio.run(policy.bootstrap(scoped_client, session_id="s1", scoped=True))
    assert ("Page.setDownloadBehavior", {"behavior": "deny"}) in scoped_client.calls
    assert ("Browser.setDownloadBehavior", {"behavior": "deny"}) not in scoped_client.calls


def test_service_worker_is_detached() -> None:
    import asyncio

    policy = NetworkPolicy(("example.com",), resolver=_public)
    client = FakeClient()
    policy.attach(client)
    asyncio.run(
        policy._on_attached_to_target(
            {"targetInfo": {"type": "service_worker", "url": "https://example.com/sw.js"}, "sessionId": "w1"},
            None,
        )
    )
    assert ("Target.detachFromTarget", {"sessionId": "w1"}) in client.calls
    assert policy.detached_workers == 1


def test_foreign_extension_frame_is_detached() -> None:
    import asyncio

    policy = NetworkPolicy(("example.com",), resolver=_public)
    client = FakeClient()
    policy.attach(client)
    asyncio.run(
        policy._on_attached_to_target(
            {
                "targetInfo": {"type": "iframe", "url": "chrome-extension://other/x.html"},
                "sessionId": "f1",
            },
            None,
        )
    )
    assert ("Target.detachFromTarget", {"sessionId": "f1"}) in client.calls
    assert policy.detached_workers == 1
