"""`ridl` -- validate a route map and generate typed clients from it.

    ridl check     [--root DIR] [--map FILE ...]
    ridl generate  [--root DIR] [--map FILE ...] [--out DIR] [--lang L ...]
    ridl drift     [--root DIR]        # generate into memory, diff against disk

`drift` is what CI runs: it regenerates every artifact and fails if a byte
differs from what is committed. That is the mechanism that stops a route map
and its clients from disagreeing -- v1 had no such gate, so seventeen clients
hand-maintained the same path string in four incompatible dialects.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .model import RidlError, RouteMap, load_route_map
from .validate import validate

CONFIG_NAMES = ("ridl.json", "route-sync.json")


def _emitters() -> dict[str, object]:
    from .emit import dart, go, gleam, kotlin, python, rust, swift, typescript

    return {
        "rust": rust,
        "typescript": typescript,
        "dart": dart,
        "gleam": gleam,
        "go": go,
        "python": python,
        "swift": swift,
        "kotlin": kotlin,
    }


def load_config(root: Path) -> dict:
    for name in CONFIG_NAMES:
        path = root / name
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise RidlError(f"{path}: {exc}") from exc
            if isinstance(data, dict):
                return data
    return {}


def resolve_maps(root: Path, explicit: list[str], cfg: dict) -> list[Path]:
    listed = explicit or cfg.get("maps") or []
    if not listed:
        found = sorted((root / "route-maps").glob("*.route-map.json"))
        if found:
            return found
        raise RidlError(
            "no route maps: pass --map, or set `maps` in ridl.json, or put them in route-maps/"
        )
    return [(root / entry).resolve() for entry in listed]


def _load_all(paths: list[Path]) -> tuple[list[RouteMap], list[str]]:
    maps: list[RouteMap] = []
    problems: list[str] = []
    for path in paths:
        if not path.is_file():
            problems.append(f"missing route map: {path}")
            continue
        try:
            rmap = load_route_map(path)
        except (RidlError, json.JSONDecodeError) as exc:
            problems.append(f"{path}: {exc}")
            continue
        maps.append(rmap)
        problems.extend(f"{path.name}: {err}" for err in validate(rmap))
    return maps, problems


def _generate(maps: list[RouteMap], languages: list[str]) -> dict[str, str]:
    """Return {relative path -> contents} for every requested language."""
    emitters = _emitters()
    unknown = [lang for lang in languages if lang not in emitters]
    if unknown:
        raise RidlError(f"unknown language(s): {', '.join(unknown)}")
    out: dict[str, str] = {}
    for rmap in maps:
        stem = (rmap.source.name if rmap.source else rmap.service).replace(
            ".route-map.json", ""
        )
        for lang in languages:
            for emitted in emitters[lang].emit(rmap):
                # `rust/ridl_generated.rs` -> `rust/generated/api/ridl_generated.rs`,
                # so a repo with several services keeps one directory per language.
                lang_dir, _, rest = emitted.path.partition("/")
                out[f"{lang_dir}/generated/{stem}/{rest}"] = emitted.text
    return out


def _report(problems: list[str], label: str) -> int:
    if not problems:
        return 0
    print(f"{label} failed:", file=sys.stderr)
    for problem in problems:
        print(f"  - {problem}", file=sys.stderr)
    return 1


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ridl", description=__doc__)
    parser.add_argument("command", choices=("check", "generate", "drift"))
    parser.add_argument("--root", type=Path, default=None)
    parser.add_argument("--map", action="append", dest="maps", default=[])
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--lang", action="append", dest="languages", default=[])
    args = parser.parse_args(argv)

    root = (args.root or Path.cwd()).resolve()
    try:
        cfg = load_config(root)
        map_paths = resolve_maps(root, args.maps, cfg)
    except RidlError as exc:
        return _report([str(exc)], "ridl")

    maps, problems = _load_all(map_paths)
    if problems:
        return _report(problems, "route map validation")

    if args.command == "check":
        total = sum(len(m.routes) for m in maps)
        print(f"ridl check ok: {len(maps)} map(s), {total} operation(s)")
        return 0

    languages = args.languages or cfg.get("languages") or sorted(_emitters())
    out_dir = (args.out or root / cfg.get("out", "generated")).resolve()

    try:
        artifacts = _generate(maps, list(languages))
    except RidlError as exc:
        return _report([str(exc)], "ridl generate")

    if args.command == "generate":
        for rel, text in sorted(artifacts.items()):
            target = out_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        print(f"ridl generate ok: {len(artifacts)} file(s) -> {out_dir}")
        return 0

    # drift
    drift: list[str] = []
    for rel, text in sorted(artifacts.items()):
        target = out_dir / rel
        if not target.is_file():
            drift.append(f"{rel}: not generated on disk (run `ridl generate`)")
        elif target.read_text(encoding="utf-8") != text:
            drift.append(f"{rel}: differs from the route map (run `ridl generate`)")
    # Only sweep the directories this generator owns. The output root is often a
    # shared `clients/` tree that also holds hand-written code, and calling
    # somebody's hand-written client "stale" would be both wrong and alarming.
    expected = {(out_dir / rel).resolve() for rel in artifacts}
    owned = {(out_dir / rel).parent.resolve() for rel in artifacts}
    for directory in sorted(owned):
        if not directory.is_dir():
            continue
        for existing in sorted(directory.iterdir()):
            if existing.is_file() and existing.resolve() not in expected:
                drift.append(
                    f"{existing.relative_to(out_dir)}: stale, no longer in any route map"
                )
    if drift:
        return _report(drift, "generated code drift")
    print(f"ridl drift ok: {len(artifacts)} file(s) match their route maps")
    return 0


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
