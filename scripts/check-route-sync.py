#!/usr/bin/env python3
"""Keep a route map, the HTTP handlers, and the generated clients in lockstep.

This is the v2 gate. It replaces a regex checker that had three defects serious
enough to make it worse than no gate at all:

1. It matched `.route("...")` and `get(...)`/`post(...)` only when both landed
   on the same physical line, so `cargo fmt` wrapping a registration turned
   "route registered" into "route missing". That is why the premarital
   api-server gate was red: two rustfmt-wrapped registrations read as absent.
2. It silently degraded to a Draft-7 structural subset when `jsonschema` was
   not importable, so the gate answered differently on a laptop than in CI.
3. It compared paths and methods only. Handler signatures, request and response
   types, and query parameters were never checked, so a route map could -- and
   did -- claim a return type no handler produced.

This version scans with balanced-parenthesis parsing (formatting-independent),
depends on nothing outside the standard library, and checks the type layer:
a route that declares query parameters must have a handler that extracts them.

Exit 0 in sync, 1 on drift. Suitable for pre-commit, pre-push, and CI.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable

HERE = Path(__file__).resolve().parent
# In a consumer repo `ridl` is vendored at scripts/vendor/ridl; in api-docs
# itself it sits at the repo root. Try both, plus a sibling api-docs checkout,
# so the same script works everywhere it is copied to.
for candidate in (
    HERE / "vendor",
    HERE.parent,
    HERE.parent.parent,
    HERE.parent.parent / "oresoftware" / "api-docs",
    HERE.parent.parent.parent / "oresoftware" / "api-docs",
):
    if (candidate / "ridl" / "__init__.py").is_file():
        sys.path.insert(0, str(candidate))
        break

try:
    from ridl.model import RidlError, RouteMap, load_route_map, path_params_in
    from ridl.validate import validate as ridl_validate
except ImportError:  # pragma: no cover - vendored copies carry ridl alongside
    RidlError = Exception  # type: ignore[assignment]
    RouteMap = Any  # type: ignore[assignment,misc]
    load_route_map = None  # type: ignore[assignment]
    ridl_validate = None  # type: ignore[assignment]
    path_params_in = None  # type: ignore[assignment]

# Aliases the shared docs router mounts. They exist in code without being
# product operations, so they are allowed to be absent from the map.
STANDARD_DOCS_PATHS = {
    "/docs/api", "/api/docs", "/api/docs.json", "/api-docs", "/api-docs.json",
    "/openapi.json", "/openrpc.json", "/connect.json",
}

ROUTE_CALL = re.compile(r"\.route\s*\(")
# Any of the spellings a repo might use to merge the shared docs router.
DOCS_MERGE = re.compile(r"(?:docs|api_docs|ores_api_docs)\s*::\s*(?:axum_router\s*::\s*)?router\s*\(")
STRING_LIT = re.compile(r"""^\s*(?:r#*)?["']([^"']*)["']""")
METHOD_CALL = re.compile(
    r"\b(get|post|put|patch|delete|head|options|any)\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)?"
)
HANDLER_FN = re.compile(r"\b(?:pub\s+)?(?:async\s+)?fn\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(")
# Extractors may be imported under an alias -- `use axum::extract::Path as
# AxumPath` is common because `std::path::Path` is usually in scope too. A bare
# `\bPath` never matches inside `AxumPath`, so the checker silently concluded
# the handler had no path extractor and passed a route it should have failed.
EXTRACTOR_NAMES = ("Path", "Query", "Json", "Form")
EXTRACTOR_ALIAS = re.compile(
    r"\buse\s+[A-Za-z0-9_:{}\s,]*?\b(Path|Query|Json|Form)\s+as\s+([A-Za-z_][A-Za-z0-9_]*)"
)
NEST_CALL = re.compile(r"\.nest(?:_service)?\s*\(")
ROUTER_FN = re.compile(r"\b(?:pub(?:\s*\([^)]*\))?\s+)?fn\s+([A-Za-z_][A-Za-z0-9_]*)\s*\([^)]*\)\s*->\s*[^{;]*\bRouter\b")
# `let v1 = Router::new().route(..)` then `.nest("/v1", v1)`: the nested router
# is a local binding, not a function, so a call-only resolver never finds it.
ROUTER_LET = re.compile(r"\blet\s+(?:mut\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*(?::[^=]*)?=\s*Router\s*::\s*new\s*\(")
CALL_EXPR = re.compile(r"\b(?:([A-Za-z_][A-Za-z0-9_]*)\s*::\s*)?([A-Za-z_][A-Za-z0-9_]*)\s*\(")
CFG_TEST = re.compile(r"#\[cfg\(test\)\]")
IDENT_ONLY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Axum 0.7 used `:name`; 0.8 uses `{name}`. Mixing them is a real bug that the
# old exact-string comparison reported as two unrelated errors.
COLON_PARAM = re.compile(r"/:([A-Za-z_][A-Za-z0-9_]*)")

HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}


# --------------------------------------------------------------------------
# Source scanning
# --------------------------------------------------------------------------

def _balanced(text: str, open_index: int) -> tuple[str, int]:
    """Return the text inside the parentheses opened at `open_index`, ignoring
    parens inside string literals, and the index just past the closing paren."""
    depth = 0
    i = open_index
    in_str: str | None = None
    escaped = False
    start = open_index + 1
    while i < len(text):
        ch = text[i]
        if in_str is not None:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == in_str:
                in_str = None
            i += 1
            continue
        if ch in "\"'":
            in_str = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[start:i], i + 1
        i += 1
    return text[start:], len(text)


def strip_cfg_test(text: str) -> str:
    """Remove `#[cfg(test)] mod ... { ... }` bodies.

    Test modules build their own routers -- `/slow`, `/`, `/ws` -- and scanning
    them reported a crate's own unit tests as undocumented production surface.
    """
    out: list[str] = []
    i = 0
    while True:
        match = CFG_TEST.search(text, i)
        if not match:
            out.append(text[i:])
            return "".join(out)
        brace = text.find("{", match.end())
        if brace == -1:
            out.append(text[i:])
            return "".join(out)
        # Only skip when the attribute is attached to a module or block, not to
        # a single `#[cfg(test)] use ...` line.
        head = text[match.end():brace]
        if "mod " not in head and "fn " not in head:
            out.append(text[i:match.end()])
            i = match.end()
            continue
        out.append(text[i:match.start()])
        _, i = _balanced_braces(text, brace)


def _balanced_braces(text: str, open_index: int) -> tuple[str, int]:
    depth = 0
    i = open_index
    in_str: str | None = None
    escaped = False
    while i < len(text):
        ch = text[i]
        if in_str is not None:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == in_str:
                in_str = None
            i += 1
            continue
        if ch in "\"'":
            in_str = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[open_index + 1 : i], i + 1
        i += 1
    return text[open_index:], len(text)


def _statement_end(text: str, start: int) -> int:
    """End of the `let ... = Router::new()....;` statement beginning at `start`."""
    depth = 0
    i = start
    in_str: str | None = None
    escaped = False
    while i < len(text):
        ch = text[i]
        if in_str is not None:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == in_str:
                in_str = None
        elif ch in "\"'":
            in_str = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == ";" and depth <= 0:
            return i
        i += 1
    return len(text)


class RouterFn:
    """One `fn ... -> Router` body: the routes it registers directly, and the
    routers it nests under a prefix."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.routes: list[tuple[str, dict[str, set[str]]]] = []
        self.nests: list[tuple[str, str]] = []  # (prefix, callee fn name)


class SourceIndex:
    """Everything the gate needs to know about a repo's Rust handlers."""

    def __init__(self) -> None:
        self.routes: dict[str, set[str]] = {}
        # (path, method) -> handler names, so two operations sharing a path but
        # not a method are checked against their own handler rather than
        # whichever one happened to be first.
        self.handlers: dict[tuple[str, str], set[str]] = {}
        self.signatures: dict[str, set[str]] = {}
        self.docs_merged = False
        self.colon_paths: list[str] = []
        self.router_fns: dict[str, RouterFn] = {}
        self.top_level: list[str] = []

    def add_route(self, path: str, by_method: dict[str, set[str]]) -> None:
        self.routes.setdefault(path, set()).update(by_method)
        for method, handlers in by_method.items():
            self.handlers.setdefault((path, method), set()).update(
                h for h in handlers if h
            )


def join_path(prefix: str, path: str) -> str:
    if not prefix or prefix == "/":
        return path
    if path == "/":
        return prefix
    return prefix.rstrip("/") + "/" + path.lstrip("/")


def module_of(path: Path, roots: list[Path]) -> str:
    """`src/routes/api/mod.rs` -> `routes::api`; `src/routes/auth.rs` ->
    `routes::auth`. Router functions are almost always called `router`, so
    without a module qualifier four different `fn router()` in one crate
    collapse into one and every nested prefix comes out wrong."""
    for root in roots:
        try:
            rel = path.relative_to(root if root.is_dir() else root.parent)
        except ValueError:
            continue
        parts = list(rel.parts)
        if parts and parts[-1] in ("mod.rs", "lib.rs", "main.rs"):
            parts = parts[:-1]
        elif parts:
            parts[-1] = parts[-1][:-3]
        return "::".join(parts)
    return path.stem


def scan_rust(source_dirs: Iterable[Path]) -> SourceIndex:
    index = SourceIndex()
    sources: list[tuple[str, str]] = []
    for root in source_dirs:
        if not root.exists():
            raise RidlError(f"source path missing: {root}")
        files = [root] if root.is_file() else sorted(root.rglob("*.rs"))
        for path in files:
            if "ridl_generated" in path.name or "/target/" in str(path):
                continue
            raw = path.read_text(encoding="utf-8")
            sources.append((raw, strip_cfg_test(raw), module_of(path, list(source_dirs))))

    # Pass 1: collect extractor aliases across the whole crate. The `use ... as`
    # is frequently in a parent module while the handler is in a child file, so
    # a file-local map misses it and the gate silently passes a route it should
    # have failed.
    aliases: dict[str, str] = {}
    for raw, _, _ in sources:
        for real, alias in EXTRACTOR_ALIAS.findall(raw):
            aliases[alias] = real

    for raw, text, module in sources:
        if DOCS_MERGE.search(text):
            index.docs_merged = True
        _scan_router_fns(text, index, module)
        _scan_signatures(text, index, aliases)
    _resolve_nesting(index)
    return index


def _scan_router_fns(text: str, index: SourceIndex, module: str = "") -> None:
    """Record each `fn ... -> Router` separately, plus anything registered
    outside such a function, so `.nest()` prefixes can be resolved afterwards."""
    def qualify(name: str) -> str:
        return f"{module}::{name}" if module else name

    spans: list[tuple[int, int, str]] = []
    for match in ROUTER_FN.finditer(text):
        brace = text.find("{", match.end())
        if brace == -1:
            continue
        _, end = _balanced_braces(text, brace)
        spans.append((brace, end, qualify(match.group(1))))

    # Local `let x = Router::new()...;` bindings become synthetic routers so a
    # later `.nest("/v1", x)` resolves to the right set of routes.
    for match in ROUTER_LET.finditer(text):
        stop = _statement_end(text, match.end())
        spans.append((match.end() - 1, stop, qualify(match.group(1))))

    covered = set()
    for brace, end, name in spans:
        fn = index.router_fns.setdefault(name, RouterFn(name))
        # Blank out any span nested strictly inside this one, so a route
        # belongs to exactly one router.
        body = list(text[brace:end])
        for inner_start, inner_end, inner_name in spans:
            if inner_name == name:
                continue
            if inner_start >= brace and inner_end <= end and (inner_start, inner_end) != (brace, end):
                for i in range(inner_start - brace, min(inner_end - brace, len(body))):
                    body[i] = " "
        _collect("".join(body), fn, index)
        covered.update(range(brace, end))

    # Registrations outside any `-> Router` function (a `main` that builds the
    # router inline, for instance) are top-level and carry no prefix.
    outside = "".join(ch for i, ch in enumerate(text) if i not in covered)
    top = qualify("__top_level__")
    root = index.router_fns.setdefault(top, RouterFn(top))
    index.top_level.append(top)
    _collect(outside, root, index)


def _collect(body: str, fn: RouterFn, index: SourceIndex) -> None:
    pos = 0
    while True:
        match = ROUTE_CALL.search(body, pos)
        if not match:
            break
        inner, pos = _balanced(body, match.end() - 1)
        lit = STRING_LIT.match(inner)
        if not lit:
            continue
        route_path = lit.group(1)
        _, _, rest = inner.partition(",")
        by_method: dict[str, set[str]] = {}
        for verb, handler in METHOD_CALL.findall(rest):
            slot = by_method.setdefault(verb.upper(), set())
            if handler:
                slot.add(handler)
        if COLON_PARAM.search(route_path):
            index.colon_paths.append(route_path)
        if by_method:
            fn.routes.append((route_path, by_method))

    pos = 0
    while True:
        match = NEST_CALL.search(body, pos)
        if not match:
            break
        inner, pos = _balanced(body, match.end() - 1)
        lit = STRING_LIT.match(inner)
        if not lit:
            continue
        prefix = lit.group(1)
        _, _, rest = inner.partition(",")
        rest = rest.strip().rstrip(",").strip()
        call = CALL_EXPR.search(rest)
        if call:
            # `api::router()` -> prefer `<module>::api::router`, else any
            # router whose qualified name ends with `api::router`.
            fn.nests.append(
                (prefix, f"{call.group(1)}::{call.group(2)}" if call.group(1) else call.group(2))
            )
        elif IDENT_ONLY.match(rest):
            fn.nests.append((prefix, rest))


def _resolve_nesting(index: SourceIndex) -> None:
    """Walk the nest graph so a route registered as `/sync/changes` inside a
    module mounted at `/api/v1` is compared as `/api/v1/sync/changes`.

    Without this the gate reported every nested route twice -- once as a map
    path missing from source, once as a source route missing from the map --
    which is what made a correct route map look like 29 errors."""

    def resolve(name: str, caller: str) -> str | None:
        if name in index.router_fns:
            return name
        parent = caller.rsplit("::", 1)[0] if "::" in caller else ""
        candidates = [c for c in (f"{parent}::{name}",) if c in index.router_fns]
        if candidates:
            return candidates[0]
        suffix = [c for c in index.router_fns if c.endswith("::" + name)]
        return suffix[0] if len(suffix) == 1 else None

    def walk(name: str, prefix: str, seen: frozenset[str]) -> None:
        fn = index.router_fns.get(name)
        if fn is None or name in seen:
            return
        for route_path, by_method in fn.routes:
            index.add_route(join_path(prefix, route_path), by_method)
        for nested_prefix, callee in fn.nests:
            target = resolve(callee, name)
            if target:
                walk(target, join_path(prefix, nested_prefix), seen | {name})

    reachable: set[str] = set()
    for name in list(index.router_fns):
        for _, callee in index.router_fns[name].nests:
            target = resolve(callee, name)
            if target:
                reachable.add(target)

    # Start from every router that nobody nests -- those are the real entry
    # points -- so a nested module is not also counted at its bare prefix.
    roots = [n for n in index.router_fns if n not in reachable]
    for name in roots:
        walk(name, "", frozenset())


def _scan_signatures(text: str, index: SourceIndex, aliases: dict[str, str]) -> None:
    names = list(EXTRACTOR_NAMES) + list(aliases)
    pattern = re.compile(r"\b(" + "|".join(sorted(names, key=len, reverse=True)) + r")\s*<")
    for match in HANDLER_FN.finditer(text):
        name = match.group(1)
        args, _ = _balanced(text, match.end() - 1)
        found = {aliases.get(kind, kind) for kind in pattern.findall(args)}
        index.signatures.setdefault(name, set()).update(found)


# --------------------------------------------------------------------------
# Comparison
# --------------------------------------------------------------------------

def normalize_path(path: str) -> str:
    """Render `:id` and `{id}` the same way.

    Axum 0.7 spells a path parameter `:id`; 0.8 spells it `{id}`. Route maps
    always use `{id}`. The two forms match the same URLs at runtime, so a repo
    still on 0.7 is not broken -- but comparing the templates literally reported
    it as two unrelated errors per route. Normalize, then say once that the
    source is on the older spelling.
    """
    return COLON_PARAM.sub(lambda m: "/{" + m.group(1) + "}", path)


def compare(rmap: RouteMap, index: SourceIndex, *, allow_docs: bool, label: str) -> list[str]:
    errors: list[str] = []
    documented: dict[str, set[str]] = {}
    by_slot: dict[tuple[str, str], Any] = {}
    for route in rmap.routes:
        documented.setdefault(route.path, set()).update(route.methods)
        for method in route.methods:
            by_slot[(route.path, method)] = route

    scanned = {normalize_path(p): m for p, m in index.routes.items()}
    scanned_handlers = {
        (normalize_path(p), m): h for (p, m), h in index.handlers.items()
    }

    if index.colon_paths:
        # Advisory, not a failure: the URLs agree, only the template spelling
        # differs. Flagging it as drift would make an accurate map look wrong.
        print(
            f"note: {label}: source uses Axum 0.7 `:param` syntax in "
            f"{sorted(set(index.colon_paths))}; route maps use `{{param}}`. "
            f"Matched by normalizing -- migrate to axum 0.8 when convenient.",
            file=sys.stderr,
        )

    extra_ok = STANDARD_DOCS_PATHS if (allow_docs and index.docs_merged) else set()

    for path, methods in documented.items():
        if path not in scanned:
            errors.append(f"{label}: map path {path} is not registered in source")
            continue
        found = scanned[path]
        # `any(handler)` answers every verb, so it satisfies whatever the map
        # declares; reporting it as a mismatch would push people to write
        # `methods: ["ANY"]`, which is not an HTTP method.
        missing = set() if "ANY" in found else methods - found - {"HEAD"}
        if missing:
            errors.append(
                f"{label}: {path} map methods {sorted(missing)} missing in source "
                f"{sorted(scanned[path])}"
            )

    for path, methods in scanned.items():
        if path in extra_ok:
            continue
        if path not in documented:
            errors.append(f"{label}: source route {path} is not in the map")
            continue
        extra = methods - documented[path] - {"HEAD", "ANY"}
        if extra:
            errors.append(
                f"{label}: {path} source methods {sorted(extra)} are not declared in the map"
            )

    errors += _compare_signatures(rmap, scanned_handlers, by_slot, index, label)
    return errors


def _compare_signatures(
    rmap: RouteMap,
    scanned_handlers: dict[tuple[str, str], set[str]],
    by_slot: dict[tuple[str, str], Any],
    index: SourceIndex,
    label: str,
) -> list[str]:
    """A handler's extractors must match the types its own operation declares.

    Matching is per (path, method), not per path. `route("/quotes",
    get(list_quotes).post(create_quote))` is two operations with different
    shapes: the GET declares query params, the POST declares a body. Checking
    every handler on the path against one operation made that arrangement
    impossible to satisfy.
    """
    errors: list[str] = []
    for slot, handlers in scanned_handlers.items():
        route = by_slot.get(slot)
        if route is None:
            continue
        path, method = slot
        for handler in handlers:
            kinds = index.signatures.get(handler)
            if kinds is None:
                # Defined in another crate, or an inline closure.
                continue
            where = f"{label}: {route.key} ({method} {path} -> {handler})"

            if route.path_params and "Path" not in kinds:
                errors.append(
                    f"{where}: map declares path params "
                    f"{[p.wire for p in route.path_params]} but the handler has no "
                    f"`Path<..>` extractor"
                )
            if not route.path_params and "Path" in kinds:
                errors.append(
                    f"{where}: handler extracts `Path<..>` but the map declares no "
                    f"path params"
                )
            if route.query_params and "Query" not in kinds:
                errors.append(
                    f"{where}: map declares query params "
                    f"{[p.wire for p in route.query_params]} but the handler has no "
                    f"`Query<..>` extractor"
                )
            if not route.query_params and "Query" in kinds:
                errors.append(
                    f"{where}: handler extracts `Query<..>` but the map declares no "
                    f"query params -- an undocumented query parameter is how a "
                    f"contract rots"
                )
            body = {"Json", "Form"} & kinds
            if route.request is not None and not body:
                errors.append(
                    f"{where}: map declares a request body ({route.request}) but the "
                    f"handler extracts neither `Json<..>` nor `Form<..>`"
                )
            if route.request is None and body and method != "GET":
                errors.append(
                    f"{where}: handler extracts {sorted(body)} but the map declares no "
                    f"request body"
                )
    return errors


def identical(a: Path, b: Path) -> list[str]:
    """v1 called this 'byte-for-byte' but compared parsed objects, so whitespace
    and key order could differ between the two published copies. Compare bytes."""
    if a.read_bytes() == b.read_bytes():
        return []
    return [f"{a} is not byte-identical to {b}"]


def load_config(root: Path) -> dict:
    for name in ("ridl.json", "route-sync.json", "scripts/route-sync.json"):
        path = root / name
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    return {}


def resolve(root: Path, rel: str) -> Path:
    return (root / rel).resolve()


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=None)
    parser.add_argument("--map", action="append", dest="maps", default=[])
    parser.add_argument("--source", action="append", dest="sources", default=[])
    parser.add_argument("--identical", action="append", dest="identical", default=[])
    parser.add_argument("--allow-docs-merge", action="store_true")
    parser.add_argument("--skip-source", action="store_true")
    parser.add_argument(
        "--skip-drift", action="store_true", help="skip the generated-code drift check"
    )
    args = parser.parse_args(argv)

    if load_route_map is None:
        print(
            "route-map sync failed:\n  - the `ridl` package is not importable; "
            "vendor it next to this script or install it",
            file=sys.stderr,
        )
        return 1

    root = (args.root or Path.cwd()).resolve()
    cfg = load_config(root)

    # A `maps` entry is either a path, or an object pairing one map with the
    # sources that implement it. The contract repo holds both the api and web
    # maps while each is served by a different crate; without the pairing it
    # would have to check both maps against one server and report the other
    # server's entire surface as drift.
    raw_maps = args.maps or cfg.get("maps") or []
    map_paths: list[Path] = []
    per_map_sources: dict[Path, list[Path]] = {}
    for entry in raw_maps:
        if isinstance(entry, dict):
            path = resolve(root, entry["path"])
            per_map_sources[path] = [resolve(root, s) for s in entry.get("sources", [])]
        else:
            path = resolve(root, entry)
        map_paths.append(path)

    source_dirs = [resolve(root, p) for p in (args.sources or cfg.get("sources") or [])]
    twins = [resolve(root, p) for p in (args.identical or cfg.get("identical_to") or [])]
    allow_docs = args.allow_docs_merge or bool(cfg.get("allow_docs_merge"))
    skip_source = args.skip_source or bool(cfg.get("skip_source"))

    if not map_paths:
        found = sorted((root / "route-maps").glob("*.route-map.json"))
        if not found:
            parser.error("no --map and no route-maps/*.route-map.json")
        map_paths = found

    errors: list[str] = []
    maps: list[tuple[Path, RouteMap]] = []
    for path in map_paths:
        if not path.is_file():
            errors.append(f"missing map {path}")
            continue
        try:
            rmap = load_route_map(path)
        except (RidlError, json.JSONDecodeError) as exc:
            errors.append(f"{path}: {exc}")
            continue
        maps.append((path, rmap))
        errors.extend(f"{path.name}: {err}" for err in ridl_validate(rmap))

    twins_by_name = {p.name: p for p in twins}
    for twin in twins:
        if not twin.is_file():
            errors.append(f"identical-to file missing: {twin} (clone the sibling repo)")
    for path, _ in maps:
        twin = twins_by_name.get(path.name)
        if twin is not None and twin.is_file():
            errors.extend(identical(path, twin))

    if not skip_source:
        for path, rmap in maps:
            dirs = per_map_sources.get(path) or source_dirs
            if not dirs:
                continue
            try:
                index = scan_rust(dirs)
            except RidlError as exc:
                errors.append(str(exc))
                continue
            if not index.routes and not index.docs_merged:
                errors.append(f"no .route(..) registrations found under {dirs}")
            errors.extend(compare(rmap, index, allow_docs=allow_docs, label=path.name))

    if not args.skip_drift and not cfg.get("skip_drift"):
        from ridl.cli import run as ridl_run

        if ridl_run(["drift", "--root", str(root)]) != 0:
            errors.append("generated clients are out of date -- run `ridl generate`")

    if errors:
        print("route-map sync failed:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1
    print("route-map sync ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
