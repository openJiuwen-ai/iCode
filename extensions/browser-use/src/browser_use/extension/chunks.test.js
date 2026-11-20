// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import test from "node:test";
import assert from "node:assert/strict";

import { BridgeChunkReceiver, encodeBridgeMessages } from "./chunks.js";

test("small payloads pass through unchanged", () => {
  const payload = { id: 1, result: { ok: true } };
  assert.deepEqual(encodeBridgeMessages(payload), [payload]);
});

test("large payloads split into native-message-safe chunks and reassemble", () => {
  const payload = { id: 7, result: { blob: "x".repeat(1_400_000) } };
  const frames = encodeBridgeMessages(payload);
  assert.ok(frames.length > 1);
  for (const frame of frames) {
    assert.ok(Buffer.byteLength(JSON.stringify(frame), "utf8") < 1_048_576);
  }

  const receiver = new BridgeChunkReceiver();
  let restored = null;
  for (const frame of frames.slice().reverse()) {
    restored = receiver.add(frame);
  }
  assert.deepEqual(restored, payload);
});

test("chunk ids are unique per message", () => {
  const first = encodeBridgeMessages({ blob: "a".repeat(700_000) });
  const second = encodeBridgeMessages({ blob: "b".repeat(700_000) });
  assert.notEqual(first[0].__bridge_chunk.id, second[0].__bridge_chunk.id);
});

test("invalid chunks are rejected", () => {
  const receiver = new BridgeChunkReceiver();
  assert.throws(() => receiver.add({ __bridge_chunk: { id: "x", index: 0, total: 0 }, data: "" }));
  assert.throws(() => receiver.add({ __bridge_chunk: { id: "x", index: 1, total: 1 }, data: "" }));
});
