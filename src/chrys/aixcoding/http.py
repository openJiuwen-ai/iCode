# ruff: noqa: RUF002
"""统一 HTTP 出口：串行队列 + 全吞错 + token 头（对齐 aixcoding report.ts/request.ts）。

可靠性契约（方案 §2.1"值得照抄的工程实践"）：

- ``submit()`` 仅入队，fire-and-forget——上报失败只记日志，绝不向调用方抛出；
- 单 worker 逐条 POST，天然保序（tool-detail 的 save→update 链依赖此性质）；
- ``trust_env=False``：内网端点直连，不受本机代理环境变量干扰；
- ``BatchBuffer`` 提供批量缓冲语义（满 N 条或 T 秒 flush、超上限丢旧），
  供 ai-code 等可合并场景使用（对齐 pi-acp collector 的 batchSize=20/10s）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Mapping

import httpx

logger = logging.getLogger(__name__)


class TelemetryHttpClient:
    """fire-and-forget 串行上报客户端（须在事件循环内使用）。"""

    def __init__(self, report_base_url: str, token: str | None = None, *, timeout: float = 10.0) -> None:
        self._report_base_url = report_base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._queue: asyncio.Queue[tuple[str, dict[str, object]]] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None
        self._client: httpx.AsyncClient | None = None
        self._closed = False

    def submit(self, endpoint: str, payload: Mapping[str, object]) -> None:
        """入队一条上报（``endpoint`` 为相对路径，如 ``tool-detail/save``）。"""
        if self._closed:
            return
        if not self._ensure_started():
            logger.warning("telemetry submit outside event loop; report dropped: %s", endpoint)
            return
        self._queue.put_nowait((endpoint, dict(payload)))

    async def wait_drained(self, timeout: float = 10.0) -> None:
        """等待队列清空（测试与优雅停机用）。"""
        await asyncio.wait_for(self._queue.join(), timeout)

    async def stop(self) -> None:
        """取消 worker 并关闭连接（在途条目不再保证送达）。"""
        self._closed = True
        if self._worker is not None:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker
            self._worker = None
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _ensure_started(self) -> bool:
        if self._worker is not None:
            return True
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return False
        self._client = httpx.AsyncClient(timeout=self._timeout, trust_env=False)
        self._worker = asyncio.create_task(self._run())
        return True

    async def _run(self) -> None:
        while True:
            endpoint, payload = await self._queue.get()
            try:
                await self._post(endpoint, payload)
            except Exception:
                logger.warning("telemetry report failed: %s", endpoint, exc_info=True)
            finally:
                self._queue.task_done()

    async def _post(self, endpoint: str, payload: dict[str, object]) -> None:
        if self._client is None:
            return
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["token"] = self._token
        response = await self._client.post(
            f"{self._report_base_url}/{endpoint.lstrip('/')}",
            json=payload,
            headers=headers,
        )
        if response.status_code >= 400:
            logger.warning(
                "telemetry report rejected: %s -> %s %s",
                endpoint,
                response.status_code,
                response.text[:200],
            )


class BatchBuffer:
    """批量缓冲：满 ``flush_size`` 条或 ``flush_interval`` 秒后逐条 submit，超上限丢旧。"""

    def __init__(
        self,
        client: TelemetryHttpClient,
        endpoint: str,
        *,
        flush_size: int = 20,
        flush_interval: float = 10.0,
        max_pending: int = 500,
    ) -> None:
        self._client = client
        self._endpoint = endpoint
        self._flush_size = flush_size
        self._flush_interval = flush_interval
        self._max_pending = max_pending
        self._pending: list[dict[str, object]] = []
        self._timer: asyncio.Task[None] | None = None

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def add(self, payload: Mapping[str, object]) -> None:
        if len(self._pending) >= self._max_pending:
            self._pending.pop(0)
        self._pending.append(dict(payload))
        self._ensure_timer()
        if len(self._pending) >= self._flush_size:
            self.flush()

    def _ensure_timer(self) -> None:
        """在事件循环内首次使用时启动定时 flush（同步构造期无循环也能安全 add）。"""
        if self._timer is not None:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self._timer = asyncio.create_task(self._run())

    def flush(self) -> None:
        while self._pending:
            self._client.submit(self._endpoint, self._pending.pop(0))

    async def start(self) -> None:
        if self._timer is None:
            self._timer = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._timer
            self._timer = None
        self.flush()

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._flush_interval)
            self.flush()
