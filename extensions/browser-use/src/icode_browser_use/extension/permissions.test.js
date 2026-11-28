// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import test from "node:test";
import assert from "node:assert/strict";

import {
  DEFAULT_PERMISSION,
  assertPermission,
  domainMatchesHost,
  exportPermissions,
  importPermissions,
  isNonPublicHost,
  methodPolicy,
  needsPermission,
  originOf,
  permissionFor,
  readDefaultPolicy,
  readHistory,
  readUserAllowsPublicWeb,
  setBridgePublicWeb,
  setDefaultPolicy,
  setPermission,
  setUserAllowsPublicWeb,
} from "./permissions.js";

function storage(initial = {}, options = {}) {
  const data = {
    sitePermissions: { ...initial },
    defaultPermission: options.defaultPermission || "ask",
    userAllowsPublicWeb: options.userAllowsPublicWeb === true,
    bridgePublicWeb: options.bridgePublicWeb === true,
    permissionHistory: [...(options.history || [])],
    bridgeAllowedDomains: [...(options.bridgeDomains || [])],
    bridgeAllowedOrigins: [...(options.bridgeOrigins || [])],
  };
  return {
    async get() {
      return {
        sitePermissions: { ...data.sitePermissions },
        defaultPermission: data.defaultPermission,
        userAllowsPublicWeb: data.userAllowsPublicWeb,
        bridgePublicWeb: data.bridgePublicWeb,
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
        userAllowsPublicWeb: data.userAllowsPublicWeb,
        bridgePublicWeb: data.bridgePublicWeb,
        permissionHistory: [...data.permissionHistory],
        bridgeAllowedDomains: [...data.bridgeAllowedDomains],
        bridgeAllowedOrigins: [...data.bridgeAllowedOrigins],
      };
    },
  };
}

test("originOf keeps only http(s) origins", () => {
  assert.equal(originOf("https://example.com/a?b=1"), "https://example.com");
  assert.equal(originOf("https://example.com./a"), "https://example.com");
  assert.equal(originOf("https://example.com../a"), "");
  assert.equal(originOf("https://example.com:443/a"), "https://example.com");
  assert.equal(originOf("http://example.com:80/a"), "http://example.com");
  assert.equal(originOf("http://example.com:8080/a"), "http://example.com:8080");
  assert.equal(originOf("about:blank"), "");
  assert.equal(originOf("chrome-extension://abc/x"), "");
  assert.equal(originOf("not a url"), "");
});

test("trailing-dot FQDN cannot bypass explicit block or ask overrides", async () => {
  const store = storage(
    { "https://bank.example": "block", "https://ask.example": "ask" },
    { defaultPermission: "allow", userAllowsPublicWeb: true, bridgePublicWeb: true }
  );
  assert.equal(await permissionFor("https://bank.example./login", store), "block");
  assert.equal(await permissionFor("https://ask.example./", store), "ask");
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

test("explicit ask persists and overrides publicWeb and default allow", async () => {
  const store = storage({}, { defaultPermission: "allow", userAllowsPublicWeb: true });
  setBridgePublicWeb(true);
  try {
    assert.equal(await permissionFor("https://example.com", store), "allow");
    await setPermission("https://example.com", "ask", store);
    assert.equal(store.snapshot().sitePermissions["https://example.com"], "ask");
    assert.equal(await permissionFor("https://example.com", store), "ask");
  } finally {
    setBridgePublicWeb(false);
  }
});

test("default policy can allow unknown public origins but not private/loopback", async () => {
  const store = storage({}, { defaultPermission: "allow" });
  assert.equal(await readDefaultPolicy(store), "allow");
  assert.equal(await permissionFor("https://unknown.example", store), "allow");
  assert.equal(await permissionFor("http://127.0.0.1:8080", store), "ask");
  assert.equal(await permissionFor("http://169.254.169.254/latest", store), "ask");
  await setDefaultPolicy("ask", store);
  assert.equal(await permissionFor("https://unknown.example", store), "ask");
});

test("publicWeb requires userAllowsPublicWeb opt-in and excludes private/loopback hosts", async () => {
  const defaultStore = storage();
  assert.equal(await readUserAllowsPublicWeb(defaultStore), false);
  setBridgePublicWeb(true);
  try {
    // Plugin sending publicWeb=true alone must NOT grant permission when user opt-in is false.
    assert.equal(await permissionFor("https://example.org/", defaultStore), "ask");

    await setUserAllowsPublicWeb(true, defaultStore);
    assert.equal(await readUserAllowsPublicWeb(defaultStore), true);
    assert.equal(await permissionFor("https://example.org/", defaultStore), "allow");
    for (const nonPublic of [
      "http://localhost:3000/",
      "http://app.localhost:3000/",
      "http://127.0.0.1:8080/",
      "http://[::1]:8080/",
      "http://10.0.0.1/",
      "http://172.16.0.1/",
      "http://192.168.1.1/",
      "http://169.254.169.254/latest/meta-data/",
      "http://[fd00::1]/",
      "http://[fe80::1]/",
      "http://printer.local/",
    ]) {
      assert.equal(await permissionFor(nonPublic, defaultStore), "ask", nonPublic);
    }
  } finally {
    setBridgePublicWeb(false);
  }
});

test("plugin allowlist narrows user grants and cannot auto-allow unapproved sites", async () => {
  // Plugin alone cannot grant without user allow
  const unapproved = storage({}, { bridgeDomains: ["weibo.com"] });
  assert.equal(await permissionFor("https://s.weibo.com/top/summary", unapproved), "ask");

  // Intersection: user allowed both weibo and other, plugin narrows to weibo
  const narrowed = storage(
    {
      "https://s.weibo.com": "allow",
      "https://weibo.com": "allow",
      "https://other.example": "allow",
    },
    { bridgeDomains: ["weibo.com"] }
  );
  assert.equal(await permissionFor("https://s.weibo.com/top/summary", narrowed), "allow");
  assert.equal(await permissionFor("https://weibo.com/hot", narrowed), "allow");
  assert.equal(await permissionFor("https://other.example/", narrowed), "ask");

  // Explicit block always wins
  const blocked = storage({ "https://s.weibo.com": "block" }, { bridgeDomains: ["weibo.com"] });
  assert.equal(await permissionFor("https://s.weibo.com/top/summary", blocked), "block");
});

test("leading-dot domain rule matches subdomains only (aligned with Python)", async () => {
  assert.equal(domainMatchesHost("sub.example.com", ".example.com"), true);
  assert.equal(domainMatchesHost("example.com", ".example.com"), false);
  assert.equal(domainMatchesHost("example.com", "example.com"), true);
  assert.equal(domainMatchesHost("sub.example.com", "example.com"), true);
  assert.equal(domainMatchesHost("notexample.com", "example.com"), false);

  const store = storage(
    { "https://example.com": "allow", "https://api.example.com": "allow" },
    { bridgeDomains: [".example.com"] }
  );
  assert.equal(await permissionFor("https://api.example.com/v1", store), "allow");
  assert.equal(await permissionFor("https://example.com/", store), "ask");
});

test("an authorised local origin narrows to that exact origin (normalising default ports)", async () => {
  const store = storage(
    {
      "http://localhost:3000": "allow",
      "http://localhost": "allow",
      "http://localhost:3001": "allow",
    },
    { bridgeOrigins: ["http://localhost:3000", "http://localhost:80"] }
  );
  assert.equal(await permissionFor("http://localhost:3000/index.html", store), "allow");
  assert.equal(await permissionFor("http://localhost/index.html", store), "allow");
  assert.equal(await permissionFor("http://localhost:3001/index.html", store), "ask");
  assert.equal(await permissionFor("https://localhost:3000/index.html", store), "ask");
  assert.equal(await permissionFor("http://127.0.0.1:3000/index.html", store), "ask");
});

test("isNonPublicHost identifies private, loopback, link-local, ULA, NAT64, benchmark and local hosts", () => {
  for (const h of [
    "localhost",
    "a.localhost",
    "127.0.0.1",
    "10.1.2.3",
    "172.31.0.1",
    "192.168.0.1",
    "169.254.169.254",
    "198.18.0.1",
    "198.19.255.254",
    "192.0.0.1",
    "192.0.2.1",
    "198.51.100.1",
    "203.0.113.1",
    "::1",
    "::7f00:1",
    "[::7f00:1]",
    "64:ff9b::7f00:1",
    "[64:ff9b::7f00:1]",
    "fd12::1",
    "fe80::1",
    "box.local",
    "intranet",
  ]) {
    assert.equal(isNonPublicHost(h), true, h);
  }
  for (const h of ["example.com", "1.1.1.1", "93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"]) {
    assert.equal(isNonPublicHost(h), false, h);
  }
});

test("popup context reads persisted bridgePublicWeb from storage when module flag is false", async () => {
  setBridgePublicWeb(false);
  const store = storage({}, { userAllowsPublicWeb: true, bridgePublicWeb: true });
  assert.equal(await permissionFor("https://example.org/", store), "allow");
  assert.equal(await permissionFor("http://[::7f00:1]:8080/", store), "ask");
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

test("export and import roundtrip permissions, default, publicWeb, and history", async () => {
  const source = storage(
    { "https://example.com": "allow", "https://ask.example": "ask" },
    { defaultPermission: "allow", userAllowsPublicWeb: true }
  );
  await setPermission("https://blocked.example", "block", source);
  const exported = await exportPermissions(source);
  assert.equal(exported.version, 1);
  assert.equal(exported.defaultPermission, "allow");
  assert.equal(exported.userAllowsPublicWeb, true);

  const target = storage();
  const count = await importPermissions(exported, target);
  assert.equal(count, 3);
  assert.equal(await permissionFor("https://example.com/x", target), "allow");
  assert.equal(await permissionFor("https://ask.example/x", target), "ask");
  assert.equal(await permissionFor("https://blocked.example/x", target), "block");
  assert.equal(await readDefaultPolicy(target), "allow");
  assert.equal(await readUserAllowsPublicWeb(target), true);
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

test("strict CDP method allowlist rejects profile-wide and dangerous methods", () => {
  for (const forbidden of [
    "Storage.getCookies",
    "Storage.setCookies",
    "Network.getAllCookies",
    "Network.setCookie",
    "Network.setExtraHTTPHeaders",
    "Fetch.continueResponse",
    "Fetch.getResponseBody",
    "Target.getTargets",
    "Target.sendMessageToTarget",
    "Target.exposeDevToolsProtocol",
    "Browser.grantPermissions",
    "Debugger.enable",
    "IO.read",
    "Extensions.loadUnpacked",
    "UnknownFuture.method",
  ]) {
    assert.equal(methodPolicy(forbidden), null, forbidden);
    assert.equal(needsPermission(forbidden), true, forbidden);
  }
  for (const siteScoped of ["Network.getResponseBody", "Input.setInterceptDrags", "Input.dispatchDragEvent"]) {
    assert.equal(methodPolicy(siteScoped), "site", siteScoped);
    assert.equal(needsPermission(siteScoped), true, siteScoped);
  }
  for (const plumbing of [
    "Fetch.enable",
    "Fetch.continueRequest",
    "Fetch.failRequest",
    "Fetch.disable",
    "Target.setAutoAttach",
    "Target.detachFromTarget",
    "Runtime.enable",
    "Runtime.runIfWaitingForDebugger",
    "Runtime.releaseObjectGroup",
    "DOM.enable",
    "Accessibility.enable",
    "Page.enable",
    "Network.enable",
    "Network.setBlockedURLs",
  ]) {
    assert.equal(needsPermission(plumbing), false, plumbing);
  }
});
