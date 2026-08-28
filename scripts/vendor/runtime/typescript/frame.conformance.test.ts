/**
 * Asserts this port encodes and decodes exactly what `ridl/framing.py`
 * produced. If this file fails, the two languages have drifted and a frame
 * written by one will be misread by the other.
 *
 *   node --experimental-strip-types --test runtime/typescript/frame.conformance.test.ts
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

import {
  Correlator,
  type Frame,
  callFrame,
  cancelFrame,
  dataFrame,
  decode,
  decodeStream,
  encode,
  encodeTcp,
  endFrame,
  errorFrame,
  withMeta,
} from "./frame.ts";

const here = dirname(fileURLToPath(import.meta.url));
const fixtures = JSON.parse(
  readFileSync(join(here, "../../examples/frames/conformance.json"), "utf8"),
) as { cases: Array<{ name: string; encoded: string; tcp_prefix_hex: string }> };

const build: Record<string, () => Frame> = {
  "call-minimal": () => callFrame("1", "healthz", "GET", "/healthz"),
  "call-full": () =>
    callFrame("c7-2", "walk_matter", "POST", "/v1/matters/abc/walk",
      [["include_facts", "true"], ["kinds", "branch"]],
      { value: { choice_id: "c", answers: {}, confirm_override: false } }),
  "call-unicode-path": () => callFrame("3", "get_matter", "GET", "/v1/matters/caf%C3%A9"),
  "call-unicode-body": () =>
    callFrame("4", "create_note", "POST", "/v1/notes", [], { value: { text: "café — ok" } }),
  "data-object": () => dataFrame("1", { ok: true, n: 3 }),
  "data-null": () => dataFrame("1", null),
  "data-scalar": () => dataFrame("1", "plain string"),
  end: () => endFrame("1"),
  cancel: () => cancelFrame("c7-2"),
  "error-minimal": () => errorFrame("1", "503"),
  "error-message": () => errorFrame("1", "404", "no such matter"),
  "call-with-meta": () =>
    withMeta(
      withMeta(callFrame("5", "healthz", "GET", "/healthz"), "authorization", "Bearer x"),
      "traceparent", "00-a-b-01",
    ),
};

const decoder = new TextDecoder();

for (const fixture of fixtures.cases) {
  test(`encodes ${fixture.name} to the canonical bytes`, () => {
    const make = build[fixture.name];
    assert.ok(make, `no builder for fixture ${fixture.name}`);
    assert.equal(decoder.decode(encode(make())), fixture.encoded);
  });

  test(`decodes ${fixture.name} back to an equal frame`, () => {
    const round = decode(fixture.encoded);
    assert.equal(decoder.decode(encode(round)), fixture.encoded);
  });

  test(`length-prefixes ${fixture.name} identically`, () => {
    const tcp = encodeTcp(build[fixture.name]!());
    assert.equal(Buffer.from(tcp.subarray(0, 4)).toString("hex"), fixture.tcp_prefix_hex);
  });
}

test("an absent body and a null body stay distinguishable", () => {
  assert.equal(decode('{"v":1,"id":"1","t":"end"}').hasBody, false);
  const nullBody = decode('{"v":1,"id":"1","t":"data","body":null}');
  assert.equal(nullBody.hasBody, true);
  assert.equal(nullBody.body, null);
});

test("unknown members are refused, not ignored", () => {
  assert.throws(() => decode('{"v":1,"id":"1","t":"end","deadline":"5s"}'), /unknown frame member/);
});

test("a corrupt length prefix cannot force a huge allocation", () => {
  const buf = new Uint8Array(6);
  new DataView(buf.buffer).setUint32(0, 0xffffffff, false);
  assert.throws(() => decodeStream(buf), /over the .* limit/);
});

test("a partial tail is left for the next read", () => {
  const a = encodeTcp(callFrame("1", "healthz", "GET", "/healthz"));
  const b = encodeTcp(endFrame("1"));
  const buf = new Uint8Array(a.length + 3);
  buf.set(a, 0);
  buf.set(b.subarray(0, 3), a.length);
  const { frames, rest } = decodeStream(buf);
  assert.equal(frames.length, 1);
  assert.equal(rest.length, 3);
});

test("correlation ids do not collide for identical calls", () => {
  const c = new Correlator("c7-");
  assert.notEqual(c.take(), c.take());
});
