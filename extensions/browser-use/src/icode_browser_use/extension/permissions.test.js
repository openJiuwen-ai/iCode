// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import test from "node:test";
import assert from "node:assert/strict";

import {
  DEFAULT_PERMISSION,
  assertPermission,
  exportPermissions,
  importPermissions,
  needsPermission,
  originOf,
  permissionFor,
  readDefaultPolicy,
  readHistory,
  setDefaultPolicy,
  setPermission,
} from "./permissions.js";

function storage(initial = {}, options = {}) {
  const data = {
    sitePermissions: { ...initial },
    defaultPermission: options.defaultPermission || "ask",
    permissionHistory: [...(options.history || [])],
    bridgeAllowedDomains: [...(options.bridgeDomains || [])],
    bridgeAllowedOrigins: [...(options.bridgeOrigins || [])],
  };
  return {
    async get() {
      return {
        sitePermissions: { ...data.sitePermissions },
        defaultPermission: data.defaultPermission,
        permissionHistory: [...data.permissionHistory],
        bridgeAllowedDomains: [...data.bridgeAllowedDomains],
        bridgeAllowedOrigins: [...data.bridgeAllowedOrigins],
      };
    },
    async set(update) {
      Object.assign(data, update);
    },
    snapshot() {
      return {
        sitePermissions: { ...data.sitePermissions },
        defaultPermission: data.defaultPermission,
        permissionHistory: [...data.permissionHistory],
        bridgeAllowedDomains: [...data.bridgeAllowedDomains],
        bridgeAllowedOrigins: [...data.bridgeAllowedOrigins],
      };
    },
  };
}

test("originOf keeps only http(s) origins", () => {
  assert.equal(originOf("https://example.com/a?b=1"), "https://example.com");
  assert.equal(originOf("http://example.com:8080/a"), "http://example.com:8080");
  assert.equal(originOf("about:blank"), "");
  assert.equal(originOf("chrome-extension://abc/x"), "");
  assert.equal(originOf("not a url"), "");
});

test("unknown origins default to ask", async () => {
  assert.equal(DEFAULT_PERMISSION, "ask");
  assert.equal(await permissionFor("https://example.com/x", storage()), "ask");
});

test("allow and block persist per origin", async () => {
  const store = storage();
  await setPermission("https://example.com", "allow", store);
  await setPermission("https://blocked.example", "block", store);
  assert.equal(await permissionFor("https://example.com/x", store), "allow");
  assert.equal(await permissionFor("https://blocked.example/x", store), "block");
  assert.equal(await permissionFor("https://other.example/x", store), "ask");
});

test("ask clears the stored override", async () => {
  const store = storage();
  await setPermission("https://example.com", "allow", store);
  await setPermission("https://example.com", "ask", store);
  assert.deepEqual(store.snapshot().sitePermissions, {});
  assert.equal(await permissionFor("https://example.com", store), "ask");
});

test("default policy can allow unknown origins", async () => {
  const store = storage({}, { defaultPermission: "allow" });
  assert.equal(await readDefaultPolicy(store), "allow");
  assert.equal(await permissionFor("https://unknown.example", store), "allow");
  await setDefaultPolicy("ask", store);
  assert.equal(await permissionFor("https://unknown.example", store), "ask");
});

test("bridge allowlist covers subdomains but explicit blocks win", async () => {
  const allowed = storage({}, { bridgeDomains: ["weibo.com"] });
  assert.equal(await permissionFor("https://s.weibo.com/top/summary", allowed), "allow");
  assert.equal(await permissionFor("https://weibo.com/hot", allowed), "allow");
  assert.equal(await permissionFor("https://notweibo.com", allowed), "ask");

  const blocked = storage({ "https://s.weibo.com": "block" }, { bridgeDomains: ["weibo.com"] });
  assert.equal(await permissionFor("https://s.weibo.com/top/summary", blocked), "block");
});

test("an authorised local origin is allowed for that exact origin only", async () => {
  const store = storage({}, { bridgeOrigins: ["http://localhost:3000"] });
  assert.equal(await permissionFor("http://localhost:3000/index.html", store), "allow");
  assert.equal(await permissionFor("http://localhost:3001/index.html", store), "ask");
  assert.equal(await permissionFor("https://localhost:3000/index.html", store), "ask");
  assert.equal(await permissionFor("http://127.0.0.1:3000/index.html", store), "ask");

  const blocked = storage({ "http://localhost:3000": "block" }, { bridgeOrigins: ["http://localhost:3000"] });
  assert.equal(await permissionFor("http://localhost:3000/index.html", blocked), "block");
});

test("setPermission records capped history", async () => {
  const store = storage();
  await setPermission("https://example.com", "allow", store);
  await setPermission("https://example.com", "block", store);
  const history = await readHistory(store);
  assert.equal(history.length, 2);
  assert.equal(history[0].state, "block");
  assert.equal(history[1].state, "allow");
  assert.match(history[0].at, /^\d{4}-\d{2}-\d{2}T/);
});

test("export and import roundtrip permissions, default and history", async () => {
  const source = storage({ "https://example.com": "allow" }, { defaultPermission: "allow" });
  await setPermission("https://blocked.example", "block", source);
  const exported = await exportPermissions(source);
  assert.equal(exported.version, 1);
  assert.equal(exported.defaultPermission, "allow");

  const target = storage();
  const count = await importPermissions(exported, target);
  assert.equal(count, 2);
  assert.equal(await permissionFor("https://example.com/x", target), "allow");
  assert.equal(await permissionFor("https://blocked.example/x", target), "block");
  assert.equal(await readDefaultPolicy(target), "allow");
  assert.equal((await readHistory(target)).length, 1);
});

test("import rejects malformed exports", async () => {
  await assert.rejects(importPermissions(null, storage()), /unsupported/);
  await assert.rejects(
    importPermissions({ version: 1, sitePermissions: { "https://example.com": "maybe" } }, storage()),
    /invalid state/
  );
  await assert.rejects(
    importPermissions({ version: 1, sitePermissions: { "example.com": "allow" } }, storage()),
    /invalid origin/
  );
});

test("assertPermission permits blank/srcdoc and allowed origins, blocks non-http schemes", async () => {
  const store = storage({ "https://example.com": "allow" });
  await assertPermission("about:blank", store);
  await assertPermission("about:srcdoc", store);
  await assertPermission("https://example.com/x", store);
  for (const bad of [
    "file:///etc/passwd",
    "javascript:alert(1)",
    "data:text/html,<h1>hi</h1>",
    "blob:https://example.com/123",
    "chrome-extension://abc/x",
  ]) {
    await assert.rejects(assertPermission(bad, store), /site_blocked/);
  }
});

test("assertPermission fails closed with typed messages", async () => {
  const store = storage({ "https://blocked.example": "block" });
  await assert.rejects(assertPermission("https://ask.example/x", store), /site_permission_required/);
  await assert.rejects(assertPermission("https://blocked.example/x", store), /site_blocked/);
});

test("needsPermission covers tool actions and sensitive CDP methods but not policy plumbing", () => {
  for (const method of [
    "DOMSnapshot.captureSnapshot",
    "DOM.getContentQuads",
    "Accessibility.getFullAXTree",
    "Runtime.evaluate",
    "Runtime.callFunctionOn",
    "Input.dispatchMouseEvent",
    "Input.insertText",
    "Storage.getCookies",
    "Network.getAllCookies",
    "Page.addScriptToEvaluateOnNewDocument",
    "UnknownFuture.method",
    "Page.navigate",
    "Page.getFrameTree",
    "Page.captureScreenshot",
    "Emulation.setDeviceMetricsOverride",
    "Fetch.getResponseBody",
    "Target.getTargets",
    "Target.setDiscoverTargets",
    "Browser.setDownloadBehavior",
    "Browser.grantPermissions",
  ]) {
    assert.equal(needsPermission(method), true, method);
  }
  for (const method of [
    "Fetch.enable",
    "Fetch.continueRequest",
    "Fetch.failRequest",
    "Network.enable",
    "Network.setBlockedURLs",
    "Target.setAutoAttach",
    "Target.detachFromTarget",
    "Runtime.enable",
    "Runtime.runIfWaitingForDebugger",
    "DOM.enable",
    "Accessibility.enable",
    "Page.enable",
    "Page.setLifecycleEventsEnabled",
  ]) {
    assert.equal(needsPermission(method), false, method);
  }
});
