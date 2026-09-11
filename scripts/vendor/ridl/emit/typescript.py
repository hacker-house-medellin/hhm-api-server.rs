"""TypeScript emitter.

Interfaces keep the wire names verbatim, because the value is JSON and an extra
renaming layer is one more place to drift. Scalar newtypes become branded types,
so passing a bare `string` where a `MatterId` is required is a compile error --
which is the whole point of generating this rather than hand-writing it.
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

LANG = "typescript"

_SCALARS = {
    "String": "string",
    "Bool": "boolean",
    "I32": "number",
    "I64": "number",
    "F64": "number",
    "Uuid": "string",
    "DateTime": "string",
    "Decimal": "string",
    "Json": "unknown",
}


def type_name(rmap: RouteMap, expr: TypeExpr) -> str:
    if isinstance(expr, Named):
        if expr.name in BUILTINS:
            return _SCALARS[expr.name]
        return naming.pascal(expr.name)
    if isinstance(expr, ListOf):
        return f"ReadonlyArray<{type_name(rmap, expr.item)}>"
    if isinstance(expr, MapOf):
        return f"Readonly<Record<string, {type_name(rmap, expr.value)}>>"
    if isinstance(expr, OptionOf):
        return f"{type_name(rmap, expr.inner)} | null"
    return "unknown"


def _is_wildcard(route, wire: str) -> bool:
    return any(p.wire == wire and p.wildcard for p in route.path_params)


def emit(rmap: RouteMap) -> list[Emitted]:
    w = Writer("  ")
    for line in header_lines(rmap, "//"):
        w.line(line)
    w.lines(
        "//",
        "// Scalar newtypes are branded, so a bare string cannot stand in for a typed id.",
        "",
        f"export const SERVICE = {json.dumps(rmap.service)} as const;",
    )
    if rmap.version:
        w.line(f"export const VERSION = {json.dumps(rmap.version)} as const;")
    w.blank()

    _emit_types(rmap, w)
    _emit_transport(rmap, w)
    _emit_operations(rmap, w)
    _emit_manifest(rmap, w)

    return [Emitted(path="typescript/ridl-generated.ts", text=w.render())]


def _emit_types(rmap: RouteMap, w: Writer) -> None:
    if not rmap.types:
        return
    w.line("// ---------------------------------------------------------------- types")
    w.blank()
    for name in ordered_types(rmap):
        defn = rmap.types[name]
        pas = naming.pascal(name)
        if defn.doc:
            w.line("/** " + str(defn.doc) + " */")
        if isinstance(defn, RecordDef):
            with w.block(f"export interface {pas}"):
                for fld in defn.fields:
                    if fld.doc:
                        w.line("/** " + str(fld.doc) + " */")
                    optional = "" if fld.required else "?"
                    w.line(
                        f"readonly {json.dumps(fld.wire)}{optional}: "
                        f"{type_name(rmap, fld.type)};"
                    )
        elif isinstance(defn, EnumDef):
            union = " | ".join(json.dumps(v) for v in defn.variants)
            w.line(f"export type {pas} = {union};")
            values = ", ".join(json.dumps(v) for v in defn.variants)
            w.line(f"export const {naming.screaming_snake(name)}_VALUES = [{values}] as const;")
        elif isinstance(defn, ScalarDef):
            base = _SCALARS[defn.base]
            w.lines(
                f"export type {pas} = {base} & {{ readonly __ridl: {json.dumps(name)} }};",
                f"export const {pas} = (value: {base}): {pas} => value as {pas};",
            )
        elif isinstance(defn, AliasDef):
            w.line(f"export type {pas} = {type_name(rmap, defn.target)};")
        w.blank()


def _emit_transport(rmap: RouteMap, w: Writer) -> None:
    w.lines(
        "// ------------------------------------------------------------- transport",
        "",
        "/** How a call reaches the server. */",
        'export type Delivery = "direct" | "opto_sync_queued";',
        "",
        "/** Where a queued call's opto-sync record id comes from. */",
    )
    with w.block("export interface OptoSyncBinding"):
        w.lines(
            "readonly table: string;",
            'readonly operation: "upsert" | "delete";',
            'readonly recordId: { readonly from: "path" | "request" | "minted"; readonly name?: string };',
        )
    w.blank()
    w.line("/** One outbound call, fully resolved. Transports never re-derive paths. */")
    with w.block("export interface RpcRequest"):
        w.lines(
            "readonly key: string;",
            "readonly method: string;",
            "/** Path with every parameter already substituted and encoded. */",
            "readonly path: string;",
            "/** The unsubstituted template, so a transport can locate a named",
            "  * parameter inside `path` instead of guessing at its position. */",
            "readonly pathTemplate: string;",
            "readonly query: ReadonlyArray<readonly [string, string]>;",
            "/** JSON body, or undefined for operations that carry none. */",
            "readonly body?: string;",
            "readonly delivery: Delivery;",
            "readonly optoSync?: OptoSyncBinding;",
        )
    w.blank()
    w.lines(
        "/**",
        " * The single seam between generated code and the network.",
        " *",
        " * Implement it over `fetch` for direct calls, and over an opto-sync queue for",
        " * queued ones. The dependency runs one way: this module calls opto-sync,",
        " * never the reverse.",
        " */",
    )
    with w.block("export interface RpcTransport"):
        w.line("call(request: RpcRequest): Promise<string>;")
    w.blank()
    w.lines(
        "/** Encode one path segment so an id containing `/` cannot reshape the URL. */",
        "export const encodeSegment = (value: string): string => encodeURIComponent(value);",
        "",
        "const queryValue = (value: unknown): string =>",
        '  typeof value === "string" ? value : String(value);',
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
        w.line(f"/** Query parameters for `{route.key}`. */")
        with w.block(f"export interface {naming.pascal(route.key)}Query"):
            for param in route.query_params:
                if param.doc:
                    w.line("/** " + str(param.doc) + " */")
                optional = "" if param.required else "?"
                w.line(
                    f"readonly {json.dumps(param.wire)}{optional}: "
                    f"{type_name(rmap, param.type)};"
                )
        w.blank()

    for route in client_routes(rmap):
        _emit_path_fn(rmap, route, w)
    for route in client_routes(rmap):
        _emit_call_fn(rmap, route, w)


def _emit_path_fn(rmap: RouteMap, route: Route, w: Writer) -> None:
    fn = naming.escape(naming.camel(f"{route.key}_path"), LANG)
    args = ", ".join(f"{_param_ident(p)}: {type_name(rmap, p.type)}" for p in route.path_params)
    if not route.path_params:
        w.line(f"/** `{route.primary_method} {route.path}` */")
        w.line(f"export const {fn} = (): string => {json.dumps(route.path)};")
        w.blank()
        return
    parts: list[str] = []
    for text, is_param in path_segments(route.path):
        if is_param:
            parts.append("${encodeSegment(String(" + _param_ident_from(route, text) + "))}")
        else:
            parts.append(text)
    w.line(f"/** `{route.primary_method} {route.path}` */")
    w.line(f"export const {fn} = ({args}): string =>")
    w.indent()
    w.line("`" + "".join(parts) + "`;")
    w.dedent()
    w.blank()


def _param_ident_from(route: Route, wire: str) -> str:
    for param in route.path_params:
        if param.wire == wire:
            return _param_ident(param)
    return naming.camel(wire)


def _emit_call_fn(rmap: RouteMap, route: Route, w: Writer) -> None:
    fn = naming.escape(naming.camel(route.key), LANG)
    args = ["transport: RpcTransport"]
    args += [f"{_param_ident(p)}: {type_name(rmap, p.type)}" for p in route.path_params]
    if route.query_params:
        required_query = any(p.required for p in route.query_params)
        suffix = "" if required_query else " = {}"
        args.append(f"queryParams: {naming.pascal(route.key)}Query{suffix}")
    if route.request is not None:
        args.append(f"bodyValue: {type_name(rmap, route.request)}")

    ret = type_name(rmap, route.response) if route.response is not None else "void"
    if route.summary or route.doc:
        w.line("/** " + str(route.summary or route.doc) + " */")
    if route.deprecated:
        w.line("/** @deprecated declared deprecated in the route map */")
    with w.block(
        f"export async function {fn}({', '.join(args)}): Promise<{ret}>"
    ):
        path_args = ", ".join(_param_ident(p) for p in route.path_params)
        w.line(
            f"const path = {naming.camel(route.key + '_path')}({path_args});"
        )
        w.line("const query: Array<readonly [string, string]> = [];")
        for param in route.query_params:
            key = json.dumps(param.wire)
            src = f"queryParams[{key}]"
            is_list = isinstance(rmap.underlying(param.type), ListOf)
            guard = f"if ({src} !== undefined && {src} !== null)"
            if is_list:
                with w.block(guard):
                    with w.block(f"for (const item of {src})"):
                        w.line(f"query.push([{key}, queryValue(item)] as const);")
            else:
                with w.block(guard):
                    w.line(f"query.push([{key}, queryValue({src})] as const);")

        if route.request is not None:
            w.line("const body = JSON.stringify(bodyValue);")
        w.line("const raw = await transport.call({")
        w.indent()
        w.lines(
            f"key: {json.dumps(route.key)},",
            f"method: {json.dumps(route.primary_method)},",
            "path,",
            f"pathTemplate: {json.dumps(route.path)},",
            "query,",
        )
        if route.request is not None:
            w.line("body,")
        w.line(
            "delivery: "
            + json.dumps(
                "opto_sync_queued" if route.delivery == DELIVERY_OPTO_SYNC else "direct"
            )
            + ","
        )
        if route.opto_sync is not None:
            src = route.opto_sync.record_id_from
            if src == "uuid":
                rid = '{ from: "minted" }'
            else:
                scope, _, nm = src.partition(".")
                rid = f'{{ from: {json.dumps(scope)}, name: {json.dumps(nm)} }}'
            w.line(
                "optoSync: { table: "
                + json.dumps(route.opto_sync.table)
                + ", operation: "
                + json.dumps(route.opto_sync.operation)
                + f", recordId: {rid} }},"
            )
        w.dedent()
        w.line("});")
        if route.response is None:
            w.line("void raw;")
        else:
            w.line(f"return JSON.parse(raw) as {ret};")
    w.blank()


def _emit_manifest(rmap: RouteMap, w: Writer) -> None:
    w.lines(
        "// -------------------------------------------------------------- manifest",
        "",
        "/** One row per declared operation. */",
    )
    with w.block("export interface OperationInfo"):
        w.lines(
            "readonly key: string;",
            "readonly path: string;",
            "readonly methods: ReadonlyArray<string>;",
            "readonly delivery: Delivery;",
            "/** Which wires may carry this operation. */",
            "readonly transports: ReadonlyArray<string>;",
            '/** "none", "server", or "bidi". */',
            "readonly stream: string;",
        )
    w.blank()
    w.line("/** Every operation in the route map, in declaration order. */")
    with w.block("export const OPERATIONS: ReadonlyArray<OperationInfo> =", "[", "] as const;"):
        for route in rmap.routes:
            methods = ", ".join(json.dumps(m) for m in route.methods)
            delivery = json.dumps(
                "opto_sync_queued" if route.delivery == DELIVERY_OPTO_SYNC else "direct"
            )
            transports = ", ".join(json.dumps(t) for t in route.transports)
            w.line(
                f"{{ key: {json.dumps(route.key)}, path: {json.dumps(route.path)}, "
                f"methods: [{methods}], delivery: {delivery}, "
                f"transports: [{transports}], stream: {json.dumps(route.stream)} }},"
            )
    w.blank()
    queued = queued_routes(rmap)
    w.line("/** Operations that route through opto-sync's durable queue. */")
    values = ", ".join(json.dumps(r.key) for r in queued)
    w.line(f"export const QUEUED_OPERATIONS = [{values}] as const;")
    w.blank()
