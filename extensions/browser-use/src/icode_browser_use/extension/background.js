// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

// iCode browser bridge: translate the plugin's CDP-shaped frames into
// chrome.debugger / chrome.tabs calls on tabs this extension owns.
//
// Chrome 125+ supports flat debugger sessions: a Target.attachedToTarget event
// carries `sessionId`, and sendCommand({tabId, sessionId}) routes to that child
// (OOPIF). Nested OOPIFs need setAutoAttach again on the child session.

import {
  BRIDGE_METHODS,
  METHOD_FETCH_CONTINUE,
  METHOD_FETCH_ENABLE,
  METHOD_NETWORK_COOKIES,
  METHOD_PLUMBING,
  METHOD_PROBE_SCRIPT,
  METHOD_SITE,
  MUTATION_PROBE_SHA256,
  assertPermission,
  isSafeBlankUrl,
  methodPolicy,
  needsPermission,
  originOf,
  permissionError,
  permissionFor,
  setBridgePublicWeb,
  stripQueryAndFragment,
} from "./permissions.js";
import { BridgeChunkReceiver, encodeBridgeMessages } from "./chunks.js";

const HOST = "com.chrys.browser";
const GROUP_TITLE = "iCode";
const OWNED_TABS_KEY = "chrysOwnedTabsV1";
const BROWSER_SESSION_KEY = "chrysBrowserSessionV1";
const BRIDGE_STATE_KEY = "chrysBridgeStateV1";
const OWN_ORIGIN = `chrome-extension://${chrome.runtime.id}/`;
const MIN_RECONNECT_MS = 1000;
const MAX_RECONNECT_MS = 30000;

let port = null;
let groupId = null;
let connecting = false;
let reconnectDelayMs = MIN_RECONNECT_MS;
let reconnectTimer = null;
let ownedWrite = Promise.resolve();
let policyWrite = Promise.resolve();
let dnrWrite = Promise.resolve();
const ownedTabs = new Set();
const closingTabs = new Set();
const adoptingTabs = new Set();
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

function enqueuePolicyWrite(operation) {
  const next = policyWrite.then(operation, operation);
  policyWrite = next.catch(() => {});
  return next;
}

function enqueueDnrSync() {
  const next = dnrWrite.then(syncDnrRules, syncDnrRules);
  dnrWrite = next.catch(() => {});
  return next;
}

async function syncDnrRules() {
  if (!chrome.declarativeNetRequest || !chrome.declarativeNetRequest.updateSessionRules) return;
  try {
    const existing = chrome.declarativeNetRequest.getSessionRules
      ? await chrome.declarativeNetRequest.getSessionRules()
      : [];
    const removeRuleIds = existing.map((rule) => rule.id);
    const tabIds = Array.from(ownedTabs);
    // Without a connected plugin the tabs are released to the user: no rules.
    if (tabIds.length === 0 || !port) {
      if (removeRuleIds.length > 0) {
        await chrome.declarativeNetRequest.updateSessionRules({ removeRuleIds });
      }
      return;
    }
    const data = await chrome.storage.local.get({
      sitePermissions: {},
      defaultPermission: "ask",
      userAllowsPublicWeb: false,
      bridgePublicWeb: false,
      bridgeAllowedDomains: [],
      bridgeAllowedOrigins: [],
    });
    const permissions = data.sitePermissions || {};
    const domains = Array.isArray(data.bridgeAllowedDomains) ? data.bridgeAllowedDomains : [];
    const origins = Array.isArray(data.bridgeAllowedOrigins) ? data.bridgeAllowedOrigins : [];
    const hasNarrowing = domains.length > 0 || origins.length > 0;
    const publicFallbackAllow =
      !hasNarrowing && ((data.bridgePublicWeb === true && data.userAllowsPublicWeb === true) || data.defaultPermission === "allow");

    const addRules = [];
    let nextId = 1;
    const resourceTypes = ["main_frame", "sub_frame"];
    // "|https://a.com/" anchors the whole origin; without the trailing "/" the
    // filter is a prefix and would also match https://a.com.evil.net/.
    const originFilter = (origin) => `|${origin}/`;

    // Layer 1 pre-request navigation boundary on owned tabs only:
    // Priority 1: block main_frame navigations by default (fail-closed before cookies leave).
    if (!publicFallbackAllow) {
      addRules.push({
        id: nextId++,
        priority: 1,
        action: { type: "block" },
        condition: { urlFilter: "|http", tabIds, resourceTypes },
      });
    }
    // Priority 10: allow explicitly permitted origins (when compatible with plugin narrowing).
    for (const [origin, state] of Object.entries(permissions)) {
      if (state === "allow" && originOf(origin) === origin) {
        const effective = await permissionFor(origin);
        if (effective === "allow") {
          addRules.push({
            id: nextId++,
            priority: 10,
            action: { type: "allow" },
            condition: { urlFilter: originFilter(origin), tabIds, resourceTypes },
          });
        }
      }
    }
    // With plugin narrowing, permissionFor() allows listed origins/domains when the
    // default policy or the user's public-web opt-in allows them: mirror that here
    // so layer 1 is never stricter than layer 2.
    const narrowedFallbackAllow =
      hasNarrowing && (data.defaultPermission === "allow" || (data.bridgePublicWeb === true && data.userAllowsPublicWeb === true));
    if (narrowedFallbackAllow) {
      for (const origin of origins) {
        const clean = originOf(String(origin || ""));
        if (!clean) continue;
        addRules.push({
          id: nextId++,
          priority: 10,
          action: { type: "allow" },
          condition: { urlFilter: originFilter(clean), tabIds, resourceTypes },
        });
      }
      const requestDomains = domains.map((domain) => String(domain || "").replace(/^\./, "")).filter(Boolean);
      if (requestDomains.length) {
        addRules.push({
          id: nextId++,
          priority: 10,
          action: { type: "allow" },
          condition: { requestDomains, tabIds, resourceTypes },
        });
      }
    }
    // Priority 20: explicit block/ask overrides always block before sending cookies.
    for (const [origin, state] of Object.entries(permissions)) {
      if ((state === "block" || state === "ask") && originOf(origin) === origin) {
        addRules.push({
          id: nextId++,
          priority: 20,
          action: { type: "block" },
          condition: { urlFilter: originFilter(origin), tabIds, resourceTypes },
        });
      }
    }
    await chrome.declarativeNetRequest.updateSessionRules({ removeRuleIds, addRules });
  } catch {
    // Best effort: webNavigation.onCommitted and assertPermission still enforce the boundary.
  }
}

async function releaseOwnedTabs() {
  for (const tabId of Array.from(ownedTabs)) {
    closingTabs.add(tabId);
    try {
      await chrome.debugger.sendCommand({ tabId }, "Fetch.disable");
    } catch {
      // Best effort; tab may not be attached or Fetch may not be enabled.
    }
    try {
      if (chrome.debugger && chrome.debugger.detach) {
        await chrome.debugger.detach({ tabId });
      }
    } catch {
      // Best effort; tab may already be detached or closed.
    }
    forgetSessions(tabId);
    closingTabs.delete(tabId);
  }
  await enqueueDnrSync();
}

function scheduleReconnect() {
  if (port || connecting || reconnectTimer !== null) return;
  if (reconnectDelayMs > MAX_RECONNECT_MS) {
    // Hand off to the 30s service-worker alarm so Chrome does not churn processes.
    return;
  }
  const delay = reconnectDelayMs;
  reconnectDelayMs = delay >= MAX_RECONNECT_MS ? MAX_RECONNECT_MS + 1 : Math.min(delay * 2, MAX_RECONNECT_MS);
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connect();
  }, delay);
}

function handlePortDisconnect(targetPort) {
  if (port !== targetPort) return;
  port = null;
  bridgeReady = false;
  incomingChunks.clear();
  void enqueuePolicyWrite(() => clearBridgePolicy());
  void releaseOwnedTabs();
  scheduleReconnect();
}

function connect() {
  if (port || connecting) return;
  if (reconnectTimer !== null) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  connecting = true;
  void restoreOwnedTabs().finally(() => {
    let nextPort;
    try {
      nextPort = chrome.runtime.connectNative(HOST);
    } catch {
      connecting = false;
      scheduleReconnect();
      return;
    }
    port = nextPort;
    nextPort.onMessage.addListener((raw) => {
      let message;
      try {
        message = incomingChunks.add(raw);
      } catch {
        // Calling disconnect() locally does not fire onDisconnect on the
        // calling side in Chrome, so tear down and reschedule explicitly.
        try {
          nextPort.disconnect();
        } catch {
          // Already disconnected.
        }
        handlePortDisconnect(nextPort);
        return;
      }
      if (message === null) return;
      if (message && message.method === "Bridge.ready") {
        bridgeReady = true;
        reconnectDelayMs = MIN_RECONNECT_MS;
        return;
      }
      handle(message, nextPort).catch((error) => {
        if (message && message.id !== undefined) {
          postBridge({ id: message.id, error: String((error && error.message) || error) }, nextPort);
        }
      });
    });
    nextPort.onDisconnect.addListener(() => {
      // Read lastError to avoid unchecked runtime.lastError warnings in Chrome.
      void chrome.runtime.lastError;
      handlePortDisconnect(nextPort);
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

async function clearBridgePolicy() {
  bridgeLeaseId = null;
  setBridgePublicWeb(false);
  try {
    await chrome.storage.local.remove("bridgeAllowedDomains");
    await chrome.storage.local.remove("bridgeAllowedOrigins");
    await chrome.storage.local.remove("bridgePublicWeb");
    if (chrome.storage.session && chrome.storage.session.remove) {
      await chrome.storage.session.remove(BRIDGE_STATE_KEY);
    }
  } catch {
    // Best effort; the next Bridge.configure replaces it anyway.
  }
  await enqueueDnrSync();
}

async function persistBridgeState(leaseId, publicWeb) {
  try {
    if (chrome.storage.session && chrome.storage.session.set) {
      await chrome.storage.session.set({
        [BRIDGE_STATE_KEY]: { leaseId: leaseId || null, publicWeb: publicWeb === true },
      });
    }
  } catch {
    // Best effort; in-memory state remains authoritative for this SW lifetime.
  }
}

async function restoreBridgeState() {
  if (bridgeLeaseId !== null) return;
  try {
    if (!chrome.storage.session || !chrome.storage.session.get) return;
    const data = await chrome.storage.session.get({ [BRIDGE_STATE_KEY]: null });
    const state = data[BRIDGE_STATE_KEY];
    if (state && typeof state === "object") {
      if (state.leaseId && bridgeLeaseId === null) {
        bridgeLeaseId = String(state.leaseId);
      }
      if (typeof state.publicWeb === "boolean") {
        setBridgePublicWeb(state.publicWeb);
      }
    }
  } catch {
    // Ignore storage errors; fail-closed defaults stay in place.
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
  await enqueueDnrSync();
}

async function forgetOwned(tabId) {
  ownedTabs.delete(tabId);
  await enqueueOwnedWrite(async () => {
    const records = await readOwnedRecords();
    if (records === null) return;
    await writeOwnedRecords(records.filter((record) => record.tabId !== tabId));
  });
  await enqueueDnrSync();
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
  await assertPermission((tab && tab.url) || "");
  if (tab && tab.pendingUrl) {
    await assertPermission(tab.pendingUrl);
  }
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

async function sha256Hex(text) {
  const bytes = new TextEncoder().encode(String(text || ""));
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
}

function validateFetchEnable(params) {
  const patterns = Array.isArray(params && params.patterns) ? params.patterns : [];
  for (const pattern of patterns) {
    const stage = pattern && pattern.requestStage;
    if (stage !== undefined && stage !== "Request") {
      throw new Error(`Fetch.enable requestStage=${stage} is not allowed in bridge mode`);
    }
  }
  return params || {};
}

function stripFetchContinueRequest(params) {
  const requestId = params && params.requestId;
  if (!requestId) {
    throw new Error("Fetch.continueRequest requires requestId");
  }
  return { requestId };
}

function cookieMatchesHost(cookieDomain, host) {
  const raw = String(cookieDomain || "").trim().toLowerCase();
  const cleanHost = String(host || "").trim().toLowerCase();
  if (!raw || !cleanHost) return false;
  if (raw.startsWith(".")) {
    const base = raw.slice(1);
    return cleanHost === base || cleanHost.endsWith(`.${base}`);
  }
  return cleanHost === raw;
}

async function runCheckedGetCookies(target, params) {
  const urls = Array.isArray(params && params.urls) ? params.urls : [];
  if (urls.length === 0) {
    throw new Error("Network.getCookies requires non-empty urls in bridge mode");
  }
  const allowedHosts = [];
  for (const rawUrl of urls) {
    const urlStr = String(rawUrl || "");
    if (!originOf(urlStr)) {
      throw new Error(`Network.getCookies url is not an allowed http(s) origin: ${stripQueryAndFragment(urlStr)}`);
    }
    await assertPermission(urlStr);
    allowedHosts.push(new URL(urlStr).hostname.toLowerCase());
  }
  const raw = (await chrome.debugger.sendCommand(target, "Network.getCookies", { urls })) || {};
  const cookies = Array.isArray(raw.cookies) ? raw.cookies : [];
  return {
    ...raw,
    cookies: cookies.filter((cookie) =>
      allowedHosts.some((host) => cookieMatchesHost(cookie && cookie.domain, host))
    ),
  };
}

async function runCheckedProbeScript(target, params) {
  const source = params && typeof params.source === "string" ? params.source : "";
  const hash = await sha256Hex(source);
  if (hash !== MUTATION_PROBE_SHA256) {
    throw new Error("Page.addScriptToEvaluateOnNewDocument is restricted to the built-in mutation probe");
  }
  return (await chrome.debugger.sendCommand(target, "Page.addScriptToEvaluateOnNewDocument", { source })) || {};
}

async function enableFocusEmulation(tabId) {
  try {
    await chrome.debugger.sendCommand({ tabId }, "Emulation.setFocusEmulationEnabled", { enabled: true });
  } catch {
    // Best effort: older Chromium builds or minimal test doubles may not support it.
  }
}

async function adoptPopupTab({ tabId, openerTabId, url = "" }) {
  if (!port || tabId === undefined || openerTabId === undefined) return;
  if (!ownedTabs.has(openerTabId) || ownedTabs.has(tabId) || closingTabs.has(tabId) || adoptingTabs.has(tabId)) return;
  adoptingTabs.add(tabId);
  try {
    let initialUrl = url || "";
    if (!initialUrl) {
      try {
        const t = await chrome.tabs.get(tabId);
        initialUrl = (t && (t.pendingUrl || t.url)) || "";
      } catch {
        return;
      }
    }
    if (isForeignExtension(initialUrl)) return;
    if (!isSafeBlankUrl(initialUrl)) {
      const state = await permissionFor(initialUrl);
      if (state !== "allow") return;
    }
    const sourcePort = port;
    await rememberOwned(tabId);
    try {
      await groupTab(tabId);
    } catch {
      // Best effort: popup windows may disallow tab grouping.
    }
    try {
      await chrome.debugger.attach({ tabId }, "1.3");
    } catch {
      await forgetOwned(tabId);
      return;
    }
    await enableFocusEmulation(tabId);
    let currentTab = null;
    try {
      currentTab = await chrome.tabs.get(tabId);
    } catch {
      await forgetOwned(tabId);
      return;
    }
    const finalUrl = (currentTab && (currentTab.url || currentTab.pendingUrl)) || initialUrl;
    const allowed = !isForeignExtension(finalUrl) && (isSafeBlankUrl(finalUrl) || (await permissionFor(finalUrl)) === "allow");
    if (!allowed || port !== sourcePort) {
      closingTabs.add(tabId);
      try {
        await chrome.debugger.detach({ tabId });
      } catch {
        // Best effort.
      } finally {
        closingTabs.delete(tabId);
      }
      if (!allowed) await forgetOwned(tabId);
      return;
    }
    postBridge(
      {
        method: "Target.attachedToTarget",
        params: {
          sessionId: String(tabId),
          targetInfo: {
            type: "page",
            targetId: String(tabId),
            openerId: String(openerTabId),
            url: finalUrl,
            title: (currentTab && currentTab.title) || "",
          },
          waitingForDebugger: false,
        },
        sessionId: null,
      },
      sourcePort
    );
  } finally {
    adoptingTabs.delete(tabId);
  }
}

async function handle(message, sourcePort = port) {
  if (!message || typeof message.method !== "string") return;
  const { id, method, params, sessionId } = message;
  if (method !== "Bridge.configure") {
    await policyWrite;
  }
  await restoreBridgeState();
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
    await enableFocusEmulation(tabId);
    result = { sessionId: String(tabId) };
  } else if (method === "Target.closeTarget") {
    const tabId = Number(params && params.targetId);
    const owned = ownedTabs.has(tabId);
    if (owned) {
      closingTabs.add(tabId);
      try {
        await forgetOwned(tabId);
        forgetSessions(tabId);
        await chrome.tabs.remove(tabId);
      } finally {
        closingTabs.delete(tabId);
      }
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
    const publicWeb = params?.publicWeb === true && !domains.length && !origins.length;
    await enqueuePolicyWrite(async () => {
      if (port !== sourcePort) return;
      bridgeLeaseId = message.leaseId;
      await chrome.storage.local.set({
        bridgeAllowedDomains: domains,
        bridgeAllowedOrigins: origins,
        bridgePublicWeb: publicWeb,
      });
      if (port === sourcePort && bridgeLeaseId === message.leaseId) {
        setBridgePublicWeb(publicWeb);
        await persistBridgeState(bridgeLeaseId, publicWeb);
      }
      await enqueueDnrSync();
    });
    result = { domains: domains.length, origins: origins.length, capabilities: ["ping", "page_log_gated", "tab_state"] };
  } else if (method === "Bridge.tabState") {
    const tabId = Number((params && params.targetId) || sessionId);
    requireOwned(tabId);
    const tab = await chrome.tabs.get(tabId);
    const rawUrl = (tab && tab.url) || "";
    const allowed = isSafeBlankUrl(rawUrl) || (await permissionFor(rawUrl)) === "allow";
    result = {
      active: Boolean(tab && tab.active),
      url: allowed ? rawUrl : originOf(rawUrl),
      title: allowed ? (tab && tab.title) || "" : "",
      windowId: tab ? tab.windowId : null,
    };
  } else if (method === "Target.activateTarget") {
    const tabId = Number((params && params.targetId) || sessionId);
    requireOwned(tabId);
    await chrome.tabs.update(tabId, { active: true });
    result = { activated: true };
  } else if (method === "Bridge.ping") {
    result = { pong: true };
  } else {
    // Strict CDP allowlist: every command not listed in BRIDGE_METHODS
    // (including Browser.*, Storage.*, Target.getTargets, Network.getAllCookies, etc.)
    // is rejected outright, even on about:blank.
    const policy = methodPolicy(method);
    if (!policy) {
      throw new Error(`${method} is not allowed in bridge mode`);
    }
    const target = debuggeeFor(sessionId);
    if (policy === METHOD_FETCH_ENABLE) {
      const checked = validateFetchEnable(params);
      result = (await chrome.debugger.sendCommand(target, method, checked)) || {};
    } else if (policy === METHOD_FETCH_CONTINUE) {
      const stripped = stripFetchContinueRequest(params);
      result = (await chrome.debugger.sendCommand(target, method, stripped)) || {};
    } else if (policy === METHOD_NETWORK_COOKIES) {
      await assertSessionPermission(target);
      result = await runCheckedGetCookies(target, params);
    } else if (policy === METHOD_PROBE_SCRIPT) {
      result = await runCheckedProbeScript(target, params);
    } else if (policy === METHOD_PLUMBING) {
      result = (await chrome.debugger.sendCommand(target, method, params || {})) || {};
    } else if (policy === METHOD_SITE) {
      if (needsPermission(method)) await assertSessionPermission(target);
      result = (await chrome.debugger.sendCommand(target, method, params || {})) || {};
    } else {
      throw new Error(`${method} is not allowed in bridge mode`);
    }
  }
  if (id !== undefined) {
    postBridge({ id, result }, sourcePort);
  }
}

// Strict event allowlist: only events Python subscribes to are forwarded.
const PAGE_LOG_EVENTS = new Set([
  "Runtime.consoleAPICalled",
  "Runtime.exceptionThrown",
  "Network.requestWillBeSent",
  "Network.responseReceived",
  "Network.loadingFailed",
  "Network.loadingFinished",
]);

const PASS_THROUGH_EVENTS = new Set([
  "Page.frameStoppedLoading",
  "Page.lifecycleEvent",
  "Page.javascriptDialogClosed",
  "DOM.documentUpdated",
  "Target.detachedFromTarget",
  "Input.dragIntercepted",
]);

const REDACTED_URL_EVENTS = new Set([
  "Page.frameNavigated",
  "Page.navigatedWithinDocument",
  "Page.javascriptDialogOpening",
  "Target.targetInfoChanged",
]);

function isGatedPageEvent(method) {
  return PAGE_LOG_EVENTS.has(method);
}

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

function forwardFetchRequestPaused(source, params, parentTabId) {
  const sessionId = source.sessionId || String(parentTabId);
  const reqUrl = (params && params.request && params.request.url) || "";
  permissionFor(reqUrl)
    .then((state) => {
      if (!port) {
        if (params && params.requestId) {
          chrome.debugger
            .sendCommand(source, "Fetch.failRequest", {
              requestId: params.requestId,
              errorReason: "BlockedByClient",
            })
            .catch(() => {});
        }
        return;
      }
      if (state === "allow") {
        postBridge({ method: "Fetch.requestPaused", params, sessionId });
        return;
      }
      // Redact headers, cookies, postData, and query string for unapproved origins.
      postBridge({
        method: "Fetch.requestPaused",
        params: {
          requestId: params.requestId,
          request: { url: stripQueryAndFragment(reqUrl) },
          resourceType: params.resourceType,
          frameId: params.frameId,
        },
        sessionId,
      });
    })
    .catch(() => {});
}

function forwardWithRedaction(source, method, params, parentTabId) {
  const sessionId = source.sessionId || String(parentTabId);
  const candidateUrl =
    (method === "Page.frameNavigated" && params.frame && params.frame.url) ||
    (method === "Page.navigatedWithinDocument" && params.url) ||
    (method === "Page.javascriptDialogOpening" && params.url) ||
    (method === "Target.targetInfoChanged" && params.targetInfo && params.targetInfo.url) ||
    "";
  const check = candidateUrl
    ? permissionFor(candidateUrl)
    : eventSourceUrl(source).then((u) => (u ? permissionFor(u) : "ask"));
  check
    .then((state) => {
      if (!port) return;
      if (state === "allow") {
        postBridge({ method, params, sessionId });
        return;
      }
      if (method === "Page.frameNavigated") {
        const frame = { ...(params.frame || {}) };
        frame.url = originOf(frame.url || "");
        frame.name = "";
        postBridge({ method, params: { ...params, frame }, sessionId });
      } else if (method === "Page.navigatedWithinDocument") {
        postBridge({ method, params: { ...params, url: originOf(params.url || "") }, sessionId });
      } else if (method === "Page.javascriptDialogOpening") {
        postBridge({
          method,
          params: {
            ...params,
            url: originOf(params.url || ""),
            message: "",
            defaultPrompt: "",
          },
          sessionId,
        });
      } else if (method === "Target.targetInfoChanged") {
        const targetInfo = { ...(params.targetInfo || {}) };
        targetInfo.url = originOf(targetInfo.url || "");
        targetInfo.title = "";
        postBridge({ method, params: { ...params, targetInfo }, sessionId });
      }
    })
    .catch(() => {});
}

chrome.debugger.onEvent.addListener((source, method, params) => {
  if (source.tabId === undefined) return;
  const parentTabId = source.tabId;
  const safeParams = params || {};
  if (method === "Target.targetInfoChanged") {
    const info = safeParams.targetInfo;
    if (info && info.targetId && info.url) {
      // In CDP flat sessions, Target.targetInfoChanged arrives on the parent
      // session, so match by targetId across tracked child sessions.
      for (const child of sessions.values()) {
        if (child.tabId === parentTabId && child.targetId === info.targetId) {
          child.url = info.url;
        }
      }
    }
  }
  if (method === "Target.attachedToTarget") {
    const info = safeParams.targetInfo || {};
    const child = safeParams.sessionId;
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
    postBridge({ method, params: safeParams, sessionId: source.sessionId || null });
    return;
  }
  if (method === "Target.detachedFromTarget") {
    sessions.delete(safeParams.sessionId);
  }
  if (method === "Fetch.requestPaused") {
    if (!port) {
      if (safeParams.requestId) {
        chrome.debugger
          .sendCommand(source, "Fetch.failRequest", {
            requestId: safeParams.requestId,
            errorReason: "BlockedByClient",
          })
          .catch(() => {});
      }
      return;
    }
    forwardFetchRequestPaused(source, safeParams, parentTabId);
    return;
  }
  if (!port) return;
  if (isGatedPageEvent(method)) {
    forwardPageLogEvent(source, method, safeParams, parentTabId);
    return;
  }
  if (REDACTED_URL_EVENTS.has(method)) {
    forwardWithRedaction(source, method, safeParams, parentTabId);
    return;
  }
  if (PASS_THROUGH_EVENTS.has(method)) {
    postBridge({
      method,
      params: safeParams,
      sessionId: source.sessionId || String(parentTabId),
    });
  }
  // Unlisted events (Network.responseReceivedExtraInfo, Log.entryAdded, etc.) are dropped.
});

chrome.debugger.onDetach.addListener((source, reason) => {
  if (source.tabId !== undefined) {
    forgetSessions(source.tabId);
    if (closingTabs.has(source.tabId) || reason === "target_closed") {
      return;
    }
    postBridge({
      method: "Inspector.detached",
      params: { reason: reason || "detached" },
      sessionId: String(source.tabId),
    });
  }
});

if (chrome.webNavigation && chrome.webNavigation.onCommitted) {
  chrome.webNavigation.onCommitted.addListener(async ({ tabId, frameId, url }) => {
    // No plugin connected: the tab is the user's again, never hijack it.
    if (!port || !ownedTabs.has(tabId) || isSafeBlankUrl(url)) return;
    const state = await permissionFor(url);
    if (state === "allow") return;
    if (frameId !== 0) {
      // Subframe (e.g. ad/YouTube/SSO iframe): never detach or drop the main tab.
      // Subframe network/child sessions stay isolated by Fetch & assertSessionPermission.
      return;
    }
    try {
      if (chrome.debugger && chrome.debugger.detach) {
        await chrome.debugger.detach({ tabId });
      }
    } catch {
      // Tab may already be detached.
    }
    forgetSessions(tabId);
    if (chrome.tabs && chrome.tabs.update) {
      try {
        await chrome.tabs.update(tabId, { url: "about:blank" });
      } catch {
        // Tab may already be closing.
      }
    }
    postBridge({
      method: "Bridge.siteBlocked",
      params: { tabId: String(tabId), origin: originOf(url) || stripQueryAndFragment(url) },
    });
  });
}

if (chrome.storage && chrome.storage.onChanged) {
  chrome.storage.onChanged.addListener((changes, areaName) => {
    if (areaName !== "local") return;
    if (changes.sitePermissions || changes.defaultPermission || changes.userAllowsPublicWeb) {
      void enqueueDnrSync();
    }
  });
}

if (chrome.webNavigation && chrome.webNavigation.onCreatedNavigationTarget) {
  chrome.webNavigation.onCreatedNavigationTarget.addListener((details) => {
    if (!details) return;
    void adoptPopupTab({ tabId: details.tabId, openerTabId: details.sourceTabId, url: details.url || "" });
  });
}

if (chrome.tabs && chrome.tabs.onCreated) {
  chrome.tabs.onCreated.addListener((tab) => {
    if (!tab) return;
    void adoptPopupTab({ tabId: tab.id, openerTabId: tab.openerTabId, url: tab.pendingUrl || tab.url || "" });
  });
}

chrome.tabs.onRemoved.addListener((tabId) => {
  const wasOwned = ownedTabs.has(tabId);
  const wasClosing = closingTabs.has(tabId);
  void forgetOwned(tabId);
  forgetSessions(tabId);
  if (wasOwned && !wasClosing && port) {
    postBridge({
      method: "Target.detachedFromTarget",
      params: { sessionId: String(tabId), targetId: String(tabId) },
    });
  }
});

chrome.runtime.onStartup.addListener(async () => {
  // A browser restart (including after a crash) ends every plugin lease; a
  // persisted public-web flag from the previous run must not survive it.
  try {
    await chrome.storage.local.remove("bridgePublicWeb");
  } catch {
    // Best effort; permissionFor still requires the user's public-web opt-in.
  }
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
