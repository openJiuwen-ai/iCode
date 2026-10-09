# ruff: noqa: RUF002, RUF003, S101
"""telemetry-mock 行为契约测试（factory import 驱动：进程内起停、零子进程）。

蓝本：agent_studio_new TS 版 ``apps/telemetry-mock/test/telemetry-mock.test.ts``；
M1 验收项"mock 起停/5 端点/观测/故障注入自测"落点。
"""

from __future__ import annotations

import http.client
import json
import time
from pathlib import Path

import pytest
import server
from server import start_telemetry_mock

CSAS = "/csas/telemetry/api/v1"


def _port(mock: server.TelemetryMockServer) -> int:
    return int(mock.origin.rsplit(":", 1)[1])


def _request(
    mock: server.TelemetryMockServer,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    content_length: int | None = None,
) -> tuple[int, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", _port(mock), timeout=10)
    try:
        if content_length is not None:
            conn.putrequest(method, path)
            conn.putheader("Content-Length", str(content_length))
            conn.endheaders()
        else:
            conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def _post_json(mock: server.TelemetryMockServer, path: str, payload: object) -> tuple[int, dict]:
    status, data = _request(mock, "POST", path, body=json.dumps(payload).encode("utf-8"))
    return status, json.loads(data.decode("utf-8"))


def _get_json(mock: server.TelemetryMockServer, path: str) -> tuple[int, dict]:
    status, data = _request(mock, "GET", path)
    return status, json.loads(data.decode("utf-8"))


@pytest.fixture
def mock() -> server.TelemetryMockServer:
    instance = start_telemetry_mock(port=0, quiet=True)
    yield instance
    instance.close()


def _save_payload(session_id: str = "s-1", func_id: str = "f-1", **overrides: object) -> dict:
    payload: dict[str, object] = {
        "productName": "iCode",
        "funcType": 3,
        "funcName": "read_file",
        "funcId": func_id,
        "sessionId": session_id,
        "unknownExtraField": "kept-by-passthrough",
    }
    payload.update(overrides)
    return payload


def test_health(mock: server.TelemetryMockServer) -> None:
    status, payload = _get_json(mock, "/health")
    assert status == 200
    assert payload == {"service": "chrys-telemetry-mock", "status": "ok"}


def test_tool_detail_save_envelope_and_columns(mock: server.TelemetryMockServer) -> None:
    status, payload = _post_json(mock, f"{CSAS}/tool-detail/save", _save_payload(value="/tmp/a.py"))
    assert status == 200
    assert payload["success"] is True
    assert payload["code"] == 200
    assert payload["message"] == "保存成功"
    assert payload["e"] is None

    rows = mock.store.query("tool-detail/save")
    assert len(rows) == 1
    row = rows[0]
    assert row["session_id"] == "s-1"
    assert row["func_id"] == "f-1"
    assert row["func_type"] == 3
    assert row["func_name"] == "read_file"
    assert row["has_value"] == 1
    body = json.loads(row["body_json"])
    assert body["unknownExtraField"] == "kept-by-passthrough"


def test_tool_detail_save_rejected_lands_in_rejected_table(mock: server.TelemetryMockServer) -> None:
    payload = _save_payload()
    del payload["funcType"]  # 必填缺失
    status, response = _post_json(mock, f"{CSAS}/tool-detail/save", payload)
    assert status == 400
    assert response["e"] == "invalid_request"
    assert "funcType" in response["message"]

    assert mock.store.query("tool-detail/save") == []
    rejected = mock.store.query("rejected")
    assert len(rejected) == 1
    assert rejected[0]["interface"] == "tool-detail/save"
    assert "funcType" in rejected[0]["problem"]


def test_tool_detail_save_rejects_wrong_type(mock: server.TelemetryMockServer) -> None:
    status, response = _post_json(mock, f"{CSAS}/tool-detail/save", _save_payload(funcType="three"))
    assert status == 400
    assert "funcType" in response["message"]


def test_batch_save_requires_nonempty_array(mock: server.TelemetryMockServer) -> None:
    status, _ = _post_json(mock, f"{CSAS}/tool-detail/batch-save", [])
    assert status == 400
    assert mock.store.query("tool-detail/batch-save") == []


def test_batch_save_stores_item_count(mock: server.TelemetryMockServer) -> None:
    items = [
        {"funcType": 0, "funcName": "my-skill", "sessionId": "s-9"},
        {"funcType": 1, "funcName": "mcp_tool"},
    ]
    status, _ = _post_json(mock, f"{CSAS}/tool-detail/batch-save", items)
    assert status == 200
    rows = mock.store.query("tool-detail/batch-save")
    assert len(rows) == 1
    assert rows[0]["item_count"] == 2
    assert rows[0]["session_id"] == "s-9"


def test_update_orphan_is_accepted(mock: server.TelemetryMockServer) -> None:
    status, _ = _post_json(mock, f"{CSAS}/tool-detail/update", {"funcId": "ghost", "codeStatus": 1})
    assert status == 200
    rows = mock.store.query("tool-detail/update")
    assert len(rows) == 1
    assert rows[0]["func_id"] == "ghost"
    assert rows[0]["code_status"] == 1


def test_ai_code_save_envelope_and_columns(mock: server.TelemetryMockServer) -> None:
    payload = {
        "reportId": "r-1",
        "sourceType": "edit",
        "filepath": "src/a.py",
        "blocks": [{"snippet": "print()", "rangeStart": 1, "rangeEnd": 2}, {"rangeStart": 5, "rangeEnd": 5}],
    }
    status, response = _post_json(mock, f"{CSAS}/ai-code/save", payload)
    assert status == 200
    assert response == {"code": 200, "message": "success", "data": {"reportId": "r-1"}}

    rows = mock.store.query("ai-code/save")
    assert len(rows) == 1
    assert rows[0]["report_id"] == "r-1"
    assert rows[0]["block_count"] == 2
    assert rows[0]["filepath"] == "src/a.py"


def test_ai_code_rejects_too_many_blocks(mock: server.TelemetryMockServer) -> None:
    payload = {"reportId": "r-2", "sourceType": "edit", "blocks": [{"rangeStart": 0, "rangeEnd": 0}] * 65}
    status, _ = _post_json(mock, f"{CSAS}/ai-code/save", payload)
    assert status == 400


def test_event_reaction_accepts_anything(mock: server.TelemetryMockServer) -> None:
    status, response = _post_json(mock, f"{CSAS}/event-reaction/save", {"anything": "kept"})
    assert status == 200
    assert response["success"] is True
    assert len(mock.store.query("event-reaction/save")) == 1


def test_unknown_path_is_404(mock: server.TelemetryMockServer) -> None:
    status, response = _get_json(mock, "/nope")
    assert status == 404
    assert response == {"error": "not_found"}


def test_invalid_json_is_400(mock: server.TelemetryMockServer) -> None:
    status, data = _request(mock, "POST", f"{CSAS}/tool-detail/save", body=b"not json at all")
    assert status == 400
    assert json.loads(data.decode("utf-8")) == {"error": "invalid_json"}


def test_body_too_large_is_413_without_reading(mock: server.TelemetryMockServer) -> None:
    # 伪造超大 Content-Length：服务器应直接拒绝，不需要客户端真的发送 9MB。
    status, data = _request(mock, "POST", f"{CSAS}/tool-detail/save", content_length=9 * 1024 * 1024)
    assert status == 413
    assert json.loads(data.decode("utf-8")) == {"error": "body_too_large"}


def test_token_auth(mock: server.TelemetryMockServer) -> None:
    with_token = start_telemetry_mock(port=0, quiet=True, require_token="secret-token")  # noqa: S106
    try:
        status, _ = _post_json(with_token, f"{CSAS}/tool-detail/save", _save_payload())
        assert status == 401

        status, _ = _request(
            with_token,
            "POST",
            f"{CSAS}/tool-detail/save",
            body=json.dumps(_save_payload()).encode("utf-8"),
            headers={"token": "wrong"},
        )
        assert status == 401

        status, _ = _request(
            with_token,
            "POST",
            f"{CSAS}/tool-detail/save",
            body=json.dumps(_save_payload()).encode("utf-8"),
            headers={"token": "secret-token"},
        )
        assert status == 200
    finally:
        with_token.close()


def test_fault_http500_still_stores(mock: server.TelemetryMockServer) -> None:
    _, response = _post_json(mock, "/debug/faults", {"mode": "http500"})
    assert response["faults"]["mode"] == "http500"

    status, payload = _post_json(mock, f"{CSAS}/tool-detail/save", _save_payload())
    assert status == 500
    assert payload == {"error": "http500"}
    assert len(mock.store.query("tool-detail/save")) == 1


def test_fault_envelope_reject_business_failure(mock: server.TelemetryMockServer) -> None:
    _post_json(mock, "/debug/faults", {"mode": "envelope_reject"})
    status, response = _post_json(mock, f"{CSAS}/tool-detail/save", _save_payload())
    assert status == 200  # HTTP 层成功，业务层失败
    assert response["success"] is False
    assert response["e"] == "BusinessException"
    assert len(mock.store.query("tool-detail/save")) == 1

    status, response = _post_json(
        mock,
        f"{CSAS}/ai-code/save",
        {"reportId": "r-3", "sourceType": "edit", "blocks": []},
    )
    assert status == 200
    assert response == {"code": 500, "message": "simulated business failure", "data": None}


def test_fault_drop_body_disconnects_without_storing(mock: server.TelemetryMockServer) -> None:
    _post_json(mock, "/debug/faults", {"mode": "drop_body"})
    with pytest.raises((http.client.RemoteDisconnected, ConnectionError, OSError)):
        _request(mock, "POST", f"{CSAS}/tool-detail/save", body=json.dumps(_save_payload()).encode("utf-8"))
    assert mock.store.query("tool-detail/save") == []


def test_fault_slow_delays_then_stores(mock: server.TelemetryMockServer) -> None:
    _post_json(mock, "/debug/faults", {"mode": "slow", "slowMs": 400})
    started = time.monotonic()
    status, _ = _post_json(mock, f"{CSAS}/tool-detail/save", _save_payload())
    elapsed = time.monotonic() - started
    assert status == 200
    assert elapsed >= 0.3
    assert len(mock.store.query("tool-detail/save")) == 1


def test_faults_targeting_by_short_name(mock: server.TelemetryMockServer) -> None:
    _post_json(mock, "/debug/faults", {"mode": "http503", "interfaces": ["tool-detail/update"]})

    status, _ = _post_json(mock, f"{CSAS}/tool-detail/save", _save_payload())
    assert status == 200  # 未定向的接口不受影响

    status, _ = _post_json(mock, f"{CSAS}/tool-detail/update", {"funcId": "f-1", "codeStatus": 1})
    assert status == 503

    _, response = _post_json(mock, "/debug/faults", {"mode": "none"})
    assert response["faults"]["mode"] == "none"
    status, _ = _post_json(mock, f"{CSAS}/tool-detail/update", {"funcId": "f-1", "codeStatus": 1})
    assert status == 200


def test_debug_faults_rejects_invalid_config(mock: server.TelemetryMockServer) -> None:
    status, data = _post_json(mock, "/debug/faults", {"mode": "explode"})
    assert status == 400
    assert data == {"error": "invalid_fault_config"}


def test_debug_reports_filters_by_session(mock: server.TelemetryMockServer) -> None:
    _post_json(mock, f"{CSAS}/tool-detail/save", _save_payload(session_id="s-1", func_id="f-1"))
    _post_json(mock, f"{CSAS}/tool-detail/save", _save_payload(session_id="s-2", func_id="f-2"))

    status, payload = _get_json(mock, "/debug/reports?interface=tool-detail/save&sessionId=s-1")
    assert status == 200
    assert payload["count"] == 1
    assert payload["reports"][0]["session_id"] == "s-1"

    status, _ = _get_json(mock, "/debug/reports?interface=nope")
    assert status == 400


_DUMP_KEYS = {
    "tool-detail/save",
    "tool-detail/batch-save",
    "tool-detail/update",
    "ai-code/save",
    "event-reaction/save",
    "rejected",
}


def test_debug_dump_and_clear(mock: server.TelemetryMockServer) -> None:
    _post_json(mock, f"{CSAS}/tool-detail/save", _save_payload())
    status, dump = _get_json(mock, "/debug/dump")
    assert status == 200
    assert set(dump) == _DUMP_KEYS
    assert dump["tool-detail/save"]

    status, payload = _post_json(mock, "/debug/clear", {})
    assert status == 200
    assert payload == {"cleared": True}
    assert mock.store.dump()["tool-detail/save"] == []


def test_view_page_serves_html(mock: server.TelemetryMockServer) -> None:
    status, data = _request(mock, "GET", "/debug/view")
    assert status == 200
    html = data.decode("utf-8")
    assert "Chrys Telemetry Mock" in html
    assert "/debug/dump" in html

    status, _ = _request(mock, "GET", "/")
    assert status == 200


def test_file_database_persists(tmp_path: Path) -> None:
    database_path = tmp_path / "mock.db"
    instance = start_telemetry_mock(port=0, quiet=True, database_path=str(database_path))
    try:
        _post_json(instance, f"{CSAS}/tool-detail/save", _save_payload())
    finally:
        instance.close()
    assert database_path.is_file()
    assert database_path.stat().st_size > 0
