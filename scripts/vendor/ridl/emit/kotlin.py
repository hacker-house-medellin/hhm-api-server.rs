"""Kotlin emitter: kotlinx.serialization data classes, enums, typed suspend calls."""

from __future__ import annotations

import json

from .. import naming
from ..model import (
    BUILTINS, DELIVERY_OPTO_SYNC, AliasDef, EnumDef, ListOf, MapOf, Named,
    OptionOf, Param, RecordDef, Route, RouteMap, ScalarDef, TypeExpr,
)
from .base import client_routes, Emitted, Writer, header_lines, ordered_types, path_segments, queued_routes

LANG = "kotlin"

_SCALARS = {
    "String": "String", "Bool": "Boolean", "I32": "Int", "I64": "Long",
    "F64": "Double", "Uuid": "String", "DateTime": "String",
    "Decimal": "String", "Json": "JsonElement",
}


def type_name(rmap: RouteMap, expr: TypeExpr) -> str:
    if isinstance(expr, Named):
        return _SCALARS[expr.name] if expr.name in BUILTINS else naming.pascal(expr.name)
    if isinstance(expr, ListOf):
        return f"List<{type_name(rmap, expr.item)}>"
    if isinstance(expr, MapOf):
        return f"Map<String, {type_name(rmap, expr.value)}>"
    if isinstance(expr, OptionOf):
        inner = type_name(rmap, expr.inner)
        return inner if inner.endswith("?") else f"{inner}?"
    return "JsonElement"


def field_type(rmap: RouteMap, expr: TypeExpr, required: bool) -> str:
    inner = type_name(rmap, expr)
    return inner if required or inner.endswith("?") else f"{inner}?"


def _kt_literal(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return "emptyList()"
    if isinstance(value, dict):
        return "emptyMap()"
    return "null"


def _is_wildcard(route, wire: str) -> bool:
    return any(p.wire == wire and p.wildcard for p in route.path_params)


def emit(rmap: RouteMap) -> list[Emitted]:
    pkg = "ridl." + naming.snake(rmap.service).replace("_", "")
    w = Writer("    ")
    for line in header_lines(rmap, "//"):
        w.line(line)
    w.lines(
        "", f"package {pkg}", "",
        "import kotlinx.serialization.SerialName",
        "import kotlinx.serialization.Serializable",
        "import kotlinx.serialization.encodeToString",
        "import kotlinx.serialization.json.Json",
        "import kotlinx.serialization.json.JsonElement",
        "",
        f"public const val SERVICE: String = {json.dumps(rmap.service)}",
    )
    if rmap.version:
        w.line(f"public const val VERSION: String = {json.dumps(rmap.version)}")
    w.blank()
    w.line("private val ridlJson: Json = Json { ignoreUnknownKeys = false; encodeDefaults = true }")
    w.blank()
    _emit_types(rmap, w)
    _emit_transport(w)
    _emit_operations(rmap, w)
    _emit_manifest(rmap, w)
    return [Emitted(path=f"kotlin/{pkg.replace('.', '/')}/RidlGenerated.kt", text=w.render())]


def _emit_types(rmap: RouteMap, w: Writer) -> None:
    for name in ordered_types(rmap):
        defn = rmap.types[name]
        pas = naming.pascal(name)
        w.doc(defn.doc, "///")
        if isinstance(defn, ScalarDef):
            base = _SCALARS[defn.base]
            w.line("@Serializable")
            w.line("@JvmInline")
            w.line(f"public value class {pas}(public val value: {base}) {{")
            w.indent()
            w.line("override fun toString(): String = value.toString()")
            w.dedent()
            w.line("}")
        elif isinstance(defn, EnumDef):
            w.line("@Serializable")
            with w.block(f"public enum class {pas}(public val wire: String)"):
                for i, variant in enumerate(defn.variants):
                    ident = naming.escape(naming.screaming_snake(variant), LANG)
                    tail = ";" if i == len(defn.variants) - 1 else ","
                    w.line(f'@SerialName({json.dumps(variant)}) {ident}({json.dumps(variant)}){tail}')
                w.blank()
                with w.block("public companion object"):
                    w.line(
                        f"public fun fromWire(wire: String): {pas} = "
                        f"entries.firstOrNull {{ it.wire == wire }}"
                    )
                    w.indent()
                    w.line(
                        f'?: throw IllegalArgumentException("$wire is not a {pas} variant")'
                    )
                    w.dedent()
        elif isinstance(defn, RecordDef):
            w.line("@Serializable")
            with w.block(f"public data class {pas}", "(", ")"):
                for fld in defn.fields:
                    w.doc(fld.doc, "///")
                    ident = naming.escape(naming.camel(fld.wire), LANG)
                    if ident != fld.wire:
                        w.line(f"@SerialName({json.dumps(fld.wire)})")
                    ann = field_type(rmap, fld.type, fld.required)
                    if fld.has_default:
                        w.line(f"public val {ident}: {type_name(rmap, fld.type)} = "
                               f"{_kt_literal(fld.default)},")
                    elif fld.required:
                        w.line(f"public val {ident}: {ann},")
                    else:
                        w.line(f"public val {ident}: {ann} = null,")
        elif isinstance(defn, AliasDef):
            w.line(f"public typealias {pas} = {type_name(rmap, defn.target)}")
        w.blank()


def _emit_transport(w: Writer) -> None:
    w.lines("// ------------------------------------------------------------- transport", "")
    with w.block("public enum class Delivery(public val wire: String)"):
        w.lines('DIRECT("direct"),', 'OPTO_SYNC_QUEUED("opto_sync_queued");')
    w.blank()
    with w.block("public enum class RecordIdFrom(public val wire: String)"):
        w.lines('PATH("path"),', 'REQUEST("request"),', 'MINTED("minted");')
    w.blank()
    w.line("/** Static description of a queued call's opto-sync record. */")
    with w.block("public data class OptoSyncBinding", "(", ")"):
        w.lines(
            "public val table: String,", "public val operation: String,",
            "public val from: RecordIdFrom,", "public val name: String? = null,",
        )
    w.blank()
    w.line("/** One outbound call, fully resolved. Transports never re-derive paths. */")
    with w.block("public data class RpcRequest", "(", ")"):
        w.lines(
            "public val key: String,", "public val method: String,",
            "public val path: String,",
            "/** The unsubstituted template, so a transport can locate a named",
            "  * parameter inside [path] instead of guessing at its position. */",
            'public val pathTemplate: String = "",',
            "public val query: List<Pair<String, String>> = emptyList(),",
            "public val body: String? = null,",
            "public val delivery: Delivery = Delivery.DIRECT,",
            "public val optoSync: OptoSyncBinding? = null,",
        )
    w.blank()
    w.lines(
        "/**",
        " * The single seam between generated code and the network.",
        " *",
        " * Implement it over your HTTP client for direct calls and over an opto-sync",
        " * queue for queued ones. The dependency runs one way: this module calls",
        " * opto-sync, never the reverse.",
        " */",
    )
    with w.block("public interface RpcTransport"):
        w.line("public suspend fun call(request: RpcRequest): String")
    w.blank()
    w.lines(
        "/** Escape one path segment so an id containing `/` cannot reshape the URL. */",
        "public fun encodeSegment(value: String): String =",
        '    java.net.URLEncoder.encode(value, "UTF-8").replace("+", "%20")',
        "",
        "private fun queryValue(value: Any?): String = value.toString()",
        "",
    )


def _param_ident(p: Param) -> str:
    return naming.escape(naming.camel(p.wire), LANG)


def _emit_operations(rmap: RouteMap, w: Writer) -> None:
    for route in client_routes(rmap):
        if not route.query_params:
            continue
        cls = f"{naming.pascal(route.key)}Query"
        w.line(f"/** Query parameters for `{route.key}`. */")
        with w.block(f"public data class {cls}", "(", ")"):
            for param in route.query_params:
                w.doc(param.doc, "///")
                ann = field_type(rmap, param.type, param.required)
                default = "" if param.required else " = null"
                w.line(f"public val {_param_ident(param)}: {ann}{default},")
        w.line("{")
        w.indent()
        with w.block("public fun pairs(): List<Pair<String, String>>"):
            w.line("val out = mutableListOf<Pair<String, String>>()")
            for param in route.query_params:
                ident = _param_ident(param)
                key = json.dumps(param.wire)
                is_list = isinstance(rmap.underlying(param.type), ListOf)
                if param.required:
                    if is_list:
                        w.line(f"{ident}.forEach {{ out += {key} to queryValue(it) }}")
                    else:
                        w.line(f"out += {key} to queryValue({ident})")
                else:
                    if is_list:
                        w.line(f"{ident}?.forEach {{ out += {key} to queryValue(it) }}")
                    else:
                        w.line(f"{ident}?.let {{ out += {key} to queryValue(it) }}")
            w.line("return out")
        w.dedent()
        w.line("}")
        w.blank()

    for route in client_routes(rmap):
        fn = naming.escape(naming.camel(f"{route.key}_path"), LANG)
        args = ", ".join(f"{_param_ident(p)}: {type_name(rmap, p.type)}" for p in route.path_params)
        w.line(f"/** `{route.primary_method} {route.path}` */")
        if not route.path_params:
            w.line(f"public fun {fn}(): String = {json.dumps(route.path)}")
        else:
            parts = []
            for text, is_param in path_segments(route.path):
                if is_param:
                    ident = next(
                        (_param_ident(p) for p in route.path_params if p.wire == text),
                        naming.camel(text),
                    )
                    parts.append("${encodeSegment(" + ident + ".toString())}")
                else:
                    parts.append(text)
            w.line(f'public fun {fn}({args}): String = "' + "".join(parts) + '"')
        w.blank()

    for route in client_routes(rmap):
        _emit_call_fn(rmap, route, w)


def _emit_call_fn(rmap: RouteMap, route: Route, w: Writer) -> None:
    fn = naming.escape(naming.camel(route.key), LANG)
    args = ["transport: RpcTransport"]
    args += [f"{_param_ident(p)}: {type_name(rmap, p.type)}" for p in route.path_params]
    if route.query_params:
        cls = f"{naming.pascal(route.key)}Query"
        default = "" if any(p.required for p in route.query_params) else f" = {cls}()"
        args.append(f"query: {cls}{default}")
    if route.request is not None:
        args.append(f"body: {type_name(rmap, route.request)}")
    ret = type_name(rmap, route.response) if route.response is not None else "Unit"
    w.doc(route.summary or route.doc, "///")
    if route.deprecated:
        w.line('@Deprecated("declared deprecated in the route map")')
    with w.block(f"public suspend fun {fn}({', '.join(args)}): {ret}"):
        path_args = ", ".join(_param_ident(p) for p in route.path_params)
        w.line(f"val path = {naming.camel(route.key + '_path')}({path_args})")
        w.line("val pairs = query.pairs()" if route.query_params
               else "val pairs = emptyList<Pair<String, String>>()")
        if route.request is not None:
            w.line("val payload = ridlJson.encodeToString(body)")
        opto = "null"
        if route.opto_sync is not None:
            src = route.opto_sync.record_id_from
            if src == "uuid":
                frm, nm = "RecordIdFrom.MINTED", "null"
            else:
                scope, _, name = src.partition(".")
                frm, nm = f"RecordIdFrom.{scope.upper()}", json.dumps(name)
            opto = (
                f"OptoSyncBinding({json.dumps(route.opto_sync.table)}, "
                f"{json.dumps(route.opto_sync.operation)}, {frm}, {nm})"
            )
        delivery = (
            "Delivery.OPTO_SYNC_QUEUED" if route.delivery == DELIVERY_OPTO_SYNC
            else "Delivery.DIRECT"
        )
        w.line("val raw = transport.call(")
        w.indent()
        w.line("RpcRequest(")
        w.indent()
        w.lines(
            f"key = {json.dumps(route.key)},",
            f"method = {json.dumps(route.primary_method)},",
            "path = path,", f"pathTemplate = {json.dumps(route.path)},", "query = pairs,",
            "body = payload," if route.request is not None else "body = null,",
            f"delivery = {delivery},", f"optoSync = {opto},",
        )
        w.dedent()
        w.line(")")
        w.dedent()
        w.line(")")
        if route.response is None:
            w.line("check(raw.isEmpty() || raw.isNotEmpty())")
        else:
            w.line(f"return ridlJson.decodeFromString<{ret}>(raw)")
    w.blank()


def _emit_manifest(rmap: RouteMap, w: Writer) -> None:
    w.line("/** One row per declared operation. */")
    with w.block("public data class OperationInfo", "(", ")"):
        w.lines(
            "public val key: String,", "public val path: String,",
            "public val methods: List<String>,", "public val delivery: Delivery,",
        )
    w.blank()
    w.line("/** Every operation in the route map, in declaration order. */")
    with w.block("public val OPERATIONS: List<OperationInfo> = listOf", "(", ")"):
        for route in rmap.routes:
            methods = ", ".join(json.dumps(m) for m in route.methods)
            delivery = (
                "Delivery.OPTO_SYNC_QUEUED" if route.delivery == DELIVERY_OPTO_SYNC
                else "Delivery.DIRECT"
            )
            w.line(
                f"OperationInfo({json.dumps(route.key)}, {json.dumps(route.path)}, "
                f"listOf({methods}), {delivery}),"
            )
    w.blank()
    queued = ", ".join(json.dumps(r.key) for r in queued_routes(rmap))
    w.line("/** Operations that route through opto-sync's durable queue. */")
    w.line(f"public val QUEUED_OPERATIONS: List<String> = listOf({queued})")
    w.blank()
