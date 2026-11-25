# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import json
from pathlib import Path

EXTENSION = Path(__file__).resolve().parents[1] / "src" / "icode_browser_use" / "extension"


def test_manifest_grants_only_the_bridge_apis() -> None:
    manifest = json.loads((EXTENSION / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["manifest_version"] == 3
    assert manifest["minimum_chrome_version"] == "125"
    assert set(manifest["permissions"]) == {
        "debugger",
        "tabs",
        "tabGroups",
        "nativeMessaging",
        "alarms",
        "storage",
        "clipboardWrite",
        "webNavigation",
        "declarativeNetRequest",
    }
    assert manifest["action"]["default_popup"] == "popup.html"
    assert "host_permissions" not in manifest
    assert manifest["options_ui"] == {"page": "options.html", "open_in_tab": True}
    assert manifest["background"] == {"service_worker": "background.js", "type": "module"}


def test_background_uses_real_tabs_and_refuses_browser_close() -> None:
    source = (EXTENSION / "background.js").read_text(encoding="utf-8")
    for needle in (
        "connectNative",
        "chrome.debugger.attach",
        "chrome.tabs.group",
        "chrome.debugger.onEvent",
        "methodPolicy",
        "is not allowed in bridge mode",
        "webNavigation",
        "Bridge.siteBlocked",
        "Inspector.detached",
        "releaseOwnedTabs",
        "isGatedPageEvent",
        "handlePortDisconnect",
        "requireOwned",
        "chrome.runtime.onStartup",
        "chrome.runtime.onInstalled",
        "chrome.alarms.create",
        "chrome.tabs.onRemoved",
        "OWNED_TABS_KEY",
        "BROWSER_SESSION_KEY",
        "chrome.storage.local",
        "chrome.storage.session",
        "clearBridgePolicy",
        "bridgeAllowedDomains",
        "rememberOwned",
        "forgetOwned",
        "enqueueOwnedWrite",
        "assertSessionPermission",
        "child session origin unknown",
        "Target.targetInfoChanged",
        "restoreOwnedTabs",
        "Target.attachedToTarget",
        "debuggeeFor",
        "flatten: true",
        "Runtime.runIfWaitingForDebugger",
        "assertPermission",
        "permissionError",
        "needsPermission",
        "Page.navigate",
        "BridgeChunkReceiver",
        "encodeBridgeMessages",
        "Bridge.ping",
        'capabilities: ["ping", "page_log_gated", "tab_state"]',
        "Bridge.tabState",
        "Target.activateTarget",
        "bridgeLeaseId",
        "bridge_lease_mismatch",
        "postBridge",
        "sourcePort",
    ):
        assert needle in source
    assert "chrome.tabGroups.query" not in source
    assert "eval(" not in source


def test_popup_lists_tabs_and_saves_permissions() -> None:
    for name in ("popup.html", "popup.js", "popup.css", "permissions.js"):
        assert (EXTENSION / name).is_file(), name
    source = (EXTENSION / "popup.js").read_text(encoding="utf-8")
    assert "chrome.tabs.query" in source
    assert "setPermission" in source
    assert "originOf" in source
    assert "openOptionsPage" in source


def test_options_page_manages_default_history_and_import_export() -> None:
    for name in ("options.html", "options.js", "options.css"):
        assert (EXTENSION / name).is_file(), name
    source = (EXTENSION / "options.js").read_text(encoding="utf-8")
    for needle in (
        "readDefaultPolicy",
        "setDefaultPolicy",
        "readHistory",
        "exportPermissions",
        "importPermissions",
        "chrome.storage.local.set",
    ):
        assert needle in source
    html = (EXTENSION / "options.html").read_text(encoding="utf-8")
    for needle in ('id="default-policy"', 'id="export"', 'id="import"', 'id="history"'):
        assert needle in html


def test_onboarding_opens_on_install_and_reports_bridge_status() -> None:
    for name in ("onboarding.html", "onboarding.js", "onboarding.css"):
        assert (EXTENSION / name).is_file(), name
    background = (EXTENSION / "background.js").read_text(encoding="utf-8")
    for needle in (
        'details.reason === "install"',
        'chrome.runtime.getURL("onboarding.html")',
        '"bridge_status"',
        '"Bridge.ready"',
        "bridgeReady",
    ):
        assert needle in background
    page = (EXTENSION / "onboarding.js").read_text(encoding="utf-8")
    assert "chrome.runtime.sendMessage" in page
    assert "openOptionsPage" in page
    assert "navigator.clipboard.writeText" in page
    assert "readPermissions" in page
    html = (EXTENSION / "onboarding.html").read_text(encoding="utf-8")
    for needle in ('id="sample-prompt"', 'id="check-bridge"', 'id="check-site"', 'id="check-copy"'):
        assert needle in html
