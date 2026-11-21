// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import test from "node:test";
import assert from "node:assert/strict";

const OWNED_KEY = "chrysOwnedTabsV1";
const SESSION_KEY = "chrysBrowserSessionV1";
const groupCalls = [];
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

globalThis.chrome = {
  runtime: {
    id: "test-extension",
    getURL: (path) => `chrome-extension://test-extension/${path}`,
    connectNative: () => port,
    onStartup: event(),
    onInstalled: event(),
    onMessage: event(),
  },
  debugger: {
    onEvent: event(),
    onDetach: event(),
    sendCommand: async () => ({}),
  },
  tabs: {
    onRemoved: event(),
    get: async (tabId) => ({ id: tabId, windowId: 1, url: "https://example.com/" }),
    group: async (options) => {
      groupCalls.push(options);
      return 1;
    },
  },
  tabGroups: { update: async () => {} },
  storage: {
    local: storage(localData),
    session: storage(sessionData),
  },
  alarms: { create() {}, onAlarm: event() },
};

test("restore ignores owned tabs from a previous browser session", async () => {
  await import(`./background.js?test=${Date.now()}`);
  await new Promise((resolve) => setTimeout(resolve, 50));
  assert.deepEqual(groupCalls, []);
  assert.deepEqual(localData[OWNED_KEY], []);
});

test("disconnect clears the bridge allowlist", async () => {
  const { permissionFor } = await import("./permissions.js");
  const configure = port.onMessage.listeners[0];
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
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(await permissionFor(url), "allow");
  localData.sitePermissions = { "https://example.org": "block" };
  assert.equal(await permissionFor(url), "block");
  delete localData.sitePermissions;
  for (const listener of port.onDisconnect.listeners) listener();
  await new Promise((resolve) => setTimeout(resolve, 10));
  assert.equal(localData.bridgeAllowedDomains, undefined);
  assert.equal(localData.bridgeAllowedOrigins, undefined);
  assert.equal(await permissionFor(url), "ask");
});

test("configure without origins clears a previously granted origin list", async () => {
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

test("a missing configure lease is rejected", async () => {
  const configure = port.onMessage.listeners[0];
  port.messages.length = 0;
  configure({ id: 46, method: "Bridge.configure", params: { allowedDomains: [] } });
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.match(JSON.stringify(port.messages), /bridge_lease_missing/);
});

test("a stale lease command is rejected", async () => {
  const configure = port.onMessage.listeners[0];
  configure({ id: 42, method: "Bridge.configure", params: { allowedDomains: [] }, leaseId: "lease-a" });
  await new Promise((resolve) => setTimeout(resolve, 20));
  port.messages.length = 0;
  configure({ id: 43, method: "Bridge.ping", params: {}, leaseId: "lease-b" });
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.match(JSON.stringify(port.messages), /bridge_lease_mismatch/);
});

test("a missing lease is rejected after configure", async () => {
  const configure = port.onMessage.listeners[0];
  configure({ id: 44, method: "Bridge.configure", params: { allowedDomains: [] }, leaseId: "lease-a" });
  await new Promise((resolve) => setTimeout(resolve, 20));
  port.messages.length = 0;
  configure({ id: 45, method: "Bridge.ping", params: {} });
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.match(JSON.stringify(port.messages), /bridge_lease_missing/);
});

test("a history step is checked against its destination, not the current page", async () => {
  const handle = port.onMessage.listeners[0];
  const sent = [];
  chrome.tabs.create = async ({ url }) => ({ id: 9, windowId: 1, url });
  chrome.debugger.attach = async () => {};
  chrome.debugger.sendCommand = async (target, method, params) => {
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
test("page logs of a site the user did not allow are not forwarded", async () => {
  for (const listener of chrome.runtime.onStartup.listeners) listener(); // an earlier test disconnected
  await new Promise((resolve) => setTimeout(resolve, 20));
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

test("dangerous CDP methods are refused and sensitive network/log events are gated", async () => {
  const handle = port.onMessage.listeners[0];
  const onEvent = chrome.debugger.onEvent.listeners[0];
  port.messages.length = 0;

  handle({ id: 101, method: "Browser.grantPermissions", params: {}, leaseId: "lease-tab" });
  handle({ id: 102, method: "Target.getTargets", params: {}, leaseId: "lease-tab" });
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.match(port.messages.find((m) => m.id === 101).error, /not allowed in bridge mode/);
  assert.match(port.messages.find((m) => m.id === 102).error, /not allowed in bridge mode/);

  // Attach an OOPIF child, navigate it via Target.targetInfoChanged on parent session (no source.sessionId),
  // and verify extraInfo/Log events from that blocked origin are not forwarded.
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
