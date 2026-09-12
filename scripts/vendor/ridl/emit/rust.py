"""Rust emitter.

Produces one `.rs` file: serde types, a typed operation per route, and a
transport seam. The point is that `get_matter(&t, "not-a-uuid")` does not
compile, and neither does forgetting a required query parameter -- v1's Rust
"typed surface" was a trait whose associated types had no bound and no relation
to any route map, so it enforced nothing beyond fn-pointer coercion.
"""

from __future__ import annotations

from .. import naming
from ..model import (
    BUILTINS,
    DELIVERY_OPTO_SYNC,
    AliasDef,
    EnumDef,
    ListOf,
    MapOf,
    Named,
    OptionOf,
    Param,
    RecordDef,
    Route,
    RouteMap,
    ScalarDef,
    TypeExpr,
)
from .base import client_routes, Emitted, Writer, header_lines, ordered_types, path_segments, queued_routes

LANG = "rust"

_SCALARS = {
    "String": "String",
    "Bool": "bool",
    "I32": "i32",
    "I64": "i64",
    "F64": "f64",
    "Uuid": "uuid::Uuid",
    "DateTime": "String",
    "Decimal": "String",
    "Json": "serde_json::Value",
}


def type_name(rmap: RouteMap, expr: TypeExpr, owned: bool = True) -> str:
    if isinstance(expr, Named):
        if expr.name in BUILTINS:
            return _SCALARS[expr.name]
        return naming.pascal(expr.name)
    if isinstance(expr, ListOf):
        return f"Vec<{type_name(rmap, expr.item)}>"
    if isinstance(expr, MapOf):
        return f"std::collections::BTreeMap<String, {type_name(rmap, expr.value)}>"
    if isinstance(expr, OptionOf):
        return f"Option<{type_name(rmap, expr.inner)}>"
    return "serde_json::Value"


def field_type(rmap: RouteMap, expr: TypeExpr, required: bool) -> str:
    inner = type_name(rmap, expr)
    if required or inner.startswith("Option<"):
        return inner
    return f"Option<{inner}>"


def _default_fn_name(type_name_: str, field_wire: str) -> str:
    return f"default_{naming.snake(type_name_)}_{naming.snake(field_wire)}"


def rust_literal(value: object, ty: str) -> str:
    """Render a route-map default as a Rust expression of type `ty`."""
    import json as _json

    if value is None:
        return "Default::default()"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        if ty == "String":
            return _json.dumps(value) + ".to_string()"
        return f"{ty}({_json.dumps(value)}.to_string())"
    return "Default::default()"


def _is_wildcard(route, wire: str) -> bool:
    return any(p.wire == wire and p.wildcard for p in route.path_params)


def emit(rmap: RouteMap) -> list[Emitted]:
    w = Writer()
    for line in header_lines(rmap, "//!"):
        w.line(line)
    w.lines(
        "//!",
        "//! Every operation below takes exactly the parameters the route map declares.",
        "//! A missing or wrongly-typed argument is a compile error, not a 404.",
        "",
        "#![allow(clippy::too_many_arguments, dead_code)]",
        "",
        "use serde::{Deserialize, Serialize};",
        "",
    )

    w.line(f'pub const SERVICE: &str = "{rmap.service}";')
    if rmap.version:
        w.line(f'pub const VERSION: &str = "{rmap.version}";')
    w.blank()

    _emit_types(rmap, w)
    _emit_error(w)
    _emit_transport(rmap, w)
    _emit_operations(rmap, w)

    return [Emitted(path="rust/ridl_generated.rs", text=w.render())]


# --------------------------------------------------------------------------

def _emit_types(rmap: RouteMap, w: Writer) -> None:
    if not rmap.types:
        return
    w.line("// ---------------------------------------------------------------- types")
    w.blank()
    defaults: list[tuple[str, str, object]] = []
    for name in ordered_types(rmap):
        defn = rmap.types[name]
        w.doc(defn.doc, "///")
        if isinstance(defn, RecordDef):
            w.line("#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]")
            w.line("#[serde(deny_unknown_fields)]")
            with w.block(f"pub struct {naming.pascal(name)}"):
                for fld in defn.fields:
                    ident = naming.escape(naming.snake(fld.wire), LANG)
                    w.doc(fld.doc, "///")
                    if ident.lstrip("r#") != fld.wire:
                        w.line(f'#[serde(rename = "{fld.wire}")]')
                    if fld.has_default:
                        # A declared default is a real value, not an absence, so
                        # the field keeps its concrete type and serde fills it in.
                        helper = _default_fn_name(name, fld.wire)
                        defaults.append((helper, type_name(rmap, fld.type), fld.default))
                        w.line(f'#[serde(default = "{helper}")]')
                        w.line(f"pub {ident}: {type_name(rmap, fld.type)},")
                        continue
                    if not fld.required:
                        w.line("#[serde(default, skip_serializing_if = \"Option::is_none\")]")
                    w.line(f"pub {ident}: {field_type(rmap, fld.type, fld.required)},")
        elif isinstance(defn, EnumDef):
            w.line("#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]")
            w.line('#[serde(rename_all = "snake_case")]')
            with w.block(f"pub enum {naming.pascal(name)}"):
                for variant in defn.variants:
                    w.line(f"{naming.pascal(variant)},")
            with w.block(f"impl {naming.pascal(name)}"):
                with w.block("pub fn as_str(&self) -> &'static str"):
                    with w.block("match self"):
                        for variant in defn.variants:
                            w.line(f'Self::{naming.pascal(variant)} => "{variant}",')
            with w.block(f"impl std::fmt::Display for {naming.pascal(name)}"):
                with w.block(
                    "fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result"
                ):
                    w.line("f.write_str(self.as_str())")
        elif isinstance(defn, ScalarDef):
            base = _SCALARS[defn.base]
            w.line("#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]")
            w.line("#[serde(transparent)]")
            w.line(f"pub struct {naming.pascal(name)}(pub {base});")
            with w.block(f"impl std::fmt::Display for {naming.pascal(name)}"):
                with w.block(
                    "fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result"
                ):
                    w.line("std::fmt::Display::fmt(&self.0, f)")
        elif isinstance(defn, AliasDef):
            w.line(f"pub type {naming.pascal(name)} = {type_name(rmap, defn.target)};")
        w.blank()

    for helper, ty, value in defaults:
        with w.block(f"fn {helper}() -> {ty}"):
            w.line(rust_literal(value, ty))
        w.blank()


def _emit_error(w: Writer) -> None:
    w.lines(
        "// ------------------------------------------------------------- transport",
        "",
        "/// How a call reaches the server.",
        "#[derive(Clone, Copy, Debug, PartialEq, Eq)]",
    )
    with w.block("pub enum Delivery"):
        w.lines(
            "/// Straight to the server; the caller awaits the response.",
            "Direct,",
            "/// Enqueued as an opto-sync record mutation, so it survives being offline.",
            "/// Only mutating operations with a JSON-object payload can be queued.",
            "OptoSyncQueued,",
        )
    w.blank()
    w.line("/// Where a queued call's opto-sync record id comes from.")
    w.line("#[derive(Clone, Copy, Debug, PartialEq, Eq)]")
    with w.block("pub enum RecordIdSource"):
        w.lines("PathParam(&'static str),", "RequestField(&'static str),", "Minted,")
    w.blank()
    w.line("/// Static description of a queued call's opto-sync binding.")
    w.line("#[derive(Clone, Copy, Debug, PartialEq, Eq)]")
    with w.block("pub struct OptoSyncBinding"):
        w.lines(
            "pub table: &'static str,",
            "pub operation: &'static str,",
            "pub record_id: RecordIdSource,",
        )
    w.blank()
    w.line("/// One outbound call, fully resolved. Transports never re-derive paths.")
    w.line("#[derive(Clone, Debug)]")
    with w.block("pub struct RpcRequest"):
        w.lines(
            "pub key: &'static str,",
            "pub method: &'static str,",
            "/// Path with every parameter already substituted and percent-encoded.",
            "pub path: String,",
            "/// The unsubstituted template, so a transport can locate a named",
            "/// parameter inside `path` instead of guessing at its position.",
            "pub path_template: &'static str,",
            "pub query: Vec<(String, String)>,",
            "/// JSON body, or `None` for operations that carry none.",
            "pub body: Option<String>,",
            "pub delivery: Delivery,",
            "pub opto_sync: Option<OptoSyncBinding>,",
        )
    w.blank()
    with w.block("pub enum RpcError<E>"):
        w.lines(
            "Transport(E),",
            "Decode(serde_json::Error),",
            "Encode(serde_json::Error),",
        )
    w.blank()
    with w.block("impl<E: std::fmt::Debug> std::fmt::Debug for RpcError<E>"):
        with w.block("fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result"):
            with w.block("match self"):
                w.lines(
                    'Self::Transport(e) => write!(f, "transport: {e:?}"),',
                    'Self::Decode(e) => write!(f, "decode: {e}"),',
                    'Self::Encode(e) => write!(f, "encode: {e}"),',
                )
    w.blank()


def _emit_transport(rmap: RouteMap, w: Writer) -> None:
    w.lines(
        "/// The single seam between generated code and the network.",
        "///",
        "/// Implement this over plain HTTP for `Delivery::Direct`, and over an",
        "/// opto-sync queue for `Delivery::OptoSyncQueued`. The dependency runs one",
        "/// way: this crate calls opto-sync, never the reverse.",
    )
    with w.block("pub trait RpcTransport"):
        w.lines(
            "type Error;",
            "/// Perform `request` and return the raw JSON response body.",
            "fn call(&self, request: RpcRequest) -> Result<String, Self::Error>;",
        )
    w.blank()
    w.lines(
        "/// Percent-encode one path segment. Generated code always calls this, so a",
        "/// record id containing `/` cannot silently reshape the URL.",
    )
    with w.block("pub fn encode_segment(value: &str) -> String"):
        w.line("let mut out = String::with_capacity(value.len());")
        with w.block("for byte in value.as_bytes()"):
            with w.block("match byte"):
                w.lines(
                    "b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {",
                    "    out.push(*byte as char)",
                    "}",
                    'other => out.push_str(&format!("%{other:02X}")),',
                )
        w.line("out")
    w.blank()


# --------------------------------------------------------------------------

def _param_ident(param: Param) -> str:
    return naming.escape(naming.snake(param.wire), LANG)


def _query_struct(route: Route) -> str:
    return f"{naming.pascal(route.key)}Query"


def _emit_operations(rmap: RouteMap, w: Writer) -> None:
    w.line("// ------------------------------------------------------------- operations")
    w.blank()

    for route in client_routes(rmap):
        if route.query_params:
            w.doc(f"Query parameters for `{route.key}`.", "///")
            w.line("#[derive(Clone, Debug, Default, Serialize)]")
            with w.block(f"pub struct {_query_struct(route)}"):
                for param in route.query_params:
                    w.doc(param.doc, "///")
                    w.line(
                        f"pub {_param_ident(param)}: "
                        f"{field_type(rmap, param.type, param.required)},"
                    )
            w.blank()

    for route in client_routes(rmap):
        _emit_path_fn(rmap, route, w)

    for route in client_routes(rmap):
        _emit_call_fn(rmap, route, w)

    _emit_manifest(rmap, w)


def _emit_path_fn(rmap: RouteMap, route: Route, w: Writer) -> None:
    fn = naming.escape(f"{naming.snake(route.key)}_path", LANG)
    args = ", ".join(
        f"{_param_ident(p)}: &{type_name(rmap, p.type)}" for p in route.path_params
    )
    w.doc(f"`{route.primary_method} {route.path}`", "///")
    with w.block(f"pub fn {fn}({args}) -> String"):
        if not route.path_params:
            w.line(f'"{route.path}".to_string()')
        else:
            w.line("let mut out = String::new();")
            for text, is_param in path_segments(route.path):
                if is_param:
                    ident = naming.escape(naming.snake(text), LANG)
                    w.line(f"out.push_str(&encode_segment(&{ident}.to_string()));")
                else:
                    w.line(f'out.push_str("{text}");')
            w.line("out")
    w.blank()


def _emit_call_fn(rmap: RouteMap, route: Route, w: Writer) -> None:
    fn = naming.escape(naming.snake(route.key), LANG)
    args = ["transport: &T"]
    args += [f"{_param_ident(p)}: &{type_name(rmap, p.type)}" for p in route.path_params]
    if route.query_params:
        args.append(f"query: &{_query_struct(route)}")
    if route.request is not None:
        args.append(f"body: &{type_name(rmap, route.request)}")

    ret = type_name(rmap, route.response) if route.response is not None else "()"
    sig = (
        f"pub fn {fn}<T: RpcTransport>({', '.join(args)}) "
        f"-> Result<{ret}, RpcError<T::Error>>"
    )

    w.doc(route.summary or route.doc, "///")
    if route.deprecated:
        w.line('#[deprecated(note = "declared deprecated in the route map")]')
    with w.block(sig):
        path_args = ", ".join(_param_ident(p) for p in route.path_params)
        w.line(f"let path = {naming.snake(route.key)}_path({path_args});")

        if route.query_params:
            w.line("let mut query_pairs: Vec<(String, String)> = Vec::new();")
            for param in route.query_params:
                ident = _param_ident(param)
                # A list-valued query parameter repeats the key once per item,
                # which is the only encoding every target language agrees on.
                is_list = isinstance(rmap.underlying(param.type), ListOf)
                if param.required:
                    if is_list:
                        with w.block(f"for item in &query.{ident}"):
                            w.line(
                                f'query_pairs.push(("{param.wire}".to_string(), '
                                f"query_value(item)));"
                            )
                    else:
                        w.line(
                            f'query_pairs.push(("{param.wire}".to_string(), '
                            f"query_value(&query.{ident})));"
                        )
                else:
                    with w.block(f"if let Some(value) = &query.{ident}"):
                        if is_list:
                            with w.block("for item in value"):
                                w.line(
                                    f'query_pairs.push(("{param.wire}".to_string(), '
                                    f"query_value(item)));"
                                )
                        else:
                            w.line(
                                f'query_pairs.push(("{param.wire}".to_string(), '
                                f"query_value(value)));"
                            )
        else:
            w.line("let query_pairs: Vec<(String, String)> = Vec::new();")

        if route.request is not None:
            w.line("let body = Some(serde_json::to_string(body).map_err(RpcError::Encode)?);")
        else:
            w.line("let body: Option<String> = None;")

        w.line("let request = RpcRequest {")
        w.indent()
        w.lines(
            f'key: "{route.key}",',
            f'method: "{route.primary_method}",',
            "path,",
            f'path_template: "{route.path}",',
            "query: query_pairs,",
            "body,",
            f"delivery: Delivery::{'OptoSyncQueued' if route.delivery == DELIVERY_OPTO_SYNC else 'Direct'},",
        )
        if route.opto_sync is not None:
            src = route.opto_sync.record_id_from
            if src == "uuid":
                rid = "RecordIdSource::Minted"
            elif src.startswith("path."):
                rid = f'RecordIdSource::PathParam("{src.split(".", 1)[1]}")'
            else:
                rid = f'RecordIdSource::RequestField("{src.split(".", 1)[1]}")'
            w.line("opto_sync: Some(OptoSyncBinding {")
            w.indent()
            w.lines(
                f'table: "{route.opto_sync.table}",',
                f'operation: "{route.opto_sync.operation}",',
                f"record_id: {rid},",
            )
            w.dedent()
            w.line("}),")
        else:
            w.line("opto_sync: None,")
        w.dedent()
        w.line("};")

        w.line("let raw = transport.call(request).map_err(RpcError::Transport)?;")
        if route.response is None:
            w.line("let _ = raw;")
            w.line("Ok(())")
        else:
            w.line("serde_json::from_str(&raw).map_err(RpcError::Decode)")
    w.blank()


def _emit_manifest(rmap: RouteMap, w: Writer) -> None:
    """A runtime-inspectable list of every operation, so a server can assert it
    mounted the whole contract instead of trusting a regex over source text."""
    w.lines(
        "// -------------------------------------------------------------- manifest",
        "",
        "/// One row per declared operation.",
        "#[derive(Clone, Copy, Debug, PartialEq, Eq)]",
    )
    with w.block("pub struct OperationInfo"):
        w.lines(
            "pub key: &'static str,",
            "pub path: &'static str,",
            "pub methods: &'static [&'static str],",
            "pub delivery: Delivery,",
            "/// Which wires may carry this operation.",
            "pub transports: &'static [&'static str],",
            "/// \"unary\", \"server_stream\", \"client_stream\", or \"bidi\".",
            "pub stream: &'static str,",
        )
    w.blank()
    w.line("/// Every operation in the route map, in declaration order.")
    with w.block(f"pub const OPERATIONS: [OperationInfo; {len(rmap.routes)}] =", "[", "];"):
        for route in rmap.routes:
            methods = ", ".join(f'"{m}"' for m in route.methods)
            delivery = (
                "Delivery::OptoSyncQueued"
                if route.delivery == DELIVERY_OPTO_SYNC
                else "Delivery::Direct"
            )
            transports = ", ".join(f'"{t}"' for t in route.transports)
            w.line(
                f'OperationInfo {{ key: "{route.key}", path: "{route.path}", '
                f"methods: &[{methods}], delivery: {delivery}, "
                f'transports: &[{transports}], stream: "{route.stream}" }},'
            )
    w.blank()
    queued = queued_routes(rmap)
    w.line(f"/// Operations that route through opto-sync's durable queue ({len(queued)}).")
    with w.block(f"pub const QUEUED_OPERATIONS: [&str; {len(queued)}] =", "[", "];"):
        for route in queued:
            w.line(f'"{route.key}",')
    w.blank()
    w.lines(
        "/// Render a query value. Kept in one place so every operation encodes",
        "/// booleans and numbers the same way.",
    )
    with w.block("fn query_value<T: std::fmt::Display>(value: &T) -> String"):
        w.line("value.to_string()")
    w.blank()
