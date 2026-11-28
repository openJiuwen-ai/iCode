// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import {
  exportPermissions,
  importPermissions,
  originOf,
  readDefaultPolicy,
  readHistory,
  readPermissions,
  readUserAllowsPublicWeb,
  setDefaultPolicy,
  setPermission,
  setUserAllowsPublicWeb,
} from "./permissions.js";

const LABELS = { allow: "Allowed", ask: "Ask", block: "Blocked" };
const rows = document.getElementById("rows");
const empty = document.getElementById("empty");
const status = document.getElementById("status");
const historyList = document.getElementById("history");
const historyEmpty = document.getElementById("history-empty");
const defaultPolicy = document.getElementById("default-policy");
const allowPublicWeb = document.getElementById("allow-public-web");
const originInput = document.getElementById("origin");
const importInput = document.getElementById("import");

function setStatus(text) {
  status.textContent = text;
}

function typedOrigin() {
  const text = originInput.value.trim();
  if (!text) return "";
  return originOf(text.includes("://") ? text : `https://${text}`);
}

async function render() {
  const [permissions, history, defaultState, publicWebEnabled] = await Promise.all([
    readPermissions(),
    readHistory(),
    readDefaultPolicy(),
    readUserAllowsPublicWeb(),
  ]);
  defaultPolicy.value = defaultState;
  if (allowPublicWeb) allowPublicWeb.checked = publicWebEnabled;

  const origins = Object.keys(permissions).sort();
  rows.replaceChildren();
  empty.hidden = origins.length > 0;
  for (const origin of origins) {
    const state = permissions[origin];
    const row = document.createElement("tr");

    const name = document.createElement("td");
    name.className = "origin";
    name.textContent = origin;

    const stateCell = document.createElement("td");
    const badge = document.createElement("span");
    badge.className = `badge ${state}`;
    badge.textContent = LABELS[state] || state;
    stateCell.append(badge);

    const actions = document.createElement("td");
    actions.className = "actions";
    for (const [value, label] of [
      ["allow", "Allow"],
      ["ask", "Ask"],
      ["block", "Block"],
    ]) {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = label;
      button.dataset.origin = origin;
      button.dataset.state = value;
      button.className = state === value ? "active" : "";
      actions.append(button);
    }

    row.append(name, stateCell, actions);
    rows.append(row);
  }

  historyList.replaceChildren();
  const recent = history.slice(0, 20);
  historyEmpty.hidden = recent.length > 0;
  for (const entry of recent) {
    const item = document.createElement("li");
    item.textContent = `${entry.at} — ${entry.origin} → ${entry.state}`;
    historyList.append(item);
  }
}

rows.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-origin]");
  if (!button) return;
  await setPermission(button.dataset.origin, button.dataset.state);
  setStatus(`Updated ${button.dataset.origin}`);
  await render();
});

async function addPermission(state) {
  const origin = typedOrigin();
  if (!origin) {
    setStatus("Enter an http(s) origin first.");
    return;
  }
  await setPermission(origin, state);
  originInput.value = "";
  setStatus(`Saved ${origin}`);
  await render();
}

document.getElementById("add-allow").addEventListener("click", () => addPermission("allow"));
document.getElementById("add-block").addEventListener("click", () => addPermission("block"));

defaultPolicy.addEventListener("change", async () => {
  await setDefaultPolicy(defaultPolicy.value);
  setStatus(`Default policy: ${defaultPolicy.value}`);
});

if (allowPublicWeb) {
  allowPublicWeb.addEventListener("change", async () => {
    await setUserAllowsPublicWeb(allowPublicWeb.checked);
    setStatus(`Public-web mode: ${allowPublicWeb.checked ? "enabled" : "disabled"}`);
  });
}

document.getElementById("export").addEventListener("click", async () => {
  const payload = await exportPermissions();
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = "icode-browser-use-permissions.json";
  document.body.append(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
  setStatus("Exported permissions.");
});

importInput.addEventListener("change", async () => {
  const file = importInput.files?.[0];
  if (!file) return;
  try {
    const payload = JSON.parse(await file.text());
    const count = await importPermissions(payload);
    setStatus(`Imported ${count} site override(s).`);
    await render();
  } catch (error) {
    setStatus(`Import failed: ${error.message}`);
  } finally {
    importInput.value = "";
  }
});

document.getElementById("clear").addEventListener("click", async () => {
  if (!confirm("Clear all site permissions and history?")) return;
  await chrome.storage.local.set({ sitePermissions: {}, permissionHistory: [] });
  setStatus("Cleared all site permissions.");
  await render();
});

await render();
