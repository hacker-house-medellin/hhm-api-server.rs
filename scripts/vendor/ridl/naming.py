"""Deterministic identifier casing shared by every emitter.

One implementation, so a field named `matter_id` becomes `matterId` in Dart and
TypeScript, `MatterId` in Go and Swift, and stays `matter_id` in Rust, Python and
Gleam -- without each emitter inventing its own rules. The wire name is never
derived from these; it is always the key exactly as written in the route map.
"""

from __future__ import annotations

import re

_SPLIT = re.compile(r"[_\-\s]+")
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

# Words that are keywords or reserved in at least one target language. Emitters
# call `escape` with their own set; this is the shared, conservative union used
# for type names, where a collision is a hard error rather than a rename.
RESERVED = {
    "rust": {
        "as", "async", "await", "box", "break", "const", "continue", "crate", "dyn",
        "else", "enum", "extern", "false", "fn", "for", "if", "impl", "in", "let",
        "loop", "match", "mod", "move", "mut", "pub", "ref", "return", "self", "Self",
        "static", "struct", "super", "trait", "true", "type", "unsafe", "use", "where",
        "while", "yield", "abstract", "become", "final", "macro", "override", "priv",
        "typeof", "unsized", "virtual",
    },
    "typescript": {
        "break", "case", "catch", "class", "const", "continue", "debugger", "default",
        "delete", "do", "else", "enum", "export", "extends", "false", "finally", "for",
        "function", "if", "import", "in", "instanceof", "new", "null", "return", "super",
        "switch", "this", "throw", "true", "try", "typeof", "var", "void", "while",
        "with", "as", "implements", "interface", "let", "package", "private",
        "protected", "public", "static", "yield", "any", "boolean", "number", "string",
        "symbol", "type", "from", "of",
    },
    "dart": {
        "abstract", "as", "assert", "async", "await", "break", "case", "catch", "class",
        "const", "continue", "covariant", "default", "deferred", "do", "dynamic", "else",
        "enum", "export", "extends", "extension", "external", "factory", "false",
        "final", "finally", "for", "function", "get", "hide", "if", "implements",
        "import", "in", "interface", "is", "late", "library", "mixin", "new", "null",
        "on", "operator", "part", "required", "rethrow", "return", "set", "show",
        "static", "super", "switch", "sync", "this", "throw", "true", "try", "typedef",
        "var", "void", "while", "with", "yield",
    },
    "gleam": {
        "as", "assert", "auto", "case", "const", "delegate", "derive", "echo", "else",
        "fn", "if", "implement", "import", "let", "macro", "opaque", "panic", "pub",
        "test", "todo", "type", "use",
    },
    "go": {
        "break", "case", "chan", "const", "continue", "default", "defer", "else",
        "fallthrough", "for", "func", "go", "goto", "if", "import", "interface", "map",
        "package", "range", "return", "select", "struct", "switch", "type", "var",
    },
    "python": {
        "False", "None", "True", "and", "as", "assert", "async", "await", "break",
        "class", "continue", "def", "del", "elif", "else", "except", "finally", "for",
        "from", "global", "if", "import", "in", "is", "lambda", "nonlocal", "not", "or",
        "pass", "raise", "return", "try", "while", "with", "yield", "match", "case",
    },
    "swift": {
        "associatedtype", "class", "deinit", "enum", "extension", "fileprivate", "func",
        "import", "init", "inout", "internal", "let", "open", "operator", "private",
        "protocol", "public", "rethrows", "static", "struct", "subscript", "typealias",
        "var", "break", "case", "continue", "default", "defer", "do", "else",
        "fallthrough", "for", "guard", "if", "in", "repeat", "return", "switch", "where",
        "while", "as", "any", "catch", "false", "is", "nil", "super", "self", "Self",
        "throw", "throws", "true", "try", "Type", "Protocol",
    },
    "kotlin": {
        "as", "break", "class", "continue", "do", "else", "false", "for", "fun", "if",
        "in", "interface", "is", "null", "object", "package", "return", "super", "this",
        "throw", "true", "try", "typealias", "typeof", "val", "var", "when", "while",
        "by", "catch", "constructor", "delegate", "dynamic", "field", "file", "finally",
        "get", "import", "init", "param", "property", "receiver", "set", "setparam",
        "value", "where", "internal", "sealed",
    },
}


def words(name: str) -> list[str]:
    """Split an identifier into lowercase words, from snake_case or camelCase."""
    parts: list[str] = []
    for chunk in _SPLIT.split(name):
        if not chunk:
            continue
        parts.extend(p for p in _CAMEL_BOUNDARY.split(chunk) if p)
    return [p.lower() for p in parts]


def snake(name: str) -> str:
    return "_".join(words(name))


def screaming_snake(name: str) -> str:
    return "_".join(words(name)).upper()


def camel(name: str) -> str:
    parts = words(name)
    if not parts:
        return name
    return parts[0] + "".join(p.capitalize() for p in parts[1:])


def pascal(name: str) -> str:
    return "".join(p.capitalize() for p in words(name))


def escape(name: str, language: str) -> str:
    """Make `name` a legal identifier in `language`, deterministically.

    Rust prefixes with `r#` where that is legal; every other language suffixes
    with `_`. Both are stable under repeated application because the escaped
    form is no longer in the reserved set.
    """
    reserved = RESERVED.get(language, set())
    if name not in reserved:
        return name
    if language == "rust" and name not in {"self", "Self", "super", "crate"}:
        return "r#" + name
    return name + "_"
