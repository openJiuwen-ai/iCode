# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``BrowserSession`` - the single owner of page state for one plugin process.

Holds the ``pageEpoch`` counter, the reference map, the browser subprocess, the
CDP connection and the action lock. Everything else in the package is either a
pure function or a thin CDP wrapper; this module is where the invariants that
span mechanisms live (epoch bumping, budget accounting, timeout recycling,
fail-fast disconnects, login-wait bookkeeping).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import secrets
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .cdp.client import CDPConnection
from .cdp.launcher import ChildProcess, LaunchResult, launch_chrome
from .cdp.targets import TargetManager
from .config import (
    ACTION_WINDOW_SECONDS,
    LOGIN_WAIT_MAX,
    MAX_TIMEOUTS_BEFORE_RECYCLE,
    BrowserConfig,
)
from .dialog import DialogInfo, redact_message
from .downloads import DownloadInfo, display_url, file_size, remove_quiet, reserve_path, sanitize_filename
from .errors import (
    BadRequest,
    BrowserError,
    Busy,
    CDPTimeout,
    ConnectionClosed,
    DialogPending,
    LoginRequired,
    StaleRef,
    TargetNotFound,
    Unavailable,
)
from .journal import Journal
from .locking import InstanceLock, acquire_instance_lock
from .logging import log
from .page_log import PageLog
from .snapshot.fusion import FrameInfo, FusedNode
from .snapshot.scoring import rank, resolve_ambiguity
from .transport import AttachedTransport, OwnedTransport, Transport

#: Read the wall or explicitly leave it; neither escape action authorizes writes.
LOGIN_PENDING_ALLOWED = frozenset(
    {
        "browser_snapshot",
        "browser_navigate",
        "browser_back",
        "browser_forward",
        "browser_wait_for",
        "browser_downloads",
        "browser_download_cancel",
        "browser_logs",
        "browser_close",
    }
)
_LOGIN_PENDING_HINT = "、".join(sorted(LOGIN_PENDING_ALLOWED))

#: Actions that still work while a JavaScript dialog has paused the page's script.
#: Everything else needs the renderer and is refused with ``browser_dialog_open``.
DIALOG_PENDING_ALLOWED = frozenset(
    {
        "browser_dialog",
        "browser_action_status",
        "browser_tabs",
        "browser_downloads",
        "browser_download_cancel",
        "browser_logs",
        "browser_close",
    }
)

#: How many download records one handle keeps (newest last); files stay on disk.
_MAX_DOWNLOADS = 50

#: Concurrent in-progress downloads tracked; further ones are canceled at once.
_MAX_ACTIVE_DOWNLOADS = 20

_REF_RE = re.compile(r"^@e(\d+)(?:#(\d+))?$")

#: Refuse to adopt more of our own tabs than this; extra popups stay user pages.
_MAX_PAGES = 8

#: Reads viewport/scroll/DPR in one shot for coordinate conversion.
_METRICS_EXPR = (
    "(() => ({w: window.innerWidth, h: window.innerHeight, x: window.scrollX,"
    " y: window.scrollY, dpr: window.devicePixelRatio || 1, title: document.title}))()"
)

#: Confirms a resolved node is still connected and its coarse fingerprint holds.
_LIVE_CHECK_FN = (
    "function(){ if(!this || !this.isConnected) return {ok:false};"
    " const t = this.tagName ? this.tagName.toLowerCase() : '';"
    " const n = (this.getAttribute && (this.getAttribute('aria-label') ||"
    " this.getAttribute('title') || this.getAttribute('placeholder'))) ||"
    " (this.innerText || this.textContent || '');"
    " return {ok:true, tag:t, name:String(n).replace(/\\s+/g,' ').trim().slice(0,120)}; }"
)

# The count on a live Like button can change while its action stays the same.
# Keep Like and Unlike distinct so a stale ref cannot toggle the opposite state.
_COUNTED_LIKE_LABEL = re.compile(r"[0-9][0-9,]* Likes?\. (Like|Unlike)", re.IGNORECASE)


def _matches_live_ref_name(role: str, expected: str, actual: str) -> bool:
    if expected == actual:
        return True
    if role != "button":
        return False
    previous = _COUNTED_LIKE_LABEL.fullmatch(expected)
    current = _COUNTED_LIKE_LABEL.fullmatch(actual)
    return bool(previous and current and previous[1].casefold() == current[1].casefold())


#: How many finished navigations to remember per loader id.
_MAX_REMEMBERED_LOADS = 64

ClientFactory = Callable[[str], Awaitable[CDPConnection]]
LauncherFactory = Callable[[BrowserConfig, Path], Awaitable[LaunchResult]]


@dataclass
class RefEntry:
    """A model-visible reference bound to one page generation."""

    index: int
    epoch: int
    fingerprint: dict[str, str]
    backend_node_id: int
    frame_id: str
    target_id: str
    bbox: tuple[float, float, float, float]
    text_hint: str
    cdp_session_id: str | None = None
    """Child CDP session for an OOPIF entry; None for the main frame."""

    @property
    def token(self) -> str:
        """The ``@eN#epoch`` spelling the model uses."""
        return f"@e{self.index}#{self.epoch}"


@dataclass
class SnapshotState:
    """The most recent fused snapshot, used by reference resolution."""

    nodes: list[FusedNode] = field(default_factory=list)
    previous_nodes: list[FusedNode] = field(default_factory=list)
    version: int = 0
    url: str = ""
    title: str = ""


@dataclass
class PageState:
    """One page target this handle owns: the tab it created, or a popup it opened."""

    target_id: str
    session_id: str
    opener_id: str | None
    url: str = ""
    title: str = ""
    active: bool | None = None


@dataclass(frozen=True)
class PageInfo:
    """Model-facing view of one owned page (no CDP session ids)."""

    index: int
    target_id: str
    url: str
    title: str
    opener_id: str | None
    current: bool
    active: bool | None = None


@dataclass
class _OopifState:
    """Lifecycle and cached CDP state for one attached out-of-process iframe session."""

    target_id: str
    parent_session_id: str | None = None
    page_target_id: str = ""
    offset: tuple[float, float] = (0.0, 0.0)
    enabled: bool = False
    frame_id: str = ""

    def clear_connection_cache(self) -> None:
        """Drop per-connection caches on temporary disconnect while keeping topology."""
        self.offset = (0.0, 0.0)
        self.enabled = False
        self.frame_id = ""


class BrowserSession:
    """Per-process browser session; lazily starts Chrome on the first action."""

    def __init__(
        self,
        cfg: BrowserConfig,
        *,
        client_factory: ClientFactory | None = None,
        launcher_factory: LauncherFactory | None = None,
        journal_owner: str = "",
        journal_session: str = "",
    ) -> None:
        self.cfg = cfg
        self._journal_owner = journal_owner
        self._journal_session = journal_session
        self._journal: Journal | None = None
        self._client_factory: ClientFactory = client_factory or _default_client_factory
        self._launcher_factory: LauncherFactory = launcher_factory or launch_chrome
        if cfg.bridge:
            from .bridge.transport import BridgeTransport

            self._transport: Transport = BridgeTransport(cfg)
        else:
            self._transport = (
                AttachedTransport(cfg, self._client_factory)
                if cfg.cdp_url or cfg.chrome_devtools
                else OwnedTransport(cfg, self._client_factory, self._launcher_factory)
            )

        self._state = "idle"
        self._started_once = False
        self._client: CDPConnection | None = None
        self._child: ChildProcess | None = None
        self._targets: TargetManager | None = None
        self._instance_lock: InstanceLock | None = None

        self._target_id: str = ""
        self._session_id: str = ""
        self._main_frame_id: str = ""
        self._policy: Any = None

        #: Owned pages: the tab this handle created plus popups it opened.
        self._pages: dict[str, PageState] = {}
        self._page_order: list[str] = []
        #: Attached out-of-process iframes keyed by child CDP session id.
        self._oopifs: dict[str, _OopifState] = {}
        #: Duplicate sessions Chrome auto-attached to a tab we already drive.
        self._duplicate_sessions: dict[str, str] = {}
        #: Page targets opened by someone else (user, extension): detached, never closed.
        self._foreign_targets: set[str] = set()
        #: True while ``_open_page`` creates and attaches its own tab; attach events
        #: for that tab must not be mistaken for a popup or a foreign page.
        self._opening_page = False
        #: Attach-mode owned target IDs remembered across an unexpected CDP disconnect
        #: so reconnect can reattach (or close) the original tab instead of leaking it.
        self._detached_target_ids: list[str] = []
        #: Best-effort browser identity (product/revision) for Attach reconnect checks.
        self._browser_identity: str = ""
        #: Detached Attach-mode targets that could not be closed when ``aclose()`` ran.
        self._unclosed_targets: list[str] = []

        self._page_epoch: int = secrets.randbits(32)
        self._ref_map: dict[int, RefEntry] = {}
        self._next_index = 1
        self._snapshot = SnapshotState()
        self._dirty = False
        self._probe_seen: int | None = None
        self._probe_script_id: str = ""

        self._timeouts: dict[str, int] = {}
        self._actions: deque[float] = deque()
        self._lifetime_actions = 0

        self._login_pending_since: float | None = None
        self._document_status: dict[tuple[str, str, str], int] = {}

        #: The JavaScript dialog currently blocking the page, and the last one that ended.
        self._dialog: DialogInfo | None = None
        self._last_dialog: DialogInfo | None = None
        self._dialog_seq = 0
        self._dialog_task: asyncio.Task[None] | None = None
        #: Superseded dialogs whose own javascriptDialogClosed event is still due.
        self._dialog_orphans = 0

        #: Controlled downloads exist only when this handle owns the browser (and
        #: the config opted in): the folder is session-scoped, so Attach/Bridge
        #: never redirect a user's own downloads.
        self._download_dir: Path | None = (
            cfg.download_dir if cfg.download_dir is not None and self._transport.owns_browser else None
        )
        self._downloads: dict[str, DownloadInfo] = {}
        self._download_order: list[str] = []

        self._load_done: dict[str, None] = {}  # insertion-ordered, oldest evicted first
        self._load_events: dict[str, asyncio.Event] = {}
        self._frame_loader: dict[str, str] = {}
        self._nav_seq = 0
        #: Recent current-page navigations as (seq, loader id, main-frame url, child frame?);
        #: loader "" = same-document (or a child frame's commit, which keeps the main url).
        self._navs: deque[tuple[int, str, str, bool]] = deque(maxlen=32)
        #: The newest cross-document commit as (seq, loader id), kept apart so a burst of
        #: same-document navigations can never push it out of ``_navs``.
        self._last_commit: tuple[int, str] = (0, "")
        self._nav_changed = asyncio.Event()
        self._page_log = PageLog()

        self._action_lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self._page_lock = asyncio.Lock()
        self._closed = False
        self._last_active = time.monotonic()
        self._idle_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------ state

    @property
    def state(self) -> str:
        """One of ``idle`` / ``starting`` / ``ready`` / ``hung`` / ``failed`` / ``closing`` / ``closed``."""
        return self._state

    @property
    def started_once(self) -> bool:
        """False while the session is still an allocation; distinguishes allocated from idle-closed."""
        return self._started_once

    @property
    def unclosed_targets(self) -> tuple[str, ...]:
        """Detached Attach-mode tab IDs that could not be closed during ``aclose()``."""
        return tuple(self._unclosed_targets)

    @property
    def journal(self) -> Journal:
        """This session's action journal; opened lazily so a walk-away handle writes nothing."""
        if self._journal is None:
            self._journal = Journal(
                self.cfg.state_dir / "journal.sqlite3",
                owner=self._journal_owner,
                logical_session=self._journal_session,
            )
        return self._journal

    @property
    def page_epoch(self) -> int:
        """The current page generation (random 32-bit seed per process)."""
        return self._page_epoch

    @property
    def target_id(self) -> str:
        return self._target_id

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def snapshot(self) -> SnapshotState:
        return self._snapshot

    @property
    def login_pending(self) -> bool:
        return self._login_pending_since is not None

    @property
    def dialog_seq(self) -> int:
        """How many dialogs this session has seen; actions compare it before/after."""
        return self._dialog_seq

    def pending_dialog(self) -> DialogInfo | None:
        """The dialog currently blocking the page, if any."""
        return self._dialog

    def last_dialog(self) -> DialogInfo | None:
        """The most recent dialog, open or closed (its ``reason`` explains the end)."""
        return self._dialog or self._last_dialog

    def dialog_since(self, seq: int) -> DialogInfo | None:
        """The latest dialog if one opened after *seq*; closed ones still count."""
        info = self.last_dialog()
        return info if info is not None and info.seq > seq else None

    # -------------------------------------------------------------- downloads

    @property
    def download_directory(self) -> Path | None:
        """The controlled-download folder, or None when downloads are disabled."""
        return self._download_dir

    def list_downloads(self) -> list[DownloadInfo]:
        """Download records for this handle, oldest first."""
        return [self._downloads[guid] for guid in self._download_order if guid in self._downloads]

    async def cancel_download(self, guid: str) -> DownloadInfo:
        """Cancel an in-progress download; a finished record is returned unchanged."""
        info = self._downloads.get(guid)
        if info is None:
            raise BadRequest(f"未知下载：{guid}（请先 browser_downloads）")
        if info.state != "in_progress":
            return info
        # Claim the record before awaiting: a completion event must not rename the
        # payload while the cancel command is in flight.
        info.reason = "canceled by browser_download_cancel"
        info.state = "canceled"
        await self._cancel_transfer(guid)
        if info.state == "canceled":
            self._discard_download_files(info)
        return info

    @property
    def last_main_status(self) -> int | None:
        loader = self._frame_loader.get(self._main_frame_id, "")
        return self._document_status.get((self._session_id, self._main_frame_id, loader))

    def register_oopif(
        self,
        cdp_session_id: str,
        target_id: str = "",
        *,
        parent_session_id: str | None = None,
        page_target_id: str = "",
    ) -> None:
        """Record an attached OOPIF session and its parent/page topology."""
        self._oopifs[cdp_session_id] = _OopifState(
            target_id=target_id,
            parent_session_id=parent_session_id,
            page_target_id=page_target_id,
        )

    @property
    def oopif_sessions(self) -> list[str]:
        """CDP sessions of currently attached out-of-process iframes."""
        return list(self._oopifs)

    def oopif_parent(self, cdp_session_id: str) -> str | None:
        """The parent OOPIF session, or None when the frame is a direct child of the page."""
        state = self._oopifs.get(cdp_session_id)
        return state.parent_session_id if state is not None else None

    def oopif_children(self, cdp_session_id: str | None) -> list[str]:
        """Direct OOPIF children of *cdp_session_id* (None means the current page's main frame)."""
        if cdp_session_id is not None:
            return [child for child, state in self._oopifs.items() if state.parent_session_id == cdp_session_id]
        # Unmapped children only occur before a page session is known (startup/tests).
        return [
            child
            for child, state in self._oopifs.items()
            if state.parent_session_id is None and state.page_target_id in {self._target_id, ""}
        ]

    def set_oopif_offset(self, cdp_session_id: str, offset: tuple[float, float]) -> None:
        """Remember an OOPIF's iframe-origin in the parent viewport."""
        state = self._oopifs.get(cdp_session_id)
        if state is not None:
            state.offset = offset

    def oopif_offset(self, cdp_session_id: str | None) -> tuple[float, float]:
        """The parent-viewport offset of an OOPIF, or ``(0, 0)`` for the main frame."""
        if not cdp_session_id:
            return (0.0, 0.0)
        state = self._oopifs.get(cdp_session_id)
        return state.offset if state is not None else (0.0, 0.0)

    def is_oopif_enabled(self, cdp_session_id: str) -> bool:
        """Whether CDP domains have been enabled for this attached OOPIF session."""
        state = self._oopifs.get(cdp_session_id)
        return state.enabled if state is not None else False

    def mark_oopif_enabled(self, cdp_session_id: str) -> None:
        """Remember that CDP domains are enabled on *cdp_session_id* until detach or reconnect."""
        state = self._oopifs.get(cdp_session_id)
        if state is not None:
            state.enabled = True

    def oopif_frame_id(self, cdp_session_id: str) -> str:
        """Cached root frameId for an attached OOPIF session, or empty when unknown."""
        state = self._oopifs.get(cdp_session_id)
        return state.frame_id if state is not None else ""

    def set_oopif_frame_id(self, cdp_session_id: str, frame_id: str) -> None:
        """Remember the root frameId of an attached OOPIF session."""
        if not frame_id:
            return
        state = self._oopifs.get(cdp_session_id)
        if state is not None:
            state.frame_id = frame_id

    # ------------------------------------------------------------------ pages

    def pages(self) -> list[PageInfo]:
        """The tabs this handle owns, in creation order (root first)."""
        result: list[PageInfo] = []
        for target_id in self._page_order:
            page = self._pages.get(target_id)
            if page is None:
                continue
            result.append(
                PageInfo(
                    index=len(result) + 1,
                    target_id=page.target_id,
                    url=page.url,
                    title=page.title,
                    opener_id=page.opener_id,
                    current=page.target_id == self._target_id,
                    active=page.active,
                )
            )
        return result

    async def current_page_foreground(self) -> bool | None:
        """Best-effort read-only check of whether the current tab is foreground/visible."""
        client = self._client
        if client is None or not self._target_id:
            return None
        caps = getattr(client, "capabilities", None) or ()
        if not self._transport.browser_commands and "tab_state" in caps:
            try:
                res = await client.send(
                    "Bridge.tabState",
                    {"targetId": self._target_id},
                    timeout=min(2.0, self.cfg.cdp_timeout),
                )
            except Exception:
                return None
            if isinstance(res, dict) and "active" in res:
                active = bool(res["active"])
                page = self._pages.get(self._target_id)
                if page is not None:
                    page.active = active
                return active
            return None
        if not self._session_id:
            return None
        try:
            res = await client.send(
                "Runtime.evaluate",
                {"expression": "document.visibilityState === 'visible'", "returnByValue": True},
                session_id=self._session_id,
                timeout=min(2.0, self.cfg.evaluate_timeout),
            )
        except Exception:
            return None
        val = (res.get("result") or {}).get("value") if isinstance(res, dict) else None
        if isinstance(val, bool):
            page = self._pages.get(self._target_id)
            if page is not None:
                page.active = val
            return val
        return None

    async def list_pages(self) -> list[PageInfo]:
        """Refresh url/title from the browser, then return :meth:`pages`."""
        client = self._client
        if client is not None and self._pages and self._transport.browser_commands:
            response: dict[str, Any] | None = None
            with contextlib.suppress(Exception):
                response = await client.send(
                    "Target.getTargets",
                    timeout=min(5.0, self.cfg.cdp_timeout),
                )
            if response is not None:
                async with self._page_lock:
                    infos = response.get("targetInfos") or []
                    seen = {str(info.get("targetId", "")) for info in infos}
                    for info in infos:
                        page = self._pages.get(str(info.get("targetId", "")))
                        if page is None:
                            continue
                        page.url = str(info.get("url") or page.url)
                        page.title = str(info.get("title") or page.title)
                    self._foreign_targets &= seen
                    # A tab closed while it was not current emits its detach event
                    # too, but targetInfos is the authoritative list when available.
                    for target_id in list(self._pages):
                        if target_id != self._target_id and target_id not in seen:
                            self._forget_page(target_id)
            if self._dialog is None:
                await self.current_page_foreground()
        elif client is not None and self._pages and "tab_state" in (getattr(client, "capabilities", None) or ()):
            for target_id in list(self._page_order):
                page = self._pages.get(target_id)
                if page is None:
                    continue
                with contextlib.suppress(Exception):
                    state = await client.send(
                        "Bridge.tabState",
                        {"targetId": target_id},
                        timeout=min(2.0, self.cfg.cdp_timeout),
                    )
                    if isinstance(state, dict):
                        page.url = str(state.get("url") or page.url)
                        page.title = str(state.get("title") or page.title)
                        if "active" in state:
                            page.active = bool(state["active"])
        return self.pages()

    async def switch_page(self, target_id: str) -> PageInfo:
        """Make *target_id* the current page; refs from the old page become stale."""
        async with self._page_lock:
            page = self._pages.get(target_id)
            if page is None:
                available = ", ".join(info.target_id for info in self.pages()) or "无"
                raise TargetNotFound(f"未知标签页：{target_id}（可用：{available}；请先 browser_tabs）")
            if target_id != self._target_id:
                await self._activate_page(page)
            info = next((item for item in self.pages() if item.target_id == target_id), None)
            if info is None:
                raise TargetNotFound(f"标签页在切换过程中关闭：{target_id}；请重新 browser_tabs")
            return info

    async def close_page(self, target_id: str) -> None:
        """Close one owned tab; closing the current one falls back to its opener (or the root).

        The close is sent first and the record dropped only once it succeeded, so a
        failed close is reported and the tab stays tracked; holding the page lock
        across both means the detach event that follows finds nothing left to do.
        """
        async with self._page_lock:
            if target_id not in self._pages:
                available = ", ".join(info.target_id for info in self.pages()) or "无"
                raise TargetNotFound(f"未知标签页：{target_id}（可用：{available}；请先 browser_tabs）")
            if len(self._pages) == 1:
                raise BadRequest("这是此句柄唯一的标签页，不能单独关闭；结束会话请用 browser_close")
            try:
                result = await self.require_client().send(
                    "Target.closeTarget", {"targetId": target_id}, timeout=self.cfg.cdp_timeout
                )
            except CDPTimeout as exc:
                # A browser-level hang, not the current page's: recycling the current
                # page (what a CDPTimeout leads to) cannot help, so report it plainly.
                raise Unavailable(
                    f"关闭标签页 {target_id} 超时；它仍被跟踪，可稍后重试 browser_tab_close 或用 browser_tabs 复核"
                ) from exc
            if result.get("success") is False:
                raise Unavailable(f"浏览器拒绝关闭标签页 {target_id}；请 browser_tabs 复核")
            await self._drop_page(target_id)

    def require_client(self) -> CDPConnection:
        """Return the live CDP connection or raise :class:`Unavailable`."""
        if self._client is None:
            raise Unavailable("浏览器未启动或已断开，将在下一次动作时重新启动")
        if self._client.closed:
            raise ConnectionClosed("CDP 连接已关闭")
        return self._client

    @property
    def client(self) -> CDPConnection:
        """The live CDP connection (raises :class:`Unavailable` when not started)."""
        return self.require_client()

    # ------------------------------------------------------------- lifecycle

    async def ensure_started(self) -> None:
        """Start Chrome and handshake on first use; single-flight across callers."""
        if self._state == "ready" and self._client is not None:
            if not self._client.closed:
                return
            self.mark_disconnected()
        async with self._start_lock:
            if self._closed:
                raise Unavailable("会话已关闭")
            if self._state == "ready" and self._client is not None:
                return
            self._state = "starting"
            try:
                # A previous attempt may have left a live child (disconnect /
                # timeout recovery); reap it before starting a fresh one.
                await self._teardown_transport()
                await self._start()
            except BaseException:
                self._state = "failed"
                await self._teardown_transport()
                raise
            self._state = "ready"
            self._start_idle_watchdog()

    async def _start(self) -> None:
        if self._transport.owns_browser:
            self._instance_lock = await acquire_instance_lock(self.cfg.state_dir)
        client, child = await self._transport.start()
        self._client = client
        self._child = child
        self._targets = TargetManager(self._client)
        self._register_handlers()
        await self._open_page()
        self._started_once = True
        self._state = "ready"

    async def _read_browser_identity(self) -> str:
        """Read a coarse browser identity string for Attach reconnect verification."""
        client = self._client
        if client is None or not self._transport.browser_commands:
            return ""
        try:
            info = await client.send("Browser.getVersion", timeout=min(5.0, self.cfg.cdp_timeout))
        except Exception:
            return ""
        if not isinstance(info, dict):
            return ""
        parts = [str(info.get(key) or "") for key in ("product", "revision", "userAgent")]
        return "|".join(parts) if any(parts) else ""

    async def _find_reattach_target(self, targets: TargetManager) -> str:
        """Return a surviving detached Attach-mode target on the same browser, or ``""``."""
        detached = list(self._detached_target_ids)
        self._detached_target_ids.clear()
        if not detached or self._transport.owns_browser or not self._transport.browser_commands:
            return ""
        if self._browser_identity:
            current_identity = await self._read_browser_identity()
            if current_identity and current_identity != self._browser_identity:
                return ""
        client = self._client
        if client is None:
            return ""
        try:
            response = await client.send("Target.getTargets", timeout=min(5.0, self.cfg.cdp_timeout))
        except Exception:
            return ""
        infos = response.get("targetInfos") if isinstance(response, dict) else None
        if not isinstance(infos, list):
            return ""
        live_pages = {
            str(info.get("targetId", ""))
            for info in infos
            if isinstance(info, dict) and info.get("type") == "page" and info.get("targetId")
        }
        surviving = [tid for tid in detached if tid in live_pages]
        if not surviving:
            return ""
        primary = surviving[0]
        for extra in surviving[1:]:
            with contextlib.suppress(Exception):
                await targets.close(extra)
        return primary

    async def _open_page(self) -> None:
        targets = self._targets
        if targets is None:
            raise Unavailable("内部错误：标签管理器尚未初始化")
        # Clear the owned id before creating the tab: Chrome can emit
        # ``Target.attachedToTarget`` for the new tab before ``createTarget``
        # returns, and the attach handler must not treat it as a popup or a
        # foreign page while it is still being recorded here.
        previous = self._target_id
        previous_session = self._session_id
        previous_frame = self._main_frame_id
        previous_title = self._snapshot.title
        created = ""
        reattach_id = await self._find_reattach_target(targets)
        self._target_id = ""
        self._snapshot.title = ""
        self._opening_page = True
        try:
            target_id = ""
            session_id = ""
            if reattach_id:
                try:
                    self._target_id = reattach_id
                    session_id = await targets.attach(reattach_id)
                    target_id = reattach_id
                except Exception:
                    self._target_id = ""
                    with contextlib.suppress(Exception):
                        await targets.close(reattach_id)
            if not target_id:
                target_id = await targets.create_target()
                created = target_id
                self._target_id = target_id
                session_id = await targets.attach(target_id)
            self._session_id = session_id
            frame = await self._enable_page(session_id)
            self._main_frame_id = str(frame.get("id", ""))
            self._snapshot.url = str(frame.get("url", ""))
            self._record_page(
                target_id,
                session_id,
                opener_id=None,
                url=self._snapshot.url,
                title=self._snapshot.title,
            )
            from .policy import install_network_policy

            client = self.require_client()
            if self._policy is None:
                self._policy = await install_network_policy(
                    client,
                    self.cfg.allowed_domains,
                    allowed_origins=self.cfg.allowed_origins,
                    session_id=self._session_id,
                    scoped=True,
                    discover_pages=self._transport.owns_browser,
                    download_dir=str(self._download_dir) if self._download_dir is not None else None,
                    owns_session=self._owns_session,
                )
            else:
                # A recycled page keeps the policy object (and its one set of event
                # handlers); only its Fetch interception is re-armed on the new session.
                await self._policy.bootstrap(
                    client,
                    session_id=self._session_id,
                    scoped=True,
                    discover_pages=self._transport.owns_browser,
                    download_dir=str(self._download_dir) if self._download_dir is not None else None,
                )
            if not self._transport.owns_browser and self._transport.browser_commands and not self._browser_identity:
                self._browser_identity = await self._read_browser_identity()
        except BaseException:
            self._target_id = previous
            self._session_id = previous_session
            self._main_frame_id = previous_frame
            self._snapshot.title = previous_title
            if created and created != previous:
                self._forget_page(created)
                with contextlib.suppress(Exception):
                    await targets.close(created)
            raise
        finally:
            self._opening_page = False

    async def _enable_page(self, session_id: str) -> dict[str, Any]:
        """Enable the domains a driven page needs; return its main-frame info."""
        client = self.require_client()
        await client.send("Page.enable", session_id=session_id, timeout=self.cfg.cdp_timeout)
        await client.send("Page.setLifecycleEventsEnabled", {"enabled": True}, session_id=session_id)
        await client.send("Runtime.enable", session_id=session_id)
        await client.send("DOM.enable", session_id=session_id)
        await client.send("Network.enable", session_id=session_id)
        await client.send("Accessibility.enable", session_id=session_id)
        tree = await client.send("Page.getFrameTree", session_id=session_id)
        frame = tree.get("frameTree", {}).get("frame", {})
        from .snapshot.fusion import MUTATION_PROBE_SCRIPT

        installed = await client.send(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": MUTATION_PROBE_SCRIPT},
            session_id=session_id,
        )
        self._probe_script_id = str(installed.get("identifier", ""))
        return frame if isinstance(frame, dict) else {}

    def _record_page(
        self,
        target_id: str,
        session_id: str,
        *,
        opener_id: str | None,
        url: str = "",
        title: str = "",
    ) -> PageState:
        page = self._pages.get(target_id)
        if page is None:
            page = PageState(target_id=target_id, session_id=session_id, opener_id=opener_id, url=url, title=title)
            self._pages[target_id] = page
            self._page_order.append(target_id)
        else:
            page.session_id = session_id
            page.url = url or page.url
            page.title = title or page.title
        return page

    async def _adopt_page(self, target_id: str, session_id: str, opener_id: str, info: dict[str, Any]) -> None:
        """Take ownership of a popup opened by one of our pages.

        Page domains are enabled lazily on the first switch: an auto-attached
        popup can still be paused for the debugger here, and enabling it from
        this handler would race the policy's resume.
        """
        self._record_page(
            target_id,
            session_id,
            opener_id=opener_id,
            url=str(info.get("url", "")),
            title=str(info.get("title", "")),
        )

    async def _activate_page(self, page: PageState) -> None:
        """Switch the current-page context to *page* (caller holds ``_page_lock``)."""
        try:
            frame = await self._enable_page(page.session_id)
        except ConnectionClosed:
            raise
        except (CDPTimeout, BrowserError):
            # A just-adopted popup can still be resuming; retry once.
            await asyncio.sleep(0.2)
            frame = await self._enable_page(page.session_id)
        self._target_id = page.target_id
        self._session_id = page.session_id
        self._main_frame_id = str(frame.get("id", ""))
        page.url = str(frame.get("url") or page.url)
        self._snapshot.url = page.url
        self._snapshot.title = page.title or self._snapshot.title
        self._snapshot.nodes = []
        self._snapshot.previous_nodes = []
        self._login_pending_since = None
        self._bump_epoch()
        if self._targets is not None and self._transport.browser_commands:
            with contextlib.suppress(Exception):
                await self._targets.activate(page.target_id)

    def _page_by_session(self, session_id: str | None) -> PageState | None:
        if not session_id:
            return None
        for page in self._pages.values():
            if page.session_id == session_id:
                return page
        return None

    def _page_target_for_session(self, session_id: str | None) -> str | None:
        if not session_id:
            return self._target_id
        page = self._page_by_session(session_id)
        if page is not None:
            return page.target_id
        if session_id in self._duplicate_sessions:
            return self._duplicate_sessions[session_id]
        state = self._oopifs.get(session_id)
        return state.page_target_id or None if state is not None else None

    def _owns_session(self, target_id: str, session_id: str) -> bool:
        """Whether *session_id* is the session this handle drives *target_id* on."""
        page = self._pages.get(target_id)
        return page is not None and page.session_id == session_id

    def _forget_page(self, target_id: str) -> None:
        """Drop a page record and its OOPIFs without touching the browser."""
        page = self._pages.pop(target_id, None)
        if page is None:
            return
        self._page_order = [item for item in self._page_order if item != target_id]
        for session_id in [s for s, target in self._duplicate_sessions.items() if target == target_id]:
            self._duplicate_sessions.pop(session_id, None)
        for session_id in [s for s, state in self._oopifs.items() if state.page_target_id == target_id]:
            self._forget_oopif(session_id)
        self._timeouts.pop(target_id, None)
        for key in [item for item in self._document_status if item[0] == page.session_id]:
            self._document_status.pop(key, None)
        if self._dialog is not None and (self._dialog.page == target_id or self._dialog.session == page.session_id):
            self._finish_dialog("gone", "page closed")

    async def _drop_page(self, target_id: str) -> None:
        """A page session detached: fall back to its opener (or the root) or reopen."""
        page = self._pages.get(target_id)
        if page is None:
            return
        if page.target_id != self._target_id:
            self._forget_page(target_id)
            return
        fallback = None
        if page.opener_id:
            fallback = self._pages.get(page.opener_id)
        if fallback is not None and fallback.target_id == target_id:
            fallback = None
        if fallback is None:
            fallback = next(
                (self._pages[item] for item in self._page_order if item in self._pages and item != target_id),
                None,
            )
        if fallback is not None:
            try:
                # Activate before forgetting the closed page: listings never see a
                # page set with no current entry.
                await self._activate_page(fallback)
            except Exception:
                fallback = None
        if fallback is not None:
            self._forget_page(target_id)
            return
        self._target_id = ""
        self._session_id = ""
        self._main_frame_id = ""
        self._snapshot.url = ""
        self._snapshot.title = ""
        self._snapshot.nodes = []
        self._snapshot.previous_nodes = []
        self._bump_epoch()
        self._forget_page(target_id)
        if self._state != "ready" or self._closed:
            return
        try:
            await self._open_page()
            self._bump_epoch()
        except Exception:
            self._state = "failed"

    async def aclose(self) -> None:
        """Close the browser and release everything; idempotent."""
        current = asyncio.current_task()
        reentrant = getattr(self, "_action_lock_owner", None) is current
        cm = contextlib.nullcontext() if reentrant else self._action_lock
        async with cm:  # type: ignore[union-attr]
            async with self._start_lock:
                if self._closed:
                    return
                self._closed = True
                self._state = "closing"
                task = self._idle_task
                self._idle_task = None
                if task is not None and task is not current and not task.get_loop().is_closed():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                try:
                    await self._teardown_transport()
                finally:
                    self._release_lock()
                    self._state = "closed"

    async def _shutdown_idle(self) -> None:
        """Tear the browser down but leave the session restartable (idle close)."""
        async with self._start_lock:
            if self._closed:
                return
            self._state = "closing"
            await self._teardown_transport()
            self._state = "idle"

    async def _close_detached_targets(self, target_ids: list[str]) -> list[str]:
        """Best-effort reconnect on direct ``aclose()`` after an Attach disconnect to close lingering tabs."""
        if not target_ids or self._transport.owns_browser or not self._transport.browser_commands:
            return []
        cleanup_client: Any | None = None
        try:
            cleanup_client, _ = await asyncio.wait_for(
                self._transport.start(),
                timeout=min(5.0, self.cfg.cdp_timeout),
            )
            if self._browser_identity:
                info = await cleanup_client.send("Browser.getVersion", timeout=min(5.0, self.cfg.cdp_timeout))
                if isinstance(info, dict):
                    parts = [str(info.get(key) or "") for key in ("product", "revision", "userAgent")]
                    current_identity = "|".join(parts) if any(parts) else ""
                    if current_identity and current_identity != self._browser_identity:
                        return list(target_ids)
            response = await cleanup_client.send("Target.getTargets", timeout=min(5.0, self.cfg.cdp_timeout))
            infos = response.get("targetInfos") if isinstance(response, dict) else None
            live_pages = (
                {
                    str(info.get("targetId", ""))
                    for info in infos
                    if isinstance(info, dict) and info.get("type") == "page" and info.get("targetId")
                }
                if isinstance(infos, list)
                else set(target_ids)
            )
            remaining: list[str] = []
            for tid in target_ids:
                if tid not in live_pages:
                    continue
                try:
                    await cleanup_client.send("Target.closeTarget", {"targetId": tid}, timeout=5.0)
                except Exception:
                    remaining.append(tid)
            return remaining
        except Exception:
            return list(target_ids)
        finally:
            if cleanup_client is not None:
                with contextlib.suppress(Exception):
                    await cleanup_client.aclose()

    async def _teardown_transport(self) -> None:
        client = self._client
        child = self._child
        target_id = self._target_id or None
        client_dead = client is None or bool(getattr(client, "closed", False))
        owned_targets = [
            tid for tid in ([self._target_id] + [t for t in self._page_order if t != self._target_id]) if tid
        ]
        detached_to_close: list[str] = []
        if (
            client_dead
            and not self._transport.owns_browser
            and self._transport.browser_commands
            and not self._closed
            and owned_targets
        ):
            for tid in owned_targets:
                if tid not in self._detached_target_ids:
                    self._detached_target_ids.append(tid)
        elif self._closed:
            if client_dead and not self._transport.owns_browser and self._transport.browser_commands:
                detached_to_close = list(dict.fromkeys([*owned_targets, *self._detached_target_ids]))
            self._detached_target_ids.clear()
        # The next browser may reuse tab ids (Bridge ids are Chrome tab numbers).
        self._page_log.clear()
        self._client = None
        self._child = None
        self._targets = None
        self._policy = None
        self._target_id = ""
        self._session_id = ""
        self._main_frame_id = ""
        self._pages.clear()
        self._page_order.clear()
        self._oopifs.clear()
        self._duplicate_sessions.clear()
        self._foreign_targets.clear()
        self._opening_page = False
        self._cancel_dialog_task()
        self._dialog = None
        self._last_dialog = None
        self._dialog_orphans = 0
        self._login_pending_since = None
        for info in self._downloads.values():
            if info.state == "in_progress":
                info.state = "failed"
                info.reason = "browser closed"
                self._discard_download_files(info)
        self._bump_epoch()
        self._reset_budgets()
        if detached_to_close:
            self._unclosed_targets = await self._close_detached_targets(detached_to_close)
        elif self._closed:
            self._unclosed_targets = []
        if self._closed:
            self._browser_identity = ""
        if not client_dead and client is not None and not self._transport.owns_browser and self._transport.browser_commands:
            for extra_id in owned_targets:
                if extra_id != target_id:
                    with contextlib.suppress(Exception):
                        await client.send("Target.closeTarget", {"targetId": extra_id}, timeout=5.0)
        # The transport decides whether that means "close the browser" (owned) or
        # "close our tab and disconnect" (attached); it never raises.
        if client_dead and client is not None and not self._transport.owns_browser:
            with contextlib.suppress(Exception):
                await client.aclose()
            await self._transport.stop(None, child, target_id=None)
        else:
            await self._transport.stop(client, child, target_id=target_id)
        # The state-dir lock guards the profile while we own the browser; a
        # teardown (restart, failure, close) always releases it.
        self._release_lock()
        self._close_journal()

    def _close_journal(self) -> None:
        """Release the journal file handle; the next action reopens it lazily."""
        journal = self._journal
        self._journal = None
        if journal is not None:
            journal.close()

    def _release_lock(self) -> None:
        lock = self._instance_lock
        self._instance_lock = None
        if lock is not None:
            with contextlib.suppress(Exception):
                lock.release()

    def mark_disconnected(self) -> None:
        """Record that the CDP connection died; the next action restarts Chrome."""
        if self._closed or self._state == "closed":
            return
        self._state = "failed"
        self._client = None
        self._targets = None
        for state in self._oopifs.values():
            state.clear_connection_cache()
        if self._child is None:
            self._release_lock()

    # ----------------------------------------------------------------- lock

    @contextlib.asynccontextmanager
    async def action_lock(self, name: str) -> AsyncIterator[None]:
        """Serialise actions, refuse work during a login wait or an open JS dialog."""
        self._refuse_while_dialog(name)
        if self.login_pending and name not in LOGIN_PENDING_ALLOWED:
            raise Busy(f"会话忙（登录等待中），请使用 {_LOGIN_PENDING_HINT}")
        async with self._action_lock:
            self._action_lock_owner = asyncio.current_task()
            try:
                self._refuse_while_dialog(name)
                if self.login_pending and name not in LOGIN_PENDING_ALLOWED:
                    raise Busy(f"会话忙（登录等待中），请使用 {_LOGIN_PENDING_HINT}")
                pending_since = self._login_pending_since
                try:
                    yield
                except BaseException:
                    if pending_since is not None:
                        self._login_pending_since = pending_since
                    raise
            finally:
                self._action_lock_owner = None

    def _refuse_while_dialog(self, name: str) -> None:
        """Refuse actions that need the renderer: a dialog has paused the page."""
        info = self._dialog
        if info is None or name in DIALOG_PENDING_ALLOWED:
            return
        raise DialogPending(
            f"页面有未处理的 JavaScript 对话框（{info.kind}），页面脚本已暂停",
            details=(
                f"{info.fields()}\n"
                "请先调用 browser_dialog（action=accept 或 dismiss；prompt 的文本用 text 传入），"
                f"然后 browser_snapshot 复核；{self.cfg.dialog_timeout:g} 秒内未处理会按配置自动 dismiss 并记录原因。"
            ),
        )

    # ------------------------------------------------------------- idle close

    def _start_idle_watchdog(self) -> None:
        """Start this browser's one idle timer, unless it is already running."""
        if self.cfg.idle_close_seconds <= 0 or self._idle_task is not None:
            return
        self._idle_task = asyncio.get_running_loop().create_task(self._idle_watchdog())

    def _idle_expired(self) -> bool:
        """Whether the browser has gone ``idle_close_seconds`` without an action."""
        return time.monotonic() - self._last_active >= self.cfg.idle_close_seconds

    async def _idle_watchdog(self) -> None:
        """Close the browser once it has been idle long enough.

        The close takes the action lock, so a call that starts while the timer is
        waiting wins; the re-check after acquiring the lock keeps a slow action
        from being torn down underneath.
        """
        interval = max(0.05, self.cfg.idle_close_seconds / 4)
        try:
            while not self._closed:
                await asyncio.sleep(interval)
                if self._state != "ready" or self.login_pending or not self._idle_expired():
                    continue
                async with self._action_lock:
                    if self._closed or self.login_pending or not self._idle_expired():
                        continue
                    self._idle_task = None
                    await self._shutdown_idle()
                    return
        finally:
            # A loop shutdown cancels this task; never leave a stale handle that a
            # later loop would try to cancel/await.
            if self._idle_task is asyncio.current_task():
                self._idle_task = None

    # --------------------------------------------------------------- budgets

    def _reset_budgets(self) -> None:
        """A fresh browser gets a fresh rolling window and lifetime budget."""
        self._actions.clear()
        self._lifetime_actions = 0

    def reset_action_budget(self) -> None:
        """Give a reacquired Broker lease a fresh lifetime budget."""
        self._reset_budgets()

    async def close_current(self) -> None:
        """Close the current browser but keep this session restartable."""
        window = list(self._actions)
        lifetime = self._lifetime_actions
        await self._shutdown_idle()
        self._actions = type(self._actions)(window)
        self._lifetime_actions = lifetime

    def note_action(self) -> None:
        """Account one action against the rolling window and the lifetime cap."""
        now = time.monotonic()
        self._last_active = now
        while self._actions and now - self._actions[0] > ACTION_WINDOW_SECONDS:
            self._actions.popleft()
        if (
            len(self._actions) >= self.cfg.max_actions_per_window
            or self._lifetime_actions >= self.cfg.lifetime_max_actions
        ):
            raise Unavailable("动作预算已用尽（10 分钟滚动窗 / 生命周期上限），请稍后重试或重启插件")
        self._actions.append(now)
        self._lifetime_actions += 1

    def note_timeout_and_should_recycle(self, target_id: str | None) -> bool:
        """Count a timeout for a target; return True once the recycle threshold is hit."""
        if not target_id:
            return False
        count = self._timeouts.get(target_id, 0) + 1
        self._timeouts[target_id] = count
        return count >= MAX_TIMEOUTS_BEFORE_RECYCLE

    def note_success(self, target_id: str | None) -> None:
        """Clear a target's consecutive-timeout counter after a success."""
        self._last_active = time.monotonic()
        if target_id:
            self._timeouts.pop(target_id, None)

    def observe_mutations(self, count: int) -> bool:
        """Track the page's read-only mutation counter; True when it moved."""
        changed = self._probe_seen is not None and count != self._probe_seen
        self._probe_seen = count
        return changed

    # ------------------------------------------------------------------ epoch

    def _bump_epoch(self) -> None:
        """Advance the page generation; every existing reference becomes stale."""
        self._page_epoch += 1
        self._ref_map.clear()
        self._next_index = 1
        self._dirty = False
        self._probe_seen = None

    def mark_dirty(self) -> None:
        """Note that a mutating action ran; the next snapshot judges the DOM."""
        self._dirty = True

    @property
    def snapshot_dirty(self) -> bool:
        """Whether an action has run since the last capture."""
        return self._dirty

    def register_snapshot(
        self,
        nodes: list[FusedNode],
        *,
        url: str = "",
        title: str = "",
        dom_changed: bool = False,
    ) -> None:
        """Store a fresh snapshot and reuse refs for unchanged nodes."""
        self._snapshot.previous_nodes = list(self._snapshot.nodes)
        old_by_key = {(entry.frame_id, entry.backend_node_id): entry.index for entry in self._ref_map.values()}
        self._ref_map.clear()
        # Indices are never reused within an epoch: a token must not come to name a different node.
        self._next_index = max(self._next_index, max(old_by_key.values(), default=0) + 1)
        self._dirty = False
        for node in nodes:
            key = (node.frame_id, node.backend_node_id)
            index = old_by_key.pop(key, None)
            if index is None:
                index = self._next_index
                self._next_index += 1
            node.ref = index
            node.epoch = self._page_epoch
            self._ref_map[index] = RefEntry(
                index=index,
                epoch=self._page_epoch,
                fingerprint=node.fingerprint(),
                backend_node_id=node.backend_node_id,
                frame_id=node.frame_id,
                target_id=node.target_id,
                bbox=node.bbox,
                text_hint=node.name or node.text_hint,
                cdp_session_id=node.cdp_session_id,
            )
        self._snapshot.nodes = list(nodes)
        self._snapshot.version += 1
        if url:
            self._snapshot.url = url
        if title:
            self._snapshot.title = title

    # ------------------------------------------------------------ resolution

    def resolve_target(self, spec: str) -> RefEntry:
        """Resolve a ``@eN#epoch`` reference or a natural-language spec.

        Raises :class:`StaleRef`, :class:`TargetAmbiguous` or
        :class:`TargetNotFound`. A stale reference is never silently remapped.
        """
        text = (spec or "").strip()
        if not text:
            raise BadRequest("target 不能为空")
        if text.startswith("@"):
            match = _REF_RE.match(text)
            if match is None:
                raise BadRequest(f"target 引用格式非法：{text}（应为 @e序号#世代，例如 @e5#1234）")
            index = int(match.group(1))
            if match.group(2) is None:
                # A bare index is not proof of freshness: name the exact current token instead.
                current = f"@e{index}#{self._page_epoch}"
                if index in self._ref_map:
                    raise BadRequest(f"引用缺少世代：请写成 {current}（当前快照里的完整 token）")
                raise BadRequest(f"引用 {text} 不在当前快照（页面可能已变化），请重新 browser_snapshot 取 ref")
            epoch = int(match.group(2))
            entry = self._ref_map.get(index)
            if entry is None or entry.epoch != epoch or entry.epoch != self._page_epoch:
                raise StaleRef("引用已失效（页面已变化），请重新 snapshot")
            return entry
        candidates = rank(self._snapshot.nodes, text)
        best = resolve_ambiguity(candidates)
        entry = self._ref_map.get(best.node.ref or 0)
        if entry is None:
            raise StaleRef("引用已失效（页面已变化），请重新 snapshot")
        return entry

    async def resolve_object_id(self, entry: RefEntry) -> str:
        """Resolve a backend node id to an objectId and verify it is still live.

        A node that is gone, or whose coarse fingerprint (tag/name) no longer
        matches, fails closed with :class:`StaleRef`.
        """
        client = self.require_client()
        cdp_session_id = entry.cdp_session_id or self._session_id
        resolved = await client.send(
            "DOM.resolveNode",
            {"backendNodeId": entry.backend_node_id},
            session_id=cdp_session_id,
            timeout=self.cfg.cdp_timeout,
        )
        obj = resolved.get("object", {})
        object_id = str(obj.get("objectId", ""))
        if not object_id:
            raise StaleRef("引用已失效（页面已变化），请重新 snapshot")
        checked = await client.send(
            "Runtime.callFunctionOn",
            {
                "objectId": object_id,
                "functionDeclaration": _LIVE_CHECK_FN,
                "returnByValue": True,
            },
            session_id=cdp_session_id,
            timeout=self.cfg.evaluate_timeout,
        )
        value = checked.get("result", {}).get("value") or {}
        if not value.get("ok"):
            raise StaleRef("引用已失效（页面已变化），请重新 snapshot")
        expected_tag = (entry.fingerprint.get("tag") or "").lower()
        actual_tag = str(value.get("tag", "")).lower()
        if expected_tag and actual_tag and expected_tag != actual_tag:
            raise StaleRef("引用已失效（元素已被替换），请重新 snapshot")
        expected_name = (entry.fingerprint.get("name") or "").strip()
        actual_name = str(value.get("name", "")).strip()
        name_source = entry.fingerprint.get("name_source")
        # A <select>'s accessible name (its label or selected option) is never its
        # textContent (all option labels), and a name taken from href is a path, not
        # text: the coarse text-name check cannot apply to either.
        if (
            expected_name
            and actual_name
            and actual_tag != "select"
            and name_source != "href"
            and not _matches_live_ref_name(entry.fingerprint.get("role", ""), expected_name, actual_name)
            and (name_source != "ax" or not await self._ax_name_matches(entry, cdp_session_id, expected_name))
        ):
            raise StaleRef("引用已失效（元素内容已变化），请重新 snapshot")
        return object_id

    async def _ax_name_matches(self, entry: RefEntry, cdp_session_id: str, expected: str) -> bool:
        """Compare AX names with AX names when DOM text differs (e.g. aria-labelledby)."""
        try:
            tree = await self.require_client().send(
                "Accessibility.getPartialAXTree",
                {"backendNodeId": entry.backend_node_id, "fetchRelatives": False},
                session_id=cdp_session_id,
                timeout=self.cfg.cdp_timeout,
            )
        except ConnectionClosed:
            raise
        except BrowserError:
            return False
        expected_name = " ".join(expected.split())
        return any(
            not node.get("ignored")
            and node.get("backendDOMNodeId") == entry.backend_node_id
            and _matches_live_ref_name(
                entry.fingerprint.get("role", ""),
                expected_name,
                " ".join(str((node.get("name") or {}).get("value") or "").split()),
            )
            for node in tree.get("nodes") or []
        )

    # ----------------------------------------------------------- navigation

    async def read_frame_metrics(self, cdp_session_id: str | None = None) -> FrameInfo:
        """Read viewport, scroll offset and DPR for the active frame or a child session."""
        client = self.require_client()
        session_id = cdp_session_id or self._session_id
        frame_id = self._main_frame_id
        url = self._snapshot.url
        title = self._snapshot.title
        if cdp_session_id:
            tree = await client.send(
                "Page.getFrameTree",
                session_id=session_id,
                timeout=self.cfg.cdp_timeout,
            )
            frame = tree.get("frameTree", {}).get("frame", {})
            frame_id = str(frame.get("id", ""))
            url = str(frame.get("url", ""))
            if frame_id:
                self.set_oopif_frame_id(cdp_session_id, frame_id)
        response = await client.send(
            "Runtime.evaluate",
            {"expression": _METRICS_EXPR, "returnByValue": True},
            session_id=session_id,
            timeout=self.cfg.evaluate_timeout,
        )
        value = response.get("result", {}).get("value") or {}
        if cdp_session_id:
            title = str(value.get("title", ""))
        return FrameInfo(
            frame_id=frame_id,
            url=url,
            title=title,
            viewport=(float(value.get("w", 0.0)), float(value.get("h", 0.0))),
            scroll_x=float(value.get("x", 0.0)),
            scroll_y=float(value.get("y", 0.0)),
            dpr=float(value.get("dpr", 1.0)),
        )

    async def wait_for_load(self, loader_id: str | None, *, timeout: float) -> bool:
        """Wait for the load signal of one navigation, correlated by loader id."""
        if not loader_id:
            return False
        if loader_id in self._load_done:
            return True
        event = self._load_events.setdefault(loader_id, asyncio.Event())
        try:
            await asyncio.wait_for(event.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False
        finally:
            self._load_events.pop(loader_id, None)

    @property
    def nav_seq(self) -> int:
        """Count of current-page main-frame navigations; read it before sending one."""
        return self._nav_seq

    async def wait_for_navigation(
        self, *, after: int, timeout: float, cross_document: bool = False, url: str | None = None
    ) -> str | None:
        """Wait for a current-page main-frame navigation newer than *after*.

        Returns the loader id of the newest cross-document commit since *after* (even
        if same-document navigations followed it), ``""`` for a same-document
        navigation (only when *cross_document* is false, and only one that reached
        *url* when given, so the old page's own pushState does not count), or
        ``None`` on timeout. Only the session's own filtered handlers feed this, so
        another tab or a child frame never satisfies it.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            commit_seq, commit = self._last_commit
            if commit and commit_seq > after:
                return commit
            newer = [(nav_url, child) for seq, loader, nav_url, child in self._navs if seq > after and not loader]
            # A child frame's commit only completes a history step to the main frame's own
            # URL (an iframe-only entry); it never stands in for a main-frame navigation.
            if not cross_document and any((url is None and not child) or nav_url == url for nav_url, child in newer):
                return ""
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            try:
                await asyncio.wait_for(self._nav_changed.wait(), remaining)
            except asyncio.TimeoutError:
                return None

    def _note_navigation(self, loader_id: str, url: str, *, child: bool = False) -> None:
        self._nav_seq += 1
        self._navs.append((self._nav_seq, loader_id, url, child))
        if loader_id:
            self._last_commit = (self._nav_seq, loader_id)
        self._nav_changed.set()
        self._nav_changed = asyncio.Event()

    async def recycle_page(self) -> None:
        """Close the current tab and open a fresh one (timeout recovery)."""
        targets = self._targets
        previous = self._target_id
        async with self._page_lock:
            try:
                if targets is not None and previous:
                    with contextlib.suppress(Exception):
                        await targets.close(previous)
                # Forget before opening: a detach handler that waits on this lock
                # then finds no page for the closed target and cannot reopen it.
                self._forget_page(previous)
                await self._open_page()
            except Exception:
                self._state = "failed"
                raise
        self._bump_epoch()
        self._state = "ready"

    # ---------------------------------------------------------------- login

    def note_login_pending(self) -> None:
        if self._login_pending_since is None:
            self._login_pending_since = time.monotonic()

    def clear_login_pending(self) -> None:
        self._login_pending_since = None

    def login_required(self, *, site: str, reason: str) -> LoginRequired:
        """Build the login error, reporting waited/remaining seconds."""
        waited = 0.0 if self._login_pending_since is None else time.monotonic() - self._login_pending_since
        remaining = max(0.0, LOGIN_WAIT_MAX - waited)
        timed_out = remaining <= 0.0
        state = "timed_out" if timed_out else "waiting"
        details = (
            f"state={state} waited={int(waited)}s remaining={int(remaining)}s\n"
            "请在插件打开的浏览器窗口中完成登录，然后重发 browser_snapshot 继续；"
            "期间以 ≥120s 间隔调用 browser_snapshot 保活；"
            "也可显式调用 browser_navigate、browser_back 或 browser_forward 离开，或 browser_close 关闭会话。"
            "写操作仍返回 browser_busy，离开失败不会解除保护。"
        )
        if timed_out:
            details += "\n登录等待已超时，但不会自动授权；请使用上述离开/关闭操作，或完成登录后重新 snapshot。"
        return LoginRequired(
            f"需要登录：{site}（{reason}）。请在浏览器窗口完成登录后重试；无窗口时请以有头模式重跑",
            details=details,
        )

    # -------------------------------------------------------------- handlers

    def _register_handlers(self) -> None:
        client = self.require_client()
        client.on("Page.frameNavigated", self._on_frame_navigated)
        client.on("Page.navigatedWithinDocument", self._on_navigated_within_document)
        client.on("Page.lifecycleEvent", self._on_lifecycle_event)
        client.on("Page.frameStoppedLoading", self._on_frame_stopped_loading)
        client.on("Page.javascriptDialogOpening", self._on_dialog)
        client.on("Page.javascriptDialogClosed", self._on_dialog_closed)
        client.on("Browser.downloadWillBegin", self._on_download_will_begin)
        client.on("Browser.downloadProgress", self._on_download_progress)
        client.on("Network.responseReceived", self._on_response)
        client.on("Target.attachedToTarget", self._on_attached_to_target)
        client.on("Target.detachedFromTarget", self._on_detached_from_target)
        client.on("DOM.documentUpdated", self._on_document_updated)
        capabilities = getattr(client, "capabilities", None)
        if capabilities is not None and "page_log_gated" not in capabilities:
            # An older Bridge extension forwards every page's console and network
            # events, including sites the user did not allow: collect none of them.
            self._page_log.disabled_reason = (
                "当前 Bridge 未按站点授权过滤日志。运行 icode browser update-bridge，"
                "在 chrome://extensions 重载扩展后重连"
            )
            return
        self._page_log.disabled_reason = ""
        for method, record in (
            ("Runtime.consoleAPICalled", self._page_log.console),
            ("Runtime.exceptionThrown", self._page_log.exception),
            ("Network.requestWillBeSent", self._page_log.request),
            ("Network.responseReceived", self._page_log.response),
            ("Network.loadingFailed", self._page_log.failed),
        ):
            client.on(method, self._log_handler(record))

    def _log_handler(self, record: Callable[[str, dict[str, Any]], None]) -> Callable[..., Awaitable[None]]:
        """Feed one page-log recorder with events from owned pages (and their OOPIFs) only."""

        async def handler(params: dict[str, Any], session_id: str | None) -> None:
            target_id = self._page_target_for_session(session_id) if session_id else None
            if target_id:
                record(target_id, params)

        return handler

    @property
    def page_log(self) -> PageLog:
        """Console and network log of the pages this handle owns."""
        return self._page_log

    async def _on_document_updated(self, params: dict[str, Any], session_id: str | None) -> None:
        if session_id != self._session_id:
            return
        self._bump_epoch()

    async def _on_frame_navigated(self, params: dict[str, Any], session_id: str | None) -> None:
        frame = params.get("frame", {})
        if session_id != self._session_id:
            # A background tab keeps its cached url/title for the next listing.
            page = self._page_by_session(session_id)
            if page is not None and frame.get("parentId") is None:
                page.url = str(frame.get("url") or page.url)
            return
        loader_id = str(frame.get("loaderId", ""))
        frame_id = str(frame.get("id", ""))
        if loader_id and frame_id:
            self._frame_loader[frame_id] = loader_id
        if frame.get("parentId") is not None:
            self._bump_epoch()  # child-frame navigation also invalidates fused refs
            self._note_navigation("", self._snapshot.url, child=True)
            return
        self._main_frame_id = frame_id
        self._frame_loader[frame_id] = loader_id
        self._snapshot.url = str(frame.get("url", ""))
        page = self._page_by_session(session_id)
        if page is not None:
            page.url = self._snapshot.url
        self._bump_epoch()
        self._note_navigation(loader_id, self._snapshot.url)

    async def _on_navigated_within_document(self, params: dict[str, Any], session_id: str | None) -> None:
        if session_id != self._session_id or params.get("frameId") != self._main_frame_id:
            return
        self._snapshot.url = str(params.get("url", "")) or self._snapshot.url
        self._bump_epoch()
        self._note_navigation("", str(params.get("url", "")))

    async def _on_lifecycle_event(self, params: dict[str, Any], session_id: str | None) -> None:
        if session_id != self._session_id or params.get("name") != "load":
            return
        self._signal_loaded(str(params.get("loaderId", "")))

    async def _on_frame_stopped_loading(self, params: dict[str, Any], session_id: str | None) -> None:
        if session_id != self._session_id:
            return
        loader_id = self._frame_loader.get(str(params.get("frameId", "")))
        if loader_id:
            self._signal_loaded(loader_id)

    def _signal_loaded(self, loader_id: str) -> None:
        if not loader_id:
            return
        self._load_done[loader_id] = None
        if len(self._load_done) > _MAX_REMEMBERED_LOADS:
            del self._load_done[next(iter(self._load_done))]
        event = self._load_events.get(loader_id)
        if event is not None:
            event.set()

    async def _on_dialog(self, params: dict[str, Any], session_id: str | None) -> None:
        """Record a dialog; the current page's dialog waits, a background one is dismissed.

        Dialogs pause the renderer, so a background tab's dialog would block that
        tab forever with nobody to see it. Only the dialog of the page this handle
        drives is offered to ``browser_dialog``; it is dismissed automatically when
        the configured timeout expires so a headless run never waits forever.
        """
        kind = str(params.get("type") or "alert")
        message = redact_message(params.get("message"))
        url = str(params.get("url") or "")
        if session_id is not None and session_id != self._session_id:
            # Never touch the current page's dialog record: both pages are live.
            page = self._page_by_session(session_id)
            log(f"dismissing a background-page dialog on {page.target_id if page else session_id}: {kind} {message!r}")
            await self._send_dialog_close(session_id, accept=False)
            return
        if self._dialog is not None:
            # The old dialog's own Page.javascriptDialogClosed may still arrive;
            # remember it so that event cannot finalise this new dialog.
            self._dialog_orphans += 1
            self._finish_dialog("superseded", "superseded by a new dialog")
        page = self._page_by_session(session_id)
        info = self._record_dialog(kind, message, url, page.target_id if page else "", session_id or "")
        self._arm_dialog_timeout(info, session_id)

    async def _on_dialog_closed(self, params: dict[str, Any], session_id: str | None) -> None:
        if session_id is not None and session_id != self._session_id:
            return
        if self._dialog_orphans > 0:
            # This is the closed event of a dialog a newer one already superseded.
            self._dialog_orphans -= 1
            return
        info = self._dialog
        if info is None:
            return  # already finalised by browser_dialog or the timeout
        accepted = bool(params.get("result"))
        user_input = str(params.get("userInput") or "")
        # The closer set the reason before sending; this event cannot tell who it was.
        self._finish_dialog(
            "accepted" if accepted else "dismissed",
            info.reason or "closed in the page",
            user_input=user_input,
        )

    def _record_dialog(self, kind: str, message: str, url: str, page: str, session: str) -> DialogInfo:
        self._dialog_seq += 1
        info = DialogInfo(seq=self._dialog_seq, kind=kind, message=message, url=url, page=page, session=session)
        self._dialog = info
        return info

    def _finish_dialog(
        self,
        closed: str,
        reason: str,
        *,
        user_input: str = "",
        seq: int | None = None,
    ) -> DialogInfo | None:
        """Stamp the pending dialog with how it ended; ignore another dialog's seq."""
        info = self._dialog
        if info is None or (seq is not None and info.seq != seq):
            return None
        self._dialog = None
        info.closed = closed
        info.reason = reason
        info.prompt_chars = len(user_input)
        self._last_dialog = info
        self._cancel_dialog_task()
        return info

    def _arm_dialog_timeout(self, info: DialogInfo, session_id: str | None) -> None:
        self._cancel_dialog_task()
        timeout = max(0.01, float(self.cfg.dialog_timeout))
        self._dialog_task = asyncio.get_running_loop().create_task(
            self._auto_dismiss_dialog(info.seq, session_id, timeout)
        )

    def _cancel_dialog_task(self) -> None:
        task = self._dialog_task
        self._dialog_task = None
        if task is not None and task is not asyncio.current_task() and not task.get_loop().is_closed():
            task.cancel()

    async def _auto_dismiss_dialog(self, seq: int, session_id: str | None, timeout: float) -> None:
        try:
            await asyncio.sleep(timeout)
        except asyncio.CancelledError:
            return
        info = self._dialog
        if info is None or info.seq != seq:
            return
        mode = "headless" if self.cfg.headless else "no browser_dialog call"
        info.reason = f"auto-dismissed after {timeout:g}s ({mode})"
        try:
            sent = await self._send_dialog_close(session_id, accept=False)
            if not sent:
                info.reason += "; the dialog was already gone"
        finally:
            # Whatever happened above, the record must not outlive its timer.
            self._finish_dialog("dismissed", info.reason, seq=seq)

    async def _send_dialog_close(self, session_id: str | None, *, accept: bool, text: str = "") -> bool:
        """``Page.handleJavaScriptDialog``; returns whether the command was accepted."""
        client = self._client
        if client is None:
            return False
        params: dict[str, Any] = {"accept": accept}
        if accept and text:
            params["promptText"] = text
        try:
            await client.send(
                "Page.handleJavaScriptDialog",
                params,
                session_id=session_id or self._session_id,
                timeout=min(self.cfg.cdp_timeout, 10.0),
            )
        except ConnectionClosed:
            return False
        except BrowserError as exc:  # "No dialog is showing" after the page closed it
            log(f"dialog close failed: {exc!r}")
            return False
        return True

    async def handle_dialog(self, *, accept: bool, text: str = "") -> tuple[DialogInfo, bool]:
        """Accept or dismiss the pending dialog; return ``(record, sent)``.

        ``text`` answers a ``prompt`` and is refused everywhere else, so a model
        cannot silently drop an answer it meant to type; ``sent`` is whether
        ``Page.handleJavaScriptDialog`` actually reached the dialog.
        """
        info = self._dialog
        if info is None:
            raise BadRequest("当前没有打开的 JavaScript 对话框")
        if text and not accept:
            raise BadRequest("text 只在 accept 时使用；dismiss 不接受文本")
        if text and info.kind != "prompt":
            raise BadRequest(f"text 只用于 prompt 对话框（当前为 {info.kind}）")
        if accept and info.kind == "prompt" and not text.strip():
            raise BadRequest("prompt 对话框需要 text 才能 accept：页面收到的是这个参数（不会替你提交空答案）")
        # Stamp the reason before sending: Page.javascriptDialogClosed races the
        # command response and cannot say who closed the dialog.
        info.reason = "handled by browser_dialog"
        sent = await self._send_dialog_close(info.session or self._session_id, accept=accept, text=text)
        # The page may have closed the dialog (or opened the next one) while the
        # command was in flight, so only this dialog's own seq may be finalised.
        closed = self._finish_dialog(
            "gone" if not sent else ("accepted" if accept else "dismissed"),
            "browser_dialog reached no dialog (closed by the page)" if not sent else "handled by browser_dialog",
            seq=info.seq,
        )
        if closed is None:
            last = self._last_dialog
            closed = last if last is not None and last.seq == info.seq else info
        if not sent:
            closed.closed = "gone"
            closed.reason = "browser_dialog reached no dialog (closed by the page)"
        return closed, sent

    # ------------------------------------------------------- download events

    async def _on_download_will_begin(self, params: dict[str, Any], session_id: str | None) -> None:
        if session_id is not None or self._download_dir is None:
            return
        guid = str(params.get("guid") or "")
        if not guid:
            return
        existing = self._downloads.get(guid)
        if existing is not None and existing.state == "in_progress":
            # Chromium can repeat the begin event for one transfer; keep its reserved name.
            existing.url = display_url(str(params.get("url") or "")) or existing.url
            return
        if existing is not None:
            self._discard_download_files(existing)
            if guid in self._download_order:
                self._download_order.remove(guid)
            self._downloads.pop(guid, None)
        name = sanitize_filename(str(params.get("suggestedFilename") or ""))
        info = DownloadInfo(
            guid=guid,
            name=name,
            url=display_url(str(params.get("url") or "")),
            partial_path=self._download_dir / guid,
        )
        if sum(1 for item in self._downloads.values() if item.state == "in_progress") >= _MAX_ACTIVE_DOWNLOADS:
            info.state = "failed"
            info.reason = f"too many concurrent downloads (limit {_MAX_ACTIVE_DOWNLOADS})"
            self._remember_download(info)
            await self._cancel_transfer(guid)
            self._discard_download_files(info)
            return
        try:
            info.path = reserve_path(self._download_dir, name)
        except OSError as exc:
            info.state = "failed"
            info.reason = f"could not reserve a file: {exc}"
            self._remember_download(info)
            await self._cancel_transfer(guid)
            self._discard_download_files(info)
            return
        info.name = info.path.name
        self._remember_download(info)

    async def _on_download_progress(self, params: dict[str, Any], session_id: str | None) -> None:
        if session_id is not None or self._download_dir is None:
            return
        info = self._downloads.get(str(params.get("guid") or ""))
        state = str(params.get("state") or "")
        if info is None:
            return
        if info.state != "in_progress":
            if info.state == "canceled" and state == "completed":
                self._discard_download_files(info)
                partial = self._partial_file(params, info.guid)
                if partial is not None:
                    with contextlib.suppress(OSError):
                        partial.unlink()
            return
        total = int(params.get("totalBytes") or -1)
        info.total_bytes = total if total >= 0 else None
        info.bytes_received = max(0, int(params.get("receivedBytes") or 0))
        if state == "inProgress":
            limit = self.cfg.download_max_bytes
            observed = max(info.total_bytes or 0, info.bytes_received)
            if observed > limit:
                # Claim the record before awaiting: a completed event must not rename it.
                info.reason = f"size_limit: {observed} > {limit} bytes"
                info.state = "canceled"
                await self._cancel_transfer(info.guid)
                if info.state == "canceled":
                    self._discard_download_files(info)
            return
        if state == "completed":
            self._complete_download(info, params)
            return
        if state in {"canceled", "failed"}:
            info.state = "canceled" if state == "canceled" else "failed"
            info.reason = info.reason or (
                "canceled by the page or the user" if state == "canceled" else "download failed"
            )
            self._discard_download_files(info)

    def _complete_download(self, info: DownloadInfo, params: dict[str, Any]) -> None:
        limit = self.cfg.download_max_bytes
        partial = self._partial_file(params, info.guid)
        size = file_size(partial) if partial is not None else None
        if partial is None or size is None or partial == info.path:
            info.state = "failed"
            info.reason = "the downloaded file was not found"
            self._discard_download_files(info)
            return
        if size > limit:
            info.state = "failed"
            info.reason = f"size_limit: {size} > {limit} bytes"
            self._discard_download_files(info)
            return
        target = info.path
        try:
            if target is None:
                raise OSError("no reserved file name")
            os.replace(partial, target)
        except OSError as exc:
            info.state = "failed"
            info.reason = f"could not finalise the file: {exc}"
            self._discard_download_files(info)
            return
        info.path = target
        info.name = target.name
        info.bytes_received = size
        info.state = "completed"
        info.partial_path = None

    def _partial_file(self, params: dict[str, Any], guid: str) -> Path | None:
        directory = self._download_dir
        if directory is None:
            return None
        raw = str(params.get("filePath") or "")
        if raw:
            candidate = Path(raw)
            try:
                candidate.resolve().relative_to(directory.resolve())
            except (OSError, ValueError):
                candidate = None
            if candidate is not None and candidate.exists():
                return candidate
        for name in (guid, f"{guid}.crdownload"):
            candidate = directory / name
            if candidate.exists():
                return candidate
        return None

    async def _cancel_transfer(self, guid: str) -> None:
        client = self._client
        if client is None:
            return
        with contextlib.suppress(BrowserError):
            await client.send(
                "Browser.cancelDownload",
                {"guid": guid},
                timeout=min(self.cfg.cdp_timeout, 10.0),
            )

    def _discard_download_files(self, info: DownloadInfo) -> None:
        directory = self._download_dir
        if directory is not None:
            remove_quiet(directory / info.guid)
            remove_quiet(directory / f"{info.guid}.crdownload")
        remove_quiet(info.partial_path)
        remove_quiet(info.path)
        info.path = None
        info.partial_path = None

    def _remember_download(self, info: DownloadInfo) -> None:
        self._downloads[info.guid] = info
        if info.guid in self._download_order:
            self._download_order.remove(info.guid)
        self._download_order.append(info.guid)
        if len(self._download_order) <= _MAX_DOWNLOADS:
            return
        # Only finished records are evicted: dropping an in-progress one would leave
        # its transfer unmonitored (no size check, no cancel, no cleanup).
        for guid in list(self._download_order):
            item = self._downloads.get(guid)
            if item is not None and item.state != "in_progress":
                self._download_order.remove(guid)
                self._downloads.pop(guid, None)
                if len(self._download_order) <= _MAX_DOWNLOADS:
                    return

    async def _on_response(self, params: dict[str, Any], session_id: str | None) -> None:
        if session_id != self._session_id or params.get("type") != "Document":
            return
        frame_id = str(params.get("frameId", ""))
        loader_id = str(params.get("loaderId", ""))
        if not frame_id or frame_id != self._main_frame_id or not loader_id:
            return
        # Responses can precede frameNavigated; expose only the committed loader.
        key = (self._session_id, frame_id, loader_id)
        self._document_status[key] = int(params.get("response", {}).get("status", 0))
        if len(self._document_status) > _MAX_REMEMBERED_LOADS:
            del self._document_status[next(iter(self._document_status))]

    async def _on_attached_to_target(self, params: dict[str, Any], session_id: str | None) -> None:
        info = params.get("targetInfo", {})
        child_session = str(params.get("sessionId") or "")
        target_id = str(info.get("targetId", ""))
        target_type = info.get("type")
        if target_type == "iframe" and child_session:
            parent = session_id if session_id in self._oopifs else None
            owner = self._page_target_for_session(session_id) or ""
            self.register_oopif(child_session, target_id, parent_session_id=parent, page_target_id=owner)
            return
        if target_type != "page" or not child_session or not target_id:
            return
        if target_id == self._target_id:
            # Our current tab (startup or a re-attached session).
            page = self._pages.get(target_id)
            if page is None:
                if child_session == self._session_id or not params.get("waitingForDebugger"):
                    return  # the manual attach this handle is setting up
                # A browser-level auto-attach session for the tab being opened:
                # it is not recorded, and keeping it would be a second Fetch owner.
                with contextlib.suppress(Exception):
                    await self.require_client().send(
                        "Target.detachFromTarget",
                        {"sessionId": child_session},
                        timeout=5.0,
                    )
                return
            if page.session_id == child_session:
                return
            if not page.session_id:
                page.session_id = child_session
                return
            # Browser-level auto-attach also opens a second session for a tab we
            # already drive manually; keep one owner and let the extra session go
            # (detaching a paused target resumes it). OOPIFs still attach under the
            # duplicate session, so remember who owns it for frame fusion.
            self._duplicate_sessions[child_session] = target_id
            with contextlib.suppress(Exception):
                await self.require_client().send(
                    "Target.detachFromTarget",
                    {"sessionId": child_session},
                    timeout=5.0,
                )
            return
        if self._opening_page:
            # ``_open_page`` is creating its own tab; it records it when attach returns.
            return
        known = self._pages.get(target_id)
        if known is not None:
            known.session_id = child_session
            return
        opener_id = str(info.get("openerId") or "") or None
        if opener_id is not None and opener_id in self._pages and len(self._pages) < _MAX_PAGES:
            await self._adopt_page(target_id, child_session, opener_id, info)
            return
        if target_id in self._foreign_targets:
            return
        # A user/extension page, a no-opener flow, or one popup too many: never
        # drive or close it. In Owned mode the policy still guards its requests
        # (its browser-level auto-attach session stays mounted); in Attach/Bridge
        # it should not appear at all, so detaching keeps the user's connection clean.
        self._foreign_targets.add(target_id)
        if self._transport.owns_browser:
            return
        with contextlib.suppress(Exception):
            await self.require_client().send(
                "Target.detachFromTarget",
                {"sessionId": child_session},
                timeout=5.0,
            )

    async def _on_detached_from_target(self, params: dict[str, Any], session_id: str | None) -> None:
        child_session = params.get("sessionId")
        if not child_session:
            return
        session_key = str(child_session)
        if session_key in self._duplicate_sessions:
            self._duplicate_sessions.pop(session_key, None)
            return
        if session_key in self._oopifs:
            self._forget_oopif(session_key)
            return
        page = self._page_by_session(session_key)
        if page is None:
            return
        if self._closed or self._state != "ready":
            self._forget_page(page.target_id)
            return
        async with self._page_lock:
            await self._drop_page(page.target_id)

    def _forget_oopif(self, child_session: str) -> None:
        self._oopifs.pop(child_session, None)
        for descendant in self.oopif_children(child_session):
            self._forget_oopif(descendant)


async def _default_client_factory(ws_url: str) -> CDPConnection:
    from .cdp.client import CDPClient

    return await CDPClient.connect(ws_url)
