# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Loopback origin authorisation for local development (W02)."""

from __future__ import annotations

import pytest

from icode_browser_use.config import BrowserConfig, ConfigError, parse_origins
from icode_browser_use.policy import (
    NetworkPolicy,
    canonical_origin,
    decide,
    is_loopback_ip,
    origin_allowed,
)

PUBLIC_IP = "93.184.216.34"
LOOPBACK_V4 = "127.0.0.1"
LOOPBACK_V6 = "::1"


def test_parse_origins_normalises_scheme_host_and_effective_port() -> None:
    accepted, rejected = parse_origins(
        "HTTP://LocalHost:3000, http://127.0.0.1:5173/, https://localhost, http://[::1]:8080"
    )
    assert accepted == (
        "http://localhost:3000",
        "http://127.0.0.1:5173",
        "https://localhost",
        "http://[::1]:8080",
    )
    assert rejected == []
    assert parse_origins("http://localhost") == (("http://localhost",), [])
    assert parse_origins("http://localhost:80") == (("http://localhost",), [])
    assert parse_origins("https://localhost:443") == (("https://localhost",), [])


@pytest.mark.parametrize(
    "entry",
    [
        "http://localhost:3000/app",
        "http://localhost:3000?x=1",
        "http://localhost:3000#frag",
        "http://user:pass@localhost:3000",
        "ftp://localhost:3000",
        "http://192.168.1.5:8080",
        "http://example.com:3000",
        "http://localhost:0",
        "http://localhost:70000",
        "http://",
        "localhost:3000",
    ],
)
def test_parse_origins_rejects_non_origin_or_non_loopback_entries(entry: str) -> None:
    accepted, rejected = parse_origins(entry)
    assert accepted == ()
    assert rejected == [entry]


def test_parse_origins_accepts_localhost_subdomains_and_deduplicates() -> None:
    accepted, rejected = parse_origins("http://app.localhost:3000,http://localhost:3000,http://localhost:3000")
    assert accepted == ("http://app.localhost:3000", "http://localhost:3000")
    assert rejected == []


def test_canonical_origin_keeps_effective_port_and_drops_path() -> None:
    assert canonical_origin("https://example.com/a/b?c=1#d") == "https://example.com"
    assert canonical_origin("https://example.com:443/a") == "https://example.com"
    assert canonical_origin("http://example.com:80/a") == "http://example.com"
    assert canonical_origin("http://localhost:3000/app?x=1") == "http://localhost:3000"
    assert canonical_origin("http://[::1]:8080/") == "http://[::1]:8080"
    assert canonical_origin("chrome-error://chromewebdata/") == ""
    assert canonical_origin("about:blank") == ""


def test_origin_allowed_matches_exact_origin_only() -> None:
    origins = ("http://localhost:3000",)
    assert origin_allowed(origins, "http://localhost:3000/page")
    assert not origin_allowed(origins, "http://localhost:3001/page")
    assert not origin_allowed(origins, "http://127.0.0.1:3000/page")
    assert not origin_allowed(origins, "https://localhost:3000/page")


def test_is_loopback_ip_accepts_loopback_ranges_only() -> None:
    assert is_loopback_ip(LOOPBACK_V4)
    assert is_loopback_ip(LOOPBACK_V6)
    assert is_loopback_ip("127.10.20.30")
    assert is_loopback_ip("[::1]")
    assert is_loopback_ip("::ffff:127.0.0.1")
    assert not is_loopback_ip("10.0.0.1")
    assert not is_loopback_ip(PUBLIC_IP)
    assert not is_loopback_ip("not-an-ip")


def test_decide_allows_authorised_loopback_origin_without_domain_entry() -> None:
    origins = ("http://localhost:3000",)
    assert decide(
        "http://localhost:3000/index.html",
        (),
        lambda _host: [LOOPBACK_V4],
        allowed_origins=origins,
    )


def test_decide_blocks_authorised_origin_that_resolves_off_loopback() -> None:
    origins = ("http://localhost:3000",)
    assert not decide("http://localhost:3000/x", (), lambda _: [PUBLIC_IP], allowed_origins=origins)
    assert not decide("http://localhost:3000/x", (), lambda _: [LOOPBACK_V4, PUBLIC_IP], allowed_origins=origins)
    assert not decide("http://localhost:3000/x", (), lambda _: [], allowed_origins=origins)


def test_decide_keeps_public_domains_off_private_addresses() -> None:
    assert decide("https://example.com/x", ("example.com",), lambda _: [PUBLIC_IP])
    assert not decide("https://example.com/x", ("example.com",), lambda _: [LOOPBACK_V4])


def test_unconfigured_loopback_origin_stays_blocked() -> None:
    assert not decide("http://localhost:3001/x", (), lambda _: [LOOPBACK_V4])


def test_network_policy_drops_loopback_backstop_patterns_only_when_origins_configured() -> None:
    plain = NetworkPolicy(("example.com",))
    assert "*://localhost*" in plain.blocked_patterns
    assert "*://127.*" in plain.blocked_patterns

    local = NetworkPolicy((), allowed_origins=("http://localhost:3000",))
    assert "*://localhost*" not in local.blocked_patterns
    assert "*://127.*" not in local.blocked_patterns
    # Private ranges stay a backstop: W02 authorises only loopback origins.
    assert "*://10.*" in local.blocked_patterns


def resolver(*addresses: str):
    async def resolve(_host: str) -> list[str]:
        return list(addresses)

    return resolve


def test_network_policy_evaluates_origin_and_domain_paths() -> None:
    async def scenario() -> None:
        policy = NetworkPolicy((), allowed_origins=("http://localhost:3000",), resolver=resolver(LOOPBACK_V4))
        assert await policy.evaluate("http://localhost:3000/app.js")
        assert not await policy.evaluate("http://localhost:3001/app.js")
        assert not await policy.evaluate("https://example.com/app.js")

        public = NetworkPolicy(("example.com",), resolver=resolver(PUBLIC_IP))
        assert await public.evaluate("https://example.com/app.js")
        assert not await public.evaluate("http://localhost:3000/app.js")

        # An authorised origin whose name resolves off loopback is refused.
        rebinding = NetworkPolicy((), allowed_origins=("http://localhost:3000",), resolver=resolver(PUBLIC_IP))
        assert not await rebinding.evaluate("http://localhost:3000/app.js")

    import asyncio

    asyncio.run(scenario())


def test_from_env_accepts_origins_without_public_domains() -> None:
    cfg = BrowserConfig.from_env({"ICODE_BROWSER_ALLOWED_ORIGINS": "http://localhost:3000"})
    assert cfg.allowed_origins == ("http://localhost:3000",)
    assert cfg.allowed_domains == ()


def test_from_env_reads_domains_and_origins_together() -> None:
    cfg = BrowserConfig.from_env(
        {"ICODE_BROWSER_ALLOWED_DOMAINS": "example.com", "ICODE_BROWSER_ALLOWED_ORIGINS": "http://127.0.0.1:5173"}
    )
    assert cfg.allowed_origins == ("http://127.0.0.1:5173",)
    assert cfg.allowed_domains == ("example.com",)


def test_from_env_still_requires_one_rule_when_nothing_valid() -> None:
    with pytest.raises(ConfigError):
        BrowserConfig.from_env({"ICODE_BROWSER_ALLOWED_ORIGINS": "http://192.168.1.5:8080"})
    with pytest.raises(ConfigError):
        BrowserConfig.from_env({"ICODE_BROWSER_ALLOWED_DOMAINS": "", "ICODE_BROWSER_ALLOWED_ORIGINS": "#bad"})


def test_invalid_origin_diagnostic_never_echoes_the_value(capsys) -> None:
    sentinel = "http://user:SECRET-TOKEN@localhost:3000"
    with pytest.raises(ConfigError):
        BrowserConfig.from_env({"ICODE_BROWSER_ALLOWED_ORIGINS": sentinel})
    captured = capsys.readouterr()
    assert "SECRET-TOKEN" not in captured.out + captured.err
