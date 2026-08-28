"""Swift emitter: Codable structs, RawRepresentable enums, typed async calls."""

from __future__ import annotations

import json

from .. import naming
from ..model import (
    BUILTINS, DELIVERY_OPTO_SYNC, AliasDef, EnumDef, ListOf, MapOf, Named,
    OptionOf, Param, RecordDef, Route, RouteMap, ScalarDef, TypeExpr,
)
from .base import client_routes, Emitted, Writer, header_lines, ordered_types, path_segments, queued_routes

LANG = "swift"

_SCALARS = {
    "String": "String", "Bool": "Bool", "I32": "Int32", "I64": "Int64",
    "F64": "Double", "Uuid": "String", "DateTime": "String",
    "Decimal": "String", "Json": "RidlJSON",
}


def type_name(rmap: RouteMap, expr: TypeExpr) -> str:
    if isinstance(expr, Named):
        return _SCALARS[expr.name] if expr.name in BUILTINS else naming.pascal(expr.name)
    if isinstance(expr, ListOf):
        return f"[{type_name(rmap, expr.item)}]"
    if isinstance(expr, MapOf):
        return f"[String: {type_name(rmap, expr.value)}]"
    if isinstance(expr, OptionOf):
        inner = type_name(rmap, expr.inner)
        return inner if inner.endswith("?") else f"{inner}?"
    return "RidlJSON"


def field_type(rmap: RouteMap, expr: TypeExpr, required: bool) -> str:
    inner = type_name(rmap, expr)
    return inner if required or inner.endswith("?") else f"{inner}?"


def _is_wildcard(route, wire: str) -> bool:
    return any(p.wire == wire and p.wildcard for p in route.path_params)


def emit(rmap: RouteMap) -> list[Emitted]:
    w = Writer("    ")
    for line in header_lines(rmap, "//"):
        w.line(line)
    w.lines("", "import Foundation", "",
            f"public let ridlService = {json.dumps(rmap.service)}")
    if rmap.version:
        w.line(f"public let ridlVersion = {json.dumps(rmap.version)}")
    w.blank()
    w.lines(
        "/// An opaque JSON value, for fields the route map declares as `Json`.",
        "public typealias RidlJSON = AnyCodableValue",
        "",
        "/// Minimal `Codable` box so `Json` fields survive a round trip.",
    )
    with w.block("public struct AnyCodableValue: Codable, Equatable"):
        w.lines(
            "public let raw: Data",
            "",
            "public init(raw: Data) { self.raw = raw }",
            "",
            "public init(from decoder: Decoder) throws {",
            "    let container = try decoder.singleValueContainer()",
            "    if let value = try? container.decode(String.self) {",
            "        raw = Data(value.utf8)",
            "    } else {",
            "        raw = Data()",
            "    }",
            "}",
            "",
            "public func encode(to encoder: Encoder) throws {",
            "    var container = encoder.singleValueContainer()",
            "    try container.encode(String(decoding: raw, as: UTF8.self))",
            "}",
        )
    w.blank()
    _emit_types(rmap, w)
    _emit_transport(w)
    _emit_operations(rmap, w)
    _emit_manifest(rmap, w)
    return [Emitted(path="swift/RidlGenerated.swift", text=w.render())]


def _emit_types(rmap: RouteMap, w: Writer) -> None:
    for name in ordered_types(rmap):
        defn = rmap.types[name]
        pas = naming.pascal(name)
        w.doc(defn.doc, "///")
        if isinstance(defn, ScalarDef):
            base = _SCALARS[defn.base]
            with w.block(
                f"public struct {pas}: Codable, Equatable, RawRepresentable, "
                f"CustomStringConvertible"
            ):
                w.lines(
                    f"public let rawValue: {base}",
                    f"public init(_ rawValue: {base}) {{ self.rawValue = rawValue }}",
                    f"public init?(rawValue: {base}) {{ self.rawValue = rawValue }}",
                    'public var description: String { "\\(rawValue)" }',
                    "",
                    "public init(from decoder: Decoder) throws {",
                    f"    rawValue = try decoder.singleValueContainer().decode({base}.self)",
                    "}",
                    "",
                    "public func encode(to encoder: Encoder) throws {",
                    "    var container = encoder.singleValueContainer()",
                    "    try container.encode(rawValue)",
                    "}",
                )
        elif isinstance(defn, EnumDef):
            with w.block(
                f"public enum {pas}: String, Codable, CaseIterable, CustomStringConvertible"
            ):
                for variant in defn.variants:
                    ident = naming.escape(naming.camel(variant), LANG)
                    w.line(f"case {ident} = {json.dumps(variant)}")
                w.line("")
                w.line("public var description: String { rawValue }")
        elif isinstance(defn, RecordDef):
            with w.block(f"public struct {pas}: Codable, Equatable"):
                for fld in defn.fields:
                    w.doc(fld.doc, "///")
                    ident = naming.escape(naming.camel(fld.wire), LANG)
                    w.line(f"public let {ident}: {field_type(rmap, fld.type, fld.required)}")
                w.blank()
                params = ", ".join(
                    f"{naming.escape(naming.camel(f.wire), LANG)}: "
                    f"{field_type(rmap, f.type, f.required)}"
                    + ("" if f.required else " = nil")
                    for f in defn.fields
                )
                with w.block(f"public init({params})"):
                    for fld in defn.fields:
                        ident = naming.escape(naming.camel(fld.wire), LANG)
                        w.line(f"self.{ident} = {ident}")
                w.blank()
                with w.block("private enum CodingKeys: String, CodingKey"):
                    for fld in defn.fields:
                        ident = naming.escape(naming.camel(fld.wire), LANG)
                        w.line(f"case {ident} = {json.dumps(fld.wire)}")
        elif isinstance(defn, AliasDef):
            w.line(f"public typealias {pas} = {type_name(rmap, defn.target)}")
        w.blank()


def _emit_transport(w: Writer) -> None:
    w.lines("// ------------------------------------------------------------- transport", "")
    with w.block("public enum RidlDelivery: String, Codable"):
        w.lines('case direct = "direct"', 'case optoSyncQueued = "opto_sync_queued"')
    w.blank()
    with w.block("public enum RidlRecordIdFrom: String, Codable"):
        w.lines('case path = "path"', 'case request = "request"', 'case minted = "minted"')
    w.blank()
    w.line("/// Static description of a queued call's opto-sync record.")
    with w.block("public struct RidlOptoSyncBinding: Equatable"):
        w.lines(
            "public let table: String", "public let operation: String",
            "public let from: RidlRecordIdFrom", "public let name: String?",
            "",
            "public init(table: String, operation: String, from: RidlRecordIdFrom, "
            "name: String?) {",
            "    self.table = table; self.operation = operation",
            "    self.from = from; self.name = name",
            "}",
        )
    w.blank()
    w.line("/// One outbound call, fully resolved. Transports never re-derive paths.")
    with w.block("public struct RidlRequest"):
        w.lines(
            "public let key: String", "public let method: String",
            "public let path: String",
            "/// The unsubstituted template, so a transport can locate a named",
            "/// parameter inside `path` instead of guessing at its position.",
            "public let pathTemplate: String",
            "public let query: [(String, String)]",
            "public let body: Data?",
            "public let delivery: RidlDelivery",
            "public let optoSync: RidlOptoSyncBinding?",
            "",
            "public init(key: String, method: String, path: String, "
            "pathTemplate: String = \"\", query: [(String, String)] = [], body: Data? = nil, "
            "delivery: RidlDelivery = .direct, optoSync: RidlOptoSyncBinding? = nil) {",
            "    self.key = key; self.method = method; self.path = path",
            "    self.pathTemplate = pathTemplate",
            "    self.query = query; self.body = body",
            "    self.delivery = delivery; self.optoSync = optoSync",
            "}",
        )
    w.blank()
    w.lines(
        "/// The single seam between generated code and the network.",
        "///",
        "/// Implement it over URLSession for direct calls and over an opto-sync queue",
        "/// for queued ones. The dependency runs one way: this module calls",
        "/// opto-sync, never the reverse.",
    )
    with w.block("public protocol RidlTransport"):
        w.line("func call(_ request: RidlRequest) async throws -> Data")
    w.blank()
    w.lines(
        "/// Escape one path segment so an id containing `/` cannot reshape the URL.",
        "public func ridlEncodeSegment(_ value: String) -> String {",
        "    value.addingPercentEncoding("
        "withAllowedCharacters: .alphanumerics) ?? value",
        "}",
        "",
    )


def _param_ident(p: Param) -> str:
    return naming.escape(naming.camel(p.wire), LANG)


def _emit_operations(rmap: RouteMap, w: Writer) -> None:
    for route in client_routes(rmap):
        if not route.query_params:
            continue
        cls = f"{naming.pascal(route.key)}Query"
        w.line(f"/// Query parameters for `{route.key}`.")
        with w.block(f"public struct {cls}"):
            for param in route.query_params:
                w.doc(param.doc, "///")
                w.line(
                    f"public let {_param_ident(param)}: "
                    f"{field_type(rmap, param.type, param.required)}"
                )
            w.blank()
            params = ", ".join(
                f"{_param_ident(p)}: {field_type(rmap, p.type, p.required)}"
                + ("" if p.required else " = nil")
                for p in route.query_params
            )
            with w.block(f"public init({params})"):
                for param in route.query_params:
                    w.line(f"self.{_param_ident(param)} = {_param_ident(param)}")
            w.blank()
            with w.block("public func pairs() -> [(String, String)]"):
                w.line("var out: [(String, String)] = []")
                for param in route.query_params:
                    ident = _param_ident(param)
                    key = json.dumps(param.wire)
                    is_list = isinstance(rmap.underlying(param.type), ListOf)
                    if param.required:
                        if is_list:
                            with w.block(f"for item in {ident}"):
                                w.line(f'out.append(({key}, "\\(item)"))')
                        else:
                            w.line(f'out.append(({key}, "\\({ident})"))')
                    else:
                        with w.block(f"if let value = {ident}"):
                            if is_list:
                                with w.block("for item in value"):
                                    w.line(f'out.append(({key}, "\\(item)"))')
                            else:
                                w.line(f'out.append(({key}, "\\(value)"))')
                w.line("return out")
        w.blank()

    for route in client_routes(rmap):
        fn = naming.escape(naming.camel(f"{route.key}_path"), LANG)
        args = ", ".join(
            f"_ {_param_ident(p)}: {type_name(rmap, p.type)}" for p in route.path_params
        )
        w.line(f"/// `{route.primary_method} {route.path}`")
        with w.block(f"public func {fn}({args}) -> String"):
            if not route.path_params:
                w.line(f"return {json.dumps(route.path)}")
            else:
                parts = []
                for text, is_param in path_segments(route.path):
                    if is_param:
                        ident = next(
                            (_param_ident(p) for p in route.path_params if p.wire == text),
                            naming.camel(text),
                        )
                        parts.append('\\(ridlEncodeSegment("\\(' + ident + ')"))')
                    else:
                        parts.append(text)
                w.line('return "' + "".join(parts) + '"')
        w.blank()

    for route in client_routes(rmap):
        _emit_call_fn(rmap, route, w)


def _emit_call_fn(rmap: RouteMap, route: Route, w: Writer) -> None:
    fn = naming.escape(naming.camel(route.key), LANG)
    args = ["_ transport: RidlTransport"]
    args += [f"{_param_ident(p)}: {type_name(rmap, p.type)}" for p in route.path_params]
    if route.query_params:
        args.append(f"query: {naming.pascal(route.key)}Query")
    if route.request is not None:
        args.append(f"body: {type_name(rmap, route.request)}")
    ret = type_name(rmap, route.response) if route.response is not None else "Void"
    w.doc(route.summary or route.doc, "///")
    if route.deprecated:
        w.line('@available(*, deprecated, message: "declared deprecated in the route map")')
    with w.block(f"public func {fn}({', '.join(args)}) async throws -> {ret}"):
        path_args = ", ".join(_param_ident(p) for p in route.path_params)
        w.line(f"let path = {naming.camel(route.key + '_path')}({path_args})")
        w.line("let pairs = query.pairs()" if route.query_params
               else "let pairs: [(String, String)] = []")
        if route.request is not None:
            w.line("let payload = try JSONEncoder().encode(body)")
        opto = "nil"
        if route.opto_sync is not None:
            src = route.opto_sync.record_id_from
            if src == "uuid":
                frm, nm = ".minted", "nil"
            else:
                scope, _, name = src.partition(".")
                frm, nm = f".{scope}", json.dumps(name)
            opto = (
                f"RidlOptoSyncBinding(table: {json.dumps(route.opto_sync.table)}, "
                f"operation: {json.dumps(route.opto_sync.operation)}, from: {frm}, name: {nm})"
            )
        delivery = ".optoSyncQueued" if route.delivery == DELIVERY_OPTO_SYNC else ".direct"
        w.line("let raw = try await transport.call(RidlRequest(")
        w.indent()
        w.lines(
            f"key: {json.dumps(route.key)},",
            f"method: {json.dumps(route.primary_method)},",
            "path: path,", f"pathTemplate: {json.dumps(route.path)},", "query: pairs,",
            "body: payload," if route.request is not None else "body: nil,",
            f"delivery: {delivery},", f"optoSync: {opto}",
        )
        w.dedent()
        w.line("))")
        if route.response is None:
            w.line("_ = raw")
        else:
            w.line(f"return try JSONDecoder().decode({ret}.self, from: raw)")
    w.blank()


def _emit_manifest(rmap: RouteMap, w: Writer) -> None:
    w.line("/// One row per declared operation.")
    with w.block("public struct RidlOperationInfo"):
        w.lines(
            "public let key: String", "public let path: String",
            "public let methods: [String]", "public let delivery: RidlDelivery",
        )
    w.blank()
    w.line("/// Every operation in the route map, in declaration order.")
    with w.block("public let ridlOperations: [RidlOperationInfo] =", "[", "]"):
        for route in rmap.routes:
            methods = ", ".join(json.dumps(m) for m in route.methods)
            delivery = (
                ".optoSyncQueued" if route.delivery == DELIVERY_OPTO_SYNC else ".direct"
            )
            w.line(
                f"RidlOperationInfo(key: {json.dumps(route.key)}, "
                f"path: {json.dumps(route.path)}, methods: [{methods}], "
                f"delivery: {delivery}),"
            )
    w.blank()
    queued = ", ".join(json.dumps(r.key) for r in queued_routes(rmap))
    w.line("/// Operations that route through opto-sync's durable queue.")
    w.line(f"public let ridlQueuedOperations: [String] = [{queued}]")
    w.blank()
