# ruff: noqa: T201, RUF001, RUF002, RUF003
"""csas telemetry 上报 mock server（Python 标准库零依赖版）。

从 agent_studio_new 的 TS 版 ``apps/telemetry-mock`` 重写，行为契约对齐：

- 5 个 csas 端点（``/csas/telemetry/api/v1/`` 前缀，``token`` 头鉴权，401）；
- 校验失败落 ``rejected_reports`` 表后回 400（zod 风格 problem 文本）；
- 成功 envelope 两种形态：ai-code 用 ``code/message/data``，其余用
  ``success/message/code/timestamp/result/e`` 六键；
- 故障注入 5 模式（http500/http503/slow/envelope_reject/drop_body），
  http500/503/envelope_reject/slow 均**先落库再报错**，drop_body 不落库直接断连；
- 观测端点 6 个：/health、/debug/view（HTML）、/debug/reports、/debug/dump、
  /debug/clear、/debug/faults（运行期切换）；
- SQLite 每接口一表存 ``body_json`` 原文；已按决策清理 rev.5 残留列与幂等机制。

用法（CLI）::

    uv run python aixcoding/telemetry-mock/server.py [--port N] [--db PATH]
        [--require-token TOKEN] [--quiet]

环境变量：``CHRYS_TELEMETRY_MOCK_PORT``（默认 4321）、``CHRYS_TELEMETRY_MOCK_TOKEN``。
pytest 集成测试用 ``start_telemetry_mock(port=0, ...)`` 进程内起停。
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

import faults
from faults import FaultConfig, active_fault, parse_fault_config, short_name
from store import INTERFACE_TABLES, MockStore

CSAS_PREFIX = faults.CSAS_PREFIX
MAX_BODY_BYTES = 8 * 1024 * 1024

# ---------------------------------------------------------------------------
# 校验（对齐 packages/contracts/src/chrys-telemetry-report.ts：必填集最小化，
# 未知字段放行；类型/长度错误一律 400）
# ---------------------------------------------------------------------------

_COMMON_STR_FIELDS = (
    "sessionId",
    "userId",
    "requestId",
    "spanId",
    "projectName",
    "channelType",
    "channelName",
    "channelVersion",
    "pluginVersion",
    "gitRemote",
    "gitBranch",
    "gitRevision",
    "gitOwner",
    "gitRepo",
)


def _check_str(field: str, value: object, *, prefix: str = "", min_len: int = 0, max_len: int = 65536) -> str | None:
    if not isinstance(value, str):
        return f"invalid_type at {prefix}{field}: expected string"
    if len(value) < min_len:
        return f"too_small at {prefix}{field}: min {min_len}"
    if len(value) > max_len:
        return f"too_big at {prefix}{field}: max {max_len}"
    return None


def _check_int(field: str, value: object, *, prefix: str = "") -> str | None:
    ok = isinstance(value, int | float) and not isinstance(value, bool) and value == int(value) and value >= 0
    return None if ok else f"invalid_type at {prefix}{field}: expected non-negative integer"


def _validate_object(
    body: object,
    *,
    required_str: tuple[tuple[str, int, int], ...] = (),
    optional_str: tuple[str, ...] = (),
    required_int: tuple[str, ...] = (),
    optional_int: tuple[str, ...] = (),
    prefix: str = "",
) -> str | None:
    if not isinstance(body, dict):
        return f"invalid_type at {prefix[:-1] if prefix else '(root)'}: expected object"
    for field, min_len, max_len in required_str:
        if field not in body:
            return f"invalid_type at {prefix}{field}: Required"
        problem = _check_str(field, body[field], prefix=prefix, min_len=min_len, max_len=max_len)
        if problem is not None:
            return problem
    for field in required_int:
        if field not in body:
            return f"invalid_type at {prefix}{field}: Required"
        problem = _check_int(field, body[field], prefix=prefix)
        if problem is not None:
            return problem
    for field in optional_str:
        if field in body:
            problem = _check_str(field, body[field], prefix=prefix)
            if problem is not None:
                return problem
    for field in optional_int:
        if field in body:
            problem = _check_int(field, body[field], prefix=prefix)
            if problem is not None:
                return problem
    return None


def _validate_tool_detail_save(body: object) -> str | None:
    problem = _validate_object(
        body,
        required_str=(("productName", 0, 256), ("funcName", 1, 256)),
        optional_str=("funcId", "value", "fileName", *_COMMON_STR_FIELDS),
        required_int=("funcType",),
        optional_int=("codeStatus",),
    )
    if problem is not None:
        return problem
    if isinstance(body, dict) and "extra" in body and not isinstance(body["extra"], dict):
        return "invalid_type at extra: expected object"
    return None


def _validate_batch_save(body: object) -> str | None:
    if not isinstance(body, list):
        return "invalid_type at (root): expected array"
    if len(body) < 1:
        return "too_small at (root): min 1"
    for index, item in enumerate(body):
        problem = _validate_object(
            item,
            required_str=(("funcName", 1, 256),),
            optional_str=_COMMON_STR_FIELDS,
            required_int=("funcType",),
            prefix=f"[{index}].",
        )
        if problem is not None:
            return problem
    return None


def _validate_tool_detail_update(body: object) -> str | None:
    return _validate_object(
        body,
        required_str=(("funcId", 1, 512),),
        optional_str=("funcName", "funcErrorMessage"),
        required_int=("codeStatus",),
        optional_int=("originalLines", "addedLines", "deletedLines"),
    )


def _validate_ai_code_save(body: object) -> str | None:
    problem = _validate_object(
        body,
        required_str=(("reportId", 1, 256), ("sourceType", 1, 64)),
        optional_str=(
            "sessionId",
            "spanId",
            "requestId",
            "channelType",
            "inputMethod",
            "language",
            "remoteUrl",
            "branch",
            "gitUserName",
            "gitUserEmail",
            "filepath",
        ),
    )
    if problem is not None or not isinstance(body, dict):
        return problem if problem is not None else "invalid_type at (root): expected object"
    blocks = body.get("blocks")
    if blocks is None:
        return None
    if not isinstance(blocks, list):
        return "invalid_type at blocks: expected array"
    if len(blocks) > 64:
        return "too_big at blocks: max 64"
    for index, block in enumerate(blocks):
        prefix = f"blocks[{index}]."
        if not isinstance(block, dict):
            return f"invalid_type at {prefix[:-1]}: expected object"
        for field in ("rangeStart", "rangeEnd"):
            if field not in block:
                return f"invalid_type at {prefix}{field}: Required"
            problem = _check_int(field, block[field], prefix=prefix)
            if problem is not None:
                return problem
        if "snippet" in block:
            problem = _check_str("snippet", block["snippet"], prefix=prefix)
            if problem is not None:
                return problem
    return None


_VALIDATORS: dict[str, Any] = {
    "tool-detail/save": _validate_tool_detail_save,
    "tool-detail/batch-save": _validate_batch_save,
    "tool-detail/update": _validate_tool_detail_update,
    "ai-code/save": _validate_ai_code_save,
    # event-reaction/save：宽收，任意 JSON 均通过
    "event-reaction/save": lambda _body: None,
}

_INSERTERS: dict[str, Any] = {
    "tool-detail/save": lambda st, body: st.insert_tool_detail_save(body),
    "tool-detail/batch-save": lambda st, body: st.insert_batch_save(body),
    "tool-detail/update": lambda st, body: st.insert_update(body),
    "ai-code/save": lambda st, body: st.insert_ai_code(body),
    "event-reaction/save": lambda st, body: st.insert_event_reaction(body),
}


# ---------------------------------------------------------------------------
# HTTP 服务
# ---------------------------------------------------------------------------


@dataclass
class _Runtime:
    store: MockStore
    faults_lock: threading.Lock
    fault_config: FaultConfig
    require_token: str | None
    quiet: bool


class _MockHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], runtime: _Runtime) -> None:
        super().__init__(address, _Handler)
        self.runtime = runtime


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "chrys-telemetry-mock/1.0"

    @property
    def _runtime(self) -> _Runtime:
        return self.server.runtime  # type: ignore[attr-defined,no-any-return]

    def log_message(self, format: str, *args: Any) -> None:
        if not self._runtime.quiet:
            super().log_message(format, *args)

    def _log(self, line: str) -> None:
        if not self._runtime.quiet:
            print(f"[telemetry-mock] {line}")

    # -- 响应辅助 -----------------------------------------------------------

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> bytes | None:
        """读取请求体；超过上限返回 ``None``（调用方回 413 并断连）。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > MAX_BODY_BYTES:
            return None
        return self.rfile.read(length) if length > 0 else b""

    # -- 路由 ---------------------------------------------------------------

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/health":
            self._send_json(200, {"service": "chrys-telemetry-mock", "status": "ok"})
        elif path in ("/", "/debug/view"):
            self._send_html(VIEW_PAGE_HTML)
        elif path == "/debug/reports":
            self._handle_reports_query()
        elif path == "/debug/dump":
            self._send_json(200, self._runtime.store.dump())
        else:
            self._send_json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/debug/clear":
            self._read_body()
            self._runtime.store.clear()
            self._send_json(200, {"cleared": True})
        elif path == "/debug/faults":
            self._handle_faults()
        elif path.startswith(CSAS_PREFIX):
            self._handle_report(path)
        else:
            self._read_body()
            self._send_json(404, {"error": "not_found"})

    # -- debug 端点 ---------------------------------------------------------

    def _handle_reports_query(self) -> None:
        params = {key: values[-1] for key, values in parse_qs(urlparse(self.path).query).items()}
        interface = params.get("interface")
        if interface not in INTERFACE_TABLES:
            self._send_json(400, {"error": "invalid_query"})
            return
        session_id = params.get("sessionId") or None
        func_id = params.get("funcId") or None
        try:
            limit = int(params.get("limit", "100"))
        except ValueError:
            self._send_json(400, {"error": "invalid_query"})
            return
        if not 1 <= limit <= 500:
            self._send_json(400, {"error": "invalid_query"})
            return
        rows = self._runtime.store.query(interface, session_id=session_id, func_id=func_id, limit=limit)
        self._send_json(200, {"interface": interface, "count": len(rows), "reports": rows})

    def _handle_faults(self) -> None:
        raw = self._read_body()
        if raw is None:
            self.close_connection = True
            self._send_json(413, {"error": "body_too_large"})
            return
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError, UnicodeDecodeError:
            self._send_json(400, {"error": "invalid_json"})
            return
        config = parse_fault_config(data)
        if config is None:
            self._send_json(400, {"error": "invalid_fault_config"})
            return
        with self._runtime.faults_lock:
            self._runtime.fault_config = config
        self._send_json(200, {"faults": config.as_dict()})

    # -- csas 上报端点 ------------------------------------------------------

    def _handle_report(self, path: str) -> None:
        name = short_name(path)
        runtime = self._runtime

        raw = self._read_body()
        if raw is None:
            # 超限：拒绝读取并断连，避免 keep-alive 流中残留未读字节。
            self.close_connection = True
            self._send_json(413, {"error": "body_too_large"})
            return

        if runtime.require_token is not None and self.headers.get("token") != runtime.require_token:
            self._send_json(401, {"success": False, "message": "unauthorized", "code": 401})
            return

        try:
            body: object = json.loads(raw.decode("utf-8")) if raw else None
        except ValueError, UnicodeDecodeError:
            self._send_json(400, {"error": "invalid_json"})
            return

        problem = _VALIDATORS[name](body)
        if problem is not None:
            runtime.store.insert_rejected(name, problem, body)
            self._log(f"{name} REJECTED: {problem}")
            now_ms = int(time.time() * 1000)
            self._send_json(
                400,
                {
                    "success": False,
                    "message": problem,
                    "code": 400,
                    "timestamp": now_ms,
                    "result": None,
                    "e": "invalid_request",
                },
            )
            return

        with runtime.faults_lock:
            config = runtime.fault_config
        fault = active_fault(config, path, time.time() * 1000)
        if fault == "drop_body":
            self._log(f"{name} dropped (fault)")
            self.close_connection = True
            return
        if fault == "slow":
            time.sleep(config.slow_ms / 1000.0)

        _INSERTERS[name](runtime.store, body)

        if fault in ("http500", "http503"):
            self._send_json(500 if fault == "http500" else 503, {"error": fault})
            return
        if fault == "envelope_reject":
            if name == "ai-code/save":
                self._send_json(200, {"code": 500, "message": "simulated business failure", "data": None})
            else:
                self._send_json(
                    200,
                    {
                        "success": False,
                        "message": "simulated business failure",
                        "code": 500,
                        "timestamp": int(time.time() * 1000),
                        "result": None,
                        "e": "BusinessException",
                    },
                )
            return

        self._log(_summary_line(name, body))
        now_ms = int(time.time() * 1000)
        if name == "ai-code/save":
            report_id = body.get("reportId") if isinstance(body, dict) else None
            data = {"reportId": report_id} if isinstance(report_id, str) else {"reportId": None}
            self._send_json(200, {"code": 200, "message": "success", "data": data})
        else:
            self._send_json(
                200,
                {
                    "success": True,
                    "message": "保存成功",
                    "code": 200,
                    "timestamp": now_ms,
                    "result": None,
                    "e": None,
                },
            )


def _clip(value: object, limit: int = 40) -> str:
    text = str(value) if value is not None else ""
    return text if len(text) <= limit else f"{text[:limit]}…"


def _summary_line(name: str, body: object) -> str:
    if isinstance(body, list):
        return f"{name} ok items={len(body)} [inserted]"
    if not isinstance(body, dict):
        return f"{name} ok ({_clip(body)}) [inserted]"
    if name == "ai-code/save":
        return f"{name} ok reportId={_clip(body.get('reportId'))} [inserted]"
    parts = [
        f"{key}={_clip(body.get(key))}"
        for key in ("sessionId", "funcId", "funcName", "codeStatus")
        if body.get(key) is not None
    ]
    return f"{name} ok {' '.join(parts)} [inserted]"


# ---------------------------------------------------------------------------
# /debug/view HTML 观察页（零依赖：内联 CSS/JS，数据由前端 fetch /debug/dump）
# ---------------------------------------------------------------------------

VIEW_PAGE_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>Chrys Telemetry Mock</title>
<style>
  body { font-family: system-ui, sans-serif; margin: 16px; color: #1f2328; background: #fff; }
  h1 { font-size: 18px; }
  #bar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin: 8px 0 16px; }
  #bar .spacer { flex: 1; }
  input[type=text] { padding: 4px 8px; width: 280px; }
  button { padding: 4px 10px; cursor: pointer; }
  table { border-collapse: collapse; width: 100%; margin-bottom: 8px; font-size: 13px; }
  th, td { border: 1px solid #d0d7de; padding: 3px 6px; text-align: left; vertical-align: top;
           white-space: nowrap; max-width: 360px; overflow: hidden; text-overflow: ellipsis; }
  th { background: #f6f8fa; position: sticky; top: 0; }
  td.bodycell { color: #0969da; cursor: pointer; }
  tr.detail td { white-space: pre-wrap; max-width: none; font-family: ui-monospace, monospace; }
  section.rejected > h2 { color: #fff; background: #cf222e; display: inline-block; padding: 1px 8px;
                          border-radius: 4px; }
  .empty { color: #6e7781; padding: 4px 0 16px; }
  #status { color: #6e7781; font-size: 12px; }
</style>
</head>
<body>
<h1>Chrys Telemetry Mock</h1>
<div id="bar">
  <input type="text" id="filter" placeholder="过滤：sessionId / funcId / requestId / 报文原文">
  <label><input type="checkbox" id="auto"> 自动刷新(2s)</label>
  <button id="refresh">刷新</button>
  <button id="expand-all">全部展开</button>
  <button id="collapse-all">全部收起</button>
  <button id="clear">清空</button>
  <span class="spacer"></span>
  <span id="status"></span>
</div>
<div id="sections"></div>
<script>
"use strict";
const ORDER = ["rejected", "tool-detail/save", "tool-detail/update", "tool-detail/batch-save",
                "ai-code/save", "event-reaction/save"];
const TITLES = {
  "rejected": "被拒报文（校验失败，红色=映射 bug 嫌疑）",
  "tool-detail/save": "tool-detail/save",
  "tool-detail/update": "tool-detail/update",
  "tool-detail/batch-save": "tool-detail/batch-save",
  "ai-code/save": "ai-code/save",
  "event-reaction/save": "event-reaction/save",
};
const COLUMNS = {
  "rejected": ["received_at", "interface", "problem", "body_json"],
  "tool-detail/save": ["received_at", "session_id", "func_id", "request_id", "span_id",
                       "func_name", "code_status", "body_json"],
  "tool-detail/update": ["received_at", "func_id", "code_status", "original_lines",
                         "added_lines", "deleted_lines", "body_json"],
  "tool-detail/batch-save": ["received_at", "session_id", "item_count", "body_json"],
  "ai-code/save": ["received_at", "report_id", "session_id", "filepath", "block_count", "body_json"],
  "event-reaction/save": ["received_at", "body_json"],
};
const LABELS = {
  "received_at": "时间", "session_id": "sessionId", "func_id": "funcId",
  "request_id": "requestId", "span_id": "spanId", "func_name": "funcName",
  "code_status": "codeStatus", "item_count": "条数", "report_id": "reportId",
  "original_lines": "原行", "added_lines": "增行", "deleted_lines": "删行",
  "problem": "拒绝原因", "interface": "接口", "body_json": "原始报文", "filepath": "filepath",
  "block_count": "blocks",
};
const expanded = new Set();

function textCell(value) {
  const td = document.createElement("td");
  td.textContent = value === null || value === undefined ? "" : String(value);
  return td;
}

function filterText(row) {
  return [row.session_id, row.func_id, row.request_id, row.body_json]
    .filter(Boolean).join(" ").toLowerCase();
}

function renderRow(interfaceName, row) {
  const tr = document.createElement("tr");
  for (const column of COLUMNS[interfaceName]) {
    if (column === "body_json") {
      const td = document.createElement("td");
      td.className = "bodycell";
      td.textContent = "点击展开…";
      td._bodyJson = row.body_json;
      td.addEventListener("click", () => setDetail(tr, td._bodyJson, !hasDetail(tr)));
      tr.appendChild(td);
    } else {
      tr.appendChild(textCell(row[column]));
    }
  }
  return tr;
}

function hasDetail(tr) {
  return Boolean(tr.nextSibling && tr.nextSibling.className === "detail");
}

function setDetail(tr, bodyJson, show) {
  const existing = hasDetail(tr) ? tr.nextSibling : null;
  if (show && !existing) {
    const detail = document.createElement("tr");
    detail.className = "detail";
    const td = document.createElement("td");
    td.colSpan = tr.cells.length;
    const pre = document.createElement("pre");
    try { pre.textContent = JSON.stringify(JSON.parse(bodyJson), null, 2); }
    catch { pre.textContent = bodyJson; }
    td.appendChild(pre);
    detail.appendChild(td);
    tr.parentNode.insertBefore(detail, tr.nextSibling);
  }
  if (!show && existing) tr.parentNode.removeChild(existing);
}

async function refresh() {
  let data;
  try {
    const response = await fetch("/debug/dump");
    data = await response.json();
  } catch (err) {
    document.getElementById("status").textContent = "刷新失败: " + err;
    return;
  }
  const needle = document.getElementById("filter").value.trim().toLowerCase();
  const sections = document.getElementById("sections");
  sections.textContent = "";
  let total = 0;
  for (const interfaceName of ORDER) {
    const rows = (data[interfaceName] || []).filter((row) => !needle || filterText(row).includes(needle));
    total += rows.length;
    const section = document.createElement("section");
    section.className = interfaceName === "rejected" ? "rejected" : "";
    const heading = document.createElement("h2");
    heading.textContent = TITLES[interfaceName] + " · " + rows.length + " 条";
    section.appendChild(heading);
    if (rows.length === 0) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = "（无记录）";
      section.appendChild(empty);
    } else {
      const table = document.createElement("table");
      const headRow = document.createElement("tr");
      for (const column of COLUMNS[interfaceName]) {
        const th = document.createElement("th");
        th.textContent = LABELS[column] || column;
        headRow.appendChild(th);
      }
      table.appendChild(headRow);
      for (const row of rows) table.appendChild(renderRow(interfaceName, row));
      section.appendChild(table);
    }
    sections.appendChild(section);
  }
  document.getElementById("status").textContent = "共 " + total + " 条 · " + new Date().toLocaleTimeString();
}

document.getElementById("refresh").addEventListener("click", refresh);
document.getElementById("filter").addEventListener("input", refresh);
document.getElementById("expand-all").addEventListener("click", () => {
  document.querySelectorAll("td.bodycell").forEach((cell) => setDetail(cell.parentElement, cell._bodyJson, true));
});
document.getElementById("collapse-all").addEventListener("click", () => {
  document.querySelectorAll("tr.detail").forEach((detail) => detail.parentNode.removeChild(detail));
});
document.getElementById("clear").addEventListener("click", async () => {
  await fetch("/debug/clear", { method: "POST" });
  refresh();
});
setInterval(() => {
  if (document.getElementById("auto").checked && !document.hidden) refresh();
}, 2000);
refresh();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# factory 与 CLI
# ---------------------------------------------------------------------------


class TelemetryMockServer:
    """进程内 mock 实例（pytest 与 CLI 共用）。"""

    def __init__(self, http_server: _MockHTTPServer, thread: threading.Thread, store: MockStore) -> None:
        self._http_server = http_server
        self._thread = thread
        self._store = store

    @property
    def origin(self) -> str:
        host, port = self._http_server.server_address[:2]
        display = f"[{host}]" if ":" in host else host
        return f"http://{display}:{port}"

    @property
    def database_path(self) -> str:
        return self._store.database_path

    @property
    def store(self) -> MockStore:
        return self._store

    def close(self) -> None:
        self._http_server.shutdown()
        self._http_server.server_close()
        self._thread.join(timeout=5)
        self._store.close()


def start_telemetry_mock(
    *,
    host: str = "127.0.0.1",
    port: int | None = None,
    database_path: str = ":memory:",
    require_token: str | None = None,
    quiet: bool = False,
) -> TelemetryMockServer:
    """启动 mock server（后台线程）并返回可 ``close()`` 的实例。

    ``port=None`` 时取环境变量 ``CHRYS_TELEMETRY_MOCK_PORT``（默认 4321）；
    ``port=0`` 由系统分配（测试用）。仅允许绑定 loopback。
    """
    if host not in ("127.0.0.1", "::1"):
        raise ValueError("telemetry mock binds loopback only")
    if port is None:
        port = int(os.environ.get("CHRYS_TELEMETRY_MOCK_PORT", "4321"))
    if not 0 <= port <= 65535:
        raise ValueError(f"invalid port: {port}")

    mock_store = MockStore(database_path)
    runtime = _Runtime(
        store=mock_store,
        faults_lock=threading.Lock(),
        fault_config=FaultConfig(),
        require_token=require_token,
        quiet=quiet,
    )
    http_server = _MockHTTPServer((host, port), runtime)
    thread = threading.Thread(target=http_server.serve_forever, name="telemetry-mock", daemon=True)
    thread.start()
    return TelemetryMockServer(http_server, thread, mock_store)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="csas telemetry mock server (loopback only)")
    parser.add_argument(
        "--port", type=int, default=None, help="listen port (default: CHRYS_TELEMETRY_MOCK_PORT or 4321)"
    )
    parser.add_argument("--db", default=":memory:", help="SQLite path (default: :memory:)")
    parser.add_argument("--require-token", default=None, help="require exact 'token' header value")
    parser.add_argument("--quiet", action="store_true", help="suppress request logging")
    args = parser.parse_args(argv)

    token = args.require_token or os.environ.get("CHRYS_TELEMETRY_MOCK_TOKEN")
    server = start_telemetry_mock(
        port=args.port,
        database_path=args.db,
        require_token=token,
        quiet=args.quiet,
    )
    print(f"[telemetry-mock] listening at {server.origin}")
    print(f"[telemetry-mock] database: {server.database_path}")
    print(f"[telemetry-mock] view: {server.origin}/debug/view")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        server.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
