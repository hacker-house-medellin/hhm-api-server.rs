"""Python emitter.

Frozen dataclasses, `Literal` unions for enums, `NewType` for scalar newtypes,
and one typed function per operation. Type checkers (mypy, pyright) reject a
call with a missing or wrongly-typed argument; at runtime the dataclasses give
you real attribute access instead of dictionary spelunking.
"""

from __future__ import annotations

import json

from .. import naming
from ..model import (
    BUILTINS, DELIVERY_OPTO_SYNC, AliasDef, EnumDef, ListOf, MapOf, Named,
    OptionOf, Param, RecordDef, Route, RouteMap, ScalarDef, TypeExpr,
)
from .base import client_routes, Emitted, Writer, header_lines, ordered_types, path_segments, queued_routes

LANG = "python"

_SCALARS = {
    "String": "str", "Bool": "bool", "I32": "int", "I64": "int", "F64": "float",
    "Uuid": "str", "DateTime": "str", "Decimal": "str", "Json": "Any",
}


def type_name(rmap: RouteMap, expr: TypeExpr) -> str:
    if isinstance(expr, Named):
        return _SCALARS[expr.name] if expr.name in BUILTINS else naming.pascal(expr.name)
    if isinstance(expr, ListOf):
        return f"list[{type_name(rmap, expr.item)}]"
    if isinstance(expr, MapOf):
        return f"dict[str, {type_name(rmap, expr.value)}]"
    if isinstance(expr, OptionOf):
        return f"{type_name(rmap, expr.inner)} | None"
    return "Any"


def field_type(rmap: RouteMap, expr: TypeExpr, required: bool) -> str:
    inner = type_name(rmap, expr)
    return inner if required or inner.endswith("| None") else f"{inner} | None"


def _is_wildcard(route, wire: str) -> bool:
    return any(p.wire == wire and p.wildcard for p in route.path_params)


def emit(rmap: RouteMap) -> list[Emitted]:
    w = Writer()
    w.line('"""')
    for line in header_lines(rmap, ""):
        w.line(line.strip())
    w.lines('"""', "", "from __future__ import annotations", "",
            "import json", "from dataclasses import dataclass, field",
            "from typing import Any, Literal, NewType, Protocol", "")
    w.line(f"SERVICE = {json.dumps(rmap.service)}")
    if rmap.version:
        w.line(f"VERSION = {json.dumps(rmap.version)}")
    w.blank()
    _emit_types(rmap, w)
    _emit_transport(w)
    _emit_operations(rmap, w)
    _emit_manifest(rmap, w)
    return [Emitted(path="python/ridl_generated.py", text=w.render())]


def _emit_types(rmap: RouteMap, w: Writer) -> None:
    for name in ordered_types(rmap):
        defn = rmap.types[name]
        pas = naming.pascal(name)
        if isinstance(defn, ScalarDef):
            w.line(f"{pas} = NewType({json.dumps(pas)}, {_SCALARS[defn.base]})")
            if defn.doc:
                w.line(f'"""{defn.doc}"""')
        elif isinstance(defn, EnumDef):
            union = ", ".join(json.dumps(v) for v in defn.variants)
            w.line(f"{pas} = Literal[{union}]")
            if defn.doc:
                w.line(f'"""{defn.doc}"""')
            values = ", ".join(json.dumps(v) for v in defn.variants)
            w.line(f"{naming.screaming_snake(name)}_VALUES: tuple[{pas}, ...] = ({values},)")
        elif isinstance(defn, RecordDef):
            w.line("@dataclass(frozen=True, slots=True)")
            with w.block(f"class {pas}:", "", ""):
                if defn.doc:
                    w.line(f'"""{defn.doc}"""')
                    w.blank()
                # Required fields first: Python forbids a non-default after a default.
                ordered = [f for f in defn.fields if f.required and not f.has_default]
                ordered += [f for f in defn.fields if not (f.required and not f.has_default)]
                for fld in ordered:
                    ident = naming.escape(naming.snake(fld.wire), LANG)
                    ann = field_type(rmap, fld.type, fld.required)
                    if fld.has_default:
                        w.line(f"{ident}: {type_name(rmap, fld.type)} = {_py_literal(fld.default)}")
                    elif fld.required:
                        w.line(f"{ident}: {ann}")
                    else:
                        w.line(f"{ident}: {ann} = None")
                    if fld.doc:
                        w.line(f'"""{fld.doc}"""')
                w.blank()
                with w.block("def to_json(self) -> dict[str, Any]:", "", ""):
                    w.line("out: dict[str, Any] = {}")
                    for fld in defn.fields:
                        ident = naming.escape(naming.snake(fld.wire), LANG)
                        val = _to_json(rmap, fld.type, f"self.{ident}")
                        if fld.required or fld.has_default:
                            w.line(f"out[{json.dumps(fld.wire)}] = {val}")
                        else:
                            with w.block(f"if self.{ident} is not None:", "", ""):
                                w.line(f"out[{json.dumps(fld.wire)}] = {val}")
                    w.line("return out")
                w.blank()
                w.line("@classmethod")
                with w.block(f'def from_json(cls, data: dict[str, Any]) -> "{pas}":', "", ""):
                    w.line("return cls(")
                    w.indent()
                    for fld in ordered:
                        ident = naming.escape(naming.snake(fld.wire), LANG)
                        src = f"data.get({json.dumps(fld.wire)})"
                        w.line(f"{ident}={_from_json(rmap, fld.type, src, fld)},")
                    w.dedent()
                    w.line(")")
        elif isinstance(defn, AliasDef):
            w.line(f"{pas} = {type_name(rmap, defn.target)}")
        w.blank()


def _py_literal(value: object) -> str:
    if value is None:
        return "None"
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return "field(default_factory=list)"
    if isinstance(value, dict):
        return "field(default_factory=dict)"
    return "None"


def _to_json(rmap: RouteMap, expr: TypeExpr, src: str) -> str:
    target = rmap.underlying(expr)
    if isinstance(target, OptionOf):
        return _to_json(rmap, target.inner, src)
    if isinstance(target, ListOf):
        inner = _to_json(rmap, target.item, "item")
        return src if inner == "item" else f"[{inner} for item in {src} or []]"
    if isinstance(target, MapOf):
        inner = _to_json(rmap, target.value, "value")
        return src if inner == "value" else f"{{key: {inner} for key, value in ({src} or {{}}).items()}}"
    if isinstance(target, Named) and rmap.is_record(target):
        return f"{src}.to_json() if {src} is not None else None"
    return src


def _from_json(rmap: RouteMap, expr: TypeExpr, src: str, fld: object) -> str:
    target = rmap.underlying(expr)
    required = getattr(fld, "required", True)
    has_default = getattr(fld, "has_default", False)
    if isinstance(target, ListOf):
        inner = _from_json(rmap, target.item, "item", _Req())
        base = f"[{inner} for item in ({src} or [])]" if inner != "item" else f"({src} or [])"
        return base
    if isinstance(target, MapOf):
        inner = _from_json(rmap, target.value, "value", _Req())
        if inner == "value":
            return f"({src} or {{}})"
        return f"{{key: {inner} for key, value in ({src} or {{}}).items()}}"
    if isinstance(target, Named) and rmap.is_record(target):
        pas = naming.pascal(rmap.underlying(target).name)
        call = f"{pas}.from_json({src})"
        if required and not has_default:
            return call
        return f"({call} if {src} is not None else {_py_literal(getattr(fld, 'default', None))})"
    if has_default:
        return f"({src} if {src} is not None else {_py_literal(getattr(fld, 'default', None))})"
    return src


class _Req:
    required = True
    has_default = False
    default = None


def _emit_transport(w: Writer) -> None:
    w.lines(
        '# ------------------------------------------------------------- transport',
        "",
        'Delivery = Literal["direct", "opto_sync_queued"]',
        'RecordIdFrom = Literal["path", "request", "minted"]',
        "",
        "@dataclass(frozen=True, slots=True)",
    )
    with w.block("class OptoSyncBinding:", "", ""):
        w.line('"""Static description of a queued call\'s opto-sync record."""')
        w.blank()
        w.lines("table: str", "operation: str", "record_id_from: RecordIdFrom",
                "record_id_name: str | None = None")
    w.blank()
    w.line("@dataclass(frozen=True, slots=True)")
    with w.block("class RpcRequest:", "", ""):
        w.line('"""One outbound call, fully resolved. Transports never re-derive paths."""')
        w.blank()
        w.lines(
            "key: str", "method: str", "path: str",
            "#: The unsubstituted template, so a transport can locate a named",
            "#: parameter inside `path` instead of guessing at its position.",
            'path_template: str = ""',
            "query: tuple[tuple[str, str], ...] = ()",
            "body: str | None = None",
            'delivery: Delivery = "direct"',
            "opto_sync: OptoSyncBinding | None = None",
        )
    w.blank()
    with w.block("class RpcTransport(Protocol):", "", ""):
        w.lines(
            '"""The single seam between generated code and the network.',
            "",
            "    Implement it over your HTTP client for direct calls and over an",
            "    opto-sync queue for queued ones. The dependency runs one way: this",
            "    module calls opto-sync, never the reverse.",
            '    """',
            "",
            "def call(self, request: RpcRequest) -> str: ...",
        )
    w.blank()
    w.lines(
        "def encode_segment(value: str) -> str:",
        '    """Escape one path segment so an id containing `/` cannot reshape the URL."""',
        "    from urllib.parse import quote",
        "",
        '    return quote(str(value), safe="")',
        "",
        "",
        "def _query_value(value: Any) -> str:",
        "    if isinstance(value, bool):",
        '        return "true" if value else "false"',
        "    return value if isinstance(value, str) else str(value)",
        "",
    )


def _param_ident(param: Param) -> str:
    return naming.escape(naming.snake(param.wire), LANG)


def _emit_operations(rmap: RouteMap, w: Writer) -> None:
    for route in client_routes(rmap):
        if not route.query_params:
            continue
        cls = f"{naming.pascal(route.key)}Query"
        w.line("@dataclass(frozen=True, slots=True)")
        with w.block(f"class {cls}:", "", ""):
            w.line(f'"""Query parameters for `{route.key}`."""')
            w.blank()
            ordered = [p for p in route.query_params if p.required]
            ordered += [p for p in route.query_params if not p.required]
            for param in ordered:
                ann = field_type(rmap, param.type, param.required)
                default = "" if param.required else " = None"
                w.line(f"{_param_ident(param)}: {ann}{default}")
            w.blank()
            with w.block("def pairs(self) -> tuple[tuple[str, str], ...]:", "", ""):
                w.line("out: list[tuple[str, str]] = []")
                for param in route.query_params:
                    ident = f"self.{_param_ident(param)}"
                    key = json.dumps(param.wire)
                    is_list = isinstance(rmap.underlying(param.type), ListOf)
                    with w.block(f"if {ident} is not None:", "", ""):
                        if is_list:
                            with w.block(f"for item in {ident}:", "", ""):
                                w.line(f"out.append(({key}, _query_value(item)))")
                        else:
                            w.line(f"out.append(({key}, _query_value({ident})))")
                w.line("return tuple(out)")
        w.blank()

    for route in client_routes(rmap):
        fn = naming.escape(naming.snake(f"{route.key}_path"), LANG)
        args = ", ".join(f"{_param_ident(p)}: {type_name(rmap, p.type)}" for p in route.path_params)
        with w.block(f"def {fn}({args}) -> str:", "", ""):
            w.line(f'"""`{route.primary_method} {route.path}`"""')
            if not route.path_params:
                w.line(f"return {json.dumps(route.path)}")
            else:
                parts = []
                for text, is_param in path_segments(route.path):
                    if is_param:
                        ident = next(
                            (_param_ident(p) for p in route.path_params if p.wire == text),
                            naming.snake(text),
                        )
                        parts.append(f"encode_segment(_query_value({ident}))")
                    else:
                        parts.append(json.dumps(text))
                w.line("return " + " + ".join(parts))
        w.blank()

    for route in client_routes(rmap):
        _emit_call_fn(rmap, route, w)


def _emit_call_fn(rmap: RouteMap, route: Route, w: Writer) -> None:
    fn = naming.escape(naming.snake(route.key), LANG)
    args = ["transport: RpcTransport"]
    args += [f"{_param_ident(p)}: {type_name(rmap, p.type)}" for p in route.path_params]
    if route.query_params:
        default = "" if any(p.required for p in route.query_params) else \
            f" = {naming.pascal(route.key)}Query()"
        args.append(f"query: {naming.pascal(route.key)}Query{default}")
    if route.request is not None:
        args.append(f"body: {type_name(rmap, route.request)}")
    ret = type_name(rmap, route.response) if route.response is not None else "None"

    with w.block(f"def {fn}({', '.join(args)}) -> {ret}:", "", ""):
        w.line(f'"""{route.summary or route.doc or route.key}"""')
        path_args = ", ".join(_param_ident(p) for p in route.path_params)
        w.line(f"path = {naming.snake(route.key)}_path({path_args})")
        w.line("pairs = query.pairs()" if route.query_params else "pairs: tuple[tuple[str, str], ...] = ()")
        if route.request is not None:
            w.line("payload = json.dumps(body.to_json())")
        opto = "None"
        if route.opto_sync is not None:
            src = route.opto_sync.record_id_from
            if src == "uuid":
                frm, nm = '"minted"', "None"
            else:
                scope, _, name = src.partition(".")
                frm, nm = json.dumps(scope), json.dumps(name)
            opto = (
                f"OptoSyncBinding(table={json.dumps(route.opto_sync.table)}, "
                f"operation={json.dumps(route.opto_sync.operation)}, "
                f"record_id_from={frm}, record_id_name={nm})"
            )
        delivery = json.dumps(
            "opto_sync_queued" if route.delivery == DELIVERY_OPTO_SYNC else "direct"
        )
        w.line("raw = transport.call(")
        w.indent()
        w.line("RpcRequest(")
        w.indent()
        w.lines(
            f"key={json.dumps(route.key)},",
            f"method={json.dumps(route.primary_method)},",
            "path=path,", f"path_template={json.dumps(route.path)},", "query=pairs,",
            "body=payload," if route.request is not None else "body=None,",
            f"delivery={delivery},", f"opto_sync={opto},",
        )
        w.dedent()
        w.line(")")
        w.dedent()
        w.line(")")
        if route.response is None:
            w.line("del raw")
            w.line("return None")
        else:
            target = rmap.underlying(route.response)
            if isinstance(target, Named) and rmap.is_record(target):
                w.line(f"return {naming.pascal(target.name)}.from_json(json.loads(raw))")
            else:
                w.line(f"return json.loads(raw)  # type: ignore[no-any-return]")
    w.blank()


def _emit_manifest(rmap: RouteMap, w: Writer) -> None:
    w.line("@dataclass(frozen=True, slots=True)")
    with w.block("class OperationInfo:", "", ""):
        w.line('"""One row per declared operation."""')
        w.blank()
        w.lines("key: str", "path: str", "methods: tuple[str, ...]", "delivery: Delivery")
    w.blank()
    w.line("OPERATIONS: tuple[OperationInfo, ...] = (")
    w.indent()
    for route in rmap.routes:
        methods = ", ".join(json.dumps(m) for m in route.methods)
        delivery = json.dumps(
            "opto_sync_queued" if route.delivery == DELIVERY_OPTO_SYNC else "direct"
        )
        w.line(
            f"OperationInfo(key={json.dumps(route.key)}, path={json.dumps(route.path)}, "
            f"methods=({methods},), delivery={delivery}),"
        )
    w.dedent()
    w.line(")")
    w.blank()
    queued = ", ".join(json.dumps(r.key) for r in queued_routes(rmap))
    w.line("#: Operations that route through opto-sync's durable queue.")
    w.line(f"QUEUED_OPERATIONS: tuple[str, ...] = ({queued}{',' if queued else ''})")
    w.blank()
