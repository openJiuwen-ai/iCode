// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

const CHUNK_BYTES = 500_000;
const THRESHOLD = 600_000;
const MAX_CHUNKS = 128;
const MAX_TOTAL = 64 << 20;
const MAX_PENDING_CHUNKS = 8;
const MAX_BUFFERED_CHARS = Math.floor((MAX_TOTAL * 4) / 3) + 4096;

function randomId() {
  if (crypto.randomUUID) return crypto.randomUUID();
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), (b) => b.toString(16).padStart(2, "0")).join("");
}

function bytesToBase64(bytes) {
  let binary = "";
  for (let offset = 0; offset < bytes.length; offset += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
  }
  return btoa(binary);
}

function base64ToBytes(text) {
  const binary = atob(text);
  const out = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) out[index] = binary.charCodeAt(index);
  return out;
}

export function encodeBridgeMessages(payload) {
  const body = new TextEncoder().encode(JSON.stringify(payload));
  if (body.length <= THRESHOLD) return [payload];
  const total = Math.ceil(body.length / CHUNK_BYTES);
  if (total > MAX_CHUNKS) throw new Error("bridge message is too large");
  const id = randomId();
  const frames = [];
  for (let index = 0; index < total; index += 1) {
    frames.push({
      __bridge_chunk: { id, index, total },
      data: bytesToBase64(body.subarray(index * CHUNK_BYTES, (index + 1) * CHUNK_BYTES)),
    });
  }
  return frames;
}

export class BridgeChunkReceiver {
  constructor() {
    this._parts = new Map();
    this._bufferedChars = 0;
  }

  clear() {
    this._parts.clear();
    this._bufferedChars = 0;
  }

  _drop(id) {
    const entry = this._parts.get(id);
    if (!entry) return;
    this._parts.delete(id);
    this._bufferedChars = Math.max(0, this._bufferedChars - entry.chars);
  }

  add(message) {
    const meta = message && message.__bridge_chunk;
    if (!meta) return message;
    const id = typeof meta.id === "string" ? meta.id : "";
    const index = Number(meta.index);
    const total = Number(meta.total);
    if (
      !id ||
      !Number.isInteger(index) ||
      !Number.isInteger(total) ||
      total < 1 ||
      total > MAX_CHUNKS ||
      index < 0 ||
      index >= total ||
      typeof message.data !== "string"
    ) {
      throw new Error("invalid bridge chunk");
    }
    let entry = this._parts.get(id);
    if (!entry) {
      if (this._parts.size >= MAX_PENDING_CHUNKS) {
        throw new Error("too many concurrent incomplete bridge chunk messages");
      }
      entry = { parts: new Array(total).fill(null), total, chars: 0 };
      this._parts.set(id, entry);
    }
    if (entry.total !== total) {
      this._drop(id);
      throw new Error("bridge chunk total changed");
    }
    if (entry.parts[index] !== null) {
      this._drop(id);
      throw new Error("duplicate bridge chunk index");
    }
    if (this._bufferedChars + message.data.length > MAX_BUFFERED_CHARS) {
      this._drop(id);
      throw new Error("buffered bridge chunks exceed size cap");
    }
    entry.parts[index] = message.data;
    entry.chars += message.data.length;
    this._bufferedChars += message.data.length;
    if (entry.parts.some((part) => part === null)) return null;

    this._drop(id);
    const decoded = entry.parts.map((part) => base64ToBytes(part));
    const length = decoded.reduce((sum, bytes) => sum + bytes.length, 0);
    if (length > MAX_TOTAL) throw new Error("bridge message is too large");
    const bytes = new Uint8Array(length);
    let offset = 0;
    for (const part of decoded) {
      bytes.set(part, offset);
      offset += part.length;
    }
    return JSON.parse(new TextDecoder().decode(bytes));
  }
}
