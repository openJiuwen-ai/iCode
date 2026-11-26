# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
#
"""Unit tests for ``browser_wait_for`` (W08)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from icode_browser_use.actions import wait_for
from icode_browser_use.errors import (
    BadRequest,
    BrowserError,
    ConnectionClosed,
    StaleRef,
    TargetAmbiguous,
    TargetNotFound,
)
from icode_browser_use.snapshot.fusion import FusedNode


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.step = 0.0

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += max(seconds, self.step)


class FakeClient:
    """CDP surface used by the wait: node resolution, node probes and page values."""

    def __init__(
        self,
        *,
        probes: list[dict] | None = None,
        values: list[object] | None = None,
        node_error: BaseException | None = None,
        on_probe=None,
    ):
        self.probes = list(probes or [])
        self.values = list(values or [])
        self.node_error = node_error
        self.calls: list[tuple[str, dict, str | None]] = []
        self.on_probe = on_probe

    async def send(self, method, params=None, session_id=None, timeout=None):
        self.calls.append((method, dict(params or {}), session_id))
        if method == "DOM.resolveNode":
            if self.node_error is not None:
                raise self.node_error
            return {"object": {"objectId": "obj"}}
        if method == "Runtime.callFunctionOn":
            if self.on_probe is not None:
                self.on_probe()
            item = self.probes.pop(0) if len(self.probes) > 1 else (self.probes[0] if self.probes else {})
            if isinstance(item, Exception):
                raise item
            return {"result": {"value": item}}
        item = self.values.pop(0) if len(self.values) > 1 else (self.values[0] if self.values else "")
        if isinstance(item, Exception):
            raise item
        return {"result": {"value": item}}

    def method_count(self, method: str) -> int:
        return sum(1 for name, _, _ in self.calls if name == method)

    def sessions_for(self, method: str) -> list[str | None]:
        return [session_id for name, _, session_id in self.calls if name == method]


class FakeSession:
    session_id = "main"
    target_id = "t"
    page_epoch = 5
    cfg = SimpleNamespace(cdp_timeout=1.0, evaluate_timeout=1.0)

    def __init__(self, *, client: FakeClient | None = None, cdp_session_id: str | None = None) -> None:
        self.client = client or FakeClient()
        self._cdp_session_id = cdp_session_id
        self.captures = 0
        self.successes = 0
        self.targets: list[BaseException] = []
        self.snapshot = SimpleNamespace(nodes=[])
        self.entry = SimpleNamespace(token="@e1#5", cdp_session_id=cdp_session_id, backend_node_id=11)

    def resolve_target(self, _target):
        if self.targets:
            error = self.targets.pop(0)
            if error is not None:
                raise error
        return self.entry

    def require_client(self):
        return self.client

    def note_success(self, _target_id) -> None:
        self.successes += 1


@pytest.fixture(autouse=True)
def deterministic_clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(wait_for, "time", clock)
    monkeypatch.setattr(wait_for, "_sleep", clock.sleep)
    return clock


async def capture(_session):
    _session.captures += 1


def run(session: FakeSession, arguments: dict) -> str:
    blocks = asyncio.run(wait_for.run(session, arguments))
    return blocks[0]["text"]


def test_visible_succeeds_on_the_first_probe(deterministic_clock) -> None:
    session = FakeSession(client=FakeClient(probes=[{"present": True, "visible": True, "text": "late button"}]))
    text = run(session, {"condition": "visible", "target": "@e1#5", "value": "", "timeout_ms": 500})
    assert "已满足条件：visible" in text
    assert "basis=visible" in text
    assert "status=succeeded" in text
    assert session.client.method_count("Runtime.callFunctionOn") == 1


def test_hidden_waits_until_the_element_disappears(deterministic_clock) -> None:
    probes = [
        {"present": True, "visible": True, "text": ""},
        {"present": True, "visible": False, "text": ""},
    ]
    session = FakeSession(client=FakeClient(probes=probes))
    text = run(session, {"condition": "hidden", "target": "@e1#5", "value": "", "timeout_ms": 500})
    assert "已满足条件：hidden" in text
    assert "basis=invisible" in text
    assert session.client.method_count("Runtime.callFunctionOn") == 2
    assert session.client.method_count("DOM.resolveNode") == 1, "the ref is resolved once per wait"


def test_hidden_does_not_treat_changed_text_as_hidden(deterministic_clock) -> None:
    # The node still exists (its text changed between polls); that is not "hidden".
    deterministic_clock.step = 0.2
    probes = [
        {"present": True, "visible": True, "text": "before"},
        {"present": True, "visible": True, "text": "after"},
    ]
    session = FakeSession(client=FakeClient(probes=probes))
    text = run(session, {"condition": "hidden", "target": "@e1#5", "value": "", "timeout_ms": 500})
    assert "status=timeout" in text
    assert "visible=True" in text


def test_a_protocol_error_is_not_treated_as_hidden(deterministic_clock) -> None:
    deterministic_clock.step = 0.2
    session = FakeSession(client=FakeClient(node_error=BrowserError("CDP 错误: Session closed")))
    text = run(session, {"condition": "hidden", "target": "@e1#5", "value": "", "timeout_ms": 500})
    assert "status=timeout" in text
    assert "probe_failed" in text


def test_visible_timeout_reports_the_last_observation(deterministic_clock) -> None:
    deterministic_clock.step = 0.2
    session = FakeSession(client=FakeClient(probes=[{"present": True, "visible": False, "text": ""}]))
    text = run(session, {"condition": "visible", "target": "late button", "value": "", "timeout_ms": 500})
    assert "等待超时：visible target=late button 在 500 ms 内未满足" in text
    assert "last=target=late button visible=False" in text
    assert "下一步：browser_snapshot 或 browser_extract 核对页面" in text and "不要 sleep" in text
    assert "status=timeout retryable=true" in text
    assert deterministic_clock.now >= 1000.5
    assert session.client.method_count("Runtime.callFunctionOn") <= 4


def test_page_text_condition(deterministic_clock) -> None:
    session = FakeSession(client=FakeClient(values=["almost", "the later text is here"]))
    text = run(session, {"condition": "text", "target": "", "value": "later", "timeout_ms": 500})
    assert "已满足条件：text" in text
    assert "basis=text_match" in text
    assert session.client.method_count("Runtime.evaluate") == 2


def test_url_condition(deterministic_clock) -> None:
    session = FakeSession(client=FakeClient(values=["http://fixture/", "http://fixture/slow?ms=800"]))
    text = run(session, {"condition": "url", "target": "", "value": "/slow", "timeout_ms": 500})
    assert "已满足条件：url" in text
    assert "basis=url_match" in text


def test_text_inside_a_target_uses_the_node_text(deterministic_clock) -> None:
    session = FakeSession(client=FakeClient(probes=[{"present": True, "visible": True, "text": "opened now"}]))
    text = run(session, {"condition": "text", "target": "@e1#5", "value": "opened", "timeout_ms": 500})
    assert "已满足条件：text target=@e1#5" in text


def test_delayed_free_text_target_refreshes_the_snapshot(monkeypatch, deterministic_clock) -> None:
    session = FakeSession(client=FakeClient(probes=[{"present": True, "visible": True, "text": "late button"}]))
    session.targets = [TargetNotFound("absent"), None]
    monkeypatch.setattr(wait_for, "capture_and_register", capture)
    text = run(session, {"condition": "visible", "target": "late button", "value": "", "timeout_ms": 900})
    assert "已满足条件：visible" in text
    assert session.captures == 1


def test_snapshot_refresh_is_rate_limited(monkeypatch, deterministic_clock) -> None:
    deterministic_clock.step = 0.15
    session = FakeSession()
    session.targets = [TargetNotFound("absent")] * 40
    monkeypatch.setattr(wait_for, "capture_and_register", capture)
    text = run(session, {"condition": "visible", "target": "never there", "value": "", "timeout_ms": 2000})
    assert "status=timeout" in text
    # 2 s of waiting refreshes at most once per _REFRESH_MIN_SECONDS (+1 at the start), never per poll.
    assert session.captures <= 5
    assert session.captures >= 2


def test_stale_ref_fails_fast_for_visible(deterministic_clock) -> None:
    session = FakeSession(client=FakeClient(node_error=BrowserError("CDP 错误 -32000: Could not find node")))
    with pytest.raises(StaleRef):
        asyncio.run(wait_for.run(session, {"condition": "visible", "target": "@e1#5", "value": "", "timeout_ms": 500}))


def test_a_gone_ref_counts_as_hidden(deterministic_clock) -> None:
    session = FakeSession(client=FakeClient(node_error=BrowserError("CDP 错误 -32000: Could not find node")))
    text = run(session, {"condition": "hidden", "target": "@e1#5", "value": "", "timeout_ms": 500})
    assert "已满足条件：hidden" in text
    assert "basis=gone" in text


def test_probe_of_a_removed_node_is_hidden(deterministic_clock) -> None:
    session = FakeSession(
        client=FakeClient(probes=[BrowserError("CDP 错误 -32000: Could not find object"), {"present": False}])
    )
    text = run(session, {"condition": "hidden", "target": "@e1#5", "value": "", "timeout_ms": 500})
    assert "已满足条件：hidden" in text


def test_free_text_that_never_appears_times_out_as_not_found(monkeypatch, deterministic_clock) -> None:
    deterministic_clock.step = 0.3
    session = FakeSession()
    session.targets = [TargetNotFound("absent")] * 40
    monkeypatch.setattr(wait_for, "capture_and_register", capture)
    text = run(session, {"condition": "visible", "target": "never there", "value": "", "timeout_ms": 400})
    assert "status=timeout" in text
    assert "not_found" in text
    assert session.captures == 1


def test_hidden_free_text_is_reported_with_its_basis(monkeypatch, deterministic_clock) -> None:
    session = FakeSession()
    session.targets = [TargetNotFound("spinner")] * 5
    monkeypatch.setattr(wait_for, "capture_and_register", capture)
    text = run(session, {"condition": "hidden", "target": "spinner", "value": "", "timeout_ms": 500})
    assert "basis=not_found" in text
    assert "status=succeeded" in text
    assert "不存在按已隐藏处理" in text


def test_hidden_free_text_is_not_satisfied_by_a_stale_snapshot(monkeypatch, deterministic_clock) -> None:
    """After a document change the old snapshot proves nothing: wait for a fresh one instead of success."""
    deterministic_clock.step = 0.4
    session = FakeSession()
    session.snapshot = SimpleNamespace(nodes=[SimpleNamespace(epoch=4)])
    session.targets = [TargetNotFound("spinner")] * 40
    monkeypatch.setattr(wait_for, "capture_and_register", capture)
    text = run(session, {"condition": "hidden", "target": "spinner", "value": "", "timeout_ms": 1000})
    assert "status=timeout" in text
    assert "快照来自旧文档" in text


def test_count_does_not_report_success_from_a_stale_snapshot(monkeypatch, deterministic_clock) -> None:
    """A count predicate that already holds on a pre-wait view is not proof: wait for a fresh capture."""
    deterministic_clock.step = 0.4
    session = FakeSession()
    session.snapshot = SimpleNamespace(nodes=[_conversation(1)])

    async def failing(_session) -> None:
        _session.captures += 1
        raise BrowserError("CDP 错误 -32000: capture failed")

    monkeypatch.setattr(wait_for, "capture_and_register", failing)
    text = run(session, {"condition": "count", "target": "conv", "value": "=1", "timeout_ms": 1000})
    assert "status=timeout" in text
    assert "count snapshot refresh failed" in text
    assert session.captures >= 1


def test_count_does_not_use_a_snapshot_from_the_previous_document(monkeypatch, deterministic_clock) -> None:
    deterministic_clock.step = 0.5
    session = FakeSession()
    session.snapshot = SimpleNamespace(nodes=[_conversation(1)])

    async def moving(_session) -> None:
        _session.captures += 1
        _session.snapshot.nodes = [_conversation(1)]
        _session.page_epoch += 1

    monkeypatch.setattr(wait_for, "capture_and_register", moving)
    text = run(session, {"condition": "count", "target": "conv", "value": "=1", "timeout_ms": 1200})
    assert "status=timeout" in text
    assert session.captures >= 2


def test_free_text_visible_survives_a_rerendered_node(deterministic_clock) -> None:
    class OnceGone(FakeClient):
        def __init__(self) -> None:
            super().__init__(probes=[{"present": True, "visible": True, "text": "late button"}])
            self.resolutions = 0

        async def send(self, method, params=None, session_id=None, timeout=None):
            if method == "DOM.resolveNode":
                self.resolutions += 1
                if self.resolutions == 1:
                    self.calls.append((method, dict(params or {}), session_id))
                    raise BrowserError("CDP 错误 -32000: Could not find node")
            return await super().send(method, params=params, session_id=session_id, timeout=timeout)

    session = FakeSession(client=OnceGone())
    text = run(session, {"condition": "visible", "target": "late button", "value": "", "timeout_ms": 800})
    assert "status=succeeded" in text
    assert session.successes == 1


def test_a_hanging_snapshot_refresh_stays_within_the_budget(monkeypatch) -> None:
    import time as std_time

    monkeypatch.setattr(wait_for, "time", std_time)  # real clock: the guard is a wall-clock bound
    monkeypatch.setattr(wait_for, "_sleep", asyncio.sleep)
    session = FakeSession()
    session.targets = [TargetNotFound("never")] * 10

    async def hang(_session):
        await asyncio.sleep(5)

    monkeypatch.setattr(wait_for, "capture_and_register", hang)
    started = std_time.monotonic()
    blocks = asyncio.run(
        wait_for.run(session, {"condition": "visible", "target": "never", "value": "", "timeout_ms": 300})
    )
    elapsed = std_time.monotonic() - started
    assert "status=timeout" in blocks[0]["text"]
    assert elapsed < 2.0, f"the refresh ignored the deadline: {elapsed:.2f}s"


def test_iframe_target_resolves_and_probes_in_the_child_session(deterministic_clock) -> None:
    session = FakeSession(
        client=FakeClient(probes=[{"present": True, "visible": True, "text": "frame action"}]),
        cdp_session_id="child",
    )
    run(session, {"condition": "visible", "target": "@e1#5", "value": "", "timeout_ms": 500})
    assert session.client.sessions_for("DOM.resolveNode") == ["child"]
    assert session.client.sessions_for("Runtime.callFunctionOn") == ["child"]


def test_ambiguous_free_text_is_reported_not_swallowed(monkeypatch, deterministic_clock) -> None:
    session = FakeSession()
    session.targets = [TargetAmbiguous("two equally good matches")]
    monkeypatch.setattr(wait_for, "capture_and_register", capture)
    with pytest.raises(TargetAmbiguous):
        asyncio.run(wait_for.run(session, {"condition": "visible", "target": "button", "value": "", "timeout_ms": 500}))


def test_connection_loss_is_not_swallowed(deterministic_clock) -> None:
    session = FakeSession(client=FakeClient(values=[ConnectionClosed("cdp gone")]))
    with pytest.raises(ConnectionClosed):
        asyncio.run(wait_for.run(session, {"condition": "url", "target": "", "value": "/slow", "timeout_ms": 500}))


@pytest.mark.parametrize(
    "arguments",
    [
        {"condition": "visible", "target": "", "value": "", "timeout_ms": 500},
        {"condition": "hidden", "target": "", "value": "", "timeout_ms": 500},
        {"condition": "text", "target": "", "value": "", "timeout_ms": 500},
        {"condition": "url", "target": "", "value": "", "timeout_ms": 500},
        {"condition": "url", "target": "@e1#5", "value": "/slow", "timeout_ms": 500},
    ],
)
def test_incomplete_conditions_are_rejected_before_any_poll(arguments: dict) -> None:
    session = FakeSession()
    with pytest.raises(BadRequest):
        asyncio.run(wait_for.run(session, arguments))
    assert session.client.calls == []


def test_cancelling_the_task_stops_the_wait(monkeypatch) -> None:
    release = asyncio.Event()

    def on_probe() -> None:
        release.set()

    client = FakeClient(probes=[{"present": True, "visible": False, "text": ""}], on_probe=on_probe)
    session = FakeSession(client=client)
    monkeypatch.setattr(wait_for, "_sleep", asyncio.sleep)  # a real await the cancel can interrupt

    async def scenario() -> None:
        task = asyncio.create_task(
            wait_for.run(session, {"condition": "visible", "target": "@e1#5", "value": "", "timeout_ms": 60000})
        )
        await asyncio.wait_for(release.wait(), timeout=5)
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert client.method_count("Runtime.callFunctionOn") == 1


# --- text_changed / stable / count and long waits -------------------------------------------


def _stable(quiet_ms: float, n: int = 1) -> dict:
    return {"present": True, "n": n, "quiet_ms": quiet_ms}


def test_page_text_changed_succeeds_on_the_first_difference(deterministic_clock) -> None:
    session = FakeSession(client=FakeClient(values=["a", "ab"]))
    text = run(session, {"condition": "text_changed", "target": "", "value": "", "timeout_ms": 500})
    assert "已满足条件：text_changed page" in text
    assert "basis=text_changed" in text
    assert "text='ab'" in text
    assert "status=succeeded" in text
    assert session.client.method_count("Runtime.evaluate") == 2
    assert session.successes == 1


def test_target_text_changed_uses_the_node_text(deterministic_clock) -> None:
    probes = [
        {"present": True, "visible": True, "text": "x"},
        {"present": True, "visible": True, "text": "xy"},
    ]
    session = FakeSession(client=FakeClient(probes=probes))
    text = run(session, {"condition": "text_changed", "target": "@e1#5", "value": "", "timeout_ms": 500})
    assert "已满足条件：text_changed target=@e1#5" in text
    assert "basis=text_changed" in text
    assert "text='xy'" in text
    assert session.client.method_count("Runtime.callFunctionOn") == 2
    probe_functions = [
        params["functionDeclaration"] for name, params, _ in session.client.calls if name == "Runtime.callFunctionOn"
    ]
    assert probe_functions == [wait_for._NODE_PROBE_FN] * 2


def test_page_stable_waits_for_the_default_quiet_window(deterministic_clock) -> None:
    session = FakeSession(client=FakeClient(values=[_stable(200), _stable(1600)]))
    text = run(session, {"condition": "stable", "target": "", "value": "", "timeout_ms": 2000})
    assert "已满足条件：stable page" in text
    assert "basis=mutations_quiet" in text
    assert "page quiet_ms=1600 mutations=" in text, "the success names the quiet window it saw"
    assert "status=succeeded" in text
    expressions = [params["expression"] for name, params, _ in session.client.calls if name == "Runtime.evaluate"]
    assert len(expressions) == 3
    assert expressions[0] == expressions[1]
    assert "window.__chrys_stable" in expressions[0]
    assert "delete window.__chrys_stable" in expressions[2]


def test_page_stable_rejects_a_timeout_shorter_than_the_quiet_window() -> None:
    session = FakeSession()
    with pytest.raises(BadRequest):
        asyncio.run(wait_for.run(session, {"condition": "stable", "target": "", "value": "", "timeout_ms": 1000}))
    assert session.client.calls == []


def test_target_stable_uses_the_node_observer(deterministic_clock) -> None:
    session = FakeSession(client=FakeClient(probes=[_stable(50, n=3), _stable(400, n=3)]))
    text = run(session, {"condition": "stable", "target": "@e1#5", "value": "300", "timeout_ms": 1000})
    assert "已满足条件：stable target=@e1#5 value='300'" in text
    assert "basis=mutations_quiet" in text
    assert "target=@e1#5 quiet_ms=400 mutations=3" in text
    assert "status=succeeded" in text
    probe_functions = [
        params["functionDeclaration"] for name, params, _ in session.client.calls if name == "Runtime.callFunctionOn"
    ]
    assert len(probe_functions) == 3
    assert probe_functions[0] == probe_functions[1]
    assert "__chrys_stable" in probe_functions[0]
    assert "map.delete(this)" in probe_functions[2]


def test_stable_timeout_reports_the_last_quiet_window(deterministic_clock) -> None:
    deterministic_clock.step = 0.4
    session = FakeSession(client=FakeClient(values=[_stable(100, n=2)]))
    text = run(session, {"condition": "stable", "target": "", "value": "", "timeout_ms": 2000})
    assert "等待超时：stable page 在 2000 ms 内未满足" in text
    assert "last=page quiet_ms=100 mutations=2" in text
    assert "status=timeout retryable=true" in text


def _conversation(ref: int) -> FusedNode:
    return FusedNode(ref, "f", "t", "link", f"conv {ref}", "a", 0, (0, 0, 10, 10), ref=ref, epoch=5)


async def _capture_conversations(session) -> None:
    session.captures += 1
    session.snapshot.nodes = [_conversation(i) for i in range(1, 2 + session.captures)]  # 2 nodes, then 3


def test_count_refreshes_the_snapshot_until_the_predicate_holds(monkeypatch, deterministic_clock) -> None:
    deterministic_clock.step = 0.5
    session = FakeSession()
    session.snapshot = SimpleNamespace(nodes=[])
    monkeypatch.setattr(wait_for, "capture_and_register", _capture_conversations)
    text = run(session, {"condition": "count", "target": "conv", "value": ">2", "timeout_ms": 3000})
    assert "已满足条件：count target=conv value='>2'" in text
    assert "basis=find_count" in text
    assert "status=succeeded" in text
    assert session.captures == 2
    assert session.client.calls == []


def test_count_timeout_reports_the_last_count(monkeypatch, deterministic_clock) -> None:
    deterministic_clock.step = 0.5
    session = FakeSession()
    session.snapshot = SimpleNamespace(nodes=[])

    async def two_conversations(_session) -> None:
        _session.captures += 1
        _session.snapshot.nodes = [_conversation(1), _conversation(2)]

    monkeypatch.setattr(wait_for, "capture_and_register", two_conversations)
    text = run(session, {"condition": "count", "target": "conv", "value": ">2", "timeout_ms": 1000})
    assert "等待超时：count target=conv value='>2'" in text
    assert "last=count=2" in text
    assert "status=timeout" in text


@pytest.mark.parametrize(
    "arguments",
    [
        {"condition": "count", "target": "conv", "value": "bogus", "timeout_ms": 500},
        {"condition": "count", "target": "@e1#7", "value": ">2", "timeout_ms": 500},
        {"condition": "count", "target": "", "value": ">2", "timeout_ms": 500},
        {"condition": "count", "target": "conv", "value": "", "timeout_ms": 500},
        {"condition": "stable", "target": "", "value": "soon", "timeout_ms": 5000},
    ],
)
def test_malformed_count_and_stable_requests_are_rejected(monkeypatch, arguments: dict) -> None:
    session = FakeSession()
    session.snapshot = SimpleNamespace(nodes=[])
    monkeypatch.setattr(wait_for, "capture_and_register", capture)
    with pytest.raises(BadRequest):
        asyncio.run(wait_for.run(session, arguments))
    assert session.client.calls == []
    assert session.captures == 0


def test_a_long_wait_reports_its_elapsed_time_on_timeout(deterministic_clock) -> None:
    deterministic_clock.step = 61.0
    session = FakeSession(client=FakeClient(values=["http://fixture/"]))
    text = run(session, {"condition": "url", "target": "", "value": "/never", "timeout_ms": 61000})
    assert "等待超时：url value='/never' 在 61000 ms 内未满足" in text
    assert "elapsed_ms=61000" in text
    assert "status=timeout retryable=true" in text


def test_a_short_wait_does_not_report_elapsed_time(deterministic_clock) -> None:
    deterministic_clock.step = 5.0
    session = FakeSession(client=FakeClient(values=["http://fixture/"]))
    text = run(session, {"condition": "url", "target": "", "value": "/never", "timeout_ms": 5000})
    assert "status=timeout" in text
    assert "elapsed_ms=" not in text


def test_params_expose_the_new_conditions_and_the_timeout_cap() -> None:
    assert wait_for.PARAMS["timeout_ms"]["max"] == 180000
    assert {"text_changed", "stable", "count"} <= set(wait_for.PARAMS["condition"]["enum"])


def test_stable_observers_do_not_reuse_the_snapshot_probe_counter() -> None:
    """``window.__chrys_mutations`` is the snapshot probe's plain number: reading ``.at`` from it gives NaN."""
    from icode_browser_use.snapshot.fusion import MUTATION_COUNT_EXPRESSION

    counter = MUTATION_COUNT_EXPRESSION.split("||")[0].strip()
    assert counter == "window.__chrys_mutations"
    for script in (wait_for._STABLE_PAGE_EXPR, wait_for._STABLE_NODE_FN):
        assert counter not in script
        assert "__chrys_stable" in script


def test_editable_condition_waits_until_visible_and_editable(deterministic_clock) -> None:
    probes = [
        {"present": True, "visible": False, "editable": False, "tag": "div"},
        {"present": True, "visible": True, "editable": False, "disabled": True, "tag": "div"},
        {"present": True, "visible": True, "editable": True, "disabled": False, "tag": "div"},
    ]
    session = FakeSession(client=FakeClient(probes=probes))
    text = run(session, {"condition": "editable", "target": "@e1#5", "value": "", "timeout_ms": 1000})
    assert "已满足条件：editable" in text
    assert "basis=editable" in text
    assert "status=succeeded" in text


def test_editable_timeout_reports_structured_state(deterministic_clock) -> None:
    deterministic_clock.step = 0.3
    session = FakeSession(
        client=FakeClient(
            probes=[{"present": True, "visible": True, "editable": False, "readOnly": True, "tag": "div"}]
        )
    )
    text = run(session, {"condition": "editable", "target": "@e1#5", "value": "", "timeout_ms": 500})
    assert "status=timeout" in text
    assert "state=found_not_editable" in text
    assert "readonly" in text


def test_text_wait_falls_back_to_snapshot_for_closed_shadow_dom(monkeypatch, deterministic_clock) -> None:
    session = FakeSession(client=FakeClient(values=["light dom only"]))

    async def shadow_capture(sess) -> None:
        sess.captures += 1
        sess.snapshot.nodes = [
            FusedNode(9, "f", "t", "generic", "没有更多评论", "div", 0, (0, 0, 100, 20), visible=True, ref=9, epoch=5)
        ]

    monkeypatch.setattr(wait_for, "capture_and_register", shadow_capture)
    text = run(session, {"condition": "text", "target": "", "value": "没有更多评论", "timeout_ms": 1000})
    assert "status=succeeded" in text
    assert "basis=shadow_text_match" in text


def test_text_changed_uses_full_text_digest_beyond_preview_limit(deterministic_clock) -> None:
    same_preview = "x" * 50
    page_session = FakeSession(
        client=FakeClient(
            values=[
                {"text": same_preview, "text_digest": "22000:aaa:bbb"},
                {"text": same_preview, "text_digest": "22008:ccc:ddd"},
            ]
        )
    )
    page_text = run(page_session, {"condition": "text_changed", "target": "", "value": "", "timeout_ms": 500})
    assert "status=succeeded" in page_text
    assert "basis=text_changed" in page_text

    baseline_session = FakeSession(client=FakeClient(values=[{"text": same_preview, "text_digest": "22000:aaa:bbb"}]))
    assert asyncio.run(wait_for.read_text_baseline(baseline_session)) == "22000:aaa:bbb"

    node_session = FakeSession(
        client=FakeClient(
            probes=[
                {"present": True, "visible": True, "text": same_preview, "text_digest": "22000:111:222"},
                {"present": True, "visible": True, "text": same_preview, "text_digest": "22008:333:444"},
            ]
        )
    )
    node_text = run(node_session, {"condition": "text_changed", "target": "@e1#5", "value": "", "timeout_ms": 500})
    assert "status=succeeded" in node_text
    assert "basis=text_changed" in node_text


def test_text_condition_uses_full_text_hit_beyond_preview_limit(deterministic_clock) -> None:
    same_preview = "x" * 50
    page_session = FakeSession(
        client=FakeClient(
            values=[
                {"text": same_preview, "text_digest": "22000:aaa:bbb", "text_hit": False},
                {"text": same_preview, "text_digest": "22008:ccc:ddd", "text_hit": True},
            ]
        )
    )
    page_text = run(page_session, {"condition": "text", "target": "", "value": "finished", "timeout_ms": 500})
    assert "status=succeeded" in page_text
    assert "basis=text_match" in page_text

    node_session = FakeSession(
        client=FakeClient(
            probes=[
                {"present": True, "visible": True, "text": same_preview, "text_digest": "22000:111", "text_hit": False},
                {"present": True, "visible": True, "text": same_preview, "text_digest": "22008:222", "text_hit": True},
            ]
        )
    )
    node_text = run(node_session, {"condition": "text", "target": "@e1#5", "value": "finished", "timeout_ms": 500})
    assert "status=succeeded" in node_text
    assert "basis=text_match" in node_text


def test_hidden_times_out_when_budget_is_too_short_to_refresh_snapshot(monkeypatch, deterministic_clock) -> None:
    deterministic_clock.step = 0.05
    orig_monotonic = deterministic_clock.monotonic

    def ticking_monotonic() -> float:
        # Advance 1 ms on each read so remaining budget falls below _REFRESH_FLOOR_SECONDS (0.1s), matching real wall time.
        deterministic_clock.now += 0.001
        return orig_monotonic()

    monkeypatch.setattr(deterministic_clock, "monotonic", ticking_monotonic)
    session = FakeSession()
    session.targets = [TargetNotFound("absent")] * 10
    monkeypatch.setattr(wait_for, "capture_and_register", capture)
    text = run(session, {"condition": "hidden", "target": "Stop generating", "value": "", "timeout_ms": 100})
    assert "status=timeout" in text
    assert "basis=not_found" not in text
    assert session.captures == 0


def test_stable_scripts_observe_open_shadow_roots() -> None:
    assert "shadowRoot" in wait_for._STABLE_PAGE_EXPR
    assert "observeTree" in wait_for._STABLE_PAGE_EXPR
    assert "inScope" in wait_for._STABLE_PAGE_EXPR
    assert "observeTree" in wait_for._STABLE_NODE_FN


def test_hidden_free_text_rechecks_page_when_previous_node_is_replaced(monkeypatch, deterministic_clock) -> None:
    deterministic_clock.step = 0.2

    class ReplacingClient(FakeClient):
        def __init__(self) -> None:
            super().__init__(probes=[{"present": True, "visible": True, "text": "Stop generating"}])
            self.resolve_calls = 0

        async def send(self, method, params=None, session_id=None, timeout=None):
            if method == "DOM.resolveNode":
                self.resolve_calls += 1
                if self.resolve_calls == 1:
                    raise BrowserError("CDP 错误 -32000: Could not find node")
                return {"object": {"objectId": "new-obj"}}
            return await super().send(method, params, session_id=session_id, timeout=timeout)

    session = FakeSession(client=ReplacingClient())
    monkeypatch.setattr(wait_for, "capture_and_register", capture)
    # Free-text hidden must NOT succeed with basis=gone when the old node died and a replacement is visible.
    text = run(session, {"condition": "hidden", "target": "Stop generating", "value": "", "timeout_ms": 500})
    assert "status=timeout" in text
    assert "basis=gone" not in text
    assert session.captures >= 1


def test_hidden_free_text_refreshes_and_succeeds_when_detached_node_still_resolves(
    monkeypatch, deterministic_clock
) -> None:
    deterministic_clock.step = 0.1
    # CDP still returns an objectId for the detached node, while Runtime.callFunctionOn reports present=False.
    session = FakeSession(
        client=FakeClient(
            probes=[
                {"present": True, "visible": True, "text": "Stop generating"},
                {"present": False, "visible": False, "text": ""},
            ]
        )
    )

    async def capture_removes_button(sess) -> None:
        sess.captures += 1
        sess.targets = [TargetNotFound("absent")] * 10

    monkeypatch.setattr(wait_for, "capture_and_register", capture_removes_button)
    text = run(session, {"condition": "hidden", "target": "Stop generating", "value": "", "timeout_ms": 1000})
    assert "status=succeeded" in text
    assert "basis=not_found" in text
    assert session.captures == 1


def test_page_text_changed_and_text_observe_same_origin_and_oopif_iframes(deterministic_clock) -> None:
    assert "contentDocument" in wait_for._PAGE_TEXT_EXPR
    assert "IFRAME" in wait_for._PAGE_TEXT_EXPR

    class OopifClient(FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.oopif_reads = 0

        async def send(self, method, params=None, session_id=None, timeout=None):
            self.calls.append((method, params, session_id))
            if method == "Runtime.evaluate":
                if session_id == "oopif-1":
                    self.oopif_reads += 1
                    txt = "No selection" if self.oopif_reads == 1 else "FRAME-204"
                    return {
                        "result": {
                            "value": {
                                "text": txt,
                                "text_digest": f"d:{txt}",
                                "text_hit": "FRAME-204" in txt,
                            }
                        }
                    }
                return {
                    "result": {
                        "value": {
                            "text": "Embedded detail",
                            "text_digest": "d:outer",
                            "text_hit": False,
                        }
                    }
                }
            return await super().send(method, params, session_id=session_id, timeout=timeout)

    client = OopifClient()
    session = FakeSession(client=client)
    session.current_page_oopif_sessions = lambda: ["oopif-1"]  # type: ignore[attr-defined]
    baseline = asyncio.run(wait_for.read_text_baseline(session, ""))
    assert baseline is not None and "oopif-1:d:No selection" in baseline

    blocks = asyncio.run(
        wait_for.run_with_baseline(
            session,
            {"condition": "text_changed", "target": "", "value": "", "timeout_ms": 500, "return_mode": "text"},
            baseline=baseline,
        )
    )
    rendered = blocks[0]["text"]
    assert "status=succeeded" in rendered
    assert "basis=text_changed" in rendered
    assert "FRAME-204" in rendered

    blocks_text = asyncio.run(
        wait_for.run(
            session,
            {"condition": "text", "target": "", "value": "FRAME-204", "timeout_ms": 500, "return_mode": "text"},
        )
    )
    rendered_text = blocks_text[0]["text"]
    assert "status=succeeded" in rendered_text
    assert "basis=text_match" in rendered_text
    assert "FRAME-204" in rendered_text
    assert "isRendered" in wait_for._PAGE_TEXT_EXPR
    assert "getBoundingClientRect" in wait_for._PAGE_TEXT_EXPR
    assert "window.innerWidth" in wait_for._page_text_expr(child_frame=True)
