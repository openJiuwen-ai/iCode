// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import test from "node:test";
import assert from "node:assert/strict";

const OWNED_KEY = "chrysOwnedTabsV1";
const SESSION_KEY = "chrysBrowserSessionV1";
const groupCalls = [];
const detachedTabs = [];
const dnrSessionRules = [];
const dnrUpdates = [];
const localData = {
  [OWNED_KEY]: [{ tabId: 7, windowId: 1, createdAt: 1, sessionId: "old-session" }],
};
const sessionData = { [SESSION_KEY]: "current-session" };

function event() {
  return {
    listeners: [],
    addListener(fn) {
      this.listeners.push(fn);
    },
  };
}

function storage(data) {
  return {
    async get(defaults = {}) {
      return { ...defaults, ...data };
    },
    async set(values) {
      Object.assign(data, values);
    },
    async remove(key) {
      delete data[key];
    },
  };
}

const port = {
  messages: [],
  onMessage: event(),
  onDisconnect: event(),
  postMessage(message) {
    this.messages.push(message);
  },
  disconnect() {},
};

const MUTATION_PROBE = `
(() => {
  if (window.__chrys_probe_installed) { return; }
  window.__chrys_probe_installed = true;
  if (typeof window.__chrys_mutations !== "number") { window.__chrys_mutations = 0; }
  try {
    const observer = new MutationObserver((records) => {
      window.__chrys_mutations += records.length;
    });
    observer.observe(document, {
      childList: true,
      subtree: true,
      attributes: true,
      characterData: true,
    });
  } catch (error) {
    /* read-only probe: swallow observation errors, never touch the DOM */
  }
})();
`;

globalThis.chrome = {
  runtime: {
    id: "test-extension",
    lastError: undefined,
    getManifest: () => ({ version: "0.3.3" }),
    getURL: (path) => `chrome-extension://test-extension/${path}`,
    connectNative: () => port,
    onStartup: event(),
    onInstalled: event(),
    onMessage: event(),
  },
  debugger: {
    onEvent: event(),
    onDetach: event(),
    attach: async () => {},
    detach: async (target) => {
      detachedTabs.push(target.tabId);
    },
    sendCommand: async () => ({}),
  },
  webNavigation: {
    onCommitted: event(),
  },
  tabs: {
    onRemoved: event(),
    create: async ({ url }) => ({ id: 50, windowId: 1, url }),
    get: async (tabId) => ({ id: tabId, windowId: 1, url: "https://example.com/" }),
    update: async (tabId, props) => ({ id: tabId, ...props }),
    remove: async () => {},
    group: async (options) => {
      groupCalls.push(options);
      return 1;
    },
  },
  tabGroups: { update: async () => {} },
  storage: {
    local: storage(localData),
    session: storage(sessionData),
    onChanged: event(),
  },
  declarativeNetRequest: {
    async getSessionRules() {
      return [...dnrSessionRules];
    },
    async updateSessionRules({ removeRuleIds = [], addRules = [] } = {}) {
      dnrUpdates.push({ removeRuleIds, addRules });
      const removeSet = new Set(removeRuleIds);
      for (let i = dnrSessionRules.length - 1; i >= 0; i -= 1) {
        if (removeSet.has(dnrSessionRules[i].id)) dnrSessionRules.splice(i, 1);
      }
      dnrSessionRules.push(...addRules);
    },
  },
  alarms: { create() {}, onAlarm: event() },
};

function reconnect() {
  for (const listener of chrome.runtime.onStartup.listeners) listener();
}

test("restore ignores owned tabs from a previous browser session", async () => {
  await import(`./background.js?test=${Date.now()}`);
  await new Promise((resolve) => setTimeout(resolve, 50));
  assert.deepEqual(groupCalls, []);
  assert.deepEqual(localData[OWNED_KEY], []);
});

test("disconnect clears bridge allowlist and detaches owned tabs (S4/S5)", async () => {
  const { permissionFor } = await import("./permissions.js");
  const configure = port.onMessage.listeners[0];
  localData.userAllowsPublicWeb = true;
  configure({
    id: 41,
    method: "Bridge.configure",
    params: { allowedDomains: ["example.com"], allowedOrigins: ["http://localhost:3000"] },
    leaseId: "lease-a",
  });
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.deepEqual(localData.bridgeAllowedDomains, ["example.com"]);
  assert.deepEqual(localData.bridgeAllowedOrigins, ["http://localhost:3000"]);
  const url = "https://example.org/";
  configure({ id: 49, method: "Bridge.configure", params: { publicWeb: true }, leaseId: "lease-web" });
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.equal(await permissionFor(url), "allow");
  localData.sitePermissions = { "https://example.org": "block" };
  assert.equal(await permissionFor(url), "block");
  delete localData.sitePermissions;
  delete localData.userAllowsPublicWeb;
  for (const listener of port.onDisconnect.listeners) listener();
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.equal(localData.bridgeAllowedDomains, undefined);
  assert.equal(localData.bridgeAllowedOrigins, undefined);
  assert.equal(await permissionFor(url), "ask");
});

test("configure without origins clears a previously granted origin list", async () => {
  reconnect();
  await new Promise((resolve) => setTimeout(resolve, 20));
  const configure = port.onMessage.listeners[0];
  port.messages.length = 0;
  configure({
    id: 47,
    method: "Bridge.configure",
    params: { allowedDomains: ["example.com"], allowedOrigins: ["http://localhost:3000"] },
    leaseId: "lease-b",
  });
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.deepEqual(localData.bridgeAllowedOrigins, ["http://localhost:3000"]);
  port.messages.length = 0;
  configure({ id: 48, method: "Bridge.configure", params: { allowedDomains: ["example.com"] }, leaseId: "lease-c" });
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.deepEqual(localData.bridgeAllowedOrigins, []);
});

test("a missing or stale lease is rejected", async () => {
  const configure = port.onMessage.listeners[0];
  port.messages.length = 0;
  configure({ id: 46, method: "Bridge.configure", params: { allowedDomains: [] } });
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.match(JSON.stringify(port.messages), /bridge_lease_missing/);

  configure({ id: 42, method: "Bridge.configure", params: { allowedDomains: [] }, leaseId: "lease-a" });
  await new Promise((resolve) => setTimeout(resolve, 20));
  port.messages.length = 0;
  configure({ id: 43, method: "Bridge.ping", params: {}, leaseId: "lease-b" });
  configure({ id: 45, method: "Bridge.ping", params: {} });
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.match(port.messages.find((m) => m.id === 43).error, /bridge_lease_mismatch/);
  assert.match(port.messages.find((m) => m.id === 45).error, /bridge_lease_missing/);
});

test("a history step is checked against its destination, not the current page", async () => {
  const handle = port.onMessage.listeners[0];
  const sent = [];
  chrome.tabs.create = async ({ url }) => ({ id: 9, windowId: 1, url });
  chrome.debugger.sendCommand = async (target, method) => {
    sent.push(method);
    if (method === "Page.getNavigationHistory") {
      return {
        currentIndex: 0,
        entries: [
          { id: 1, url: "https://example.com/" },
          { id: 2, url: "https://blocked.example/" },
        ],
      };
    }
    return {};
  };
  localData.sitePermissions = { "https://example.com": "allow", "https://blocked.example": "block" };
  handle({ id: 60, method: "Bridge.configure", params: { allowedDomains: [] }, leaseId: "lease-h" });
  handle({ id: 61, method: "Target.createTarget", params: { url: "https://example.com/" }, leaseId: "lease-h" });
  await new Promise((resolve) => setTimeout(resolve, 30));

  port.messages.length = 0;
  sent.length = 0;
  handle({ id: 62, method: "Page.navigateToHistoryEntry", params: { entryId: 2 }, sessionId: "9", leaseId: "lease-h" });
  await new Promise((resolve) => setTimeout(resolve, 30));
  assert.match(JSON.stringify(port.messages), /site_blocked: https:\/\/blocked\.example/);
  assert.ok(!sent.includes("Page.navigateToHistoryEntry"), "the blocked destination must not be loaded");

  port.messages.length = 0;
  sent.length = 0;
  handle({ id: 63, method: "Page.navigateToHistoryEntry", params: { entryId: 1 }, sessionId: "9", leaseId: "lease-h" });
  await new Promise((resolve) => setTimeout(resolve, 30));
  assert.ok(sent.includes("Page.navigateToHistoryEntry"), JSON.stringify(port.messages));
});

test("S1: profile-wide CDP methods rejected on about:blank, cookies URL-checked, probe hash verified", async () => {
  const handle = port.onMessage.listeners[0];
  const sent = [];
  chrome.tabs.create = async ({ url }) => ({ id: 31, windowId: 1, url });
  chrome.tabs.get = async (tabId) => ({ id: tabId, windowId: 1, url: "about:blank" });
  chrome.debugger.sendCommand = async (_target, method, params) => {
    sent.push({ method, params });
    if (method === "Network.getCookies") {
      return {
        cookies: [
          { name: "ok", domain: "example.com" },
          { name: "leak", domain: "bank.example" },
        ],
      };
    }
    return {};
  };
  localData.sitePermissions = { "https://example.com": "allow", "https://blocked.example": "block" };
  handle({ id: 100, method: "Bridge.configure", params: { allowedDomains: [] }, leaseId: "lease-s1" });
  handle({ id: 101, method: "Target.createTarget", params: { url: "about:blank" }, leaseId: "lease-s1" });
  await new Promise((resolve) => setTimeout(resolve, 30));

  port.messages.length = 0;
  handle({ id: 102, method: "Storage.getCookies", params: {}, sessionId: "31", leaseId: "lease-s1" });
  handle({ id: 103, method: "Network.getAllCookies", params: {}, sessionId: "31", leaseId: "lease-s1" });
  handle({ id: 104, method: "Target.sendMessageToTarget", params: {}, sessionId: "31", leaseId: "lease-s1" });
  handle({ id: 105, method: "Network.getCookies", params: { urls: ["https://blocked.example/"] }, sessionId: "31", leaseId: "lease-s1" });
  handle({ id: 106, method: "Network.getCookies", params: {}, sessionId: "31", leaseId: "lease-s1" });
  handle({ id: 107, method: "Page.addScriptToEvaluateOnNewDocument", params: { source: "alert(1)" }, sessionId: "31", leaseId: "lease-s1" });
  handle({ id: 108, method: "Page.addScriptToEvaluateOnNewDocument", params: { source: MUTATION_PROBE }, sessionId: "31", leaseId: "lease-s1" });
  handle({ id: 109, method: "Network.getCookies", params: { urls: ["https://example.com/app"] }, sessionId: "31", leaseId: "lease-s1" });
  await new Promise((resolve) => setTimeout(resolve, 40));

  assert.match(port.messages.find((m) => m.id === 102).error, /not allowed in bridge mode/);
  assert.match(port.messages.find((m) => m.id === 103).error, /not allowed in bridge mode/);
  assert.match(port.messages.find((m) => m.id === 104).error, /not allowed in bridge mode/);
  assert.match(port.messages.find((m) => m.id === 105).error, /site_blocked/);
  assert.match(port.messages.find((m) => m.id === 106).error, /non-empty urls/);
  assert.match(port.messages.find((m) => m.id === 107).error, /restricted to the built-in mutation probe/);
  assert.equal(port.messages.find((m) => m.id === 108).error, undefined);
  const cookiesResp = port.messages.find((m) => m.id === 109).result.cookies;
  assert.deepEqual(cookiesResp, [{ name: "ok", domain: "example.com" }]);
});

test("S2: webNavigation.onCommitted detaches debugger and resets unapproved owned tab", async () => {
  const onCommitted = chrome.webNavigation.onCommitted.listeners[0];
  let updated = null;
  chrome.tabs.update = async (tabId, props) => {
    updated = { tabId, ...props };
    return { id: tabId, ...props };
  };
  detachedTabs.length = 0;
  port.messages.length = 0;
  localData.sitePermissions = { "https://example.com": "allow" };

  await onCommitted({ tabId: 31, frameId: 0, url: "https://unapproved.example/secret?tok=1" });
  assert.ok(detachedTabs.includes(31), "debugger must detach on blocked navigation");
  assert.deepEqual(updated, { tabId: 31, url: "about:blank" });
  const notice = port.messages.find((m) => m.method === "Bridge.siteBlocked");
  assert.deepEqual(notice.params, { tabId: "31", origin: "https://unapproved.example" });

  // Subframe (frameId !== 0) navigating to an unapproved third-party origin MUST NOT detach or reset the main tab
  detachedTabs.length = 0;
  updated = null;
  port.messages.length = 0;
  await onCommitted({ tabId: 31, frameId: 2, url: "https://ads.unapproved.example/widget" });
  assert.deepEqual(detachedTabs, [], "unapproved subframe must not detach the main tab");
  assert.equal(updated, null, "unapproved subframe must not reset the main tab");
  assert.equal(port.messages.find((m) => m.method === "Bridge.siteBlocked"), undefined);
});

test("S2 Layer 1 DNR rules scope to owned tabs and update on configure", async () => {
  const handle = port.onMessage.listeners[0];
  try {
    dnrUpdates.length = 0;
    localData.sitePermissions = { "https://example.com": "allow" };
    handle({
      id: 260,
      method: "Bridge.configure",
      params: { allowedDomains: [], publicWeb: true },
      leaseId: "lease-dnr",
    });
    await new Promise((resolve) => setTimeout(resolve, 30));
    assert.equal(localData.bridgePublicWeb, true, "bridgePublicWeb must persist to storage for popup");
    assert.ok(dnrUpdates.length > 0, "Bridge.configure must synchronize declarativeNetRequest rules");
    const blockRule = dnrSessionRules.find((r) => r.action && r.action.type === "block");
    assert.ok(blockRule, "default-deny DNR block rule must be installed for owned tabs");
    assert.ok(Array.isArray(blockRule.condition.tabIds) && blockRule.condition.tabIds.length > 0);
    assert.deepEqual(blockRule.condition.resourceTypes, ["main_frame", "sub_frame"], "iframes are blocked before the request too");
    const allowRule = dnrSessionRules.find((r) => r.action && r.action.type === "allow");
    assert.equal(allowRule.condition.urlFilter, "|https://example.com/", "origin filter must be anchored, not a prefix");

    // Plugin narrowing + defaultPermission=allow: permissionFor allows listed domains, so must DNR.
    localData.defaultPermission = "allow";
    handle({ id: 263, method: "Bridge.configure", params: { allowedDomains: [".docs.example"] }, leaseId: "lease-dnr" });
    await new Promise((resolve) => setTimeout(resolve, 30));
    const domainRule = dnrSessionRules.find((r) => r.condition && r.condition.requestDomains);
    assert.deepEqual(domainRule && domainRule.condition.requestDomains, ["docs.example"]);
    delete localData.defaultPermission;

    // Disconnect clears persisted bridgePublicWeb and releases the tabs to the user.
    for (const listener of port.onDisconnect.listeners) listener();
    await new Promise((resolve) => setTimeout(resolve, 30));
    assert.equal(localData.bridgePublicWeb, undefined, "disconnect must clear persisted bridgePublicWeb");
    assert.deepEqual(dnrSessionRules, [], "no DNR rules may stay on released tabs");
    detachedTabs.length = 0;
    let updated = null;
    chrome.tabs.update = async (tabId, props) => {
      updated = { tabId, ...props };
    };
    await chrome.webNavigation.onCommitted.listeners[0]({ tabId: 31, frameId: 0, url: "https://unapproved.example/" });
    assert.deepEqual(detachedTabs, [], "a released tab must not be detached");
    assert.equal(updated, null, "a released tab must not be reset to about:blank");
  } finally {
    reconnect();
    await new Promise((resolve) => setTimeout(resolve, 20));
    handle({ id: 261, method: "Bridge.configure", params: { allowedDomains: [] }, leaseId: "lease-s1" });
    await new Promise((resolve) => setTimeout(resolve, 20));
    chrome.tabs.create = async ({ url }) => ({ id: 31, windowId: 1, url });
    handle({ id: 262, method: "Target.createTarget", params: { url: "about:blank" }, leaseId: "lease-s1" });
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
});

test("S3: Fetch.enable/continueRequest tightened, requestPaused & dialog & tabState redacted, pendingUrl checked", async () => {
  const handle = port.onMessage.listeners[0];
  const onEvent = chrome.debugger.onEvent.listeners[0];
  const sent = [];
  chrome.debugger.sendCommand = async (_target, method, params) => {
    sent.push({ method, params });
    return {};
  };
  localData.sitePermissions = { "https://example.com": "allow" };

  port.messages.length = 0;
  handle({
    id: 201,
    method: "Fetch.enable",
    params: { patterns: [{ urlPattern: "*", requestStage: "Response" }] },
    sessionId: "31",
    leaseId: "lease-s1",
  });
  handle({
    id: 202,
    method: "Fetch.continueRequest",
    params: { requestId: "req-1", url: "https://evil.example", headers: [{ name: "X-Evil", value: "1" }] },
    sessionId: "31",
    leaseId: "lease-s1",
  });
  await new Promise((resolve) => setTimeout(resolve, 30));
  assert.match(port.messages.find((m) => m.id === 201).error, /requestStage=Response is not allowed/);
  const contCall = sent.find((c) => c.method === "Fetch.continueRequest");
  assert.deepEqual(contCall.params, { requestId: "req-1" });

  // Fetch.requestPaused on unapproved origin strips headers/postData/query
  port.messages.length = 0;
  onEvent(
    { tabId: 31 },
    "Fetch.requestPaused",
    {
      requestId: "r2",
      resourceType: "Document",
      frameId: "F1",
      request: {
        url: "https://blocked.example/path?secret=123",
        headers: { Authorization: "Bearer xyz" },
        postData: "password=abc",
      },
    }
  );
  onEvent(
    { tabId: 31 },
    "Page.javascriptDialogOpening",
    { type: "alert", url: "https://blocked.example/secret", message: "TOP SECRET", defaultPrompt: "pw" }
  );
  await new Promise((resolve) => setTimeout(resolve, 30));
  const paused = port.messages.find((m) => m.method === "Fetch.requestPaused");
  assert.deepEqual(paused.params, {
    requestId: "r2",
    request: { url: "https://blocked.example/path" },
    resourceType: "Document",
    frameId: "F1",
  });
  const dlg = port.messages.find((m) => m.method === "Page.javascriptDialogOpening");
  assert.equal(dlg.params.message, "");
  assert.equal(dlg.params.defaultPrompt, "");
  assert.equal(dlg.params.url, "https://blocked.example");

  // Bridge.tabState redacts unapproved tab url/title; assertTabPermission checks pendingUrl
  chrome.tabs.get = async (tabId) => ({
    id: tabId,
    windowId: 1,
    active: false,
    url: "https://blocked.example/private?token=1",
    title: "Secret Title",
    pendingUrl: "https://blocked.example/next",
  });
  port.messages.length = 0;
  handle({ id: 203, method: "Bridge.tabState", params: { targetId: "31" }, leaseId: "lease-s1" });
  await new Promise((resolve) => setTimeout(resolve, 20));
  const stateMsg = port.messages.find((m) => m.id === 203);
  assert.equal(stateMsg.result.url, "https://blocked.example");
  assert.equal(stateMsg.result.title, "");

  chrome.tabs.get = async (tabId) => ({
    id: tabId,
    windowId: 1,
    url: "https://example.com/",
    pendingUrl: "https://unapproved.example/",
  });
  port.messages.length = 0;
  handle({ id: 204, method: "Runtime.evaluate", params: { expression: "1" }, sessionId: "31", leaseId: "lease-s1" });
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.match(port.messages.find((m) => m.id === 204).error, /site_permission_required/);
});

test("S5: chrome.debugger.onDetach emits Inspector.detached for user cancel but ignores target_closed", async () => {
  const onDetach = chrome.debugger.onDetach.listeners[0];
  port.messages.length = 0;
  onDetach({ tabId: 31 }, "target_closed");
  assert.equal(
    port.messages.find((m) => m.method === "Inspector.detached"),
    undefined,
    "target_closed must not emit Inspector.detached"
  );
  onDetach({ tabId: 31 }, "canceled_by_user");
  const msg = port.messages.find((m) => m.method === "Inspector.detached");
  assert.deepEqual(msg, { method: "Inspector.detached", params: { reason: "canceled_by_user" }, sessionId: "31" });
});

test("page logs of a site the user did not allow are not forwarded", async () => {
  const emit = chrome.debugger.onEvent.listeners[0];
  chrome.tabs.get = async (tabId) => ({
    id: tabId,
    windowId: 1,
    url: tabId === 10 ? "https://blocked.example/secret" : "https://example.com/",
  });
  localData.sitePermissions = { "https://example.com": "allow", "https://blocked.example": "block" };
  const methods = () => port.messages.map((message) => JSON.stringify(message));

  port.messages.length = 0;
  emit({ tabId: 10 }, "Runtime.consoleAPICalled", { type: "log", args: [{ type: "string", value: "SECRET" }] });
  emit({ tabId: 10 }, "Network.loadingFailed", { requestId: "1", errorText: "x" });
  emit({ tabId: 10 }, "Network.responseReceived", {
    requestId: "2", type: "XHR", response: { status: 200, url: "https://blocked.example/api" },
  });
  emit({ tabId: 10 }, "Network.responseReceived", {
    requestId: "3", type: "Document", frameId: "F", loaderId: "L",
    response: { status: 404, url: "https://blocked.example/secret?x=1", headers: { a: "b" } },
  });
  await new Promise((resolve) => setTimeout(resolve, 30));
  const blocked = methods();
  assert.ok(!blocked.some((text) => text.includes("SECRET") || text.includes("loadingFailed")), blocked.join("\n"));
  assert.ok(!blocked.some((text) => text.includes("blocked.example")), blocked.join("\n"));
  const document = port.messages.find((message) => message.params && message.params.type === "Document");
  assert.deepEqual(document.params, { requestId: "3", type: "Document", frameId: "F", loaderId: "L", response: { status: 404 } });

  port.messages.length = 0;
  emit({ tabId: 11 }, "Runtime.consoleAPICalled", { type: "log", args: [{ type: "string", value: "visible" }] });
  await new Promise((resolve) => setTimeout(resolve, 30));
  assert.match(methods().join("\n"), /visible/);
});

test("closing a tab the bridge does not own reports failure", async () => {
  const handle = port.onMessage.listeners[0];
  handle({ id: 70, method: "Bridge.configure", params: { allowedDomains: [] }, leaseId: "lease-c1" });
  await new Promise((resolve) => setTimeout(resolve, 20));
  port.messages.length = 0;
  handle({ id: 71, method: "Target.closeTarget", params: { targetId: "999" }, leaseId: "lease-c1" });
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.deepEqual(port.messages.find((message) => message.id === 71), { id: 71, result: { success: false } });
});

test("page logs from a source whose URL is unknown are not forwarded", async () => {
  const emit = chrome.debugger.onEvent.listeners[0];
  port.messages.length = 0;
  emit({ tabId: 11, sessionId: "unknown-child" }, "Runtime.consoleAPICalled", {
    type: "log", args: [{ type: "string", value: "FROM-UNKNOWN-FRAME" }],
  });
  chrome.tabs.get = async () => { throw new Error("tab is closing"); };
  emit({ tabId: 12 }, "Runtime.consoleAPICalled", { type: "log", args: [{ type: "string", value: "FROM-CLOSING-TAB" }] });
  await new Promise((resolve) => setTimeout(resolve, 30));
  assert.doesNotMatch(JSON.stringify(port.messages), /FROM-UNKNOWN-FRAME|FROM-CLOSING-TAB/);
});

test("a document response reaches the page log only for an allowed page, in full", async () => {
  const emit = chrome.debugger.onEvent.listeners[0];
  chrome.tabs.get = async (tabId) => ({ id: tabId, windowId: 1, url: tabId === 10 ? "https://blocked.example/" : "https://example.com/" });
  const documentResponse = (url) => ({
    requestId: "9", type: "Document", frameId: "F", loaderId: "L", response: { status: 404, url },
  });
  port.messages.length = 0;
  emit({ tabId: 10 }, "Network.responseReceived", documentResponse("https://blocked.example/x"));
  await new Promise((resolve) => setTimeout(resolve, 30));
  assert.deepEqual(port.messages.map((message) => message.params.response), [{ status: 404 }]);

  port.messages.length = 0;
  emit({ tabId: 11 }, "Network.responseReceived", documentResponse("https://example.com/missing"));
  await new Promise((resolve) => setTimeout(resolve, 30));
  assert.deepEqual(port.messages.map((message) => message.params.response), [
    { status: 404 },
    { status: 404, url: "https://example.com/missing" },
  ]);
});

test("Bridge.tabState and Target.activateTarget operate only on owned tabs", async () => {
  const handle = port.onMessage.listeners[0];
  let updated = null;
  localData.sitePermissions = { "https://example.com": "allow" };
  chrome.tabs.create = async ({ url }) => ({ id: 88, windowId: 2, url, active: false, title: "Owned Bg" });
  chrome.tabs.get = async (tabId) => ({
    id: tabId,
    windowId: 2,
    url: "https://example.com/video",
    title: "Owned Bg",
    active: tabId === 88 ? Boolean(updated && updated.active) : true,
  });
  chrome.tabs.update = async (tabId, props) => {
    updated = { tabId, ...props };
    return { id: tabId, active: Boolean(props.active) };
  };

  handle({ id: 90, method: "Bridge.configure", params: { allowedDomains: ["example.com"] }, leaseId: "lease-tab" });
  await new Promise((resolve) => setTimeout(resolve, 20));
  handle({ id: 91, method: "Target.createTarget", params: { url: "https://example.com/video" }, leaseId: "lease-tab" });
  await new Promise((resolve) => setTimeout(resolve, 20));

  port.messages.length = 0;
  handle({ id: 92, method: "Bridge.tabState", params: { targetId: "88" }, leaseId: "lease-tab" });
  handle({ id: 93, method: "Bridge.tabState", params: { targetId: "999" }, leaseId: "lease-tab" });
  handle({ id: 94, method: "Target.activateTarget", params: { targetId: "88" }, leaseId: "lease-tab" });
  handle({ id: 95, method: "Target.activateTarget", params: { targetId: "999" }, leaseId: "lease-tab" });
  await new Promise((resolve) => setTimeout(resolve, 30));

  const stateOwned = port.messages.find((m) => m.id === 92);
  assert.equal(stateOwned.result.active, false);
  assert.equal(stateOwned.result.title, "Owned Bg");

  const stateForeign = port.messages.find((m) => m.id === 93);
  assert.match(stateForeign.error, /not owned/);

  const actOwned = port.messages.find((m) => m.id === 94);
  assert.deepEqual(actOwned.result, { activated: true });
  assert.deepEqual(updated, { tabId: 88, active: true });

  const actForeign = port.messages.find((m) => m.id === 95);
  assert.match(actForeign.error, /not owned/);
});

test("dangerous CDP methods are refused and unlisted events are dropped", async () => {
  const handle = port.onMessage.listeners[0];
  const onEvent = chrome.debugger.onEvent.listeners[0];
  port.messages.length = 0;

  handle({ id: 111, method: "Browser.grantPermissions", params: {}, leaseId: "lease-tab" });
  handle({ id: 112, method: "Target.getTargets", params: {}, leaseId: "lease-tab" });
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.match(port.messages.find((m) => m.id === 111).error, /not allowed in bridge mode/);
  assert.match(port.messages.find((m) => m.id === 112).error, /not allowed in bridge mode/);

  // Attach an OOPIF child, navigate it via Target.targetInfoChanged on parent session,
  // and verify unlisted events (extraInfo / Log.entryAdded) are never forwarded.
  port.messages.length = 0;
  onEvent(
    { tabId: 88 },
    "Target.attachedToTarget",
    { sessionId: "child-1", targetInfo: { targetId: "tgt-child-1", url: "https://example.com/frame" } }
  );
  onEvent(
    { tabId: 88 },
    "Target.targetInfoChanged",
    { targetInfo: { targetId: "tgt-child-1", url: "https://unapproved.invalid/secret" } }
  );
  await new Promise((resolve) => setTimeout(resolve, 20));
  port.messages.length = 0;
  onEvent(
    { tabId: 88, sessionId: "child-1" },
    "Network.responseReceivedExtraInfo",
    { headers: { "set-cookie": "sid=secret" } }
  );
  onEvent({ tabId: 88, sessionId: "child-1" }, "Log.entryAdded", { entry: { text: "secret" } });
  await new Promise((resolve) => setTimeout(resolve, 30));
  assert.deepEqual(port.messages, []);
});
