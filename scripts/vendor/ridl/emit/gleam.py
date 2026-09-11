"""Gleam emitter.

Gleam has no runtime reflection, so every type gets an explicit decoder built
with `gleam/dynamic/decode` and an explicit encoder built with `gleam/json`.
That is more generated code than the other targets need, and it is exactly why
hand-writing it does not scale: the v1 Gleam client had no JSON parsing and no
validation at all, and its method inference silently disagreed with Rust's.
"""

from __future__ import annotations

import json

from .. import naming
from ..model import (
    BUILTINS, DELIVERY_OPTO_SYNC, AliasDef, EnumDef, ListOf, MapOf, Named,
    OptionOf, Param, RecordDef, Route, RouteMap, ScalarDef, TypeExpr,
)
from .base import client_routes, Emitted, Writer, header_lines, ordered_types, path_segments, queued_routes

LANG = "gleam"

_SCALARS = {
    "String": "String", "Bool": "Bool", "I32": "Int", "I64": "Int",
    "F64": "Float", "Uuid": "String", "DateTime": "String",
    "Decimal": "String", "Json": "dynamic.Dynamic",
}
_DECODERS = {
    "String": "decode.string", "Bool": "decode.bool", "I32": "decode.int",
    "I64": "decode.int", "F64": "decode.float", "Uuid": "decode.string",
    "DateTime": "decode.string", "Decimal": "decode.string",
    "Json": "decode.dynamic",
}
_ENCODERS = {
    "String": "json.string", "Bool": "json.bool", "I32": "json.int",
    "I64": "json.int", "F64": "json.float", "Uuid": "json.string",
    "DateTime": "json.string", "Decimal": "json.string",
}


def type_name(rmap: RouteMap, expr: TypeExpr) -> str:
    if isinstance(expr, Named):
        return _SCALARS[expr.name] if expr.name in BUILTINS else naming.pascal(expr.name)
    if isinstance(expr, ListOf):
        return f"List({type_name(rmap, expr.item)})"
    if isinstance(expr, MapOf):
        return f"dict.Dict(String, {type_name(rmap, expr.value)})"
    if isinstance(expr, OptionOf):
        return f"option.Option({type_name(rmap, expr.inner)})"
    return "dynamic.Dynamic"


def field_type(rmap: RouteMap, expr: TypeExpr, required: bool) -> str:
    inner = type_name(rmap, expr)
    return inner if required or inner.startswith("option.Option(") else f"option.Option({inner})"


def decoder(rmap: RouteMap, expr: TypeExpr) -> str:
    target = rmap.underlying(expr)
    if isinstance(target, ListOf):
        return f"decode.list({decoder(rmap, target.item)})"
    if isinstance(target, MapOf):
        return f"decode.dict(decode.string, {decoder(rmap, target.value)})"
    if isinstance(target, OptionOf):
        return f"decode.optional({decoder(rmap, target.inner)})"
    if isinstance(target, Named):
        if target.name in BUILTINS:
            return _DECODERS[target.name]
        defn = rmap.types.get(target.name)
        if isinstance(defn, ScalarDef):
            return _DECODERS[defn.base]
        return f"{naming.snake(target.name)}_decoder()"
    return "decode.dynamic"


def encoder(rmap: RouteMap, expr: TypeExpr, src: str) -> str:
    target = rmap.underlying(expr)
    if isinstance(target, ListOf):
        return f"json.array({src}, fn(item) {{ {encoder(rmap, target.item, 'item')} }})"
    if isinstance(target, MapOf):
        return (
            f"json.object(dict.to_list({src}) "
            f"|> list.map(fn(pair) {{ #(pair.0, {encoder(rmap, target.value, 'pair.1')}) }}))"
        )
    if isinstance(target, OptionOf):
        return (
            f"case {src} {{ option.Some(value) -> "
            f"{encoder(rmap, target.inner, 'value')} "
            f"option.None -> json.null() }}"
        )
    if isinstance(target, Named):
        if target.name in BUILTINS:
            return _ENCODERS.get(target.name, "json.null()") + f"({src})"
        defn = rmap.types.get(target.name)
        if isinstance(defn, ScalarDef):
            return _ENCODERS.get(defn.base, "json.string") + f"({src})"
        if isinstance(defn, EnumDef):
            return f"json.string({naming.snake(target.name)}_to_wire({src}))"
        return f"{naming.snake(target.name)}_to_json({src})"
    return "json.null()"


def _is_wildcard(route, wire: str) -> bool:
    return any(p.wire == wire and p.wildcard for p in route.path_params)


def emit(rmap: RouteMap) -> list[Emitted]:
    module = naming.snake(rmap.service) + "_ridl"
    w = Writer("  ")
    for line in header_lines(rmap, "////"):
        w.line(line)
    w.lines(
        "",
        "import gleam/dict",
        "import gleam/dynamic",
        "import gleam/dynamic/decode",
        "import gleam/json",
        "import gleam/list",
        "import gleam/option",
        "import gleam/string",
        "",
        f"pub const service = {json.dumps(rmap.service)}",
    )
    if rmap.version:
        w.line(f"pub const version = {json.dumps(rmap.version)}")
    w.blank()
    _emit_types(rmap, w)
    _emit_transport(w)
    _emit_operations(rmap, w)
    _emit_manifest(rmap, w)
    return [Emitted(path=f"gleam/{module}.gleam", text=w.render())]


def _emit_types(rmap: RouteMap, w: Writer) -> None:
    for name in ordered_types(rmap):
        defn = rmap.types[name]
        pas = naming.pascal(name)
        snake = naming.snake(name)
        w.doc(defn.doc, "///")
        if isinstance(defn, ScalarDef):
            w.line(f"pub type {pas} = {_SCALARS[defn.base]}")
        elif isinstance(defn, EnumDef):
            with w.block(f"pub type {pas}"):
                for variant in defn.variants:
                    w.line(naming.pascal(variant))
            w.blank()
            w.line(f"/// The wire value for a `{pas}`.")
            with w.block(f"pub fn {snake}_to_wire(value: {pas}) -> String"):
                with w.block("case value"):
                    for variant in defn.variants:
                        w.line(f"{naming.pascal(variant)} -> {json.dumps(variant)}")
            w.blank()
            with w.block(f"pub fn {snake}_from_wire(wire: String) -> Result({pas}, String)"):
                with w.block("case wire"):
                    for variant in defn.variants:
                        w.line(f"{json.dumps(variant)} -> Ok({naming.pascal(variant)})")
                    w.line(
                        f'other -> Error("not a {pas} variant: " <> other)'
                    )
            w.blank()
            with w.block(f"pub fn {snake}_decoder() -> decode.Decoder({pas})"):
                w.line("use wire <- decode.then(decode.string)")
                with w.block(f"case {snake}_from_wire(wire)"):
                    w.line("Ok(value) -> decode.success(value)")
                    w.line(
                        f"Error(_) -> decode.failure({naming.pascal(defn.variants[0])}, "
                        f"{json.dumps(pas)})"
                    )
        elif isinstance(defn, RecordDef):
            with w.block(f"pub type {pas}"):
                with w.block(pas, "(", ")"):
                    for fld in defn.fields:
                        ident = naming.escape(naming.snake(fld.wire), LANG)
                        w.line(f"{ident}: {field_type(rmap, fld.type, fld.required)},")
            w.blank()
            with w.block(f"pub fn {snake}_decoder() -> decode.Decoder({pas})"):
                for fld in defn.fields:
                    ident = naming.escape(naming.snake(fld.wire), LANG)
                    dec = decoder(rmap, fld.type)
                    if fld.required:
                        w.line(
                            f"use {ident} <- decode.field({json.dumps(fld.wire)}, {dec})"
                        )
                    else:
                        w.line(
                            f"use {ident} <- decode.optional_field("
                            f"{json.dumps(fld.wire)}, option.None, decode.optional({dec}))"
                        )
                args = ", ".join(
                    naming.escape(naming.snake(f.wire), LANG) for f in defn.fields
                )
                w.line(f"decode.success({pas}({args}))")
            w.blank()
            with w.block(f"pub fn {snake}_to_json(value: {pas}) -> json.Json"):
                w.line("json.object([")
                w.indent()
                for fld in defn.fields:
                    ident = naming.escape(naming.snake(fld.wire), LANG)
                    if fld.required:
                        enc = encoder(rmap, fld.type, f"value.{ident}")
                    else:
                        enc = encoder(rmap, OptionOf(fld.type), f"value.{ident}")
                    w.line(f"#({json.dumps(fld.wire)}, {enc}),")
                w.dedent()
                w.line("])")
        elif isinstance(defn, AliasDef):
            w.line(f"pub type {pas} = {type_name(rmap, defn.target)}")
        w.blank()


def _emit_transport(w: Writer) -> None:
    w.lines("//// ---------------------------------------------------- transport", "")
    w.line("/// How a call reaches the server.")
    with w.block("pub type Delivery"):
        w.lines("Direct", "OptoSyncQueued")
    w.blank()
    w.line("/// Where a queued call's opto-sync record id comes from.")
    with w.block("pub type RecordIdSource"):
        w.lines("FromPath(name: String)", "FromRequest(name: String)", "Minted")
    w.blank()
    w.line("/// Static description of a queued call's opto-sync record.")
    with w.block("pub type OptoSyncBinding"):
        with w.block("OptoSyncBinding", "(", ")"):
            w.lines("table: String,", "operation: String,", "record_id: RecordIdSource,")
    w.blank()
    w.line("/// One outbound call, fully resolved. Transports never re-derive paths.")
    with w.block("pub type RpcRequest"):
        with w.block("RpcRequest", "(", ")"):
            w.lines(
                "key: String,", "method: String,", "path: String,",
                "path_template: String,",
                "query: List(#(String, String)),",
                "body: option.Option(String),",
                "delivery: Delivery,",
                "opto_sync: option.Option(OptoSyncBinding),",
            )
    w.blank()
    w.lines(
        "/// The single seam between generated code and the network.",
        "///",
        "/// Supply a function that performs `request` and returns the raw JSON body.",
        "/// The dependency runs one way: this module calls opto-sync, never the",
        "/// reverse.",
        "pub type Transport(e) =",
        "  fn(RpcRequest) -> Result(String, e)",
        "",
        "/// A call can fail in the transport or in decoding, and the two are",
        "/// different problems for the caller.",
    )
    with w.block("pub type RpcError(e)"):
        w.lines("TransportError(e)", "DecodeError(List(decode.DecodeError))")
    w.blank()
    w.lines(
        "/// Escape one path segment so an id containing `/` cannot reshape the URL.",
        "pub fn encode_segment(value: String) -> String {",
        "  value",
        '  |> string.replace("%", "%25")',
        '  |> string.replace("/", "%2F")',
        '  |> string.replace("?", "%3F")',
        '  |> string.replace("#", "%23")',
        '  |> string.replace(" ", "%20")',
        "}",
        "",
    )


def _param_ident(p: Param) -> str:
    return naming.escape(naming.snake(p.wire), LANG)


def _emit_operations(rmap: RouteMap, w: Writer) -> None:
    for route in client_routes(rmap):
        fn = naming.escape(naming.snake(f"{route.key}_path"), LANG)
        args = ", ".join(
            f"{_param_ident(p)} {_param_ident(p)}: {type_name(rmap, p.type)}"
            for p in route.path_params
        )
        w.line(f"/// `{route.primary_method} {route.path}`")
        with w.block(f"pub fn {fn}({args}) -> String"):
            if not route.path_params:
                w.line(json.dumps(route.path))
            else:
                parts = []
                for text, is_param in path_segments(route.path):
                    if is_param:
                        ident = next(
                            (_param_ident(p) for p in route.path_params if p.wire == text),
                            naming.snake(text),
                        )
                        parts.append(f"encode_segment({ident})")
                    else:
                        parts.append(json.dumps(text))
                w.line(" <> ".join(parts))
        w.blank()

    for route in client_routes(rmap):
        _emit_request_builder(rmap, route, w)


def _emit_request_builder(rmap: RouteMap, route: Route, w: Writer) -> None:
    """Gleam gets a request builder plus a decoder rather than a call function,
    because effects belong at the edge of a Gleam program, not in a library."""
    fn = naming.escape(naming.snake(f"{route.key}_request"), LANG)
    args = [f"{_param_ident(p)} {_param_ident(p)}: {type_name(rmap, p.type)}"
            for p in route.path_params]
    for param in route.query_params:
        args.append(
            f"{_param_ident(param)} {_param_ident(param)}: "
            f"{field_type(rmap, param.type, param.required)}"
        )
    if route.request is not None:
        args.append(f"body body: {type_name(rmap, route.request)}")

    w.doc(route.summary or route.doc, "///")
    with w.block(f"pub fn {fn}({', '.join(args)}) -> RpcRequest"):
        path_args = ", ".join(
            f"{_param_ident(p)}: {_param_ident(p)}" for p in route.path_params
        )
        w.line(f"let path = {naming.snake(route.key)}_path({path_args})")
        if route.query_params:
            w.line("let query =")
            w.indent()
            w.line("[")
            w.indent()
            for param in route.query_params:
                ident = _param_ident(param)
                key = json.dumps(param.wire)
                is_list = isinstance(rmap.underlying(param.type), ListOf)
                if is_list:
                    w.line(
                        f"..list.map({ident}, fn(item) {{ #({key}, string.inspect(item)) }})"
                    )
                elif param.required:
                    w.line(f"#({key}, string.inspect({ident})),")
                else:
                    w.line(
                        f"..case {ident} {{ option.Some(v) -> [#({key}, string.inspect(v))] "
                        f"option.None -> [] }}"
                    )
            w.dedent()
            w.line("]")
            w.dedent()
        else:
            w.line("let query = []")
        if route.request is not None:
            target = rmap.underlying(route.request)
            w.line(
                f"let body = option.Some(json.to_string("
                f"{naming.snake(target.name)}_to_json(body)))"
            )
        else:
            w.line("let body = option.None")
        opto = "option.None"
        if route.opto_sync is not None:
            src = route.opto_sync.record_id_from
            if src == "uuid":
                rid = "Minted"
            else:
                scope, _, name = src.partition(".")
                rid = f"From{naming.pascal(scope)}({json.dumps(name)})"
            opto = (
                f"option.Some(OptoSyncBinding({json.dumps(route.opto_sync.table)}, "
                f"{json.dumps(route.opto_sync.operation)}, {rid}))"
            )
        delivery = "OptoSyncQueued" if route.delivery == DELIVERY_OPTO_SYNC else "Direct"
        w.line("RpcRequest(")
        w.indent()
        w.lines(
            f"key: {json.dumps(route.key)},",
            f"method: {json.dumps(route.primary_method)},",
            "path: path,", f"path_template: {json.dumps(route.path)},",
            "query: query,", "body: body,",
            f"delivery: {delivery},", f"opto_sync: {opto},",
        )
        w.dedent()
        w.line(")")
    w.blank()

    if route.response is not None:
        target = rmap.underlying(route.response)
        dec_fn = naming.escape(naming.snake(f"{route.key}_response_decoder"), LANG)
        w.line(f"/// Decoder for the `{route.key}` success payload.")
        with w.block(
            f"pub fn {dec_fn}() -> decode.Decoder({type_name(rmap, route.response)})"
        ):
            w.line(decoder(rmap, route.response))
        w.blank()


def _emit_manifest(rmap: RouteMap, w: Writer) -> None:
    w.line("/// One row per declared operation.")
    with w.block("pub type OperationInfo"):
        with w.block("OperationInfo", "(", ")"):
            w.lines(
                "key: String,", "path: String,",
                "methods: List(String),", "delivery: Delivery,",
            )
    w.blank()
    w.line("/// Every operation in the route map, in declaration order.")
    with w.block("pub fn operations() -> List(OperationInfo)"):
        w.line("[")
        w.indent()
        for route in rmap.routes:
            methods = ", ".join(json.dumps(m) for m in route.methods)
            delivery = "OptoSyncQueued" if route.delivery == DELIVERY_OPTO_SYNC else "Direct"
            w.line(
                f"OperationInfo({json.dumps(route.key)}, {json.dumps(route.path)}, "
                f"[{methods}], {delivery}),"
            )
        w.dedent()
        w.line("]")
    w.blank()
    queued = ", ".join(json.dumps(r.key) for r in queued_routes(rmap))
    w.line("/// Operations that route through opto-sync's durable queue.")
    with w.block("pub fn queued_operations() -> List(String)"):
        w.line(f"[{queued}]")
    w.blank()
