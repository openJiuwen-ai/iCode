# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Server-owned model catalog.

``~/.chrys/models/`` has exactly one owner at a time, and there is no merge
between two sources:

* **The catalog is in charge.**  A tier base (or an explicit
  ``AIXCODING_MODEL_CATALOG_URL``) always names a source — see
  :data:`_CATALOG_BASES`, which covers every tier — so the question is only
  whether a sync has ever succeeded.  Every successful sync *replaces the
  directory wholesale* — files the catalog no longer contains are deleted —
  and its contents become server-owned.  A failed sync changes nothing.
* **Until that first success the directory's content is untouched** — no
  credential (never logged in) or every fetch failed so far, so the files
  on disk are whatever the user or an installer left there.  The read-only
  gate does not wait for a sync, though: a source is always named, which
  is what :func:`is_catalog_managed` answers, so model configuration is
  read-only from the first start.

"Synced once" is sticky: the state file marks the directory as server-owned, so
a later failed fetch (offline start) degrades to the previous catalog instead of
silently handing write access back to the user, which the next sync would undo.

Publishing is two-phase at the file level: every profile is first staged as
``<id>.yaml.tmp``, then each temp file is moved onto its final name with
``os.replace`` (atomic within a volume), then files the catalog no longer
contains are deleted, then the state file is written.  A failure mid-way leaves
the previous catalog readable — no file is lost, at worst old and new coexist
until the next sync converges — and every ``OSError`` on the publish path is
converted into the module's failure channel instead of escaping to the caller.

Catalog profiles carry no credentials by default: the host injects provider
environment variables, matching the desktop host's model-projection behaviour.
``api_key`` is honoured when the catalog does send one, and profile files are
then written 0o600.

Migration note: the poll runtime is a process-wide singleton with generation
guarding (``start_sync_runtime`` / ``stop_sync_runtime`` / ``sync_now``), so a
logout or account switch invalidates in-flight results instead of letting them
publish — see ``aixcoding/docs/batches/catalog-sync/design.md``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from chrys.foundation.text.yaml_io import dump_yaml
from chrys.service.aixcoding.config import load_config
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.profiles.models.schema import (
    API_STYLE_CHAT_COMPLETIONS,
    VALID_API_STYLES,
    VALID_PROVIDERS,
    ModelProfile,
)

# AIxCoding 方案六 (2026-11-04; revised 2026-11-05 logging-routing-fixes):
# catalog records are technical events on the file-only "aixcoding.detail.catalog"
# logger (ADR-0017) — the main volume and the F6 panel carry user-facing events
# only (DEVELOPMENT.md §9). The detail subtree never propagates to the panel or
# the upstream log.
_detail_log = logging.getLogger("aixcoding.detail.catalog")

#: AIxCoding detail logging: level of the file-only channel; mirrors
#: ``DETAIL_LEVEL`` in diagnostics.logging_support (ADR-0017), as auth does.
_DETAIL_LEVEL = 5

#: Fork-private configuration contract: read and parsed by this module alone
#: (never declared in the settings system), so they carry the ``AIXCODING_``
#: prefix per DIRECTORY.md §9.
CATALOG_URL_ENV = "AIXCODING_MODEL_CATALOG_URL"
CATALOG_TOKEN_ENV = "AIXCODING_MODEL_CATALOG_TOKEN"
#: Overrides :data:`SYNC_INTERVAL_SECONDS` for one process, in seconds — how a
#: server-side change is watched without waiting the default quarter of an hour.
SYNC_INTERVAL_ENV = "AIXCODING_MODEL_CATALOG_SYNC_INTERVAL"

#: Appended to a base URL the way the reference client builds the endpoint, so
#: a deployment configures only the host part.
CATALOG_PATH = "/llm/api/v1/continue-config/dispatch"

#: The shared ``aixcoding.toml`` base wins when configured; otherwise every tier
#: has a base in :data:`_CATALOG_BASES` below — local the loopback mock, dev and
#: prod their own hosts. The same shared environment drives auth and the model
#: catalog, so a session cannot log into one tier while pulling models from another.
#: Whether a sync runs is the credential's to decide (:func:`has_catalog_credential`
#: gates the poll; the startup path swallows the no-credential failure with a log).

#: Catalog base per deployment tier, keyed by the very values auth defines
#: (``chrys.kernel.aixcoding.auth.types.Environment``) — see the drift test in
#: ``aixcoding/tests/catalog/test_catalog_sync.py``. DEV and PROD are
#: auth's own hosts (``chrys.kernel.aixcoding.auth.environments._BASE_PATHS``):
#: one deployment, one pair of tiers, under the ``/csas`` prefix the reference
#: client's ``WebConfigs.URL_MAP`` serves the web API from, since
#: :data:`CATALOG_PATH` carries the rest (``/llm/api/v1/...``) and the two must
#: not both add ``/api/v1``. LOCAL is the loopback mock, which serves the
#: catalog straight off the origin — no prefix — on the same port auth uses for
#: the tier, written as an address because ``localhost`` may resolve to a
#: address the mock is not listening on. An unset or unknown tier resolves to
#: PROD the way ``resolve_environment`` does — an unrecognised value must not
#: silently leave the user with no catalog at all.
_CATALOG_BASES: dict[str, str] = {
    "local": "http://127.0.0.1:7777",
    "dev": "http://81.89.182.150/csas",
    "prod": "http://82.187.34.98/csas",
}
DEFAULT_ENVIRONMENT = "prod"

#: Written inside the profiles directory.  The leading dot keeps it out of the
#: ``*.yaml`` / ``*.yml`` scans that build the registry.
STATE_FILENAME = ".catalog-state.json"

#: Staging suffix for the two-phase publish: ``<id>.yaml.tmp`` matches neither
#: scan pattern, so a half-published directory never feeds the registry.
PROFILE_TMP_SUFFIX = ".yaml.tmp"

SOURCE_REMOTE = "remote"
SOURCE_HOST = "host"

MAX_PROFILES = 128
#: Same shape as the desktop host's profile-id rule, so ids agree across sides.
SAFE_PROFILE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

#: How often the catalog is re-fetched while a session runs. The reference
#: client polls the same endpoint on a clock
#: (`core/aixcoding/config/ConfigSyncService.ts`: immediate sync at startup,
#: then ``setInterval`` every 15 minutes), so that is the default: a
#: server-side model change reaches a long-lived session without a restart,
#: and a quarter of an hour keeps the traffic negligible.
#:
#: ``AIXCODING_MODEL_CATALOG_SYNC_INTERVAL`` overrides it for one process —
#: see :func:`sync_interval_seconds`, which is what actually reads the
#: environment. The constant is the default, not the interval: binding it as a
#: parameter default would freeze the value at import, too early to see the
#: variable.
SYNC_INTERVAL_SECONDS = 15 * 60

_DEFAULT_FETCH_TIMEOUT = 3.0


class ModelCatalogError(Exception):
    """Raised when a catalog cannot be fetched, parsed or applied."""


class ModelCatalogReadOnly(ModelCatalogError):
    """Raised when a user edit is attempted on a server-owned catalog."""


@dataclass(frozen=True)
class ModelCatalog:
    """One complete, authoritative set of model profiles."""

    items: tuple[ModelProfile, ...]
    version: str = ""
    default_profile_id: str = ""

    def profile_ids(self) -> tuple[str, ...]:
        return tuple(profile.id for profile in self.items)


@dataclass(frozen=True)
class CatalogState:
    """What the last successful sync wrote, persisted next to the profiles."""

    version: str
    source: str
    profile_ids: tuple[str, ...]
    default_profile_id: str = ""


@dataclass(frozen=True)
class CatalogSyncResult:
    """Outcome of one sync attempt."""

    replaced: bool
    version: str
    profile_ids: tuple[str, ...]
    default_profile_id: str = ""
    previous_profile_ids: tuple[str, ...] = ()

    @property
    def removed_profile_ids(self) -> tuple[str, ...]:
        return tuple(pid for pid in self.previous_profile_ids if pid not in self.profile_ids)


def catalog_directory() -> Path:
    """Return the directory a catalog replaces: ``~/.chrys/models``."""
    from chrys.foundation.platform import get_platform

    return get_platform().config_dir / "models"


def catalog_state_path() -> Path:
    return catalog_directory() / STATE_FILENAME


def catalog_tier() -> str:
    """Return the shared deployment tier for auth and the model catalog."""
    return load_config().environment


def catalog_base_url() -> str:
    """Return the shared TOML base, falling back to the deployment tier."""
    configured = load_config().model_catalog.base_url
    if configured:
        return configured
    return _CATALOG_BASES.get(catalog_tier(), _CATALOG_BASES[DEFAULT_ENVIRONMENT])


def catalog_url() -> str:
    """Return the full catalog endpoint — never empty while
    :func:`catalog_base_url` has a tier to draw on; the empty guard below
    only covers a future base table with a gap in it.

    An explicitly configured URL still wins — that is how an existing
    deployment points somewhere specific. Otherwise the base comes from
    :func:`catalog_base_url` and :data:`CATALOG_PATH` is appended to it, which
    is how the reference client builds the endpoint from a base.
    """
    value = os.environ.get(CATALOG_URL_ENV, "").strip()
    if value:
        return value
    base = catalog_base_url()
    return f"{base.rstrip('/')}{CATALOG_PATH}" if base else ""


def read_catalog_state() -> CatalogState | None:
    """Return the persisted state of the last successful sync, if any."""
    path = catalog_state_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return None
    if not isinstance(raw, dict):
        return None
    ids = raw.get("profile_ids")
    return CatalogState(
        version=str(raw.get("version", "")),
        source=str(raw.get("source", SOURCE_REMOTE)),
        profile_ids=tuple(str(item) for item in ids) if isinstance(ids, list) else (),
        default_profile_id=str(raw.get("default_profile_id", "")),
    )


def write_catalog_state(state: CatalogState) -> None:
    """Persist *state* next to the profiles it describes."""
    path = catalog_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": state.version,
        "source": state.source,
        "profile_ids": list(state.profile_ids),
        "default_profile_id": state.default_profile_id,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def is_catalog_managed() -> bool:
    """Return whether ``~/.chrys/models`` is owned by a catalog.

    True once a source is configured or a catalog has ever been applied, which
    is what makes the directory read-only.
    """
    return bool(catalog_url()) or read_catalog_state() is not None


def require_catalog_writable() -> None:
    """Reject user edits while a catalog owns the directory."""
    if is_catalog_managed():
        raise ModelCatalogReadOnly("Model profiles are managed by the server catalog and cannot be edited locally.")


def _profile_field_names() -> set[str]:
    return {field.name for field in fields(ModelProfile)}


def _coerce_version(catalog: ModelCatalog) -> str:
    """Fingerprint the whole item set so a version-less payload still has one.

    Every serialized field is hashed, not just the ids: the catalog owns the
    directory wholesale, so a change to *any* field — display name, base url,
    context length — is a change the files have to be rewritten for. Hashing
    the ids alone would call such a payload "unchanged" and leave the previous
    values on disk.

    What is hashed is exactly what :func:`_write_profile` puts in each YAML, so
    the fingerprint changes if and only if the directory's content would.

    Order participates: ``items`` is a tuple hashed in its own order, so
    reordering otherwise identical models counts as a change (the rewrite that
    follows is a no-op in content, not a wrong result).

    The fingerprint is a record — it lands in the state file and the logs —
    not a skip mechanism: every sync rewrites the directory wholesale, as the
    0.28 deployment did before it (no unchanged-shortcut was ever shipped).
    """
    payload = json.dumps(
        [_profile_to_dict(profile) for profile in catalog.items],
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ── Server payload translation ───────────────────────────────────────────
# The server speaks its own client's shape: a ``models`` array of ``title`` /
# ``model`` / ``apiBase`` / ``contextLength``, with no id and no wire fields.
# It is not ours to change, so the translation lives here: what the payload
# cannot carry is filled in from the one assumption that makes it derivable —
# every served model is reached through OpenRouter.

CONTINUE_PROVIDER = "openai"
CONTINUE_API_STYLE = API_STYLE_CHAT_COMPLETIONS
CONTINUE_BASE_URL = "https://openrouter.ai/api/v1"


def derive_profile_id(model: str) -> str:
    """Slug one served model name into a usable profile id.

    Pure by construction: the same ``model`` must always yield the same id, or
    the user's selection and every agent binding would break on each sync.
    """
    slug = re.sub(r"[^a-z0-9._-]+", "-", model.strip().lower())
    return slug.strip("-")


def _unique_profile_id(base: str, taken: set[str]) -> str:
    """Disambiguate two served models that slug to the same id.

    The suffix follows payload order: two same-slug models swapped by the
    server trade ids (a known limit — distinct display models sharing one
    slug is the pathological case).  Ids stay unique and stable within one
    payload, which is what the directory rewrite needs.
    """
    if base not in taken:
        return base
    suffix = 2
    while f"{base}-{suffix}" in taken:
        suffix += 1
    _detail_log.log(_DETAIL_LEVEL, "模型目录中的 profile id %r 已存在；改用 %r 消歧。", base, f"{base}-{suffix}")
    return f"{base}-{suffix}"


def _positive_int(value: object) -> int:
    """One payload number, or 0 when it is absent or not a positive integer.

    Booleans are refused even though ``bool`` subclasses ``int`` — a JSON
    ``true`` is a flag someone misplaced, not a context length of 1.
    """
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        _detail_log.log(_DETAIL_LEVEL, "模型目录数值字段 %r 不是数字；已丢弃该字段。", value)
        return 0
    try:
        number = int(value)
    except ValueError, OverflowError:
        _detail_log.log(_DETAIL_LEVEL, "模型目录数值字段 %r 无法转换为整数；已丢弃该字段。", value)
        return 0
    return max(0, number)


def translate_models_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Translate the server's ``models`` payload into iCode-native items.

    Returns the shape :func:`parse_catalog` already accepts, so validation
    stays the single gate — translation adds no second path to it.
    """
    entries = payload.get("models")
    if not isinstance(entries, list):
        raise ModelCatalogError(f"服务端目录的 'models' 必须是列表，实际为 {type(entries).__name__!r}")

    items: list[dict[str, Any]] = []
    taken: set[str] = set()
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise ModelCatalogError(f"目录第 {index} 项必须是 JSON 对象，实际为 {type(entry).__name__!r}")
        model = str(entry.get("model") or "").strip()
        if not model:
            raise ModelCatalogError(f"目录第 {index} 项缺少 'model'，无法派生 profile id")
        profile_id = _unique_profile_id(derive_profile_id(model), taken)
        taken.add(profile_id)
        item: dict[str, Any] = {
            "id": profile_id,
            # The real payload's title carries leading whitespace.
            "name": str(entry.get("title") or model).strip(),
            "provider": CONTINUE_PROVIDER,
            "api_style": CONTINUE_API_STYLE,
            "model_id": model,
            # ``apiBase`` is optional and often simply absent; the served
            # models are reached through OpenRouter either way.
            "base_url": str(entry.get("apiBase") or "").strip() or CONTINUE_BASE_URL,
            # No ``api_key`` on purpose: the catalog is not a credential
            # channel, and a key in the profile file would be one more place
            # for it to leak. The key is resolved at send time from the host's
            # environment instead (service/llm/clients.py).
        }
        context_length = _positive_int(entry.get("contextLength"))
        if context_length:
            item["max_context_tokens"] = context_length
        items.append(item)

    # The server's shape carries no version and no default: the caller
    # fingerprints the id list, and a cold start takes the first entry.
    return {"items": items, "version": "", "default_profile_id": items[0]["id"] if items else ""}


def parse_catalog(payload: object) -> ModelCatalog:
    """Validate one catalog payload and build a :class:`ModelCatalog`.

    Accepts a bare JSON array of profiles, an object with ``items``, or the
    server's own ``models`` shape, which is translated first. Unknown keys are
    dropped so the payload can evolve without breaking older clients.
    """
    items_raw: object
    version = ""
    default_profile_id = ""

    if isinstance(payload, dict):
        if "models" in payload and "items" not in payload:
            payload = translate_models_payload(payload)
        items_raw = payload.get("items")
        version = str(payload.get("version", "") or "")
        default_profile_id = str(payload.get("default_profile_id", "") or "")
    else:
        items_raw = payload

    if not isinstance(items_raw, list):
        raise ModelCatalogError(f"模型目录必须是列表或包含 'items' 的对象，实际为 {type(items_raw).__name__!r}")
    if not items_raw:
        raise ModelCatalogError("模型目录为空；拒绝用空目录替换现有模型列表。")
    if len(items_raw) > MAX_PROFILES:
        raise ModelCatalogError(f"模型目录超过 {MAX_PROFILES} 个模型配置的上限。")

    allowed = _profile_field_names()
    profiles: list[ModelProfile] = []
    seen: set[str] = set()
    for index, item in enumerate(items_raw, start=1):
        if not isinstance(item, dict):
            raise ModelCatalogError(f"目录第 {index} 项必须是 JSON 对象，实际为 {type(item).__name__!r}")
        dropped = sorted(set(item) - allowed)
        if dropped:
            _detail_log.log(_DETAIL_LEVEL, "模型目录第 %d 项：已丢弃未知字段：%s。", index, ", ".join(dropped))
        filtered = {key: value for key, value in item.items() if key in allowed}
        try:
            profile = ModelProfile(**filtered)  # type: ignore[arg-type]
            # Dataclass construction checks nothing at runtime, so the field
            # checks below meet raw payload types — a non-str id fails the
            # regex match, an unhashable provider fails the set lookup — as
            # TypeError, folded into the same conversion.  name and model_id
            # are checked for the registry and request chains that consume
            # them (``profile.name.casefold()`` ordering, wire model id).
            if not SAFE_PROFILE_ID.match(profile.id):
                raise ModelCatalogError(f"目录第 {index} 项的 profile id 不可用：{profile.id!r}")
            if not isinstance(profile.name, str):
                raise ModelCatalogError(f"目录第 {index} 项的 name 不是字符串：{profile.name!r}")
            if not isinstance(profile.model_id, str):
                raise ModelCatalogError(f"目录第 {index} 项的 model_id 不是字符串：{profile.model_id!r}")
            if profile.provider not in VALID_PROVIDERS:
                raise ModelCatalogError(f"目录第 {index} 项的 provider 不受支持：{profile.provider!r}")
            if profile.api_style not in VALID_API_STYLES:
                raise ModelCatalogError(f"目录第 {index} 项的 api_style 不受支持：{profile.api_style!r}")
        except (TypeError, ValueError) as exc:
            raise ModelCatalogError(f"目录第 {index} 项无效：{exc}") from exc
        if profile.id in seen:
            raise ModelCatalogError(f"目录第 {index} 项重复使用 profile id {profile.id!r}")
        seen.add(profile.id)
        profiles.append(profile)

    catalog = ModelCatalog(items=tuple(profiles), version=version, default_profile_id=default_profile_id)
    if not catalog.version:
        catalog = ModelCatalog(
            items=catalog.items,
            version=_coerce_version(catalog),
            default_profile_id=catalog.default_profile_id,
        )
    return catalog


def _catalog_token() -> str:
    """The credential to send with a catalog request, or "" when there is none.

    The catalog is an authenticated endpoint: the token the login flow stored
    is the one it expects. The environment still wins, so a deployment that
    issues its own credential (or a test) is not forced through a login.

    Reading it performs no network I/O — a stale token is the server's to
    reject, not ours to refresh here — but a session that cannot be read at
    all yields "", which the caller turns into a cancelled sync rather than an
    unauthenticated request answered with a 401.
    """
    return _resolve_catalog_credential()[0]


def _resolve_catalog_credential() -> tuple[str, str]:
    """``(token, source)`` resolved in one pass; source is "env" | "store" | "".

    The source drives the pre-verification in :func:`fetch_catalog`
    (catalog-auth-gating, review D3): env credentials have no login session to
    verify against and stay the server's to judge, store credentials must be
    verified live first. Token and origin are read here exactly once, so the
    two can never drift apart.
    """
    env_token = os.environ.get(CATALOG_TOKEN_ENV, "").strip()
    if env_token:
        return env_token, "env"
    try:
        from chrys.kernel.aixcoding.auth import get_login_session

        stored = get_login_session().stored_token
    except Exception:
        _detail_log.log(_DETAIL_LEVEL, "读取模型目录请求所需的登录会话失败。", exc_info=True)
        return "", ""
    return (stored or "").strip(), "store"


def has_catalog_credential() -> bool:
    """Whether a catalog request could be authenticated right now.

    The poll is only worth a thread once this is true: without a credential
    every attempt is refused before a request is made, so an unauthenticated
    session would spin one for nothing. The caller starts the poll when this
    flips — on login — rather than when the process starts.
    """
    return bool(_catalog_token())


def _catalog_user_id() -> str:
    """The logged-in user for the ``userId`` parameter, or "" when unknown.

    The remote catalog is served per user and answers an empty id with the
    public default (``ConfigSyncService.fetchRemoteModelConfig``), so unlike
    the credential an absent id does not cancel the sync — it asks for the
    default list instead.
    """
    try:
        from chrys.kernel.aixcoding.auth import get_login_session

        stored = get_login_session().stored_user_id
    except Exception:
        _detail_log.log(_DETAIL_LEVEL, "读取模型目录请求所需的登录用户失败。", exc_info=True)
        return ""
    return (stored or "").strip()


#: Query parameters the reference client hard-codes on every catalog request
#: (``ConfigSyncService.fetchRemoteModelConfig``): the dispatch endpoint
#: answers one global config scope, as JSON. ``userId`` is not among them —
#: it is appended per login by :func:`_catalog_endpoint`.
_CATALOG_FIXED_QUERY: tuple[tuple[str, str], ...] = (
    ("scopeType", "global"),
    ("scopeValue", "config"),
    ("format", "json"),
)


def _catalog_endpoint(endpoint: str, user_id: str) -> str:
    """Return *endpoint* carrying the fixed scope/format parameters and ``userId=``.

    The reference client concatenates ``&userId=`` onto a URL that already
    ends in a query; parsing instead keeps this right for a bare endpoint too,
    which is what a configured base produces. The scope/format parameters are
    part of the wire contract, so they are always sent — replacing any a
    configured URL happened to carry — while an existing ``userId`` is replaced
    rather than duplicated.
    """
    parts = urlsplit(endpoint)
    fixed = {name for name, _value in _CATALOG_FIXED_QUERY}
    query = [
        (name, value)
        for name, value in parse_qsl(parts.query, keep_blank_values=True)
        if name != "userId" and name not in fixed
    ]
    query.extend(_CATALOG_FIXED_QUERY)
    query.append(("userId", user_id))
    return urlunsplit(parts._replace(query=urlencode(query)))


async def fetch_catalog(url: str | None = None, *, timeout: float = _DEFAULT_FETCH_TIMEOUT) -> ModelCatalog:
    """Fetch and validate a catalog.

    Args:
        url: Endpoint.  Defaults to :func:`catalog_url`.
        timeout: HTTP timeout in seconds; deliberately short on the startup path.

    Raises:
        ModelCatalogError: when no source is configured, when no credential is
            available (there is no unauthenticated catalog request), or on
            transport, HTTP, decoding or validation failures.
    """
    endpoint = (url or catalog_url()).strip()
    if not endpoint:
        raise ModelCatalogError("未配置模型目录来源。")

    token, token_source = _resolve_catalog_credential()
    if not token:
        raise ModelCatalogError(
            f"没有模型目录凭证：请登录，或设置 {CATALOG_TOKEN_ENV}。本次同步已跳过，保留旧模型列表。"
        )
    if token_source != "env":
        # AIxCoding catalog-auth-gating (decision 乙): only a login verified
        # live by auth may fetch; env credentials have no session to verify
        # and stay the server's to judge. The WARNING sits before the raise
        # because the poll's _sync_once never logs ModelCatalogError itself
        # ("already logged where it was raised").
        from chrys.kernel.aixcoding.auth import get_login_session

        if await get_login_session().verify_live_credential() is None:
            _detail_log.log(_DETAIL_LEVEL, "跳过模型目录同步：登录未通过验证（未登录、凭证被拒或离线）。")
            raise ModelCatalogError("模型目录请求的登录状态未通过验证。本次同步已跳过，保留旧模型列表。")

    headers: dict[str, str] = {"Accept": "application/json", "token": token}
    endpoint = _catalog_endpoint(endpoint, _catalog_user_id())
    # The query string carries the user id: logs and error text keep path only.
    log_endpoint = endpoint.split("?", 1)[0]

    started = time.monotonic()
    _detail_log.log(_DETAIL_LEVEL, "模型目录请求：GET %s（超时 %.0f 秒）。", log_endpoint, timeout)
    _detail_log.log(_DETAIL_LEVEL, "模型目录请求：GET %s（超时 %.0f 秒）。", endpoint, timeout)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(endpoint, headers=headers)
            response.raise_for_status()
            payload: Any = response.json()
    except httpx.HTTPStatusError as exc:
        _detail_log.log(_DETAIL_LEVEL, "模型目录请求被 %s 拒绝：HTTP %d。", log_endpoint, exc.response.status_code)
        raise ModelCatalogError(f"模型目录请求失败：HTTP {exc.response.status_code}") from exc
    except httpx.HTTPError as exc:
        _detail_log.log(_DETAIL_LEVEL, "模型目录请求 %s 在收到响应前失败：%s", log_endpoint, exc)
        raise ModelCatalogError(f"模型目录请求失败：{exc}") from exc
    except ValueError as exc:
        _detail_log.log(_DETAIL_LEVEL, "来自 %s 的模型目录响应不是有效 JSON：%s", log_endpoint, exc)
        raise ModelCatalogError(f"模型目录响应不是有效 JSON：{exc}") from exc

    try:
        catalog = parse_catalog(payload)
    except ModelCatalogError as exc:
        # The poll loop treats a ModelCatalogError from here as already
        # logged; parse failures are raised below the logged fetch block,
        # so they get their own line or they would go silent on the clock.
        _detail_log.log(_DETAIL_LEVEL, "来自 %s 的模型目录响应不可用：%s", log_endpoint, exc)
        raise
    _detail_log.log(
        _DETAIL_LEVEL,
        "模型目录响应：HTTP %d，%d 个模型，版本 %.12s，耗时 %.2f 秒。",
        response.status_code,
        len(catalog.items),
        catalog.version,
        time.monotonic() - started,
    )
    _detail_log.log(
        _DETAIL_LEVEL,
        "模型目录响应详情：version=%s default_profile_id=%s models=%s",
        catalog.version,
        catalog.default_profile_id,
        [{"id": p.id, "name": p.name, "model_id": p.model_id, "base_url": p.base_url} for p in catalog.items],
    )
    return catalog


def _remove_orphan_tmp(directory: Path) -> None:
    """Delete leftover staging files from an interrupted earlier publish."""
    for path in sorted(directory.glob(f"*{PROFILE_TMP_SUFFIX}")):
        try:
            path.unlink()
        except OSError:
            _detail_log.log(_DETAIL_LEVEL, "无法删除遗留暂存文件 %s；下次同步将重试。", path)


def _write_profile(path: Path, profile: ModelProfile) -> None:
    """Serialize *profile* to *path* (final or staging — same content)."""
    path.write_text(dump_yaml(_profile_to_dict(profile)), encoding="utf-8")
    if profile.api_key:
        # Credentials were supplied by the catalog: keep them off group/other.
        path.chmod(0o600)


def _profile_to_dict(profile: ModelProfile) -> dict[str, Any]:
    from chrys.service.profiles.models.serializer import profile_to_dict

    return profile_to_dict(profile)


def apply_catalog(catalog: ModelCatalog, *, source: str = SOURCE_REMOTE) -> CatalogSyncResult:
    """Replace the whole model directory with *catalog*, in two file-level phases.

    Phase one stages every profile as ``<id>.yaml.tmp`` next to the final
    names — a failure there has not touched the previous catalog at all. Phase
    two moves each staging file onto its final name with ``os.replace`` (atomic
    within a volume), deletes files the catalog no longer contains, and writes
    the state file. A failure mid-phase-two leaves no file lost — every id
    either still holds its previous content or already holds its new one — and
    the next sync converges the directory wholesale, which is why there is no
    resume bookkeeping to keep honest.

    Raises:
        ModelCatalogError: if the catalog is unusable or the directory cannot
            be written — including every ``OSError`` on the publish path,
            which is converted here rather than escaping to the startup path.
            The staging files of a failed attempt are cleaned up. A failure
            in the last step (the state write) leaves the directory already
            new with a stale state file — readable either way, and the next
            sync rewrites the state.
    """
    if not catalog.items:
        raise ModelCatalogError("不能应用空模型目录。")

    directory = catalog_directory()
    previous_state = read_catalog_state()
    previous_ids = previous_state.profile_ids if previous_state else ()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        _remove_orphan_tmp(directory)
        staged: list[tuple[Path, Path]] = []
        for profile in catalog.items:
            tmp_path = directory / f"{profile.id}{PROFILE_TMP_SUFFIX}"
            _write_profile(tmp_path, profile)
            staged.append((tmp_path, directory / f"{profile.id}.yaml"))

        for tmp_path, final_path in staged:
            os.replace(tmp_path, final_path)

        final_names = {f"{profile.id}.yaml" for profile in catalog.items}
        for pattern in ("*.yaml", "*.yml"):
            for path in sorted(directory.glob(pattern)):
                if path.name not in final_names:
                    path.unlink()

        write_catalog_state(
            CatalogState(
                version=catalog.version,
                source=source,
                profile_ids=catalog.profile_ids(),
                default_profile_id=catalog.default_profile_id,
            )
        )
    except OSError as exc:
        for path in sorted(directory.glob(f"*{PROFILE_TMP_SUFFIX}")):
            try:
                path.unlink()
            except OSError:
                _detail_log.log(_DETAIL_LEVEL, "发布失败后无法删除暂存文件 %s。", path)
        _detail_log.log(
            _DETAIL_LEVEL,
            "模型目录发布失败（%s）；旧模型列表仍可使用，下次同步将重试。",
            exc,
        )
        raise ModelCatalogError(f"模型目录发布失败：{exc}") from exc

    result = CatalogSyncResult(
        replaced=True,
        version=catalog.version,
        profile_ids=catalog.profile_ids(),
        default_profile_id=catalog.default_profile_id,
        previous_profile_ids=previous_ids,
    )
    _detail_log.log(
        _DETAIL_LEVEL,
        "已应用模型目录 %s：来自 %s，共 %d 个模型配置。",
        catalog.version,
        len(catalog.items),
        source,
    )
    return result


async def sync_catalog(
    *,
    url: str | None = None,
    timeout: float = _DEFAULT_FETCH_TIMEOUT,
    source: str = SOURCE_REMOTE,
) -> CatalogSyncResult | None:
    """Fetch and apply the catalog in one step.

    Returns ``None`` when no source is configured (the directory stays the
    user's own).  Propagates :class:`ModelCatalogError` on failure so the
    caller decides between a silent startup fallback and a reported refresh;
    the directory is untouched in that case.
    """
    if not (url or catalog_url()):
        return None
    catalog = await fetch_catalog(url, timeout=timeout)
    return apply_catalog(catalog, source=source)


def sync_catalog_blocking(*, timeout: float = _DEFAULT_FETCH_TIMEOUT) -> CatalogSyncResult | None:
    """Run one sync from the startup path, where no event loop exists yet.

    Returns ``None`` when no source is configured.  Failures are logged and
    swallowed — any failure, not just :class:`ModelCatalogError`: the whole
    chain below sits inside one guard, so an unexpected error (a settings
    read that cannot complete, say) degrades exactly like a network one and
    the directory keeps the previous catalog, so an offline start still has
    models.  A running loop means this was called from async code that
    should have awaited :func:`sync_catalog` instead; starting a nested
    loop there would be wrong, so the sync is skipped.
    """
    try:
        if not catalog_url():
            return None
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            _detail_log.log(
                _DETAIL_LEVEL, "在运行中的事件循环里调用了 sync_catalog_blocking；请改用 await sync_catalog。"
            )
            return None
        return asyncio.run(sync_catalog(timeout=timeout))
    except ModelCatalogError as exc:
        _detail_log.log(_DETAIL_LEVEL, "同步模型目录失败，保留旧模型列表：%s", exc)
        return None
    except Exception:
        _detail_log.log(
            _DETAIL_LEVEL,
            "同步模型目录时发生意外错误；保留旧模型列表并继续启动。",
            exc_info=True,
        )
        return None


def sync_interval_seconds() -> float:
    """Return the poll interval: the environment's, else :data:`SYNC_INTERVAL_SECONDS`.

    Read at call time, not at import: the variable exists so one process can be
    started with a shorter interval than the default — watching a server-side
    change land without waiting the quarter of an hour.

    Anything unparseable or not a positive finite number falls back to the
    default and is logged: a typo must neither stop the poll (zero) nor turn
    it into a hot loop (negative, NaN) or kill the wait (infinity).
    """
    raw = os.environ.get(SYNC_INTERVAL_ENV, "").strip()
    if not raw:
        return float(SYNC_INTERVAL_SECONDS)
    try:
        value = float(raw)
    except ValueError:
        _detail_log.log(_DETAIL_LEVEL, "%s=%r 不是数字；使用 %.0f 秒。", SYNC_INTERVAL_ENV, raw, SYNC_INTERVAL_SECONDS)
        return float(SYNC_INTERVAL_SECONDS)
    if not math.isfinite(value) or value <= 0:
        _detail_log.log(
            _DETAIL_LEVEL,
            "%s=%r 不是正有限数；使用 %.0f 秒。",
            SYNC_INTERVAL_ENV,
            raw,
            SYNC_INTERVAL_SECONDS,
        )
        return float(SYNC_INTERVAL_SECONDS)
    return value


# ── Poll runtime ──────────────────────────────────────────────────────────
# A process-wide singleton instead of per-caller handles: the TUI wires it
# once from main() (start) and login_actions (login/logout), and a second
# concurrent loop would race the directory and the registry it refreshes.
#
# Generation guarding is what makes stop cheap and safe: start, stop and
# sync_now each bump a counter, and a sync whose generation no longer matches
# is dropped before it can publish — an in-flight fetch started before a
# logout must not install the logged-out account's catalog afterwards. Two
# boundaries are honest rather than absolute: a callback already picked up for
# execution cannot be revoked, and a publish that passed the generation check
# finishes its disk writes (idempotent, the next sync converges).


class _SyncRuntime:
    """The one catalog poll loop this process runs, on a daemon thread."""

    def __init__(
        self,
        registry: ModelProfileRegistry,
        refresh_ui: Callable[[CatalogSyncResult], None],
        *,
        timeout: float,
    ) -> None:
        self._cv = threading.Condition()
        self._generation = 0
        self._stopped = False
        self._pending = False
        self._registry = registry
        self._refresh_ui = refresh_ui
        self._timeout = timeout
        self._interval = sync_interval_seconds()
        self._thread = threading.Thread(
            target=self._run,
            name="chrys-model-catalog-sync",
            daemon=True,
        )

    # -- lifecycle (called under _runtime_lock) --

    def start(self, *, immediate: bool) -> None:
        self._thread.start()
        if immediate:
            # Equivalent to sync_now(), but before the thread's first wait:
            # a mid-session start (a login) needs the list now, not after a
            # whole interval.
            with self._cv:
                self._generation += 1
                self._pending = True
                self._cv.notify_all()

    def stop(self) -> None:
        with self._cv:
            self._generation += 1
            self._stopped = True
            # Drop the callbacks so a stale in-flight callback finds them
            # gone instead of firing into a torn-down app/screen.
            self._registry = None  # type: ignore[assignment]
            self._refresh_ui = None  # type: ignore[assignment]
            self._cv.notify_all()

    def sync_now(self) -> None:
        with self._cv:
            if self._stopped:
                return
            self._generation += 1
            self._pending = True
            self._cv.notify_all()

    def is_running(self) -> bool:
        with self._cv:
            return not self._stopped

    def update_refs(
        self,
        registry: ModelProfileRegistry,
        refresh_ui: Callable[[CatalogSyncResult], None],
    ) -> None:
        """Re-target the runtime at fresh wiring after a re-start-in-place."""
        with self._cv:
            if self._stopped:
                return
            self._registry = registry
            self._refresh_ui = refresh_ui

    # -- internals --

    def _wait(self, interval: float) -> bool:
        """Wait for the interval, a wake-up, or a stop.

        Returns ``False`` when the loop must end, ``True`` when it owes a sync
        — either the interval elapsed or one was requested.
        """
        with self._cv:
            if self._stopped:
                return False
            if not self._pending:
                self._cv.wait(interval)
                if self._stopped:
                    return False
            self._pending = False
        return True

    def _generation_snapshot(self) -> int:
        with self._cv:
            return self._generation

    def _run(self) -> None:
        _detail_log.log(_DETAIL_LEVEL, "模型目录同步已启动：每 %.0f 秒轮询。", self._interval)
        while self._wait(self._interval):
            self._sync_once()
        _detail_log.log(_DETAIL_LEVEL, "模型目录同步已停止。")

    def _sync_once(self) -> None:
        generation = self._generation_snapshot()
        try:
            catalog = asyncio.run(fetch_catalog(timeout=self._timeout))
        except ModelCatalogError:
            return  # already logged where it was raised
        except Exception:
            # The poll is a daemon thread nobody restarts: an unexpected
            # error must not kill the schedule, only this round.
            _detail_log.log(_DETAIL_LEVEL, "模型目录轮询发生意外错误；继续后续轮询。", exc_info=True)
            return
        if self._generation_snapshot() != generation:
            _detail_log.log(_DETAIL_LEVEL, "模型目录代次 %d 的结果已在应用前丢弃。", generation)
            return
        try:
            result = apply_catalog(catalog)
        except ModelCatalogError:
            return  # already logged where it was raised
        except Exception:
            _detail_log.log(_DETAIL_LEVEL, "应用模型目录时发生意外错误；继续后续轮询。", exc_info=True)
            return
        if self._generation_snapshot() != generation:
            # The writes already happened — they are idempotent and the next
            # sync converges — but the result must not reach the registry or
            # the UI of a runtime that moved on (a logout, an account switch).
            _detail_log.log(_DETAIL_LEVEL, "模型目录代次 %d 的结果已在发布前丢弃。", generation)
            return
        self._publish(catalog, result)

    def _publish(self, catalog: ModelCatalog, result: CatalogSyncResult) -> None:
        with self._cv:
            registry = self._registry
            refresh_ui = self._refresh_ui
        if registry is None or refresh_ui is None:
            return
        try:
            registry.install_snapshot(catalog.items)
        except Exception:
            _detail_log.log(_DETAIL_LEVEL, "模型目录注册表安装失败；下次同步将重试。", exc_info=True)
            return
        try:
            refresh_ui(result)
        except Exception:
            _detail_log.log(_DETAIL_LEVEL, "模型目录同步回调失败；下次同步仍会执行。", exc_info=True)


_runtime: _SyncRuntime | None = None
_runtime_lock = threading.Lock()
#: The wiring the last :func:`start_sync_runtime` call carried, remembered even
#: when the start was refused (no source, no credential): a login then flips
#: the credential check and :func:`resume_sync_runtime` retries with it.
_last_wiring: tuple[ModelProfileRegistry, Callable[[CatalogSyncResult], None]] | None = None


def start_sync_runtime(
    registry: ModelProfileRegistry,
    refresh_ui: Callable[[CatalogSyncResult], None],
    *,
    immediate: bool = False,
    timeout: float = _DEFAULT_FETCH_TIMEOUT,
) -> bool:
    """Start (or re-wire) the process-wide catalog poll.

    *registry* receives each applied catalog via ``install_snapshot`` (same
    items that were written to disk — one version on both sides), and
    *refresh_ui* runs on the sync thread after each install and must be
    thread-safe (the TUI passes a ``call_from_thread`` wrapper).

    Returns ``False`` when there is nothing to poll: no catalog source, or no
    credential — a request without one is refused before it is sent, so the
    loop would do nothing but log. The wiring is still remembered, so
    :func:`resume_sync_runtime` can retry once a login supplies the token.

    Already running, the call is idempotent: the wiring is refreshed, and
    *immediate* pulls one sync now (:func:`sync_now`) — a login changes whose
    models are served and that must land now rather than at the next tick.

    The thread sleeps first unless *immediate*: the startup path already ran
    its blocking sync, so a fresh start does not fetch twice.
    """
    global _runtime, _last_wiring
    _last_wiring = (registry, refresh_ui)
    if not catalog_url():
        _detail_log.log(_DETAIL_LEVEL, "模型目录同步未启动：未配置目录来源。")
        return False
    if not has_catalog_credential():
        _detail_log.log(_DETAIL_LEVEL, "模型目录同步未启动：暂无凭证，登录后将启动。")
        return False

    with _runtime_lock:
        runtime = _runtime
        if runtime is not None and runtime.is_running():
            runtime.update_refs(registry, refresh_ui)
            if immediate:
                runtime.sync_now()
            return True
        runtime = _SyncRuntime(registry, refresh_ui, timeout=timeout)
        _runtime = runtime
    runtime.start(immediate=immediate)
    return True


def resume_sync_runtime(*, immediate: bool = True) -> bool:
    """Re-arm the poll after a login, with the wiring the startup path left.

    A startup without a credential cannot start the poll, but it remembered
    the wiring it would have used; a login supplies the credential and this
    retries the start with it. Already running, this is the idempotent start
    semantics: *immediate* (the default) pulls one sync now, because a login
    changes whose models are served and that must not wait out an interval
    that began before it.

    Returns ``False`` when no wiring was ever left (this process never wired
    a registry — nothing catalog-syncable to resume) or when the retry is
    still refused; both are logged, never raised.
    """
    with _runtime_lock:
        runtime = _runtime
        if runtime is not None and runtime.is_running():
            if immediate:
                runtime.sync_now()
            return True
        wiring = _last_wiring
    if wiring is None:
        _detail_log.log(_DETAIL_LEVEL, "跳过恢复模型目录同步：启动阶段未留下接线信息。")
        return False
    return start_sync_runtime(wiring[0], wiring[1], immediate=immediate)


def stop_sync_runtime() -> None:
    """Invalidate the poll: stop scheduling, drop in-flight results, clear wiring.

    A generation bump makes a sync that is already past its fetch vanish
    before it can publish; clearing the registry/UI references de-fangs a
    callback that slipped through anyway. The thread is a daemon and is never
    joined — the process exit is not held hostage by an in-flight request.
    """
    global _runtime
    with _runtime_lock:
        runtime = _runtime
        if runtime is None:
            return
        runtime.stop()
        _runtime = None


def sync_now() -> None:
    """Pull one catalog ahead of the clock on the running poll, if any."""
    with _runtime_lock:
        if _runtime is not None:
            _runtime.sync_now()
