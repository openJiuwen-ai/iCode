# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Bridge between model profile settings and process environment state.

Model profile credentials, base URLs, and transport options flow through the
loaded ``ModelProfile`` object into the LLM client factory.  This bridge
stores the default profile in ``settings.yaml`` and tracks the live process
selection through ``CHRYS_MODEL_PROFILE``.  Provider API keys/base URLs in
the environment remain user-owned fallback settings and are not rewritten by
profile activation.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable

from chrys.foundation.config.runtime_pointer import MODEL_POINTER_KEY, get_model_pointer, set_model_pointer
from chrys.foundation.config.settings_store import persist
from chrys.foundation.config.spec import SettingOrigin, Source
from chrys.foundation.config.user_settings import flatten_user_doc, user_settings_path
from chrys.foundation.config.yaml_store import read_yaml_doc

_NO_PROXY_ENV_VARS: tuple[str, ...] = ("NO_PROXY", "no_proxy")
"""Both casings httpx consults for proxy-bypass, in precedence order.

Uppercase wins; httpx falls back to lowercase only when ``NO_PROXY`` is
unset/empty.  Anything that writes or scrubs the bypass list must touch
both forms so a stale lowercase value can't shadow the canonical one.
"""

_PROXY_URL_ENV_VARS: tuple[str, ...] = (
    "HTTP_PROXY",
    "http_proxy",
    "HTTPS_PROXY",
    "https_proxy",
    "ALL_PROXY",
    "all_proxy",
)
"""The proxy-address variables httpx reads, both casings of each.

httpx takes only the ``http``, ``https`` and ``all`` entries of
``urllib.request.getproxies()``, which prefers the lowercase name.  Each
name is checked on its own: a bad value under either casing can be the
one httpx reads.
"""

_URL_SCHEME_PREFIX = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://")


def _persist_pointer(profile_id: str) -> None:
    """Store the durable pointer value; an empty selection removes the key."""
    if profile_id:
        persist({MODEL_POINTER_KEY: profile_id})
    else:
        persist({}, remove=(MODEL_POINTER_KEY,))


def activate_model_profile(profile_id: str) -> None:
    """Persist a profile and make it this process's live selection."""
    _persist_pointer(profile_id)
    set_model_pointer(profile_id, origin=SettingOrigin(layer=Source.PROCESS_RUNTIME))


def set_global_default_profile_id(profile_id: str) -> None:
    """Persist the process-independent default profile without changing this process."""
    _persist_pointer(profile_id)


def get_global_default_profile_id() -> str:
    """Read the process-independent default profile from the user settings document.

    The durable value, deliberately not the effective one: this is what the
    *next* process starts from, which is exactly what the models screen offers
    to edit. Reading it through ``load_settings`` would fold in the shell
    export and this process's own pointer, and the screen would then offer to
    "clear" a default that was never stored.
    """
    doc = read_yaml_doc(user_settings_path()) or {}
    values, _ = flatten_user_doc(doc, frozenset({MODEL_POINTER_KEY}))
    value = values.get(MODEL_POINTER_KEY)
    return value.strip() if isinstance(value, str) else ""


def get_active_profile_id() -> str:
    """Return the active model profile ID from the runtime pointer, or empty string."""
    return get_model_pointer()[0]


def _validate_no_proxy_entry(hostname: str) -> bool:
    """Validate one comma-separated ``NO_PROXY`` entry exactly as httpx does.

    Mirrors the URL-pattern construction inside
    ``httpx._utils.get_environment_proxies`` so the validator catches
    every value that would crash the LLM client at boot — non-printable
    ASCII, malformed hosts like ``[::1]`` (brackets handled by httpx,
    not the user), bad port syntax, etc.
    """
    from httpx import InvalidURL
    from httpx._utils import URLPattern, is_ipv4_hostname, is_ipv6_hostname

    if not hostname or hostname == "*":
        return True
    try:
        if "://" in hostname:
            URLPattern(hostname)
        elif is_ipv4_hostname(hostname):
            URLPattern(f"all://{hostname}")
        elif is_ipv6_hostname(hostname):
            URLPattern(f"all://[{hostname}]")
        elif hostname.lower() == "localhost":
            URLPattern(f"all://{hostname}")
        else:
            URLPattern(f"all://*{hostname}")
    except InvalidURL, ValueError:
        return False
    return True


def is_valid_no_proxy(value: str) -> bool:
    """Return True if *value* is a valid ``NO_PROXY`` payload for httpx.

    Replicates httpx's NO_PROXY parsing so the validator agrees with
    what the LLM client constructor will accept at boot — catching both
    non-printable ASCII (rejected by ``urlparse``) and printable-but-
    malformed entries like ``[::1]`` that fail ``URLPattern``
    construction.  Empty is valid (means "no bypass list").  Kept in
    sync with the pinned httpx version in ``pyproject.toml``.
    """
    if not value:
        return True
    entries = [host.strip() for host in value.split(",")]
    # ``*`` short-circuits the bypass list in httpx
    # (``get_environment_proxies`` returns ``{}`` immediately), so any
    # later entries are ignored — even malformed ones.  Mirror that.
    if "*" in entries:
        return True
    return all(_validate_no_proxy_entry(entry) for entry in entries)


def is_valid_proxy_url(value: str) -> bool:
    """Return True if httpx can build a proxy from *value* read from the environment.

    Mirrors ``httpx._utils.get_environment_proxies`` (a value without
    ``://`` is given ``http://``) and then ``httpx.Proxy``, the step of
    client construction that rejects a proxy address: an unsupported
    scheme such as ``socks://`` or ``socks4://``, or a malformed URL.
    Asking httpx itself, not a copied scheme list, keeps the verdict in
    step with the pinned httpx.  Empty is valid: urllib skips empty values.
    """
    if not value:
        return True
    from httpx import InvalidURL, Proxy

    try:
        Proxy(value if "://" in value else f"http://{value}")
    except InvalidURL, ValueError:
        return False
    return True


def _mask_proxy_credentials(value: str) -> str:
    """Hide the user info of a proxy address before it is shown.

    The cut is textual and errs toward hiding too much: a value httpx
    rejects need not parse as a URL, and a password may hold a raw ``/``,
    ``@`` or ``://``, so everything before the last ``@`` goes but a
    leading scheme, kept only when it is one by RFC 3986's grammar.
    """
    if "@" not in value:
        return value
    scheme = _URL_SCHEME_PREFIX.match(value)
    return f"{scheme.group(0) if scheme else ''}***@{value.rpartition('@')[2]}"


def _scrub_env(names: tuple[str, ...], is_valid: Callable[[str], bool]) -> dict[str, str]:
    scrubbed: dict[str, str] = {}
    for var in names:
        raw = os.environ.get(var)
        if raw is None or is_valid(raw):
            continue
        scrubbed[var] = raw
        del os.environ[var]
    return scrubbed


def sanitize_no_proxy_env() -> dict[str, str]:
    """Remove invalid ``NO_PROXY`` / ``no_proxy`` env vars; return what was scrubbed.

    Called once at startup so an invalid environment value cannot crash
    the LLM client constructor.  Both casings
    are checked because httpx consults each (uppercase wins, lowercase is
    the fallback) — a bad lowercase ``no_proxy`` exported by the shell
    would otherwise still crash.

    Returns a mapping from var name to the scrubbed value (empty mapping
    when nothing was wrong).  Caller surfaces a user-visible warning per
    entry.
    """
    return _scrub_env(_NO_PROXY_ENV_VARS, is_valid_no_proxy)


def sanitize_proxy_url_env() -> dict[str, str]:
    """Remove proxy-address env vars httpx can't use; return what was scrubbed.

    Called once at startup, beside :func:`sanitize_no_proxy_env` and for the
    same reason: every httpx client that reads the environment (model
    requests, HTTP MCP servers, the update check) builds a proxy from these
    values when it is constructed, and one it can't build — a desktop's
    ``ALL_PROXY=socks://…`` — fails that construction.  A profile's proxy
    bypass can't help: its mounts are applied only after the proxies are
    built.  The value is dropped, never rewritten, since only the user knows
    which protocol it meant.

    Returns a mapping from var name to the scrubbed value with any
    credentials masked, since the caller shows it in a warning per entry.
    """
    return {
        var: _mask_proxy_credentials(raw) for var, raw in _scrub_env(_PROXY_URL_ENV_VARS, is_valid_proxy_url).items()
    }
