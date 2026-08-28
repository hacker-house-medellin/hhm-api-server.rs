"""Dart emitter.

Records become immutable classes with explicit `fromJson`/`toJson`, enums become
real Dart enums carrying their wire value, and scalar newtypes become extension
types -- zero-cost at runtime but distinct to the analyzer, so a raw `String`
cannot be passed where a `MatterId` is required.

Requires Dart 3.3+ for extension types.
"""

from __future__ import annotations

import json

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

LANG = "dart"

_SCALARS = {
    "String": "String",
    "Bool": "bool",
    "I32": "int",
    "I64": "int",
    "F64": "double",
    "Uuid": "String",
    "DateTime": "String",
    "Decimal": "String",
    "Json": "Object?",
}


def type_name(rmap: RouteMap, expr: TypeExpr) -> str:
    if isinstance(expr, Named):
        if expr.name in BUILTINS:
            return _SCALARS[expr.name]
        return naming.pascal(expr.name)
    if isinstance(expr, ListOf):
        return f"List<{type_name(rmap, expr.item)}>"
    if isinstance(expr, MapOf):
        return f"Map<String, {type_name(rmap, expr.value)}>"
    if isinstance(expr, OptionOf):
        inner = type_name(rmap, expr.inner)
        return inner if inner.endswith("?") else f"{inner}?"
    return "Object?"


def field_type(rmap: RouteMap, expr: TypeExpr, required: bool) -> str:
    inner = type_name(rmap, expr)
    if required or inner.endswith("?"):
        return inner
    return f"{inner}?"


def _from_json(rmap: RouteMap, expr: TypeExpr, src: str, nullable: bool) -> str:
    """A Dart expression decoding `src` (an `Object?`) into `expr`."""
    target = rmap.underlying(expr)
    if isinstance(target, OptionOf):
        return _from_json(rmap, target.inner, src, True)
    if isinstance(target, ListOf):
        item = _from_json(rmap, target.item, "e", False)
        cast = f"($src as List<Object?>).map((e) => {item}).toList(growable: false)"
        return f"$src == null ? const [] : {cast}".replace("$src", src) if not nullable else \
            f"{src} == null ? null : ({src} as List<Object?>).map((e) => {item}).toList(growable: false)"
    if isinstance(target, MapOf):
        value = _from_json(rmap, target.value, "v", False)
        body = (
            f"({src} as Map<String, Object?>).map((k, v) => MapEntry(k, {value}))"
        )
        return f"{src} == null ? const {{}} : {body}" if not nullable else \
            f"{src} == null ? null : {body}"
    if isinstance(target, Named):
        name = target.name
        if name in BUILTINS:
            dart = _SCALARS[name]
            if dart == "Object?":
                return src
            if dart == "double":
                return f"({src} as num{'?' if nullable else ''}){'?' if nullable else ''}.toDouble()"
            return f"{src} as {dart}{'?' if nullable else ''}"
        defn = rmap.types.get(name)
        pas = naming.pascal(name)
        if isinstance(defn, EnumDef):
            call = f"{pas}.fromWire({src} as String)"
            return f"{src} == null ? null : {call}" if nullable else call
        if isinstance(defn, ScalarDef):
            base = _SCALARS[defn.base]
            call = f"{pas}({src} as {base})"
            return f"{src} == null ? null : {call}" if nullable else call
        if isinstance(defn, RecordDef):
            call = f"{pas}.fromJson({src} as Map<String, Object?>)"
            return f"{src} == null ? null : {call}" if nullable else call
    return src


def _to_json(rmap: RouteMap, expr: TypeExpr, src: str, nullable: bool) -> str:
    target = rmap.underlying(expr)
    if isinstance(target, OptionOf):
        return _to_json(rmap, target.inner, src, True)
    if isinstance(target, ListOf):
        item = _to_json(rmap, target.item, "e", False)
        body = f"{src}{'?' if nullable else ''}.map((e) => {item}).toList(growable: false)"
        return body
    if isinstance(target, MapOf):
        value = _to_json(rmap, target.value, "v", False)
        return f"{src}{'?' if nullable else ''}.map((k, v) => MapEntry(k, {value}))"
    if isinstance(target, Named):
        name = target.name
        if name in BUILTINS:
            return src
        defn = rmap.types.get(name)
        if isinstance(defn, EnumDef):
            return f"{src}{'?' if nullable else ''}.wire"
        if isinstance(defn, ScalarDef):
            return f"{src}{'?' if nullable else ''}.value"
        if isinstance(defn, RecordDef):
            return f"{src}{'?' if nullable else ''}.toJson()"
    return src


def _is_wildcard(route, wire: str) -> bool:
    return any(p.wire == wire and p.wildcard for p in route.path_params)


def emit(rmap: RouteMap) -> list[Emitted]:
    w = Writer("  ")
    for line in header_lines(rmap, "//"):
        w.line(line)
    w.lines(
        "//",
        "// Scalar newtypes are extension types: free at runtime, distinct to the analyzer.",
        "",
        "// ignore_for_file: unnecessary_cast, unused_element",
        "",
        "import 'dart:convert';",
        "",
        f"const String kService = {json.dumps(rmap.service)};",
    )
    if rmap.version:
        w.line(f"const String kVersion = {json.dumps(rmap.version)};")
    w.blank()

    _emit_types(rmap, w)
    _emit_transport(w)
    _emit_operations(rmap, w)
    _emit_manifest(rmap, w)

    return [Emitted(path="dart/ridl_generated.dart", text=w.render())]


def _emit_types(rmap: RouteMap, w: Writer) -> None:
    if not rmap.types:
        return
    w.line("// ---------------------------------------------------------------- types")
    w.blank()
    for name in ordered_types(rmap):
        defn = rmap.types[name]
        pas = naming.pascal(name)
        w.doc(defn.doc, "///")
        if isinstance(defn, ScalarDef):
            base = _SCALARS[defn.base]
            w.line(f"extension type const {pas}({base} value) implements Object {{}}")
        elif isinstance(defn, EnumDef):
            with w.block(f"enum {pas}"):
                for variant in defn.variants:
                    ident = naming.escape(naming.camel(variant), LANG)
                    w.line(f"{ident}({json.dumps(variant)}),")
                w.line(";")
                w.blank()
                w.line(f"const {pas}(this.wire);")
                w.blank()
                w.line("/// The value as it appears on the wire.")
                w.line("final String wire;")
                w.blank()
                with w.block(f"static {pas} fromWire(String wire)"):
                    with w.block("for (final value in values)"):
                        w.line("if (value.wire == wire) return value;")
                    w.line(
                        f"throw ArgumentError.value(wire, 'wire', "
                        f"'not a {pas} variant');"
                    )
        elif isinstance(defn, RecordDef):
            with w.block(f"class {pas}"):
                args = ", ".join(
                    ("required " if f.required else "")
                    + f"this.{naming.escape(naming.camel(f.wire), LANG)}"
                    + ("" if f.required or not f.has_default else "")
                    for f in defn.fields
                )
                w.line(f"const {pas}({{{args}}});")
                w.blank()
                with w.block(f"factory {pas}.fromJson(Map<String, Object?> json)"):
                    w.line(f"return {pas}(")
                    w.indent()
                    for fld in defn.fields:
                        ident = naming.escape(naming.camel(fld.wire), LANG)
                        src = f"json[{json.dumps(fld.wire)}]"
                        expr = _from_json(rmap, fld.type, src, not fld.required)
                        w.line(f"{ident}: {expr},")
                    w.dedent()
                    w.line(");")
                w.blank()
                for fld in defn.fields:
                    w.doc(fld.doc, "///")
                    ident = naming.escape(naming.camel(fld.wire), LANG)
                    w.line(f"final {field_type(rmap, fld.type, fld.required)} {ident};")
                w.blank()
                with w.block("Map<String, Object?> toJson()"):
                    w.line("return <String, Object?>{")
                    w.indent()
                    for fld in defn.fields:
                        ident = naming.escape(naming.camel(fld.wire), LANG)
                        expr = _to_json(rmap, fld.type, ident, not fld.required)
                        if fld.required:
                            w.line(f"{json.dumps(fld.wire)}: {expr},")
                        else:
                            w.line(f"if ({ident} != null) {json.dumps(fld.wire)}: {expr},")
                    w.dedent()
                    w.line("};")
        elif isinstance(defn, AliasDef):
            w.line(f"typedef {pas} = {type_name(rmap, defn.target)};")
        w.blank()


def _emit_transport(w: Writer) -> None:
    w.lines(
        "// ------------------------------------------------------------- transport",
        "",
        "/// How a call reaches the server.",
    )
    with w.block("enum Delivery"):
        w.lines("direct('direct'),", "optoSyncQueued('opto_sync_queued');", "")
        w.line("const Delivery(this.wire);")
        w.blank()
        w.line("final String wire;")
    w.blank()
    w.line("/// Where a queued call's opto-sync record id comes from.")
    with w.block("enum RecordIdFrom"):
        w.line("path, request, minted;")
    w.blank()
    w.line("/// Static description of a queued call's opto-sync binding.")
    with w.block("class OptoSyncBinding"):
        w.lines(
            "const OptoSyncBinding({required this.table, required this.operation, "
            "required this.from, this.name});",
            "",
            "final String table;",
            "final String operation;",
            "final RecordIdFrom from;",
            "final String? name;",
        )
    w.blank()
    w.line("/// One outbound call, fully resolved. Transports never re-derive paths.")
    with w.block("class RpcRequest"):
        w.lines(
            "const RpcRequest({",
            "  required this.key,",
            "  required this.method,",
            "  required this.path,",
            "  required this.pathTemplate,",
            "  this.query = const [],",
            "  this.body,",
            "  this.delivery = Delivery.direct,",
            "  this.optoSync,",
            "});",
            "",
            "final String key;",
            "final String method;",
            "",
            "/// Path with every parameter already substituted and encoded.",
            "final String path;",
            "",
            "/// The unsubstituted template, so a transport can locate a named",
            "/// parameter inside [path] instead of guessing at its position.",
            "final String pathTemplate;",
            "final List<MapEntry<String, String>> query;",
            "",
            "/// JSON body, or null for operations that carry none.",
            "final String? body;",
            "final Delivery delivery;",
            "final OptoSyncBinding? optoSync;",
        )
    w.blank()
    w.lines(
        "/// The single seam between generated code and the network.",
        "///",
        "/// Implement it over HTTP for direct calls and over an opto-sync queue for",
        "/// queued ones. The dependency runs one way: this library calls opto-sync,",
        "/// never the reverse.",
    )
    with w.block("abstract interface class RpcTransport"):
        w.line("Future<String> call(RpcRequest request);")
    w.blank()
    w.lines(
        "/// Encode one path segment so an id containing `/` cannot reshape the URL.",
        "String encodeSegment(String value) => Uri.encodeComponent(value);",
        "",
        "String _queryValue(Object? value) => value is String ? value : '$value';",
        "",
    )


def _param_ident(param: Param) -> str:
    return naming.escape(naming.camel(param.wire), LANG)


def _emit_operations(rmap: RouteMap, w: Writer) -> None:
    w.line("// ------------------------------------------------------------- operations")
    w.blank()

    for route in client_routes(rmap):
        if not route.query_params:
            continue
        cls = f"{naming.pascal(route.key)}Query"
        w.line(f"/// Query parameters for `{route.key}`.")
        with w.block(f"class {cls}"):
            args = ", ".join(
                ("required " if p.required else "") + f"this.{_param_ident(p)}"
                for p in route.query_params
            )
            w.line(f"const {cls}({{{args}}});")
            w.blank()
            for param in route.query_params:
                w.doc(param.doc, "///")
                w.line(
                    f"final {field_type(rmap, param.type, param.required)} "
                    f"{_param_ident(param)};"
                )
            w.blank()
            with w.block("List<MapEntry<String, String>> toPairs()"):
                w.line("final pairs = <MapEntry<String, String>>[];")
                for param in route.query_params:
                    ident = _param_ident(param)
                    is_list = isinstance(rmap.underlying(param.type), ListOf)
                    inner = _to_json(
                        rmap,
                        rmap.underlying(param.type).item
                        if is_list
                        else param.type,
                        "item" if is_list else ident,
                        False,
                    )
                    if param.required:
                        if is_list:
                            with w.block(f"for (final item in {ident})"):
                                w.line(
                                    f"pairs.add(MapEntry({json.dumps(param.wire)}, "
                                    f"_queryValue({inner})));"
                                )
                        else:
                            w.line(
                                f"pairs.add(MapEntry({json.dumps(param.wire)}, "
                                f"_queryValue({inner})));"
                            )
                    else:
                        with w.block(f"if ({ident} != null)"):
                            if is_list:
                                with w.block(f"for (final item in {ident})"):
                                    w.line(
                                        f"pairs.add(MapEntry({json.dumps(param.wire)}, "
                                        f"_queryValue({inner})));"
                                    )
                            else:
                                w.line(
                                    f"pairs.add(MapEntry({json.dumps(param.wire)}, "
                                    f"_queryValue({inner})));"
                                )
                w.line("return pairs;")
        w.blank()

    for route in client_routes(rmap):
        _emit_path_fn(rmap, route, w)
    for route in client_routes(rmap):
        _emit_call_fn(rmap, route, w)


def _emit_path_fn(rmap: RouteMap, route: Route, w: Writer) -> None:
    fn = naming.escape(naming.camel(f"{route.key}_path"), LANG)
    args = ", ".join(f"{type_name(rmap, p.type)} {_param_ident(p)}" for p in route.path_params)
    w.line(f"/// `{route.primary_method} {route.path}`")
    if not route.path_params:
        w.line(f"String {fn}() => {json.dumps(route.path)};")
        w.blank()
        return
    parts: list[str] = []
    for text, is_param in path_segments(route.path):
        if is_param:
            ident = next(
                (_param_ident(p) for p in route.path_params if p.wire == text),
                naming.camel(text),
            )
            parts.append("${encodeSegment('$" + ident + "')}")
        else:
            parts.append(text)
    w.line(f"String {fn}({args}) =>")
    w.indent()
    w.line("'" + "".join(parts) + "';")
    w.dedent()
    w.blank()


def _emit_call_fn(rmap: RouteMap, route: Route, w: Writer) -> None:
    fn = naming.escape(naming.camel(route.key), LANG)
    args = ["RpcTransport transport"]
    args += [f"{type_name(rmap, p.type)} {_param_ident(p)}" for p in route.path_params]
    if route.query_params:
        args.append(f"{naming.pascal(route.key)}Query queryParams")
    if route.request is not None:
        args.append(f"{type_name(rmap, route.request)} bodyValue")

    ret = type_name(rmap, route.response) if route.response is not None else "void"
    w.doc(route.summary or route.doc, "///")
    if route.deprecated:
        w.line("@Deprecated('declared deprecated in the route map')")
    with w.block(f"Future<{ret}> {fn}({', '.join(args)}) async"):
        path_args = ", ".join(_param_ident(p) for p in route.path_params)
        w.line(f"final path = {naming.camel(route.key + '_path')}({path_args});")
        if route.query_params:
            w.line("final query = queryParams.toPairs();")
        else:
            w.line("const query = <MapEntry<String, String>>[];")
        if route.request is not None:
            w.line("final body = jsonEncode(bodyValue.toJson());")
        w.line("final raw = await transport.call(RpcRequest(")
        w.indent()
        w.lines(
            f"key: {json.dumps(route.key)},",
            f"method: {json.dumps(route.primary_method)},",
            "path: path,",
            f"pathTemplate: {json.dumps(route.path)},",
            "query: query,",
        )
        if route.request is not None:
            w.line("body: body,")
        delivery = (
            "Delivery.optoSyncQueued"
            if route.delivery == DELIVERY_OPTO_SYNC
            else "Delivery.direct"
        )
        w.line(f"delivery: {delivery},")
        if route.opto_sync is not None:
            src = route.opto_sync.record_id_from
            if src == "uuid":
                frm, nm = "RecordIdFrom.minted", "null"
            else:
                scope, _, name = src.partition(".")
                frm, nm = f"RecordIdFrom.{scope}", json.dumps(name)
            w.line(
                f"optoSync: const OptoSyncBinding(table: {json.dumps(route.opto_sync.table)}, "
                f"operation: {json.dumps(route.opto_sync.operation)}, from: {frm}, name: {nm}),"
            )
        w.dedent()
        w.line("));")
        if route.response is None:
            w.line("return;")
        else:
            decoded = _from_json(rmap, route.response, "jsonDecode(raw)", False)
            w.line(f"return {decoded};")
    w.blank()


def _emit_manifest(rmap: RouteMap, w: Writer) -> None:
    w.lines(
        "// -------------------------------------------------------------- manifest",
        "",
        "/// One row per declared operation.",
    )
    with w.block("class OperationInfo"):
        w.lines(
            "const OperationInfo({required this.key, required this.path, "
            "required this.methods, required this.delivery});",
            "",
            "final String key;",
            "final String path;",
            "final List<String> methods;",
            "final Delivery delivery;",
        )
    w.blank()
    w.line("/// Every operation in the route map, in declaration order.")
    with w.block("const List<OperationInfo> operations =", "[", "];"):
        for route in rmap.routes:
            methods = ", ".join(json.dumps(m) for m in route.methods)
            delivery = (
                "Delivery.optoSyncQueued"
                if route.delivery == DELIVERY_OPTO_SYNC
                else "Delivery.direct"
            )
            w.line(
                f"OperationInfo(key: {json.dumps(route.key)}, path: {json.dumps(route.path)}, "
                f"methods: [{methods}], delivery: {delivery}),"
            )
    w.blank()
    queued = ", ".join(json.dumps(r.key) for r in queued_routes(rmap))
    w.line("/// Operations that route through opto-sync's durable queue.")
    w.line(f"const List<String> queuedOperations = [{queued}];")
    w.blank()
