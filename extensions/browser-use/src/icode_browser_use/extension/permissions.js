// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

// Site permissions for the iCode browser bridge.
//
// The plugin checks network destinations. This layer adds a per-origin user
// gate for acting on a tab, including explicit blocks during public-web mode.

export const DEFAULT_PERMISSION = "ask";
const STATES = new Set(["allow", "ask", "block"]);
let bridgePublicWeb = false;

export function setBridgePublicWeb(enabled) {
  bridgePublicWeb = enabled === true;
}

export function originOf(url) {
  if (!url) return "";
  try {
    const parsed = new URL(url);
    if (parsed.protocol === "http:" || parsed.protocol === "https:") return parsed.origin;
    return "";
  } catch {
    return "";
  }
}

export async function readPermissions(storage = chrome.storage.local) {
  const data = await storage.get({ sitePermissions: {} });
  return data.sitePermissions || {};
}

export async function readDefaultPolicy(storage = chrome.storage.local) {
  const data = await storage.get({ defaultPermission: DEFAULT_PERMISSION });
  return data.defaultPermission === "allow" ? "allow" : "ask";
}

export async function readHistory(storage = chrome.storage.local) {
  const data = await storage.get({ permissionHistory: [] });
  return Array.isArray(data.permissionHistory) ? data.permissionHistory : [];
}

export function isSafeBlankUrl(url) {
  const clean = String(url || "").trim().toLowerCase();
  return clean === "" || clean === "about:blank" || clean === "about:srcdoc";
}

export async function permissionFor(url, storage = chrome.storage.local) {
  const origin = originOf(url);
  if (!origin) return isSafeBlankUrl(url) ? "allow" : "block";
  const data = await storage.get({
    sitePermissions: {},
    defaultPermission: DEFAULT_PERMISSION,
    bridgeAllowedDomains: [],
    bridgeAllowedOrigins: [],
  });
  const permissions = data.sitePermissions || {};
  const explicit = permissions[origin];
  if (explicit) return explicit;
  if (bridgePublicWeb) return "allow";
  // Exact loopback origins the plugin authorised for local development
  // (``scheme://host:port``); a different port is a different origin.
  const origins = Array.isArray(data.bridgeAllowedOrigins) ? data.bridgeAllowedOrigins : [];
  if (origins.some((allowed) => String(allowed || "").toLowerCase() === origin)) {
    return "allow";
  }
  const host = new URL(url).hostname.toLowerCase();
  const domains = Array.isArray(data.bridgeAllowedDomains) ? data.bridgeAllowedDomains : [];
  if (
    domains.some((domain) => {
      const clean = String(domain || "").toLowerCase();
      return clean && (host === clean || host.endsWith(`.${clean}`));
    })
  ) {
    return "allow";
  }
  return data.defaultPermission === "allow" ? "allow" : "ask";
}

export function permissionError(url, state) {
  const origin = originOf(url) || String(url || "").split(/[?#]/, 1)[0] || "<unknown>";
  if (state === "block") throw new Error(`site_blocked: ${origin}`);
  throw new Error(
    `site_permission_required: ${origin} (open the iCode Browser Bridge extension and allow this site)`
  );
}

export async function assertPermission(url, storage = chrome.storage.local) {
  const origin = originOf(url);
  if (!origin) {
    if (isSafeBlankUrl(url)) return;
    permissionError(url, "block");
  }
  const state = await permissionFor(url, storage);
  if (state !== "allow") permissionError(url, state);
}

export async function setPermission(origin, state, storage = chrome.storage.local) {
  if (!STATES.has(state)) throw new Error(`invalid permission state: ${state}`);
  const data = await storage.get({ sitePermissions: {}, permissionHistory: [] });
  const permissions = { ...(data.sitePermissions || {}) };
  const history = Array.isArray(data.permissionHistory) ? data.permissionHistory : [];
  if (state === "ask") delete permissions[origin];
  else permissions[origin] = state;
  history.unshift({ origin, state, at: new Date().toISOString() });
  await storage.set({ sitePermissions: permissions, permissionHistory: history.slice(0, 50) });
}

export async function setDefaultPolicy(state, storage = chrome.storage.local) {
  if (state !== "allow" && state !== "ask") throw new Error(`invalid default policy: ${state}`);
  await storage.set({ defaultPermission: state });
}

export async function exportPermissions(storage = chrome.storage.local) {
  const data = await storage.get({
    sitePermissions: {},
    defaultPermission: DEFAULT_PERMISSION,
    permissionHistory: [],
  });
  return {
    version: 1,
    defaultPermission: data.defaultPermission === "allow" ? "allow" : "ask",
    sitePermissions: { ...(data.sitePermissions || {}) },
    permissionHistory: Array.isArray(data.permissionHistory) ? data.permissionHistory.slice(0, 50) : [],
  };
}

export async function importPermissions(payload, storage = chrome.storage.local) {
  if (!payload || payload.version !== 1) throw new Error("unsupported permissions export");
  const permissions = {};
  for (const [origin, state] of Object.entries(payload.sitePermissions || {})) {
    if (originOf(origin) !== origin) throw new Error(`invalid origin: ${origin}`);
    if (!STATES.has(state)) throw new Error(`invalid state for ${origin}`);
    if (state !== "ask") permissions[origin] = state;
  }
  const defaultPermission = payload.defaultPermission === "allow" ? "allow" : "ask";
  const history = Array.isArray(payload.permissionHistory)
    ? payload.permissionHistory
        .filter(
          (entry) =>
            entry &&
            originOf(entry.origin) === entry.origin &&
            STATES.has(entry.state) &&
            typeof entry.at === "string"
        )
        .slice(0, 50)
    : [];
  await storage.set({ sitePermissions: permissions, defaultPermission, permissionHistory: history });
  return Object.keys(permissions).length;
}

// Strict allowlist of CDP plumbing commands that do not expose page content
// and may run while arming a tab or enforcing network policy. Every other
// method (including Fetch.getResponseBody, Target.getTargets, Browser.*,
// Storage.*, etc.) requires active site permission or is explicitly refused.
const POLICY_SAFE_METHODS = new Set([
  "Fetch.enable",
  "Fetch.disable",
  "Fetch.continueRequest",
  "Fetch.failRequest",
  "Fetch.continueResponse",
  "Target.setAutoAttach",
  "Target.detachFromTarget",
  "Runtime.enable",
  "Runtime.runIfWaitingForDebugger",
  "DOM.enable",
  "Accessibility.enable",
  "Page.enable",
  "Page.setLifecycleEventsEnabled",
  "Network.enable",
  "Network.setBlockedURLs",
  "Network.setExtraHTTPHeaders",
]);

export function needsPermission(method) {
  if (!method) return false;
  if (POLICY_SAFE_METHODS.has(method)) return false;
  // Fail closed: unknown/new CDP methods (Storage.getCookies, Network.getAllCookies,
  // Fetch.getResponseBody, Target.getTargets, Browser.*, ...) require the active site permission.
  return true;
}
