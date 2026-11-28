// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import { readPermissions } from "./permissions.js";

const status = document.getElementById("status");
const retry = document.getElementById("retry");
const manage = document.getElementById("manage");
const copy = document.getElementById("copy");
const prompt = document.getElementById("sample-prompt");
const SAMPLE_PROMPT =
  "Use the browser extension to open https://example.com, take a snapshot, and tell me the page title.";
let copied = false;
let statusHoldUntil = 0;

prompt.textContent = SAMPLE_PROMPT;

function setStatus(text, ok, holdMs = 0) {
  status.className = `status ${ok ? "ok" : "wait"}`;
  status.textContent = text;
  if (holdMs > 0) statusHoldUntil = Date.now() + holdMs;
}

function mark(id, done) {
  document.getElementById(id).classList.toggle("done", done);
}

async function refresh() {
  if (Date.now() >= statusHoldUntil) setStatus("Checking the bridge…", false);
  let connected = false;
  try {
    const result = await chrome.runtime.sendMessage({ type: "bridge_status" });
    connected = Boolean(result && result.connected);
  } catch {
    connected = false;
  }
  let allowed = false;
  try {
    const permissions = await readPermissions();
    allowed = Object.values(permissions).includes("allow");
  } catch {
    allowed = false;
  }
  if (Date.now() >= statusHoldUntil) {
    setStatus(connected ? "Connected to iCode" : "Waiting for iCode to start the bridge", connected);
  }
  mark("check-bridge", connected);
  mark("check-site", allowed);
  mark("check-copy", copied);
}

copy.addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(SAMPLE_PROMPT);
    copied = true;
    await refresh();
    setStatus("Prompt copied. Paste it into iCode.", true, 5000);
  } catch {
    const range = document.createRange();
    range.selectNodeContents(prompt);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    await refresh();
    setStatus("Select the highlighted prompt and copy it manually.", false, 5000);
  }
});

retry.addEventListener("click", refresh);
manage.addEventListener("click", () => chrome.runtime.openOptionsPage());
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refresh();
});

refresh();
setInterval(refresh, 3000);
