"""Route IDL v2 -- the in-memory model.

A route map is a JSON object whose keys are operations and whose values are
routes. v2 adds a closed, resolvable type layer on top of that: every path
parameter, query parameter, request body and response body names a type, and
every type is either a builtin scalar or defined in the map's own `types`
table. Nothing is a free-text source fragment, which is what v1's `binding`
strings were.

Parsing here is deliberately dependency-free (stdlib only) so the checker
behaves identically on a developer laptop and in CI. v1 relied on `jsonschema`
being importable and silently degraded to a Draft-7 structural subset when it
was not; that divergence is the reason this module hand-rolls its validation.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = "2.0.0"

# --------------------------------------------------------------------------
# Builtin scalars. `json_type` is the JSON Schema type used when projecting.
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Builtin:
    name: str
    json_type: str
    json_format: str | None = None


BUILTINS: dict[str, Builtin] = {
    b.name: b
    for b in (
        Builtin("String", "string"),
        Builtin("Bool", "boolean"),
        Builtin("I32", "integer", "int32"),
        Builtin("I64", "integer", "int64"),
        Builtin("F64", "number", "double"),
        Builtin("Uuid", "string", "uuid"),
        Builtin("DateTime", "string", "date-time"),
        # Canonical decimal-as-string, matching opto-sync's revision encoding.
        Builtin("Decimal", "string", "decimal"),
        # Deliberate escape hatch: an opaque JSON value. Emitters map it to the
        # language's dynamic type. Using it is a decision, not an accident.
        Builtin("Json", "object"),
    )
}

SCALAR_BUILTINS = {n for n in BUILTINS if n != "Json"}

HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
BODY_METHODS = {"POST", "PUT", "PATCH"}
MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# How a call reaches the server. HTTP is the only transport that needs no
# envelope; websocket and tcp frame the call explicitly (see ridl.framing).
TRANSPORT_HTTP = "http"
TRANSPORT_WEBSOCKET = "websocket"
TRANSPORT_TCP = "tcp"
# NATS is the fourth avenue between a web server and an api server: the caller
# publishes to a subject and the reply arrives asynchronously. It has no path
# and no query string, so an operation that uses it must carry a request body.
TRANSPORT_NATS = "nats"
TRANSPORTS = (TRANSPORT_HTTP, TRANSPORT_WEBSOCKET, TRANSPORT_TCP, TRANSPORT_NATS)
DEFAULT_TRANSPORTS = (TRANSPORT_HTTP,)
#: Transports that carry the ridl frame envelope, and so can stream.
FRAMED_TRANSPORTS = (TRANSPORT_WEBSOCKET, TRANSPORT_TCP)

# Exchange shape. Unary is one request, one response -- the only shape HTTP
# and the opto-sync queue can carry.
STREAM_UNARY = "unary"
STREAM_SERVER = "server_stream"
STREAM_CLIENT = "client_stream"
STREAM_BIDI = "bidi"
STREAMS = (STREAM_UNARY, STREAM_SERVER, STREAM_CLIENT, STREAM_BIDI)
STREAMING = (STREAM_SERVER, STREAM_CLIENT, STREAM_BIDI)

DELIVERY_DIRECT = "direct"
DELIVERY_OPTO_SYNC = "opto_sync_queued"
DELIVERIES = (DELIVERY_DIRECT, DELIVERY_OPTO_SYNC)

KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PATH_RE = re.compile(r"^/[^\s]*$")
# `{*rest}` is an axum catch-all: it matches the remainder of the path,
# slashes included, so it is a parameter with different encoding rules.
PATH_PARAM_RE = re.compile(r"\{(\*?[^{}]*)\}")
# opto-sync's own scope-id rule (clients/go/envelope.go): a SQL-safe identifier.
OPTO_TABLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
# Enum wire values are snake_case, matching every `rename_all` in the Rust core.
ENUM_VARIANT_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class RidlError(Exception):
    """A route map that cannot be modelled at all (as opposed to one that
    models fine but violates a rule -- those are collected, not raised)."""


# --------------------------------------------------------------------------
# Type expressions
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Named:
    name: str

    def __str__(self) -> str:
        return self.name


@dataclass(frozen=True)
class ListOf:
    item: "TypeExpr"

    def __str__(self) -> str:
        return f"list<{self.item}>"


@dataclass(frozen=True)
class MapOf:
    """A JSON object with String keys and homogeneous values."""

    value: "TypeExpr"

    def __str__(self) -> str:
        return f"map<{self.value}>"


@dataclass(frozen=True)
class OptionOf:
    inner: "TypeExpr"

    def __str__(self) -> str:
        return f"option<{self.inner}>"


TypeExpr = Named | ListOf | MapOf | OptionOf


def parse_type_expr(raw: Any, where: str) -> TypeExpr:
    if isinstance(raw, str):
        if not raw:
            raise RidlError(f"{where}: empty type name")
        return Named(raw)
    if not isinstance(raw, dict):
        raise RidlError(f"{where}: a type must be a name or an object, got {type(raw).__name__}")
    kind = raw.get("kind")
    if kind == "list":
        if "item" not in raw:
            raise RidlError(f"{where}: list needs an `item`")
        return ListOf(parse_type_expr(raw["item"], f"{where}.item"))
    if kind == "map":
        if "value" not in raw:
            raise RidlError(f"{where}: map needs a `value`")
        return MapOf(parse_type_expr(raw["value"], f"{where}.value"))
    if kind == "option":
        if "inner" not in raw:
            raise RidlError(f"{where}: option needs an `inner`")
        return OptionOf(parse_type_expr(raw["inner"], f"{where}.inner"))
    raise RidlError(f"{where}: unknown type kind {kind!r} (want list, map, or option)")


def walk_type(expr: TypeExpr) -> Iterator[TypeExpr]:
    yield expr
    if isinstance(expr, ListOf):
        yield from walk_type(expr.item)
    elif isinstance(expr, MapOf):
        yield from walk_type(expr.value)
    elif isinstance(expr, OptionOf):
        yield from walk_type(expr.inner)


# --------------------------------------------------------------------------
# Type definitions
# --------------------------------------------------------------------------

@dataclass
class Field:
    """One record field. `wire` is the JSON key and is authoritative; the
    per-language identifier is derived from it by `ridl.naming`."""

    wire: str
    type: TypeExpr
    required: bool = True
    default: Any = None
    has_default: bool = False
    doc: str | None = None


@dataclass
class RecordDef:
    name: str
    fields: list[Field]
    doc: str | None = None
    kind: str = "record"


@dataclass
class EnumDef:
    name: str
    variants: list[str]
    doc: str | None = None
    kind: str = "enum"


@dataclass
class ScalarDef:
    """A newtype over a builtin -- `Uuid`-like domain types that stay distinct
    in languages that can express that, and collapse to the base elsewhere."""

    name: str
    base: str
    format: str | None = None
    doc: str | None = None
    kind: str = "scalar"


@dataclass
class AliasDef:
    name: str
    target: TypeExpr
    doc: str | None = None
    kind: str = "alias"


TypeDef = RecordDef | EnumDef | ScalarDef | AliasDef


def _parse_field(wire: str, raw: Any, where: str) -> Field:
    if isinstance(raw, (str, list)) or (isinstance(raw, dict) and "type" not in raw and "kind" in raw):
        # Shorthand: the value *is* the type expression.
        return Field(wire=wire, type=parse_type_expr(raw, where))
    if not isinstance(raw, dict):
        raise RidlError(f"{where}: field must be a type or an object with `type`")
    if "type" not in raw:
        raise RidlError(f"{where}: field needs a `type`")
    fld = Field(
        wire=wire,
        type=parse_type_expr(raw["type"], f"{where}.type"),
        required=bool(raw.get("required", True)),
        doc=raw.get("doc"),
    )
    if "default" in raw:
        fld.default = raw["default"]
        fld.has_default = True
        # A field with a default is by definition satisfiable without the
        # caller supplying it.
        fld.required = bool(raw.get("required", False))
    return fld


def parse_type_def(name: str, raw: Any) -> TypeDef:
    where = f"types.{name}"
    if not isinstance(raw, dict):
        raise RidlError(f"{where}: a type definition must be an object")
    kind = raw.get("kind")
    doc = raw.get("doc")
    if kind == "record":
        fields_raw = raw.get("fields")
        if not isinstance(fields_raw, dict):
            raise RidlError(f"{where}: record needs a `fields` object")
        return RecordDef(
            name=name,
            fields=[_parse_field(k, v, f"{where}.fields.{k}") for k, v in fields_raw.items()],
            doc=doc,
        )
    if kind == "enum":
        variants = raw.get("variants")
        if not isinstance(variants, list) or not variants:
            raise RidlError(f"{where}: enum needs a non-empty `variants` array")
        return EnumDef(name=name, variants=[str(v) for v in variants], doc=doc)
    if kind == "scalar":
        base = raw.get("base")
        if base not in BUILTINS:
            raise RidlError(f"{where}: scalar `base` must be a builtin, got {base!r}")
        return ScalarDef(name=name, base=base, format=raw.get("format"), doc=doc)
    if kind == "alias":
        if "target" not in raw:
            raise RidlError(f"{where}: alias needs a `target`")
        return AliasDef(name=name, target=parse_type_expr(raw["target"], f"{where}.target"), doc=doc)
    raise RidlError(f"{where}: unknown type kind {kind!r} (want record, enum, scalar, or alias)")


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@dataclass
class Param:
    wire: str
    type: TypeExpr
    required: bool = True
    default: Any = None
    has_default: bool = False
    doc: str | None = None
    #: A catch-all (`{*rest}`) matches across `/`, so it is not escaped as one
    #: segment. Set from the path template, never declared by hand.
    wildcard: bool = False


@dataclass
class OptoSync:
    """How a queued call is represented as an opto-sync record mutation.

    opto-sync's queue is record-shaped, not generic: `table` must be a SQL-safe
    identifier and the payload must be a JSON object. Both are checked
    statically in `ridl.validate` rather than discovered at runtime.
    """

    table: str
    record_id_from: str
    operation: str = "upsert"


@dataclass
class Route:
    key: str
    path: str
    methods: list[str]
    summary: str | None = None
    doc: str | None = None
    path_params: list[Param] = field(default_factory=list)
    query_params: list[Param] = field(default_factory=list)
    request: TypeExpr | None = None
    response: TypeExpr | None = None
    errors: dict[str, TypeExpr] = field(default_factory=dict)
    delivery: str = DELIVERY_DIRECT
    opto_sync: OptoSync | None = None
    #: Which transports may carry this operation. Defaults to HTTP alone, so
    #: every v2 map written before transports existed keeps its meaning.
    transports: list[str] = field(default_factory=lambda: list(DEFAULT_TRANSPORTS))
    #: Exchange shape. Anything but `unary` needs a framed transport.
    stream: str = STREAM_UNARY
    binding: dict[str, Any] | None = None
    deprecated: bool = False
    # Server-rendered routes (HTML, redirects, form posts, static assets) belong
    # in the map -- the gate should still hold them to the source -- but there is
    # nothing useful to generate a typed JSON client for.
    client: bool = True
    #: "json" (default) or "form" for `application/x-www-form-urlencoded`.
    content_type: str = "json"
    #: Which transports may carry this operation. HTTP unless stated.
    transports: list[str] = field(default_factory=lambda: list(DEFAULT_TRANSPORTS))
    #: "unary", "server_stream", "client_stream", or "bidi".
    stream: str = STREAM_UNARY

    @property
    def is_connect_unary(self) -> bool:
        """PascalCase keys are Connect-shaped JSON unary: POST /{service}/{Method}."""
        return self.key[:1].isupper()

    @property
    def primary_method(self) -> str:
        return self.methods[0]

    @property
    def is_streaming(self) -> bool:
        return self.stream in STREAMING

    @property
    def framed_transports(self) -> list[str]:
        return [t for t in self.transports if t in FRAMED_TRANSPORTS]

    @property
    def over_http(self) -> bool:
        return TRANSPORT_HTTP in self.transports


def _parse_param(wire: str, raw: Any, where: str) -> Param:
    fld = _parse_field(wire, raw, where)
    return Param(
        wire=fld.wire,
        type=fld.type,
        required=fld.required,
        default=fld.default,
        has_default=fld.has_default,
        doc=fld.doc,
    )


def _mark_wildcard(param: Param, wildcard: bool) -> Param:
    param.wildcard = wildcard
    return param


def _parse_body(raw: Any, where: str) -> TypeExpr | None:
    if raw is None:
        return None
    if isinstance(raw, dict) and "type" in raw:
        return parse_type_expr(raw["type"], f"{where}.type")
    return parse_type_expr(raw, where)


def parse_route(key: str, raw: Any) -> Route:
    where = f"map.{key}"
    if isinstance(raw, str):
        # v1 shorthand is still accepted structurally so the error message can
        # say something useful; `validate` rejects it, because v2 requires
        # explicit methods (v1's five divergent inference tables are the bug
        # this removes).
        return Route(key=key, path=raw, methods=[])
    if not isinstance(raw, dict):
        raise RidlError(f"{where}: a route must be an object")
    path = raw.get("path")
    if not isinstance(path, str):
        raise RidlError(f"{where}: route needs a `path` string")

    methods_raw = raw.get("methods")
    methods = [str(m).upper() for m in methods_raw] if isinstance(methods_raw, list) else []

    opto_raw = raw.get("opto_sync")
    opto = None
    if isinstance(opto_raw, dict):
        opto = OptoSync(
            table=str(opto_raw.get("table", "")),
            record_id_from=str(opto_raw.get("record_id_from", "")),
            operation=str(opto_raw.get("operation", "upsert")),
        )

    errors_raw = raw.get("errors") if isinstance(raw.get("errors"), dict) else {}
    errors = {
        str(code): parse_type_expr(spec.get("type", spec) if isinstance(spec, dict) else spec,
                                   f"{where}.errors.{code}")
        for code, spec in errors_raw.items()
    }

    pp_raw = raw.get("path_params") if isinstance(raw.get("path_params"), dict) else {}
    qp_raw = raw.get("query_params") if isinstance(raw.get("query_params"), dict) else {}

    wildcards = wildcard_params_in(path)
    return Route(
        key=key,
        path=path,
        methods=methods,
        summary=raw.get("summary"),
        doc=raw.get("doc"),
        path_params=[
            _mark_wildcard(_parse_param(k, v, f"{where}.path_params.{k}"), k in wildcards)
            for k, v in pp_raw.items()
        ],
        query_params=[_parse_param(k, v, f"{where}.query_params.{k}") for k, v in qp_raw.items()],
        request=_parse_body(raw.get("request"), f"{where}.request"),
        response=_parse_body(raw.get("response"), f"{where}.response"),
        errors=errors,
        delivery=str(raw.get("delivery", DELIVERY_DIRECT)),
        opto_sync=opto,
        transports=(
            [str(t) for t in raw["transports"]]
            if isinstance(raw.get("transports"), list)
            else list(DEFAULT_TRANSPORTS)
        ),
        stream=str(raw.get("stream", STREAM_UNARY)),
        binding=raw.get("binding") if isinstance(raw.get("binding"), dict) else None,
        deprecated=bool(raw.get("deprecated", False)),
        client=bool(raw.get("client", True)),
        content_type=str(raw.get("content_type", "json")),
    )


# --------------------------------------------------------------------------
# The map
# --------------------------------------------------------------------------

@dataclass
class RouteMap:
    schema_version: str
    service: str
    title: str | None
    version: str | None
    description: str | None
    types: dict[str, TypeDef]
    routes: list[Route]
    source: Path | None = None

    def route(self, key: str) -> Route | None:
        for r in self.routes:
            if r.key == key:
                return r
        return None

    def resolve(self, expr: TypeExpr) -> TypeDef | Builtin | None:
        """One step of resolution: a Named to its definition, or None if the
        name is unknown. Compound expressions resolve to None -- callers that
        care about the outer shape should match on the expression itself."""
        if not isinstance(expr, Named):
            return None
        if expr.name in BUILTINS:
            return BUILTINS[expr.name]
        return self.types.get(expr.name)

    def underlying(self, expr: TypeExpr, _seen: frozenset[str] = frozenset()) -> TypeExpr:
        """Follow aliases (and only aliases) to the expression they stand for."""
        if isinstance(expr, Named) and expr.name not in _seen:
            defn = self.types.get(expr.name)
            if isinstance(defn, AliasDef):
                return self.underlying(defn.target, _seen | {expr.name})
        return expr

    def is_record(self, expr: TypeExpr) -> bool:
        return isinstance(self.resolve(self.underlying(expr)), RecordDef)

    def is_scalar_like(self, expr: TypeExpr) -> bool:
        """True for things that can be rendered into a URL segment or query
        value: builtin scalars, scalar newtypes, and string enums."""
        target = self.underlying(expr)
        if not isinstance(target, Named):
            return False
        if target.name in SCALAR_BUILTINS:
            return True
        defn = self.types.get(target.name)
        return isinstance(defn, (ScalarDef, EnumDef))


def parse_route_map(data: Any, source: Path | None = None) -> RouteMap:
    if not isinstance(data, dict):
        raise RidlError("route map must be a JSON object")
    types_raw = data.get("types") if isinstance(data.get("types"), dict) else {}
    map_raw = data.get("map")
    if not isinstance(map_raw, dict):
        raise RidlError("route map needs a `map` object")
    return RouteMap(
        schema_version=str(data.get("schema_version", "")),
        service=str(data.get("service", "")),
        title=data.get("title"),
        version=data.get("version"),
        description=data.get("description"),
        types={name: parse_type_def(name, raw) for name, raw in types_raw.items()},
        routes=[parse_route(key, raw) for key, raw in map_raw.items()],
        source=source,
    )


def load_route_map(path: Path) -> RouteMap:
    with path.open(encoding="utf-8") as handle:
        return parse_route_map(json.load(handle), source=path)


def path_params_in(path: str) -> list[str]:
    """Parameter names in a path template, with any catch-all `*` stripped."""
    return [name.lstrip("*") for name in PATH_PARAM_RE.findall(path)]


def wildcard_params_in(path: str) -> set[str]:
    return {n.lstrip("*") for n in PATH_PARAM_RE.findall(path) if n.startswith("*")}
