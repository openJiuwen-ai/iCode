// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

// Site permissions and strict CDP allowlist for the iCode browser bridge.
//
// The plugin checks network destinations. This layer adds a user-controlled
// per-origin gate for acting on a tab: plugin-supplied allowlists can only
// narrow the user's grants, and public-web mode requires an explicit user opt-in
// in the extension options page (and always excludes loopback/private hosts).

export const DEFAULT_PERMISSION = "ask";
export const MUTATION_PROBE_SHA256 =
  "5d8b2f946c54c03f99bdaf7f40fd3712c0b78425bd5ea0298ef3b05b89743018";

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

export function stripQueryAndFragment(url) {
  if (!url) return "";
  try {
    const parsed = new URL(url);
    if (parsed.protocol === "http:" || parsed.protocol === "https:") {
      return `${parsed.origin}${parsed.pathname}`;
    }
    return `${parsed.protocol}`;
  } catch {
    return String(url || "").split(/[?#]/, 1)[0];
  }
}

function isIpv4NonPublic(host) {
  const parts = host.split(".");
  if (parts.length !== 4 || !parts.every((p) => /^\d+$/.test(p))) return false;
  const nums = parts.map((p) => Number(p));
  if (nums.some((n) => n < 0 || n > 255)) return true;
  const [a, b] = nums;
  if (a === 0 || a === 10 || a === 127) return true;
  if (a === 169 && b === 254) return true; // link-local / cloud metadata
  if (a === 172 && b >= 16 && b <= 31) return true;
  if (a === 192 && b === 168) return true;
  if (a === 192 && b === 0 && (nums[2] === 0 || nums[2] === 2)) return true; // IETF / TEST-NET-1
  if (a === 198 && (b === 18 || b === 19)) return true; // benchmarking RFC 2544
  if (a === 198 && b === 51 && nums[2] === 100) return true; // TEST-NET-2
  if (a === 203 && b === 0 && nums[2] === 113) return true; // TEST-NET-3
  if (a === 100 && b >= 64 && b <= 127) return true; // CGNAT
  if (a >= 224) return true; // multicast / reserved
  return false;
}

function embeddedIpv4NonPublic(tail) {
  if (isIpv4NonPublic(tail)) return true;
  const hexParts = tail.split(":");
  if (hexParts.length === 2 && hexParts.every((p) => /^[0-9a-f]{1,4}$/.test(p))) {
    const hi = parseInt(hexParts[0], 16);
    const lo = parseInt(hexParts[1], 16);
    const dotted = `${(hi >> 8) & 255}.${hi & 255}.${(lo >> 8) & 255}.${lo & 255}`;
    return isIpv4NonPublic(dotted);
  }
  return true;
}

export function isNonPublicHost(rawHost) {
  let host = String(rawHost || "").trim().toLowerCase().replace(/\.$/, "");
  if (!host) return true;
  if (host.startsWith("[") && host.endsWith("]")) host = host.slice(1, -1);
  if (
    host === "localhost" ||
    host.endsWith(".localhost") ||
    host.endsWith(".local") ||
    host.endsWith(".internal")
  ) {
    return true;
  }
  if (host.includes(":")) {
    // IPv6 literal: exclude loopback (::1), unspecified (::), link-local (fe80::/10),
    // ULA (fc00::/7), IPv4-mapped (::ffff:...), IPv4-compatible (::x.x.x.x / ::xxxx:xxxx),
    // and NAT64 well-known prefix (64:ff9b::/96) non-public addresses.
    const compact = host.toLowerCase();
    if (compact === "::1" || compact === "::" || compact === "0:0:0:0:0:0:0:1" || compact === "0:0:0:0:0:0:0:0") {
      return true;
    }
    if (compact.startsWith("::ffff:")) {
      return embeddedIpv4NonPublic(compact.slice("::ffff:".length));
    }
    if (compact.startsWith("64:ff9b::")) {
      return embeddedIpv4NonPublic(compact.slice("64:ff9b::".length));
    }
    if (compact.startsWith("::") && compact.length > 2) {
      // Deprecated IPv4-compatible IPv6 (e.g. ::7f00:1 or ::127.0.0.1)
      return embeddedIpv4NonPublic(compact.slice(2));
    }
    const first = compact.split(":", 1)[0];
    if (/^[0-9a-f]{1,4}$/.test(first)) {
      const val = parseInt(first, 16);
      if ((val & 0xfe00) === 0xfc00) return true; // fc00::/7 ULA
      if ((val & 0xffc0) === 0xfe80) return true; // fe80::/10 link-local
      if ((val & 0xff00) === 0xff00) return true; // ff00::/8 multicast
    }
    return false;
  }
  if (/^\d+(\.\d+)*$/.test(host)) {
    return isIpv4NonPublic(host) || !host.includes(".");
  }
  // Single-label internal hostnames (e.g. "intranet") have no dot.
  if (!host.includes(".")) return true;
  return false;
}

export function domainMatchesHost(rawHost, rawDomain) {
  const host = String(rawHost || "").trim().toLowerCase().replace(/\.$/, "");
  const clean = String(rawDomain || "").trim().toLowerCase().replace(/\.$/, "");
  if (!host || !clean) return false;
  const subOnly = clean.startsWith(".");
  const base = subOnly ? clean.slice(1) : clean;
  if (!base) return false;
  return subOnly ? host.endsWith(`.${base}`) : host === base || host.endsWith(`.${base}`);
}

function normaliseAllowedOrigin(entry) {
  const parsed = originOf(entry);
  return parsed || String(entry || "").trim().toLowerCase();
}

export async function readPermissions(storage = chrome.storage.local) {
  const data = await storage.get({ sitePermissions: {} });
  return data.sitePermissions || {};
}

export async function readDefaultPolicy(storage = chrome.storage.local) {
  const data = await storage.get({ defaultPermission: DEFAULT_PERMISSION });
  return data.defaultPermission === "allow" ? "allow" : "ask";
}

export async function readUserAllowsPublicWeb(storage = chrome.storage.local) {
  const data = await storage.get({ userAllowsPublicWeb: false });
  return data.userAllowsPublicWeb === true;
}

export async function setUserAllowsPublicWeb(enabled, storage = chrome.storage.local) {
  await storage.set({ userAllowsPublicWeb: enabled === true });
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
    userAllowsPublicWeb: false,
    bridgePublicWeb: false,
    bridgeAllowedDomains: [],
    bridgeAllowedOrigins: [],
  });
  const permissions = data.sitePermissions || {};
  const explicit = permissions[origin];
  if (explicit === "block") return "block";
  if (explicit === "ask") return "ask";

  const host = new URL(url).hostname.toLowerCase();
  const domains = Array.isArray(data.bridgeAllowedDomains) ? data.bridgeAllowedDomains : [];
  const origins = Array.isArray(data.bridgeAllowedOrigins) ? data.bridgeAllowedOrigins : [];
  const hasPluginNarrowing = domains.length > 0 || origins.length > 0;
  if (hasPluginNarrowing) {
    const originMatched = origins.some((allowed) => normaliseAllowedOrigin(allowed) === origin);
    const domainMatched = domains.some((domain) => domainMatchesHost(host, domain));
    if (!originMatched && !domainMatched) {
      return "ask";
    }
  }

  if (explicit === "allow") return "allow";
  const activePublicWeb = bridgePublicWeb || data.bridgePublicWeb === true;
  if (activePublicWeb && data.userAllowsPublicWeb === true && !isNonPublicHost(host)) {
    return "allow";
  }
  if (data.defaultPermission === "allow" && !isNonPublicHost(host)) {
    return "allow";
  }
  return "ask";
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
  // Persist explicit "ask" so it overrides userAllowsPublicWeb and defaultPermission.
  permissions[origin] = state;
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
    userAllowsPublicWeb: false,
    permissionHistory: [],
  });
  return {
    version: 1,
    defaultPermission: data.defaultPermission === "allow" ? "allow" : "ask",
    userAllowsPublicWeb: data.userAllowsPublicWeb === true,
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
    permissions[origin] = state;
  }
  const defaultPermission = payload.defaultPermission === "allow" ? "allow" : "ask";
  const userAllowsPublicWeb = payload.userAllowsPublicWeb === true;
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
  await storage.set({
    sitePermissions: permissions,
    defaultPermission,
    userAllowsPublicWeb,
    permissionHistory: history,
  });
  return Object.keys(permissions).length;
}

// Strict CDP command allowlist for Bridge mode.
// Methods not listed here (Storage.*, Network.getAllCookies, Browser.*,
// Target.sendMessageToTarget, Debugger.*, IO.*, Extensions.*, etc.) are
// rejected even on about:blank.
export const METHOD_PLUMBING = "plumbing";
export const METHOD_SITE = "site";
export const METHOD_FETCH_ENABLE = "fetch_enable";
export const METHOD_FETCH_CONTINUE = "fetch_continue";
export const METHOD_NETWORK_COOKIES = "network_cookies";
export const METHOD_PROBE_SCRIPT = "probe_script";

export const BRIDGE_METHODS = new Map([
  // Plumbing (no page content exposed; safe while arming a tab or enforcing policy)
  ["Runtime.enable", METHOD_PLUMBING],
  ["Runtime.runIfWaitingForDebugger", METHOD_PLUMBING],
  ["Runtime.releaseObjectGroup", METHOD_PLUMBING],
  ["Page.enable", METHOD_PLUMBING],
  ["Page.setLifecycleEventsEnabled", METHOD_PLUMBING],
  ["DOM.enable", METHOD_PLUMBING],
  ["Accessibility.enable", METHOD_PLUMBING],
  ["Network.enable", METHOD_PLUMBING],
  ["Network.setBlockedURLs", METHOD_PLUMBING],
  ["Fetch.disable", METHOD_PLUMBING],
  ["Fetch.failRequest", METHOD_PLUMBING],
  ["Target.detachFromTarget", METHOD_PLUMBING],
  // Restricted custom validators
  ["Fetch.enable", METHOD_FETCH_ENABLE],
  ["Fetch.continueRequest", METHOD_FETCH_CONTINUE],
  ["Network.getCookies", METHOD_NETWORK_COOKIES],
  ["Page.addScriptToEvaluateOnNewDocument", METHOD_PROBE_SCRIPT],
  // Site-scoped commands (require active tab/child session permission)
  ["Runtime.evaluate", METHOD_SITE],
  ["Runtime.callFunctionOn", METHOD_SITE],
  ["DOM.resolveNode", METHOD_SITE],
  ["DOM.getBoxModel", METHOD_SITE],
  ["DOM.getContentQuads", METHOD_SITE],
  ["DOM.getNodeForLocation", METHOD_SITE],
  ["DOM.getFrameOwner", METHOD_SITE],
  ["DOM.scrollIntoViewIfNeeded", METHOD_SITE],
  ["DOM.setFileInputFiles", METHOD_SITE],
  ["DOMSnapshot.captureSnapshot", METHOD_SITE],
  ["Accessibility.getFullAXTree", METHOD_SITE],
  ["Accessibility.getPartialAXTree", METHOD_SITE],
  ["Input.dispatchMouseEvent", METHOD_SITE],
  ["Input.dispatchKeyEvent", METHOD_SITE],
  ["Input.insertText", METHOD_SITE],
  ["Input.setInterceptDrags", METHOD_SITE],
  ["Input.dispatchDragEvent", METHOD_SITE],
  ["Network.getResponseBody", METHOD_SITE],
  ["Page.captureScreenshot", METHOD_SITE],
  ["Page.getLayoutMetrics", METHOD_SITE],
  ["Page.getFrameTree", METHOD_SITE],
  ["Page.getNavigationHistory", METHOD_SITE],
  ["Page.reload", METHOD_SITE],
  ["Page.handleJavaScriptDialog", METHOD_SITE],
  ["Page.setDownloadBehavior", METHOD_SITE],
]);

export function methodPolicy(method) {
  if (!method) return null;
  return BRIDGE_METHODS.get(method) || null;
}

export function needsPermission(method) {
  if (!method) return false;
  const policy = BRIDGE_METHODS.get(method);
  if (
    policy === METHOD_PLUMBING ||
    policy === METHOD_FETCH_ENABLE ||
    policy === METHOD_FETCH_CONTINUE ||
    method === "Target.setAutoAttach"
  ) {
    return false;
  }
  return true;
}
