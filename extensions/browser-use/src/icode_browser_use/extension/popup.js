// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import { originOf, permissionFor, readPermissions, setPermission } from "./permissions.js";

const LABELS = { allow: "Allowed", ask: "Ask", block: "Blocked" };
const rows = document.getElementById("rows");
const empty = document.getElementById("empty");

async function render() {
  const [tabs, permissions] = await Promise.all([chrome.tabs.query({}), readPermissions()]);
  const origins = [
    ...new Set([...tabs.map((tab) => originOf(tab.url)).filter(Boolean), ...Object.keys(permissions)]),
  ].sort();
  rows.replaceChildren();
  empty.hidden = origins.length > 0;

  for (const origin of origins) {
    const state = await permissionFor(origin);
    const row = document.createElement("div");
    row.className = "row";

    const name = document.createElement("span");
    name.className = "origin";
    name.textContent = origin;

    const badge = document.createElement("span");
    badge.className = `badge ${state}`;
    badge.textContent = LABELS[state];

    const actions = document.createElement("div");
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

    row.append(name, badge, actions);
    rows.append(row);
  }
}

rows.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-origin]");
  if (!button) return;
  await setPermission(button.dataset.origin, button.dataset.state);
  await render();
});

const originInput = document.getElementById("origin");

function typedOrigin() {
  const text = originInput.value.trim();
  if (!text) return "";
  return originOf(text.includes("://") ? text : `https://${text}`);
}

async function addPermission(state) {
  const origin = typedOrigin();
  if (!origin) return;
  await setPermission(origin, state);
  originInput.value = "";
  await render();
}

document.getElementById("add-allow").addEventListener("click", () => addPermission("allow"));
document.getElementById("add-block").addEventListener("click", () => addPermission("block"));
document.getElementById("manage").addEventListener("click", () => chrome.runtime.openOptionsPage());

await render();
