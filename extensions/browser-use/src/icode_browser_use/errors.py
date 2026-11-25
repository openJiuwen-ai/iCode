# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Error taxonomy shared by the transport, session and action layers.

Every failure that reaches the model is a tool result with ``isError=true``
whose first line is ``<code>: <message>``. Nothing else may leak to the
protocol layer: an unmapped exception is downgraded to
``browser_unavailable`` at the adapter boundary.
"""

from __future__ import annotations

from typing import Any


class BrowserError(Exception):
    """Base class for every failure a tool call may surface to the model."""

    code = "browser_unavailable"

    def __init__(self, message: str, *, details: str = "", blocked_origin: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.details = details
        self.blocked_origin = blocked_origin

    def render(self) -> str:
        """Return the model-visible text: ``code: message`` plus optional details."""
        if self.details:
            return f"{self.code}: {self.message}\n{self.details}"
        return f"{self.code}: {self.message}"


class BadRequest(BrowserError):
    """Arguments are missing, mistyped or out of range."""

    code = "browser_bad_request"


class StaleRef(BrowserError):
    """A reference's epoch no longer matches the page, or its node died."""

    code = "browser_stale_ref"


class TargetNotFound(BrowserError):
    """No element matched the target specification."""

    code = "browser_target_not_found"


class TargetAmbiguous(BrowserError):
    """Several candidates tied; the top three are returned for disambiguation."""

    code = "browser_target_ambiguous"

    def __init__(self, message: str, *, candidates: list[Any] | None = None, details: str = "") -> None:
        super().__init__(message, details=details)
        self.candidates = list(candidates or ())


class Blocked(BrowserError):
    """The network policy refused a navigation or sub-resource request."""

    code = "browser_blocked"


class Unavailable(BrowserError):
    """No browser could be found, launched or handshaken."""

    code = "browser_unavailable"


# Name used by the launcher / WebSocket handshake path in the design document.
BrowserLaunchError = Unavailable


class Busy(BrowserError):
    """Another caller holds the session action lock (login wait)."""

    code = "browser_busy"


class DialogPending(BrowserError):
    """A JavaScript dialog is open and has paused the page's script."""

    code = "browser_dialog_open"


class PageHung(BrowserError):
    """A target timed out repeatedly and was recycled."""

    code = "browser_page_hung"


class ConnectionClosed(BrowserError):
    """The CDP WebSocket closed; in-flight commands failed fast."""

    code = "browser_disconnected"


class LoginRequired(BrowserError):
    """A login wall was detected; the user must sign in in the visible window."""

    code = "browser_login_required"


class CDPTimeout(BrowserError):
    """A single CDP command exceeded its timeout (below the recycle threshold)."""

    code = "browser_timeout"

    def __init__(self, method: str, timeout: float, *, target_id: str | None = None) -> None:
        super().__init__(f"操作超时（{method}）")
        self.method = method
        self.timeout = timeout
        self.target_id = target_id
