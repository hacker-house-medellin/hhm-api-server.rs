"""Go emitter.

Structs with explicit json tags, string-constant enums, and one typed function
per operation. Go has no sum types, so enums are `type X string` with declared
constants and a `Valid()` check -- the closest thing to exhaustiveness the
language offers without codegen'd switches.
"""

from __future__ import annotations

import json

from .. import naming
from ..model import (
    BUILTINS, DELIVERY_OPTO_SYNC, AliasDef, EnumDef, ListOf, MapOf, Named,
    OptionOf, Param, RecordDef, Route, RouteMap, ScalarDef, TypeExpr,
)
from .base import client_routes, Emitted, Writer, header_lines, ordered_types, path_segments, queued_routes

LANG = "go"

_SCALARS = {
    "String": "string", "Bool": "bool", "I32": "int32", "I64": "int64",
    "F64": "float64", "Uuid": "string", "DateTime": "string",
    "Decimal": "string", "Json": "json.RawMessage",
}


def type_name(rmap: RouteMap, expr: TypeExpr) -> str:
    if isinstance(expr, Named):
        return _SCALARS[expr.name] if expr.name in BUILTINS else naming.pascal(expr.name)
    if isinstance(expr, ListOf):
        return f"[]{type_name(rmap, expr.item)}"
    if isinstance(expr, MapOf):
        return f"map[string]{type_name(rmap, expr.value)}"
    if isinstance(expr, OptionOf):
        inner = type_name(rmap, expr.inner)
        return inner if inner.startswith(("[]", "map[", "*")) else f"*{inner}"
    return "any"


def field_type(rmap: RouteMap, expr: TypeExpr, required: bool) -> str:
    inner = type_name(rmap, expr)
    if required or inner.startswith(("[]", "map[", "*")):
        return inner
    return f"*{inner}"


def _is_wildcard(route, wire: str) -> bool:
    return any(p.wire == wire and p.wildcard for p in route.path_params)


def emit(rmap: RouteMap) -> list[Emitted]:
    pkg = naming.snake(rmap.service).replace("_", "")
    w = Writer("\t")
    for line in header_lines(rmap, "//"):
        w.line(line)
    w.lines("", f"package {pkg}", "", "import (", '\t"encoding/json"', '\t"fmt"',
            '\t"net/url"', '\t"strings"', ")", "")
    w.line(f"const Service = {json.dumps(rmap.service)}")
    if rmap.version:
        w.line(f"const Version = {json.dumps(rmap.version)}")
    w.blank()
    _emit_types(rmap, w)
    _emit_transport(w)
    _emit_operations(rmap, w)
    _emit_manifest(rmap, w)
    return [Emitted(path=f"go/{pkg}/ridl_generated.go", text=w.render())]


def _emit_types(rmap: RouteMap, w: Writer) -> None:
    for name in ordered_types(rmap):
        defn = rmap.types[name]
        pas = naming.pascal(name)
        w.doc(defn.doc, "//")
        if isinstance(defn, RecordDef):
            with w.block(f"type {pas} struct"):
                for fld in defn.fields:
                    w.doc(fld.doc, "//")
                    tag = fld.wire + ("" if fld.required else ",omitempty")
                    w.line(
                        f"{naming.pascal(fld.wire)} {field_type(rmap, fld.type, fld.required)} "
                        f"`json:\"{tag}\"`"
                    )
            w.blank()
            # A nil slice or map marshals to `null` in Go and to `[]`/`{}`
            # everywhere else. Normalize so one route map means one wire shape.
            w.line(f"// Normalize replaces nil slices and maps with empty ones, so {pas}")
            w.line("// serialises to the same JSON Go, Rust, TypeScript and Dart agree on.")
            with w.block(f"func (v {pas}) Normalize() {pas}"):
                for fld in defn.fields:
                    if not fld.required:
                        continue
                    target = rmap.underlying(fld.type)
                    if not isinstance(target, (ListOf, MapOf)):
                        continue
                    goty = type_name(rmap, fld.type)
                    name_ = naming.pascal(fld.wire)
                    with w.block(f"if v.{name_} == nil"):
                        empty = f"{goty}{{}}" if isinstance(target, ListOf) else f"{goty}{{}}"
                        w.line(f"v.{name_} = {empty}")
                w.line("return v")
        elif isinstance(defn, EnumDef):
            w.line(f"type {pas} string")
            w.blank()
            with w.block("const", "(", ")"):
                for variant in defn.variants:
                    w.line(f"{pas}{naming.pascal(variant)} {pas} = {json.dumps(variant)}")
            w.blank()
            w.line(f"// All{pas} lists every declared variant, in declaration order.")
            values = ", ".join(f"{pas}{naming.pascal(v)}" for v in defn.variants)
            w.line(f"var All{pas} = []{pas}{{{values}}}")
            w.blank()
            w.line(f"// Valid reports whether v is a declared {pas}.")
            with w.block(f"func (v {pas}) Valid() bool"):
                with w.block(f"for _, candidate := range All{pas}"):
                    w.line("if v == candidate {")
                    w.line("\t\treturn true")
                    w.line("\t}")
                w.line("return false")
        elif isinstance(defn, ScalarDef):
            base = _SCALARS[defn.base]
            w.line(
                f"// {pas} is a distinct type, not a named {base}: Go implicitly"
            )
            w.line(
                f"// converts an untyped constant into a named {base}, which would let"
            )
            w.line(f"// a bare literal stand in for a real {pas}.")
            with w.block(f"type {pas} struct"):
                w.line(f"Value {base}")
            w.blank()
            w.line(f"// New{pas} wraps a raw {base}.")
            w.line(f"func New{pas}(value {base}) {pas} {{ return {pas}{{Value: value}} }}")
            w.blank()
            w.line(f"func (v {pas}) String() string {{ return queryValue(v.Value) }}")
            w.blank()
            w.line(
                f"func (v {pas}) MarshalJSON() ([]byte, error) "
                f"{{ return json.Marshal(v.Value) }}"
            )
            w.blank()
            with w.block(f"func (v *{pas}) UnmarshalJSON(data []byte) error"):
                w.line("return json.Unmarshal(data, &v.Value)")
        elif isinstance(defn, AliasDef):
            w.line(f"type {pas} = {type_name(rmap, defn.target)}")
        w.blank()


def _emit_transport(w: Writer) -> None:
    w.lines(
        "// Delivery says how a call reaches the server.",
        "type Delivery string",
        "",
        "const (",
        '\tDeliveryDirect Delivery = "direct"',
        '\tDeliveryOptoSyncQueued Delivery = "opto_sync_queued"',
        ")",
        "",
        "// RecordIDFrom says where a queued call's opto-sync record id comes from.",
        "type RecordIDFrom string",
        "",
        "const (",
        '\tRecordIDFromPath RecordIDFrom = "path"',
        '\tRecordIDFromRequest RecordIDFrom = "request"',
        '\tRecordIDFromMinted RecordIDFrom = "minted"',
        ")",
        "",
        "// OptoSyncBinding statically describes a queued call's opto-sync record.",
    )
    with w.block("type OptoSyncBinding struct"):
        w.lines("Table string", "Operation string", "From RecordIDFrom", "Name string")
    w.blank()
    w.line("// QueryPair is one query-string entry; a list parameter repeats its key.")
    with w.block("type QueryPair struct"):
        w.lines("Key string", "Value string")
    w.blank()
    w.line("// RPCRequest is one outbound call, fully resolved.")
    with w.block("type RPCRequest struct"):
        w.lines(
            "Key string", "Method string",
            "// Path has every parameter substituted and escaped.",
            "Path string",
            "// PathTemplate is the unsubstituted form, so a transport can locate a",
            "// named parameter inside Path instead of guessing at its position.",
            "PathTemplate string", "Query []QueryPair",
            "// Body is the JSON payload, or nil for operations that carry none.",
            "Body []byte", "Delivery Delivery", "OptoSync *OptoSyncBinding",
        )
    w.blank()
    w.lines(
        "// RPCTransport is the single seam between generated code and the network.",
        "//",
        "// Implement it over net/http for direct calls and over an opto-sync queue",
        "// for queued ones. The dependency runs one way: this package calls",
        "// opto-sync, never the reverse.",
    )
    with w.block("type RPCTransport interface"):
        w.line("Call(request RPCRequest) ([]byte, error)")
    w.blank()
    w.lines(
        "// EncodeSegment escapes one path segment so an id containing / cannot",
        "// reshape the URL.",
        "func EncodeSegment(value string) string { return url.PathEscape(value) }",
        "",
        "func queryValue(value any) string {",
        "\tswitch typed := value.(type) {",
        "\tcase string:",
        "\t\treturn typed",
        "\tcase fmt.Stringer:",
        "\t\treturn typed.String()",
        "\t}",
        '\treturn fmt.Sprintf("%v", value)',
        "}",
        "",
        "var _ = strings.TrimSpace",
        "",
    )


def _param_ident(param: Param) -> str:
    return naming.escape(naming.camel(param.wire), LANG)


def _emit_operations(rmap: RouteMap, w: Writer) -> None:
    for route in client_routes(rmap):
        if not route.query_params:
            continue
        cls = f"{naming.pascal(route.key)}Query"
        w.line(f"// {cls} holds the query parameters for {route.key}.")
        with w.block(f"type {cls} struct"):
            for param in route.query_params:
                w.doc(param.doc, "//")
                w.line(
                    f"{naming.pascal(param.wire)} "
                    f"{field_type(rmap, param.type, param.required)}"
                )
        w.blank()
        with w.block(f"func (q {cls}) Pairs() []QueryPair"):
            w.line("pairs := []QueryPair{}")
            for param in route.query_params:
                fld = f"q.{naming.pascal(param.wire)}"
                key = json.dumps(param.wire)
                is_list = isinstance(rmap.underlying(param.type), ListOf)
                if is_list:
                    with w.block(f"for _, item := range {fld}"):
                        w.line(f"pairs = append(pairs, QueryPair{{{key}, queryValue(item)}})")
                elif param.required:
                    w.line(f"pairs = append(pairs, QueryPair{{{key}, queryValue({fld})}})")
                else:
                    with w.block(f"if {fld} != nil"):
                        w.line(f"pairs = append(pairs, QueryPair{{{key}, queryValue(*{fld})}})")
            w.line("return pairs")
        w.blank()

    for route in client_routes(rmap):
        fn = f"{naming.pascal(route.key)}Path"
        args = ", ".join(
            f"{_param_ident(p)} {type_name(rmap, p.type)}" for p in route.path_params
        )
        w.line(f"// {fn} builds `{route.primary_method} {route.path}`.")
        with w.block(f"func {fn}({args}) string"):
            if not route.path_params:
                w.line(f"return {json.dumps(route.path)}")
            else:
                w.line("var b strings.Builder")
                for text, is_param in path_segments(route.path):
                    if is_param:
                        ident = next(
                            (_param_ident(p) for p in route.path_params if p.wire == text),
                            naming.camel(text),
                        )
                        w.line(f"b.WriteString(EncodeSegment(queryValue({ident})))")
                    else:
                        w.line(f"b.WriteString({json.dumps(text)})")
                w.line("return b.String()")
        w.blank()

    for route in client_routes(rmap):
        _emit_call_fn(rmap, route, w)


def _emit_call_fn(rmap: RouteMap, route: Route, w: Writer) -> None:
    fn = naming.pascal(route.key)
    args = ["transport RPCTransport"]
    args += [f"{_param_ident(p)} {type_name(rmap, p.type)}" for p in route.path_params]
    if route.query_params:
        args.append(f"queryParams {naming.pascal(route.key)}Query")
    if route.request is not None:
        args.append(f"bodyValue {type_name(rmap, route.request)}")

    has_ret = route.response is not None
    ret = f"({type_name(rmap, route.response)}, error)" if has_ret else "error"
    zero = "out" if has_ret else ""
    w.doc(route.summary or route.doc, "//")
    if route.deprecated:
        w.line("//")
        w.line("// Deprecated: declared deprecated in the route map.")
    with w.block(f"func {fn}({', '.join(args)}) {ret}"):
        if has_ret:
            w.line(f"var out {type_name(rmap, route.response)}")
        path_args = ", ".join(_param_ident(p) for p in route.path_params)
        w.line(f"path := {naming.pascal(route.key)}Path({path_args})")
        w.line("query := []QueryPair{}" if not route.query_params else "query := queryParams.Pairs()")
        if route.request is not None:
            w.line("body, err := json.Marshal(bodyValue.Normalize())")
            w.line("if err != nil {")
            w.line(f"\t\treturn {zero + ', ' if has_ret else ''}err")
            w.line("\t}")
        else:
            w.line("var body []byte")
        opto = "nil"
        if route.opto_sync is not None:
            src = route.opto_sync.record_id_from
            if src == "uuid":
                frm, nm = "RecordIDFromMinted", '""'
            else:
                scope, _, name = src.partition(".")
                frm, nm = f"RecordIDFrom{naming.pascal(scope)}", json.dumps(name)
            opto = (
                f"&OptoSyncBinding{{Table: {json.dumps(route.opto_sync.table)}, "
                f"Operation: {json.dumps(route.opto_sync.operation)}, From: {frm}, Name: {nm}}}"
            )
        delivery = (
            "DeliveryOptoSyncQueued" if route.delivery == DELIVERY_OPTO_SYNC else "DeliveryDirect"
        )
        w.line("raw, err := transport.Call(RPCRequest{")
        w.indent()
        w.lines(
            f"Key: {json.dumps(route.key)},",
            f"Method: {json.dumps(route.primary_method)},",
            "Path: path,", f"PathTemplate: {json.dumps(route.path)},",
            "Query: query,", "Body: body,",
            f"Delivery: {delivery},", f"OptoSync: {opto},",
        )
        w.dedent()
        w.line("})")
        w.line("if err != nil {")
        w.line(f"\t\treturn {zero + ', ' if has_ret else ''}err")
        w.line("\t}")
        if has_ret:
            w.line("if err := json.Unmarshal(raw, &out); err != nil {")
            w.line("\t\treturn out, err")
            w.line("\t}")
            w.line("return out, nil")
        else:
            w.line("_ = raw")
            w.line("return nil")
    w.blank()


def _emit_manifest(rmap: RouteMap, w: Writer) -> None:
    w.line("// OperationInfo is one row per declared operation.")
    with w.block("type OperationInfo struct"):
        w.lines("Key string", "Path string", "Methods []string", "Delivery Delivery")
    w.blank()
    w.line("// Operations lists every operation in the route map, in declaration order.")
    with w.block("var Operations = []OperationInfo", "{", "}"):
        for route in rmap.routes:
            methods = ", ".join(json.dumps(m) for m in route.methods)
            delivery = (
                "DeliveryOptoSyncQueued"
                if route.delivery == DELIVERY_OPTO_SYNC
                else "DeliveryDirect"
            )
            w.line(
                f"{{Key: {json.dumps(route.key)}, Path: {json.dumps(route.path)}, "
                f"Methods: []string{{{methods}}}, Delivery: {delivery}}},"
            )
    w.blank()
    queued = ", ".join(json.dumps(r.key) for r in queued_routes(rmap))
    w.line("// QueuedOperations route through opto-sync's durable queue.")
    w.line(f"var QueuedOperations = []string{{{queued}}}")
    w.blank()
