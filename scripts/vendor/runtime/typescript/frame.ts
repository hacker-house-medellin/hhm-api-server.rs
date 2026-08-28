/**
 * The ridl frame envelope: HTTP-free addressing for WebSocket and TCP.
 *
 * A port of `ridl/framing.py`, pinned to it by the fixtures under
 * `examples/frames/`. Encoding lives in one normative place on purpose -- the
 * moment each language frames a call from prose instead of a spec, they drift,
 * which is exactly how the Rust and TypeScript opto-sync runtimes ended up
 * minting different record ids for the same request.
 *
 * Canonical rules: UTF-8 JSON, compact separators, a fixed member order (not
 * alphabetical, not insertion order), an absent value is an omitted member
 * rather than `null`, non-ASCII emitted literally.
 */

export const FRAME_VERSION = 1 as const;
export const MAX_FRAME_BYTES = 8 * 1024 * 1024;
export const LENGTH_PREFIX_BYTES = 4;

/** Member order on the wire. Fixed, so two ports produce identical bytes. */
const FIELD_ORDER = [
  "v", "id", "t", "key", "method", "path", "query", "body", "code", "message", "meta",
] as const;

export type FrameKind = "call" | "data" | "end" | "error" | "cancel";

export class FrameError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "FrameError";
  }
}

/**
 * `body` is absent-or-present, never nullable-as-absent: `hasBody` keeps "no
 * payload" and "a payload that is JSON null" distinguishable.
 */
export interface Frame {
  readonly v: number;
  readonly id: string;
  readonly t: FrameKind;
  readonly key?: string;
  readonly method?: string;
  readonly path?: string;
  readonly query: ReadonlyArray<readonly [string, string]>;
  readonly body?: unknown;
  readonly hasBody: boolean;
  readonly code?: string;
  readonly message?: string;
  readonly meta: ReadonlyArray<readonly [string, string]>;
}

function bare(t: FrameKind, id: string): Frame {
  return { v: FRAME_VERSION, id, t, query: [], hasBody: false, meta: [] };
}

export function callFrame(
  id: string,
  key: string,
  method: string,
  path: string,
  query: ReadonlyArray<readonly [string, string]> = [],
  body?: { value: unknown },
): Frame {
  return { ...bare("call", id), key, method, path, query, body: body?.value, hasBody: body !== undefined };
}

export const dataFrame = (id: string, body: unknown): Frame => ({ ...bare("data", id), body, hasBody: true });
export const endFrame = (id: string): Frame => bare("end", id);
export const cancelFrame = (id: string): Frame => bare("cancel", id);
export const errorFrame = (id: string, code: string, message?: string): Frame => ({
  ...bare("error", id), code, message,
});

export function withMeta(frame: Frame, name: string, value: string): Frame {
  return { ...frame, meta: [...frame.meta, [name, value] as const] };
}

export function validate(frame: Frame): void {
  if (frame.v !== FRAME_VERSION) throw new FrameError(`unsupported frame version ${frame.v}`);
  if (!frame.id || [...frame.id].length > 128) throw new FrameError("id must be 1..128 characters");
  if (frame.t === "call") {
    if (!frame.key) throw new FrameError("a call frame needs an operation key");
    if (!frame.method) throw new FrameError("a call frame needs a method");
    if (!frame.path?.startsWith("/")) throw new FrameError("a call frame needs a path starting with /");
  } else if (frame.key || frame.method || frame.path || frame.query.length) {
    throw new FrameError(`a ${frame.t} frame carries no addressing fields`);
  }
  if (frame.t === "data" && !frame.hasBody) throw new FrameError("a data frame needs a body");
  if (frame.t === "error") {
    if (!frame.code) throw new FrameError("an error frame needs a code");
  } else if (frame.code || frame.message) {
    throw new FrameError(`a ${frame.t} frame carries no code or message`);
  }
  for (const [name, value] of frame.meta) {
    if (typeof value !== "string") throw new FrameError(`meta.${name} must be a string`);
  }
}

function toObject(frame: Frame): Record<string, unknown> {
  const raw: Record<string, unknown> = { v: frame.v, id: frame.id, t: frame.t };
  if (frame.t === "call") {
    raw.key = frame.key;
    raw.method = frame.method;
    raw.path = frame.path;
    if (frame.query.length) raw.query = frame.query.map(([k, v]) => [k, v]);
  }
  if (frame.hasBody) raw.body = frame.body;
  if (frame.t === "error") {
    raw.code = frame.code;
    if (frame.message !== undefined) raw.message = frame.message;
  }
  if (frame.meta.length) raw.meta = Object.fromEntries(frame.meta.map(([k, v]) => [k, v]));

  // Rebuild in the canonical order rather than trusting object key order.
  const ordered: Record<string, unknown> = {};
  for (const name of FIELD_ORDER) {
    if (name in raw) ordered[name] = raw[name];
  }
  return ordered;
}

/** The canonical bytes. Byte-identical to `ridl.framing.Frame.encode`. */
export function encode(frame: Frame): Uint8Array {
  validate(frame);
  // JSON.stringify with no spacing already produces the compact separators and
  // leaves non-ASCII literal, which is what the canonical form asks for.
  const text = JSON.stringify(toObject(frame));
  const bytes = new TextEncoder().encode(text);
  if (bytes.length > MAX_FRAME_BYTES) {
    throw new FrameError(`frame is ${bytes.length} bytes, over the ${MAX_FRAME_BYTES} limit`);
  }
  return bytes;
}

/** Length-prefixed bytes for a byte-stream transport. */
export function encodeTcp(frame: Frame): Uint8Array {
  const payload = encode(frame);
  const out = new Uint8Array(LENGTH_PREFIX_BYTES + payload.length);
  new DataView(out.buffer).setUint32(0, payload.length, false); // big-endian
  out.set(payload, LENGTH_PREFIX_BYTES);
  return out;
}

export function decode(payload: Uint8Array | string): Frame {
  let text: string;
  if (typeof payload === "string") {
    text = payload;
  } else {
    if (payload.length > MAX_FRAME_BYTES) {
      throw new FrameError(`frame is ${payload.length} bytes, over the ${MAX_FRAME_BYTES} limit`);
    }
    text = new TextDecoder("utf-8", { fatal: true }).decode(payload);
  }

  let raw: unknown;
  try {
    raw = JSON.parse(text);
  } catch (cause) {
    throw new FrameError(`frame is not JSON: ${String(cause)}`);
  }
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
    throw new FrameError("a frame must be a JSON object");
  }
  const obj = raw as Record<string, unknown>;

  // Strict on unknown members: silently dropping one is how a peer ends up
  // believing a field was honoured when it was ignored.
  const unknown = Object.keys(obj).filter((k) => !(FIELD_ORDER as readonly string[]).includes(k));
  if (unknown.length) throw new FrameError(`unknown frame member(s): ${unknown.sort().join(", ")}`);

  const query: Array<readonly [string, string]> = [];
  if (obj.query !== undefined) {
    if (!Array.isArray(obj.query)) throw new FrameError("query must be an array of [name, value] pairs");
    for (const pair of obj.query) {
      if (!Array.isArray(pair) || pair.length !== 2 || pair.some((x) => typeof x !== "string")) {
        throw new FrameError("each query entry must be a [name, value] pair of strings");
      }
      query.push([pair[0] as string, pair[1] as string]);
    }
  }

  const meta: Array<readonly [string, string]> = [];
  if (obj.meta !== undefined) {
    if (typeof obj.meta !== "object" || obj.meta === null || Array.isArray(obj.meta)) {
      throw new FrameError("meta must be an object");
    }
    for (const [k, v] of Object.entries(obj.meta as Record<string, unknown>).sort(([a], [b]) => (a < b ? -1 : 1))) {
      if (typeof v !== "string") throw new FrameError(`meta.${k} must be a string`);
      meta.push([k, v]);
    }
  }

  const frame: Frame = {
    v: typeof obj.v === "number" ? obj.v : 0,
    id: typeof obj.id === "string" ? obj.id : "",
    t: obj.t as FrameKind,
    key: obj.key as string | undefined,
    method: obj.method as string | undefined,
    path: obj.path as string | undefined,
    query,
    body: obj.body,
    hasBody: "body" in obj,
    code: obj.code as string | undefined,
    message: obj.message as string | undefined,
    meta,
  };
  if (!["call", "data", "end", "error", "cancel"].includes(frame.t)) {
    throw new FrameError("unknown frame type");
  }
  validate(frame);
  return frame;
}

/**
 * Pull every whole length-prefixed frame out of a read buffer. Returns the
 * frames and the unconsumed tail, which the caller keeps for the next read.
 */
export function decodeStream(buffer: Uint8Array): { frames: Frame[]; rest: Uint8Array } {
  const frames: Frame[] = [];
  const view = new DataView(buffer.buffer, buffer.byteOffset, buffer.byteLength);
  let offset = 0;
  while (buffer.length - offset >= LENGTH_PREFIX_BYTES) {
    const length = view.getUint32(offset, false);
    if (length > MAX_FRAME_BYTES) {
      // Refuse before allocating: a corrupt length must not make the reader
      // reserve gigabytes.
      throw new FrameError(`declared frame length ${length} is over the ${MAX_FRAME_BYTES} limit`);
    }
    const start = offset + LENGTH_PREFIX_BYTES;
    if (buffer.length - start < length) break;
    frames.push(decode(buffer.subarray(start, start + length)));
    offset = start + length;
  }
  return { frames, rest: buffer.subarray(offset) };
}

/**
 * Per-connection correlation ids. Monotonic, never derived from the request:
 * a content-hashed id would make two genuinely separate calls with identical
 * payloads collide.
 */
export class Correlator {
  #next = 0;
  // Explicit field rather than a constructor parameter property, so every file
  // here runs under `node --experimental-strip-types` with no build step.
  readonly #prefix: string;

  constructor(prefix = "") {
    this.#prefix = prefix;
  }

  take(): string {
    this.#next += 1;
    return this.#prefix ? `${this.#prefix}${this.#next}` : String(this.#next);
  }
}
