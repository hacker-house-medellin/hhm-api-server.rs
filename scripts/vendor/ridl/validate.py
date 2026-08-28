"""Semantic validation for Route IDL v2.

JSON Schema can check that a route map is shaped like a route map. It cannot
check that `{id}` in a path has a declared type, that a queued call's payload is
a JSON object, or that two operations do not claim the same method on the same
path. Those are the rules that make generated code trustworthy, so they live
here, in plain Python, with no optional dependency that can silently change the
answer between a laptop and CI.

Every function returns a list of human-readable problems. An empty list means
the map is safe to generate from.
"""

from __future__ import annotations

from .model import (
    BODY_METHODS,
    FRAMED_TRANSPORTS,
    STREAMING,
    STREAMS,
    STREAM_BIDI,
    STREAM_CLIENT,
    STREAM_SERVER,
    STREAM_UNARY,
    TRANSPORTS,
    TRANSPORT_NATS,
    TRANSPORT_HTTP,
    BUILTINS,
    DELIVERIES,
    DELIVERY_OPTO_SYNC,
    ENUM_VARIANT_RE,
    HTTP_METHODS,
    IDENT_RE,
    KEY_RE,
    MUTATING_METHODS,
    OPTO_TABLE_RE,
    PATH_RE,
    SCHEMA_VERSION,
    AliasDef,
    EnumDef,
    ListOf,
    MapOf,
    Named,
    OptionOf,
    RecordDef,
    Route,
    RouteMap,
    ScalarDef,
    TypeExpr,
    path_params_in,
    walk_type,
    wildcard_params_in,
)

# opto-sync caps a queued payload at 255 KiB (DEFAULT_MAX_QUEUED_PAYLOAD_BYTES).
# We cannot know a payload's size statically, but we can refuse shapes that are
# unbounded by construction in a queued call.
OPTO_MAX_PAYLOAD_BYTES = 255 * 1024


def validate(rmap: RouteMap) -> list[str]:
    errors: list[str] = []
    errors += _validate_header(rmap)
    errors += _validate_types(rmap)
    errors += _validate_routes(rmap)
    errors += _validate_uniqueness(rmap)
    return errors


# --------------------------------------------------------------------------

def _validate_header(rmap: RouteMap) -> list[str]:
    errors: list[str] = []
    if rmap.schema_version != SCHEMA_VERSION:
        errors.append(
            f"schema_version must be {SCHEMA_VERSION!r}, got {rmap.schema_version!r}"
        )
    if not rmap.service:
        errors.append("service is required")
    elif not IDENT_RE.match(rmap.service.replace("-", "_").replace(".", "_")):
        errors.append(f"service {rmap.service!r} is not an identifier-safe name")
    if not rmap.routes:
        errors.append("map must declare at least one operation")
    return errors


# --------------------------------------------------------------------------

def _validate_types(rmap: RouteMap) -> list[str]:
    errors: list[str] = []
    for name, defn in rmap.types.items():
        where = f"types.{name}"
        if not IDENT_RE.match(name):
            errors.append(f"{where}: type name must be an identifier")
        if name in BUILTINS:
            errors.append(f"{where}: cannot redefine builtin {name!r}")

        if isinstance(defn, RecordDef):
            errors += _validate_record(rmap, defn, where)
        elif isinstance(defn, EnumDef):
            errors += _validate_enum(defn, where)
        elif isinstance(defn, ScalarDef):
            if defn.base not in BUILTINS:
                errors.append(f"{where}: scalar base {defn.base!r} is not a builtin")
        elif isinstance(defn, AliasDef):
            errors += _validate_refs(rmap, defn.target, f"{where}.target")

    errors += _validate_acyclic(rmap)
    return errors


def _validate_record(rmap: RouteMap, defn: RecordDef, where: str) -> list[str]:
    errors: list[str] = []
    if not defn.fields:
        errors.append(f"{where}: record has no fields (use an alias to Json for a free-form object)")
    seen: set[str] = set()
    for fld in defn.fields:
        fwhere = f"{where}.fields.{fld.wire}"
        if fld.wire in seen:
            errors.append(f"{fwhere}: duplicate field")
        seen.add(fld.wire)
        if not IDENT_RE.match(fld.wire):
            errors.append(
                f"{fwhere}: wire name must be an identifier so every target language "
                f"can name it"
            )
        errors += _validate_refs(rmap, fld.type, fwhere)
        if fld.has_default and fld.required:
            errors.append(f"{fwhere}: a field with a default cannot also be required")
    return errors


def _validate_enum(defn: EnumDef, where: str) -> list[str]:
    errors: list[str] = []
    seen: set[str] = set()
    for variant in defn.variants:
        if variant in seen:
            errors.append(f"{where}: duplicate variant {variant!r}")
        seen.add(variant)
        if not ENUM_VARIANT_RE.match(variant):
            errors.append(
                f"{where}: variant {variant!r} must be snake_case -- enum wire values are "
                f"snake_case everywhere in this stack"
            )
    return errors


def _validate_refs(rmap: RouteMap, expr: TypeExpr, where: str) -> list[str]:
    errors: list[str] = []
    for node in walk_type(expr):
        if isinstance(node, Named):
            if node.name not in BUILTINS and node.name not in rmap.types:
                errors.append(f"{where}: unknown type {node.name!r}")
        if isinstance(node, OptionOf) and isinstance(node.inner, OptionOf):
            errors.append(f"{where}: option<option<..>> is not representable on the wire")
    return errors


def _validate_acyclic(rmap: RouteMap) -> list[str]:
    """A record may contain itself only behind a list, map, or option -- those
    are the constructors that give every target language a place to stop."""
    errors: list[str] = []

    def direct_refs(expr: TypeExpr) -> list[str]:
        # Deliberately does NOT descend into list/map/option: those break cycles.
        return [expr.name] if isinstance(expr, Named) else []

    def edges(name: str) -> list[str]:
        defn = rmap.types.get(name)
        if isinstance(defn, RecordDef):
            out: list[str] = []
            for fld in defn.fields:
                if fld.required:
                    out += direct_refs(fld.type)
            return out
        if isinstance(defn, AliasDef):
            return direct_refs(defn.target)
        return []

    WHITE, GREY, BLACK = 0, 1, 2
    colour = {name: WHITE for name in rmap.types}

    def visit(name: str, stack: list[str]) -> None:
        if colour.get(name, BLACK) == BLACK:
            return
        if colour.get(name) == GREY:
            cycle = " -> ".join(stack[stack.index(name):] + [name])
            errors.append(
                f"types: cyclic definition {cycle} -- break it with option<..> or list<..>"
            )
            return
        colour[name] = GREY
        for nxt in edges(name):
            if nxt in colour:
                visit(nxt, stack + [name])
        colour[name] = BLACK

    for name in list(rmap.types):
        visit(name, [])
    return errors


# --------------------------------------------------------------------------

def _validate_routes(rmap: RouteMap) -> list[str]:
    errors: list[str] = []
    for route in rmap.routes:
        errors += _validate_route(rmap, route)
    return errors


def _validate_route(rmap: RouteMap, route: Route) -> list[str]:
    where = f"map.{route.key}"
    errors: list[str] = []

    if not KEY_RE.match(route.key):
        errors.append(f"{where}: operation key must match [A-Za-z][A-Za-z0-9_]*")
    if not PATH_RE.match(route.path):
        errors.append(f"{where}: path must start with / and contain no whitespace")

    # v2 requires explicit methods. v1 inferred them from the key name, and the
    # five implementations of that inference disagreed: `delete_matter` meant
    # DELETE in Rust and GET in TypeScript, Dart, and Gleam.
    if not route.methods:
        errors.append(
            f"{where}: `methods` is required in v2 (inference from the key name is gone -- "
            f"it disagreed across languages)"
        )
    for method in route.methods:
        if method not in HTTP_METHODS:
            errors.append(f"{where}: unknown HTTP method {method!r}")
    if len(set(route.methods)) != len(route.methods):
        errors.append(f"{where}: duplicate method")

    errors += _validate_path_params(rmap, route, where)
    errors += _validate_query_params(rmap, route, where)
    errors += _validate_bodies(rmap, route, where)
    errors += _validate_delivery(rmap, route, where)
    errors += _validate_transports(route, where)
    errors += _validate_connect(route, where)
    return errors


def _validate_transports(route: Route, where: str) -> list[str]:
    """Which wires an operation may travel, and what each one implies.

    A web server and an api server here talk over four avenues -- plain HTTP, a
    stateful TCP connection, a websocket, and NATS. They are not
    interchangeable per operation, so the map states which apply rather than
    every client assuming HTTP and discovering the rest at runtime.
    """
    errors: list[str] = []

    if not route.transports:
        errors.append(f"{where}: transports must list at least one of {list(TRANSPORTS)}")
    for transport in route.transports:
        if transport not in TRANSPORTS:
            errors.append(
                f"{where}: unknown transport {transport!r} (want {list(TRANSPORTS)})"
            )
    if len(set(route.transports)) != len(route.transports):
        errors.append(f"{where}: duplicate transport")

    if route.stream not in STREAMS:
        errors.append(f"{where}: stream must be one of {list(STREAMS)}")

    if route.stream in STREAMING and not (set(route.transports) & set(FRAMED_TRANSPORTS)):
        errors.append(
            f"{where}: stream {route.stream!r} needs a framed transport "
            f"({list(FRAMED_TRANSPORTS)}) -- plain HTTP has nowhere to put the frames"
        )

    if TRANSPORT_NATS in route.transports and route.request is None:
        # A subject carries no path and no query string, so a body is the only
        # place the call's arguments can live.
        errors.append(
            f"{where}: the nats transport needs a request body -- a subject carries "
            f"no path or query string"
        )

    if route.query_params and set(route.transports) == {TRANSPORT_NATS}:
        errors.append(
            f"{where}: query parameters have no NATS encoding; add http or tcp, or "
            f"move them into the request body"
        )

    return errors


def _validate_path_params(rmap: RouteMap, route: Route, where: str) -> list[str]:
    errors: list[str] = []
    in_path = path_params_in(route.path)

    if route.path.count("{") != route.path.count("}") or any(not n for n in in_path):
        errors.append(f"{where}: malformed path template {route.path!r}")

    if len(set(in_path)) != len(in_path):
        errors.append(f"{where}: path repeats a placeholder: {route.path!r}")

    declared = {p.wire: p for p in route.path_params}
    for name in in_path:
        if name and name not in declared:
            errors.append(f"{where}: path placeholder {{{name}}} has no entry in path_params")
    for name in declared:
        if name not in in_path:
            errors.append(f"{where}: path_params.{name} does not appear in path {route.path!r}")

    wildcards = wildcard_params_in(route.path)
    for name in wildcards:
        if not route.path.rstrip("/").endswith("{*" + name + "}"):
            errors.append(
                f"{where}: catch-all {{*{name}}} must be the last segment of the path"
            )
    if len(wildcards) > 1:
        errors.append(f"{where}: a path may declare at most one catch-all")

    for param in route.path_params:
        pwhere = f"{where}.path_params.{param.wire}"
        errors += _validate_refs(rmap, param.type, pwhere)
        if not param.required:
            errors.append(f"{pwhere}: a path parameter cannot be optional")
        if not rmap.is_scalar_like(param.type):
            errors.append(
                f"{pwhere}: path parameters must be scalar, enum, or a scalar newtype -- "
                f"{param.type} cannot be rendered into a URL segment"
            )
    return errors


def _validate_query_params(rmap: RouteMap, route: Route, where: str) -> list[str]:
    errors: list[str] = []
    seen: set[str] = set()
    for param in route.query_params:
        pwhere = f"{where}.query_params.{param.wire}"
        if param.wire in seen:
            errors.append(f"{pwhere}: duplicate query parameter")
        seen.add(param.wire)
        if not IDENT_RE.match(param.wire):
            errors.append(f"{pwhere}: query parameter name must be an identifier")
        errors += _validate_refs(rmap, param.type, pwhere)

        target = rmap.underlying(param.type)
        inner = target.item if isinstance(target, ListOf) else target
        if isinstance(target, (MapOf,)) or not rmap.is_scalar_like(inner):
            errors.append(
                f"{pwhere}: query parameters must be scalar, enum, or a list of those -- "
                f"{param.type} has no canonical URL encoding"
            )
    return errors


def _validate_bodies(rmap: RouteMap, route: Route, where: str) -> list[str]:
    errors: list[str] = []

    if route.content_type not in ("json", "form"):
        errors.append(
            f"{where}: content_type must be 'json' or 'form', got {route.content_type!r}"
        )

    if route.request is not None:
        errors += _validate_refs(rmap, route.request, f"{where}.request")
        if not (set(route.methods) & BODY_METHODS):
            errors.append(
                f"{where}: declares a request body but its methods are "
                f"{route.methods} -- only {sorted(BODY_METHODS)} carry one"
            )
        if not rmap.is_record(route.request):
            errors.append(
                f"{where}.request: a request body must be a record -- "
                f"{route.request} does not serialise to a JSON object"
            )
        elif route.content_type == "form":
            # A form post is flat key=value pairs; a nested record has no
            # canonical urlencoded spelling that every language agrees on.
            defn = rmap.resolve(rmap.underlying(route.request))
            for fld in getattr(defn, "fields", []):
                if not rmap.is_scalar_like(fld.type) and not isinstance(
                    rmap.underlying(fld.type), MapOf
                ):
                    errors.append(
                        f"{where}.request: field {fld.wire!r} is {fld.type}, which has no "
                        f"canonical form encoding -- a form body takes scalars and string maps"
                    )

    if route.response is not None:
        errors += _validate_refs(rmap, route.response, f"{where}.response")

    for code, expr in route.errors.items():
        ewhere = f"{where}.errors.{code}"
        if not (code.isdigit() and 400 <= int(code) <= 599):
            errors.append(f"{ewhere}: error key must be an HTTP status in 400..599")
        errors += _validate_refs(rmap, expr, ewhere)
    return errors


def _validate_delivery(rmap: RouteMap, route: Route, where: str) -> list[str]:
    """The rules that make `delivery: opto_sync_queued` safe.

    These mirror constraints opto-sync enforces at runtime -- payload must be a
    JSON object, table must be a SQL-safe identifier -- so a mismatch is a
    generation-time error instead of a rejected mutation in the field.
    """
    errors: list[str] = []
    if route.delivery not in DELIVERIES:
        errors.append(f"{where}: delivery must be one of {list(DELIVERIES)}, got {route.delivery!r}")
        return errors

    if route.delivery == DELIVERY_OPTO_SYNC and not route.client:
        errors.append(
            f"{where}: a queued operation needs a generated client to enqueue it; "
            f"`client: false` and `delivery: opto_sync_queued` cannot both hold"
        )

    if route.delivery != DELIVERY_OPTO_SYNC:
        if route.opto_sync is not None:
            errors.append(f"{where}: opto_sync settings require delivery: {DELIVERY_OPTO_SYNC!r}")
        return errors

    if route.stream != STREAM_UNARY:
        errors.append(
            f"{where}: only a unary operation can be queued -- opto-sync's queue has no "
            f"per-mutation response channel, so there is nowhere for a stream to arrive"
        )

    if not set(route.methods) <= MUTATING_METHODS:
        errors.append(
            f"{where}: only mutating methods can be queued through opto-sync; "
            f"{sorted(set(route.methods) - MUTATING_METHODS)} must use delivery: direct"
        )

    if route.opto_sync is None:
        errors.append(f"{where}: delivery {DELIVERY_OPTO_SYNC!r} requires an `opto_sync` block")
        return errors

    opto = route.opto_sync
    if not OPTO_TABLE_RE.match(opto.table or ""):
        errors.append(
            f"{where}.opto_sync.table: {opto.table!r} is not a valid opto-sync scope id "
            f"(^[A-Za-z_][A-Za-z0-9_]{{0,62}}$)"
        )
    if opto.operation not in ("upsert", "delete"):
        errors.append(f"{where}.opto_sync.operation: must be 'upsert' or 'delete'")

    if opto.operation == "upsert":
        if route.request is None:
            errors.append(
                f"{where}: a queued upsert needs a request body -- opto-sync requires a payload"
            )
        elif not rmap.is_record(route.request):
            errors.append(
                f"{where}.request: opto-sync payloads must be JSON objects; {route.request} is not"
            )
    elif route.request is not None:
        # Mirrors the interfaces fixture `invalid/delete-with-payload.json`.
        errors.append(
            f"{where}: a queued delete must not carry a request body -- opto-sync tombstones "
            f"carry no data"
        )

    src = opto.record_id_from
    if not src:
        errors.append(f"{where}.opto_sync.record_id_from: required")
    else:
        errors += _validate_record_id_source(rmap, route, src, f"{where}.opto_sync.record_id_from")
    return errors


def _validate_record_id_source(rmap: RouteMap, route: Route, src: str, where: str) -> list[str]:
    """`record_id_from` names where the opto-sync record id comes from:
    `path.<param>`, `request.<field>`, or the literal `uuid` to mint a fresh one."""
    if src == "uuid":
        return []
    if "." not in src:
        return [f"{where}: expected 'path.<param>', 'request.<field>', or 'uuid', got {src!r}"]
    scope, _, name = src.partition(".")
    if scope == "path":
        if not any(p.wire == name for p in route.path_params):
            return [f"{where}: no path parameter named {name!r}"]
        return []
    if scope == "request":
        if route.request is None:
            return [f"{where}: references a request field but the route has no request body"]
        defn = rmap.resolve(rmap.underlying(route.request))
        if not isinstance(defn, RecordDef):
            return [f"{where}: request body is not a record"]
        if not any(f.wire == name for f in defn.fields):
            return [f"{where}: request body has no field named {name!r}"]
        return []
    return [f"{where}: unknown source scope {scope!r}"]


def _validate_connect(route: Route, where: str) -> list[str]:
    """PascalCase keys promise Connect-shaped JSON unary. v1 let that promise
    drift -- a PascalCase key on `/healthz` produced a descriptor advertising
    the service as `default`. Here it is simply an error."""
    if not route.is_connect_unary:
        return []
    errors: list[str] = []
    if route.methods != ["POST"]:
        errors.append(
            f"{where}: a PascalCase key is Connect JSON unary and must declare exactly "
            f'["POST"], got {route.methods}'
        )
    if not route.over_http:
        errors.append(
            f"{where}: a PascalCase key promises Connect JSON unary, which is an HTTP "
            f"shape; transports must include 'http'. It may additionally list framed "
            f"transports"
        )
    if route.stream != STREAM_UNARY:
        errors.append(f"{where}: Connect JSON unary is unary; {route.stream!r} is not")
    segments = route.path.strip("/").split("/")
    if len(segments) != 2 or "." not in segments[0]:
        errors.append(
            f"{where}: Connect unary path must be /<package.Service>/<Method>, got {route.path!r}"
        )
    elif segments[1] != route.key:
        errors.append(
            f"{where}: Connect unary path method segment {segments[1]!r} must equal the key"
        )
    return errors


# --------------------------------------------------------------------------

def _validate_uniqueness(rmap: RouteMap) -> list[str]:
    """No two operations may claim the same (path, method).

    v1 allowed it and the OpenAPI projection silently dropped whichever key
    sorted first, so an entire operation vanished from the published document
    with no error anywhere.
    """
    errors: list[str] = []
    seen: dict[tuple[str, str], str] = {}
    keys: set[str] = set()
    for route in rmap.routes:
        if route.key in keys:
            errors.append(f"map.{route.key}: duplicate operation key")
        keys.add(route.key)
        for method in route.methods:
            slot = (route.path, method)
            if slot in seen:
                errors.append(
                    f"map.{route.key}: {method} {route.path} is already claimed by "
                    f"{seen[slot]!r} -- one path+method belongs to exactly one operation"
                )
            else:
                seen[slot] = route.key
    return errors
