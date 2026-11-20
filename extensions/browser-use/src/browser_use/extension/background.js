// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

// iCode browser bridge: translate the plugin's CDP-shaped frames into
// chrome.debugger / chrome.tabs calls on tabs this extension owns.
//
// Chrome 125+ supports flat debugger sessions: a Target.attachedToTarget event
// carries `sessionId`, and sendCommand({tabId, sessionId}) routes to that child
// (OOPIF). Nested OOPIFs need setAutoAttach again on the child session.

import { assertPermission, needsPermission, permissionError, permissionFor, setBridgePublicWeb } from "./permissions.js";
import { BridgeChunkReceiver, encodeBridgeMessages } from "./chunks.js";

const HOST = "com.chrys.browser";
const GROUP_TITLE = "iCode";
const OWNED_TABS_KEY = "chrysOwnedTabsV1";
const BROWSER_SESSION_KEY = "chrysBrowserSessionV1";
const OWN_ORIGIN = `chrome-extension://${chrome.runtime.id}/`;
let port = null;
let groupId = null;
let connecting = false;
let ownedWrite = Promise.resolve();
const ownedTabs = new Set();
const sessions = new Map(); // child sessionId -> { tabId, targetId, url }
let bridgeReady = false;
let bridgeLeaseId = null;
const incomingChunks = new BridgeChunkReceiver();

function postBridge(payload, targetPort = port) {
  if (!targetPort) return;
  try {
    for (const frame of encodeBridgeMessages(payload)) targetPort.postMessage(frame);
  } catch (error) {
    if (payload && payload.id !== undefined) {
      try {
        targetPort.postMessage({ id: payload.id, error: String((error && error.message) || error) });
      } catch {
        // The source port is gone; never leak its reply to the new connection.
      }
    }
  }
}

function connect() {
  if (port || connecting) return;
  connecting = true;
  void restoreOwnedTabs().finally(() => {
    let nextPort;
    try {
      nextPort = chrome.runtime.connectNative(HOST);
    } catch {
      connecting = false;
      setTimeout(connect, 1000);
      return;
    }
    port = nextPort;
    nextPort.onMessage.addListener((raw) => {
      let message;
      try {
        message = incomingChunks.add(raw);
      } catch {
        nextPort.disconnect();
        return;
      }
      if (message === null) return;
      if (message && message.method === "Bridge.ready") {
        bridgeReady = true;
        return;
      }
      handle(message, nextPort).catch((error) => {
        if (message && message.id !== undefined) {
          postBridge({ id: message.id, error: String((error && error.message) || error) }, nextPort);
        }
      });
    });
    nextPort.onDisconnect.addListener(() => {
      if (port !== nextPort) return;
      port = null;
      bridgeReady = false;
      clearBridgePolicy();
      setTimeout(connect, 1000);
    });
    connecting = false;
  });
}

async function readOwnedRecords() {
  try {
    const data = await chrome.storage.local.get({ [OWNED_TABS_KEY]: [] });
    return Array.isArray(data[OWNED_TABS_KEY]) ? data[OWNED_TABS_KEY] : [];
  } catch {
    return null;
  }
}

async function writeOwnedRecords(records) {
  try {
    await chrome.storage.local.set({ [OWNED_TABS_KEY]: records });
  } catch {
    // Storage failures must not block the page action; memory still has the set.
  }
}

function clearBridgePolicy() {
  bridgeLeaseId = null;
  setBridgePublicWeb(false);
  try {
    chrome.storage.local.remove("bridgeAllowedDomains");
    chrome.storage.local.remove("bridgeAllowedOrigins");
  } catch {
    // Best effort; the next Bridge.configure replaces it anyway.
  }
}

function enqueueOwnedWrite(operation) {
  const next = ownedWrite.then(operation, operation);
  ownedWrite = next.catch(() => {});
  return next;
}

async function browserSessionId() {
  try {
    const data = await chrome.storage.session.get({ [BROWSER_SESSION_KEY]: "" });
    if (data[BROWSER_SESSION_KEY]) return data[BROWSER_SESSION_KEY];
    const id = crypto.randomUUID();
    await chrome.storage.session.set({ [BROWSER_SESSION_KEY]: id });
    return id;
  } catch {
    return "";
  }
}

async function rememberOwned(tabId) {
  ownedTabs.add(tabId);
  await enqueueOwnedWrite(async () => {
    try {
      const tab = await chrome.tabs.get(tabId);
      const sessionId = await browserSessionId();
      if (!sessionId) return;
      const records = await readOwnedRecords();
      if (records === null) return;
      const filtered = records.filter((record) => record.tabId !== tabId);
      filtered.push({ tabId, windowId: tab.windowId, createdAt: Date.now(), sessionId });
      await writeOwnedRecords(filtered);
    } catch {
      // The tab may already be gone; memory ownership still fails closed on get.
    }
  });
}

async function forgetOwned(tabId) {
  ownedTabs.delete(tabId);
  await enqueueOwnedWrite(async () => {
    const records = await readOwnedRecords();
    if (records === null) return;
    await writeOwnedRecords(records.filter((record) => record.tabId !== tabId));
  });
}

async function restoreOwnedTabs() {
  // Read and write inside the same queue operation: a concurrent rememberOwned
  // must not be overwritten by a stale restore snapshot.
  const sessionId = await browserSessionId();
  if (!sessionId) return;
  await enqueueOwnedWrite(async () => {
    const records = await readOwnedRecords();
    if (records === null) return;
    ownedTabs.clear();
    const kept = [];
    for (const record of records) {
      if (record.sessionId !== sessionId) continue;
      try {
        const tab = await chrome.tabs.get(record.tabId);
        if (tab && tab.id !== undefined && tab.windowId === record.windowId) {
          ownedTabs.add(tab.id);
          kept.push(record);
          await groupTab(tab.id);
        }
      } catch {
        // Closed or replaced tab: drop the stale record.
      }
    }
    await writeOwnedRecords(kept);
  });
}

async function groupTab(tabId) {
  if (groupId !== null) {
    try {
      await chrome.tabs.group({ tabIds: [tabId], groupId });
      return;
    } catch {
      // Chrome removes an empty group; fall back to a fresh one.
      groupId = null;
    }
  }
  groupId = await chrome.tabs.group({ tabIds: [tabId] });
  await chrome.tabGroups.update(groupId, { title: GROUP_TITLE });
}

function requireOwned(tabId) {
  if (!ownedTabs.has(tabId)) {
    throw new Error(`tab ${tabId} is not owned by this bridge`);
  }
}

async function assertTabPermission(tabId) {
  const tab = await chrome.tabs.get(tabId);
  await assertPermission(tab.url || "");
}

async function assertSessionPermission(target) {
  if (target.sessionId) {
    const child = sessions.get(target.sessionId);
    if (!child || !child.url) {
      throw new Error("child session origin unknown; refusing to act");
    }
    await assertPermission(child.url);
    return;
  }
  await assertTabPermission(target.tabId);
}

function isForeignExtension(url) {
  return String(url || "").startsWith("chrome-extension://") && !String(url).startsWith(OWN_ORIGIN);
}

function debuggeeFor(sessionId) {
  const child = sessions.get(sessionId);
  if (child) return { tabId: child.tabId, sessionId };
  const tabId = Number(sessionId);
  requireOwned(tabId);
  return { tabId };
}

function forgetSessions(tabId) {
  for (const [sessionId, child] of sessions) {
    if (child.tabId === tabId) sessions.delete(sessionId);
  }
}

async function handle(message, sourcePort = port) {
  if (!message || typeof message.method !== "string") return;
  const { id, method, params, sessionId } = message;
  if (bridgeLeaseId && method !== "Bridge.hello" && method !== "Bridge.configure") {
    if (!message.leaseId) {
      throw new Error("bridge_lease_missing: command has no lease");
    }
    if (message.leaseId !== bridgeLeaseId) {
      throw new Error("bridge_lease_mismatch: stale bridge command");
    }
  }
  let result = {};
  if (method === "Bridge.hello") {
    result = {
      protocol: 1,
      extensionVersion: chrome.runtime.getManifest().version,
      browser: navigator.userAgent,
      capabilities: ["ping", "page_log_gated", "tab_state"],
    };
  } else if (method === "Target.createTarget") {
    const url = (params && params.url) || "about:blank";
    await assertPermission(url);
    const tab = await chrome.tabs.create({ url, active: false });
    await rememberOwned(tab.id);
    await groupTab(tab.id);
    result = { targetId: String(tab.id) };
  } else if (method === "Target.attachToTarget") {
    const tabId = Number(params && params.targetId);
    requireOwned(tabId);
    await assertTabPermission(tabId);
    await chrome.debugger.attach({ tabId }, "1.3");
    result = { sessionId: String(tabId) };
  } else if (method === "Target.closeTarget") {
    const tabId = Number(params && params.targetId);
    const owned = ownedTabs.has(tabId);
    if (owned) {
      await forgetOwned(tabId);
      forgetSessions(tabId);
      await chrome.tabs.remove(tabId);
    }
    // Same shape as CDP: a tab this bridge does not own is not closed, and says so.
    result = { success: owned };
  } else if (method === "Target.setAutoAttach") {
    const target = debuggeeFor(sessionId);
    const autoParams = { ...(params || {}), autoAttach: true, flatten: true };
    result = (await chrome.debugger.sendCommand(target, "Target.setAutoAttach", autoParams)) || {};
  } else if (method === "Page.navigate") {
    const target = debuggeeFor(sessionId);
    const url = (params && params.url) || "";
    const state = await permissionFor(url);
    if (state !== "allow") {
      permissionError(url, state);
    }
    result = (await chrome.debugger.sendCommand(target, method, params || {})) || {};
  } else if (method === "Page.navigateToHistoryEntry") {
    // A history step changes the page like Page.navigate: check its destination,
    // not the page it leaves.
    const target = debuggeeFor(sessionId);
    const history = (await chrome.debugger.sendCommand(target, "Page.getNavigationHistory")) || {};
    const entryId = params && params.entryId;
    const entry = (history.entries || []).find((item) => item && item.id === entryId);
    if (!entry) throw new Error(`unknown history entry: ${entryId}`);
    const state = await permissionFor(entry.url || "");
    if (state !== "allow") {
      permissionError(entry.url || "", state);
    }
    result = (await chrome.debugger.sendCommand(target, method, params || {})) || {};
  } else if (method === "Bridge.configure") {
    if (!message.leaseId) {
      throw new Error("bridge_lease_missing: configure requires lease");
    }
    const domains = Array.isArray(params && params.allowedDomains)
      ? params.allowedDomains.map((domain) => String(domain || "").trim().toLowerCase()).filter(Boolean)
      : [];
    const origins = Array.isArray(params && params.allowedOrigins)
      ? params.allowedOrigins.map((origin) => String(origin || "").trim().toLowerCase()).filter(Boolean)
      : [];
    bridgeLeaseId = message.leaseId;
    await chrome.storage.local.set({ bridgeAllowedDomains: domains, bridgeAllowedOrigins: origins });
    if (port === sourcePort && bridgeLeaseId === message.leaseId) {
      setBridgePublicWeb(params?.publicWeb === true && !domains.length && !origins.length);
    }
    result = { domains: domains.length, origins: origins.length, capabilities: ["ping", "page_log_gated", "tab_state"] };
  } else if (method === "Bridge.tabState") {
    const tabId = Number((params && params.targetId) || sessionId);
    requireOwned(tabId);
    const tab = await chrome.tabs.get(tabId);
    result = {
      active: Boolean(tab && tab.active),
      url: (tab && tab.url) || "",
      title: (tab && tab.title) || "",
      windowId: tab ? tab.windowId : null,
    };
  } else if (method === "Target.activateTarget") {
    const tabId = Number((params && params.targetId) || sessionId);
    requireOwned(tabId);
    await chrome.tabs.update(tabId, { active: true });
    result = { activated: true };
  } else if (method === "Bridge.ping") {
    result = { pong: true };
  } else if (method === "Browser.close") {
    throw new Error("Browser.close is not allowed in bridge mode");
  } else {
    const target = debuggeeFor(sessionId);
    if (needsPermission(method)) await assertSessionPermission(target);
    result = (await chrome.debugger.sendCommand(target, method, params || {})) || {};
  }
  if (id !== undefined) {
    postBridge({ id, result }, sourcePort);
  }
}

// Events only browser_logs reads. They carry page content (console text, request
// URLs), so a page the user has not allowed keeps them out of the bridge, the same
// way its Runtime.evaluate is refused. A main-document response also feeds the
// navigation status: a copy reduced to those fields goes out at once for every page,
// and the full response follows through the same gate as the other log events.
const PAGE_LOG_EVENTS = new Set([
  "Runtime.consoleAPICalled",
  "Runtime.exceptionThrown",
  "Network.requestWillBeSent",
  "Network.responseReceived",
  "Network.loadingFailed",
]);

async function eventSourceUrl(source) {
  if (source.sessionId) return (sessions.get(source.sessionId) || {}).url || "";
  try {
    return (await chrome.tabs.get(source.tabId)).url || "";
  } catch {
    return "";
  }
}

function forwardPageLogEvent(source, method, params, parentTabId) {
  const sessionId = source.sessionId || String(parentTabId);
  if (method === "Network.responseReceived" && params.type === "Document") {
    const { requestId, type, frameId, loaderId } = params;
    const status = (params.response || {}).status;
    postBridge({ method, params: { requestId, type, frameId, loaderId, response: { status } }, sessionId });
  }
  eventSourceUrl(source)
    .then((url) => (url ? permissionFor(url) : "unknown")) // fail closed, like assertSessionPermission
    .then((state) => {
      if (state === "allow") postBridge({ method, params, sessionId });
    })
    .catch(() => {});
}

chrome.debugger.onEvent.addListener((source, method, params) => {
  if (source.tabId === undefined) return;
  const parentTabId = source.tabId;
  if (method === "Target.targetInfoChanged" && source.sessionId) {
    const info = params && params.targetInfo;
    const child = sessions.get(source.sessionId);
    if (child && info && info.url) child.url = info.url;
  }
  if (method === "Target.attachedToTarget") {
    const info = (params && params.targetInfo) || {};
    const child = params && params.sessionId;
    if (!child) return;
    if (isForeignExtension(info.url)) {
      // Never pause or inspect another extension's frame. Release it if the
      // plugin asked for waitForDebuggerOnStart; detach when Chrome allows it.
      const childTarget = { tabId: parentTabId, sessionId: child };
      chrome.debugger.sendCommand(childTarget, "Runtime.runIfWaitingForDebugger").catch(() => {});
      chrome.debugger
        .sendCommand({ tabId: parentTabId }, "Target.detachFromTarget", { sessionId: child })
        .catch(() => {});
      return;
    }
    sessions.set(child, { tabId: parentTabId, targetId: info.targetId, url: info.url });
    if (!port) {
      // A reconnecting bridge must not leave a waitForDebuggerOnStart child frozen.
      chrome.debugger
        .sendCommand({ tabId: parentTabId, sessionId: child }, "Runtime.runIfWaitingForDebugger")
        .catch(() => {});
      return;
    }
    postBridge({ method, params: params || {}, sessionId: source.sessionId || null });
    return;
  }
  if (method === "Target.detachedFromTarget") {
    sessions.delete(params && params.sessionId);
  }
  if (!port) return;
  if (PAGE_LOG_EVENTS.has(method)) {
    forwardPageLogEvent(source, method, params || {}, parentTabId);
    return;
  }
  postBridge({
    method,
    params: params || {},
    sessionId: source.sessionId || String(parentTabId),
  });
});

chrome.debugger.onDetach.addListener((source) => {
  if (source.tabId !== undefined) {
    // A user detach pauses debugger access; it does not prove the tab is not ours.
    forgetSessions(source.tabId);
  }
});

chrome.tabs.onRemoved.addListener((tabId) => {
  void forgetOwned(tabId);
  forgetSessions(tabId);
});

chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);
chrome.runtime.onInstalled.addListener((details) => {
  if (details.reason === "install") {
    chrome.tabs.create({ url: chrome.runtime.getURL("onboarding.html") });
  }
});
chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message && message.type === "bridge_status") {
    sendResponse({ connected: Boolean(port && bridgeReady), host: HOST });
  }
});
chrome.alarms.create("bridge-keepalive", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener(() => {
  if (!port) connect();
});

connect();
