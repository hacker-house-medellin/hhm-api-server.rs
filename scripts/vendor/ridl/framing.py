"""The wire envelope for framed transports, and the one place its rules live.

HTTP needs no envelope: a request already carries a method, a path, a query and
a body, and the response is the body. WebSocket and TCP carry none of that, so a
call has to be framed explicitly -- and the moment two languages frame it from
prose rather than from a shared spec, they drift. That is exactly how the
minted-record-id divergence happened between the Rust and TypeScript opto-sync
runtimes, so the encoder here is normative and the fixtures under
`examples/frames/` pin it byte-for-byte for every port to assert against.

Canonical rules, all of which a port must reproduce exactly:

* JSON, UTF-8, no BOM.
* Compact separators: ``,`` and ``:`` with no spaces.
* Members in the fixed order given by `FIELD_ORDER`. Not alphabetical, and not
  the language's map iteration order -- a fixed list, so two ports produce the
  same bytes and a fixture can be compared with ``==``.
* An absent value is an omitted member. Never ``null`` standing in for absent;
  ``null`` is a legitimate JSON body and the two must stay distinguishable.
* Non-ASCII is emitted literally, not ``\\uXXXX`` escaped.

Transport packaging:

* **WebSocket** -- one frame per *text* message. Never split a frame across
  messages, never pack two frames into one.
* **TCP** -- a 4-byte big-endian unsigned length, then that many bytes of JSON.
  `MAX_FRAME_BYTES` bounds it so a corrupt length cannot make a reader allocate
  arbitrarily.
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass, field
from typing import Any, Iterator

FRAME_VERSION = 1

CALL = "call"
DATA = "data"
END = "end"
ERROR = "error"
CANCEL = "cancel"
FRAME_TYPES = (CALL, DATA, END, ERROR, CANCEL)

#: Member order on the wire. Fixed, so two ports produce identical bytes.
FIELD_ORDER = ("v", "id", "t", "key", "method", "path", "query", "body", "code", "message", "meta")

#: Length-prefix format for the TCP framing: big-endian unsigned 32-bit.
LENGTH_PREFIX = ">I"
LENGTH_PREFIX_BYTES = 4

#: Upper bound on one frame's JSON payload. A reader must refuse a declared
#: length above this *before* allocating for it.
MAX_FRAME_BYTES = 8 * 1024 * 1024


class FrameError(Exception):
    """A frame that cannot be decoded, or a payload that cannot be framed."""


@dataclass
class Frame:
    """One message on a framed transport.

    `body` is deliberately absent-or-present rather than nullable: `has_body`
    distinguishes "no payload" from "a payload that is JSON null".
    """

    t: str
    id: str
    key: str | None = None
    method: str | None = None
    path: str | None = None
    query: list[tuple[str, str]] = field(default_factory=list)
    body: Any = None
    has_body: bool = False
    code: str | None = None
    message: str | None = None
    meta: dict[str, str] = field(default_factory=dict)
    v: int = FRAME_VERSION

    # -- constructors ------------------------------------------------------

    @classmethod
    def call(
        cls,
        id: str,
        key: str,
        method: str,
        path: str,
        query: list[tuple[str, str]] | None = None,
        body: Any = None,
        has_body: bool = False,
        meta: dict[str, str] | None = None,
    ) -> "Frame":
        return cls(
            t=CALL,
            id=id,
            key=key,
            method=method,
            path=path,
            query=list(query or []),
            body=body,
            has_body=has_body,
            meta=dict(meta or {}),
        )

    @classmethod
    def data(cls, id: str, body: Any, meta: dict[str, str] | None = None) -> "Frame":
        return cls(t=DATA, id=id, body=body, has_body=True, meta=dict(meta or {}))

    @classmethod
    def end(cls, id: str, meta: dict[str, str] | None = None) -> "Frame":
        return cls(t=END, id=id, meta=dict(meta or {}))

    @classmethod
    def error(
        cls,
        id: str,
        code: str,
        message: str | None = None,
        body: Any = None,
        has_body: bool = False,
        meta: dict[str, str] | None = None,
    ) -> "Frame":
        return cls(
            t=ERROR,
            id=id,
            code=code,
            message=message,
            body=body,
            has_body=has_body,
            meta=dict(meta or {}),
        )

    @classmethod
    def cancel(cls, id: str, meta: dict[str, str] | None = None) -> "Frame":
        return cls(t=CANCEL, id=id, meta=dict(meta or {}))

    # -- encoding ----------------------------------------------------------

    def to_object(self) -> dict[str, Any]:
        raw: dict[str, Any] = {"v": self.v, "id": self.id, "t": self.t}
        if self.t == CALL:
            raw["key"] = self.key
            raw["method"] = self.method
            raw["path"] = self.path
            if self.query:
                raw["query"] = [[k, v] for k, v in self.query]
        if self.has_body:
            raw["body"] = self.body
        if self.t == ERROR:
            raw["code"] = self.code
            if self.message is not None:
                raw["message"] = self.message
        if self.meta:
            raw["meta"] = dict(self.meta)
        return {k: raw[k] for k in FIELD_ORDER if k in raw}

    def encode(self) -> bytes:
        """The canonical bytes. Every port must produce these exactly."""
        self.validate()
        text = json.dumps(
            self.to_object(),
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        payload = text.encode("utf-8")
        if len(payload) > MAX_FRAME_BYTES:
            raise FrameError(f"frame is {len(payload)} bytes, over the {MAX_FRAME_BYTES} limit")
        return payload

    def encode_tcp(self) -> bytes:
        """Length-prefixed bytes for a byte-stream transport."""
        payload = self.encode()
        return struct.pack(LENGTH_PREFIX, len(payload)) + payload

    # -- validation --------------------------------------------------------

    def validate(self) -> None:
        if self.v != FRAME_VERSION:
            raise FrameError(f"unsupported frame version {self.v!r}")
        if self.t not in FRAME_TYPES:
            raise FrameError(f"unknown frame type {self.t!r}")
        if not self.id or len(self.id) > 128:
            raise FrameError("id must be 1..128 characters")
        if self.t == CALL:
            if not self.key:
                raise FrameError("a call frame needs an operation key")
            if not self.method:
                raise FrameError("a call frame needs a method")
            if not self.path or not self.path.startswith("/"):
                raise FrameError("a call frame needs a path starting with /")
        elif self.key or self.method or self.path or self.query:
            raise FrameError(f"a {self.t} frame carries no addressing fields")
        if self.t == DATA and not self.has_body:
            raise FrameError("a data frame needs a body")
        if self.t == ERROR and not self.code:
            raise FrameError("an error frame needs a code")
        if self.t != ERROR and (self.code or self.message):
            raise FrameError(f"a {self.t} frame carries no code or message")
        for key, value in self.meta.items():
            if not isinstance(value, str):
                raise FrameError(f"meta.{key} must be a string, not {type(value).__name__}")


# -- decoding --------------------------------------------------------------

def decode(payload: bytes | str) -> Frame:
    """Decode one frame. Raises `FrameError` on anything malformed."""
    if isinstance(payload, bytes):
        if len(payload) > MAX_FRAME_BYTES:
            raise FrameError(f"frame is {len(payload)} bytes, over the {MAX_FRAME_BYTES} limit")
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise FrameError(f"frame is not UTF-8: {exc}") from exc
    else:
        text = payload
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise FrameError(f"frame is not JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise FrameError("a frame must be a JSON object")

    unknown = set(raw) - set(FIELD_ORDER)
    if unknown:
        # Strict: an unknown member means the peer is speaking a dialect we do
        # not have. Silently ignoring it is how one side ends up believing a
        # field was honoured when it was dropped.
        raise FrameError(f"unknown frame member(s): {', '.join(sorted(unknown))}")

    query_raw = raw.get("query") or []
    if not isinstance(query_raw, list):
        raise FrameError("query must be an array of [name, value] pairs")
    query: list[tuple[str, str]] = []
    for pair in query_raw:
        if not (isinstance(pair, list) and len(pair) == 2 and all(isinstance(x, str) for x in pair)):
            raise FrameError("each query entry must be a [name, value] pair of strings")
        query.append((pair[0], pair[1]))

    meta_raw = raw.get("meta") or {}
    if not isinstance(meta_raw, dict):
        raise FrameError("meta must be an object")

    frame = Frame(
        v=raw.get("v"),
        id=raw.get("id") or "",
        t=raw.get("t") or "",
        key=raw.get("key"),
        method=raw.get("method"),
        path=raw.get("path"),
        query=query,
        body=raw.get("body"),
        has_body="body" in raw,
        code=raw.get("code"),
        message=raw.get("message"),
        meta=meta_raw,
    )
    frame.validate()
    return frame


def decode_stream(buffer: bytes) -> tuple[list[Frame], bytes]:
    """Pull every whole length-prefixed frame out of a TCP read buffer.

    Returns the frames and the unconsumed remainder, so a caller keeps the
    partial tail and appends the next read to it.
    """
    frames: list[Frame] = []
    offset = 0
    while len(buffer) - offset >= LENGTH_PREFIX_BYTES:
        (length,) = struct.unpack_from(LENGTH_PREFIX, buffer, offset)
        if length > MAX_FRAME_BYTES:
            # Refuse before allocating: a corrupt length must not be able to
            # make the reader reserve gigabytes.
            raise FrameError(f"declared frame length {length} is over the {MAX_FRAME_BYTES} limit")
        start = offset + LENGTH_PREFIX_BYTES
        if len(buffer) - start < length:
            break
        frames.append(decode(buffer[start : start + length]))
        offset = start + length
    return frames, buffer[offset:]


# -- correlation ids -------------------------------------------------------

class Correlator:
    """Per-connection correlation ids.

    Monotonic, not derived from the request. Content-hashed ids would make two
    genuinely separate calls with identical payloads collide -- the same trap
    the opto-sync minted record id fell into.
    """

    def __init__(self, prefix: str = "") -> None:
        self._prefix = prefix
        self._next = 0

    def take(self) -> str:
        self._next += 1
        return f"{self._prefix}{self._next}" if self._prefix else str(self._next)


def unary_exchange(frames: list[Frame]) -> Iterator[Frame]:
    """The frames a well-formed unary answer consists of: one data, then end."""
    yield from frames
