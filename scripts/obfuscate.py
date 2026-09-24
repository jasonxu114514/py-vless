"""Source obfuscator for the Python Worker.

Why this exists: the deployed bundle is readable Python, so a rule that looks for
a string like `ed=2560` can fingerprint it. This makes the shipped code much
harder to pattern-match. It is **not** a security boundary.

What it deliberately leaves alone:

  * **Literals compared against foreign data.** `onmessage`, `write`, `fill` and
    friends are names in *JavaScript*. Escaping the literal we compare against
    would silently stop the comparison matching, and the Worker would break in a
    way no external test would catch. Those literals stay verbatim.
  * **The protocol's own tokens.** `vless://`, `/pyip=`, `ed=2560`, `/?ed=`, the
    DoH URL and the `ech=`/`alpn=` parameter names are generated for the outside
    world to read. Escaping them obfuscates the source, not the wire, so a
    determined fingerprint still finds them.
  * Import targets, keyword arguments, attribute names and dunder methods, all
    of which are resolved by name at runtime.

What it does:

  * renames only identifiers this module *binds*, discovered via `ast`, to
    escaped names that never appear as plain text. Python -- unlike JavaScript --
    only allows identifier characters in a name and rejects zero-width
    characters outright, so the mangling has to be textual; the original
    script's zero-width-joiner trick is not available here.
  * rewrites ordinary string literals into several concatenated pieces, each
    written with escapes;
  * drops comments and inserts a no-op statement.

Run:  python scripts/obfuscate.py src build
"""

from __future__ import annotations

import ast
import io
import keyword
import os
import sys

#: Renamed identifiers get this prefix plus a fixed-width hex counter. The
#: counter is fixed width on purpose: with variable widths one generated name
#: can be a prefix of another (Ij0H01 vs Ij0H019), and a position-based rename
#: can then clobber part of a longer identifier, turning `ConfigError` into
#: `Ij0H002gError`.
RENAME_PREFIX = "Ij0H"

#: Identifiers that must survive verbatim: module names, the JS/FFI surface, and
#: anything the runtime resolves by name.
KEEP_NAMES = frozenset({
    "js", "workers", "pyodide", "asyncio", "logging", "ipaddress", "base64",
    "urllib", "parse", "config", "page", "proxy", "vless", "sys", "math",
    "time", "json", "struct", "re", "os", "io", "jsnull", "to_js",
    "create_proxy", "import_from_javascript", "wait_until", "WorkerEntrypoint",
    "WebSocketPair", "Response", "Uint8Array", "Object", "TransformStream",
    "WritableStream", "TextEncoder", "Default", "fetch", "handler", "self",
})

#: String literals that must not be rewritten.
KEEP_STRINGS = frozenset({
    # compared against JavaScript-side object keys and values
    "onmessage", "onclose", "onopen", "write", "close", "fill", "read",
    "getWriter", "getReader", "releaseLock", "pipeTo", "opened", "readable",
    "writable", "arrayBuffer", "binaryType", "data", "value", "done",
    "subarray", "set", "length", "byteLength", "slice", "encode", "decode",
    "getBuffer", "name", "url", "text", "host", "port", "constructor",
    # emitted on the wire for the outside world to read
    "vless://", "/pyip=", "ed=2560", "/?ed=", "pyip=", "ech=", "alpn=",
    "sni=", "host=", "path=", "fp=", "security=tls", "security=none",
    "encryption=none", "type=ws", "chrome", "randomized",
    # headers and query keys we both produce and read back
    "Upgrade", "websocket", "not found", "ok",
})

_NOOP = "_obf_pad = None"


class _Names:
    def __init__(self) -> None:
        self.n = 0

    def next(self) -> str:
        self.n += 1
        return "%s%04x" % (RENAME_PREFIX, self.n)


class Obfuscator:
    """AST-driven renamer plus a literal-escaping pass.

    Renames come from an externally supplied table so that all modules in a
    package agree. Renaming per-module is wrong: `config` does
    `from vless import is_valid_uuid`, so if `vless` renames the function and
    `config` does not, the import fails at load time with
    "cannot import name 'is_valid_uuid' from 'vless'". Local tests do not catch
    that, because they import each module directly rather than through the
    package.
    """

    def __init__(
        self,
        source: str,
        renames: dict[str, str],
        modules: frozenset[str] = frozenset(),
    ) -> None:
        self.src = source
        self.renames = renames
        self.modules = modules

    def run(self) -> str:
        text = strip_comments(self.src)
        text = escape_strings(text)
        text = rename_by_position(text, self.renames, self.modules)
        return insert_noop(text)


def literal_start_and_span(src: str, i: int) -> tuple[int, int]:
    """Given the index of a quote, return (start-of-prefix, end-of-literal).

    The string prefix letters (`r`, `b`, `f`, `u`) sit immediately before the
    quote, so a scanner that starts at the quote loses them. That mistake made
    `r"..."` look like a plain string, and the raw regex it held was then split
    into pieces whose escapes were interpreted as literal backslashes.
    """
    start = i
    while start > 0 and src[start - 1] in "rRbBuUfF":
        start -= 1
    quote = src[i]
    triple = src.startswith(quote * 3, i)
    j = i + (3 if triple else 1)
    n = len(src)
    while j < n:
        if src[j] == "\\":
            j += 2
            continue
        if triple:
            if src.startswith(quote * 3, j):
                return start, j + 3
        elif src[j] == quote:
            return start, j + 1
        elif src[j] == "\n":
            return start, j
        j += 1
    return start, n


def literal_end(src: str, i: int) -> int:
    """Index just past the string literal starting at `i`."""
    return literal_start_and_span(src, i)[1]


def strip_comments(src: str) -> str:
    """Remove `#` comments, leaving string contents alone."""
    out: list[str] = []
    i, n = 0, len(src)
    while i < n:
        ch = src[i]
        if ch in "'\"":
            j = literal_end(src, i)
            out.append(src[i:j])
            i = j
            continue
        if ch == "#":
            j = src.find("\n", i)
            if j == -1:
                break
            i = j  # keep the newline itself
            continue
        out.append(ch)
        i += 1
    return "".join(out)


BACKSLASH = chr(92)
_X = BACKSLASH + "x"
_U = BACKSLASH + "u"
_UBIG = BACKSLASH + "U"


def _escape_char(ch: str) -> str:
    """Escape one character for a str literal.

    The byte-oriented escape form only reaches U+00FF, so anything above it
    needs the unicode form instead. Using the byte form for non-ASCII silently
    corrupts the text.
    """
    cp = ord(ch)
    if cp <= 0xFF:
        return _X + "%02x" % cp
    if cp <= 0xFFFF:
        return _U + "%04x" % cp
    return _UBIG + "%08x" % cp


def _escape_body(inner: str, prefix: str, original: str) -> str:
    """Re-emit a literal's body as escaped pieces joined with `+`.

    The pieces are built from the literal's *value*, recovered by evaluating the
    original source, never from its inner source text. Re-escaping the source
    text double-escapes it: `b"\\x00"` has the value of one NUL byte, but a
    naive pass turns its inner text into the four characters backslash, x, 0, 0
    and then escapes those again.

    Bytes pieces each carry their own `b` prefix, because Python will not
    implicitly concatenate a str piece onto a bytes piece.
    """
    try:
        value = eval(original, {"__builtins__": {}})
    except Exception:
        return original
    if isinstance(value, bytes):
        parts = [value[i:i + 4] for i in range(0, len(value), 4)] or [b""]
        return " + ".join(
            'b"' + "".join(_X + "%02x" % c for c in part) + '"' for part in parts
        )
    parts = [value[i:i + 3] for i in range(0, len(value), 3)] or [""]
    return " + ".join(
        '"' + "".join(_escape_char(c) for c in part) + '"' for part in parts
    )


def _is_fixed(lit: str) -> bool:
    """True when a literal must be left exactly as written.

    Raw and f-string literals are never touched. Splitting a raw literal into
    concatenated pieces is a trap: only the first piece would keep the `r`
    prefix, so every later piece would be parsed as an ordinary string. A regex
    like `r"[0-9a-f]{8}"` then had its `\\x5b`-style escapes interpreted as plain
    backslashes, and the pattern blew up at import with "unbalanced
    parenthesis".
    """
    if lit.startswith('"""') or lit.startswith("'''"):
        return True  # docstrings stay readable
    prefix, body = "", lit
    while body and body[0] in "rRbBuUnNfF":
        prefix += body[0]
        body = body[1:]
    low = prefix.lower()
    if "f" in low or "r" in low:
        return True
    if len(body) < 2:
        return True
    return body[1:-1] in KEEP_STRINGS


def escape_strings(src: str) -> str:
    """Rewrite ordinary string literals into escaped, split pieces."""
    out: list[str] = []
    i, n = 0, len(src)
    while i < n:
        if src[i] in "'\"":
            start, j = literal_start_and_span(src, i)
            # A prefix letter directly before the quote was already emitted as
            # ordinary text; drop it so the literal is handled as a whole.
            if start < i and out and out[-1].endswith(src[start:i]):
                out[-1] = out[-1][: len(out[-1]) - (i - start)]
            lit = src[start:j]
            if _is_fixed(lit):
                out.append(lit)
            else:
                prefix, body = "", lit
                while body and body[0] in "rRbBuUfF":
                    prefix += body[0]
                    body = body[1:]
                if len(body) >= 2 and body[0] in "'\"" and body[1:-1]:
                    out.append(_escape_body(body[1:-1], prefix, lit))
                else:
                    out.append(lit)
            i = j
            continue
        out.append(src[i])
        i += 1
    return "".join(out)


def rename_by_position(
    src: str,
    renames: dict[str, str],
    modules: frozenset[str] = frozenset(),
) -> str:
    """Rename identifiers using AST positions.

    A regex over the raw text would also rewrite occurrences inside string
    literals and corrupt them, so the AST's own line/column spans are used and
    every rename stays inside a real identifier.
    """
    if not renames:
        return src
    lines = src.split(chr(10))

    def replace(lineno: int, start: int, end: int, new: str) -> None:
        line = lines[lineno - 1]
        lines[lineno - 1] = line[:start] + new + line[end:]

    def locate(node, name: str) -> tuple[int, int] | None:
        """Find `name` on the node's line, without touching other literals.

        A plain `line.find(name, node.col_offset)` is not enough for f-strings:
        their span covers the whole literal including the `{...}` expressions,
        and a naive slice there splices text over the braces and produces
        "f-string: expecting '='". Searching from the node's own column keeps
        the edit inside the expression.
        """
        line = lines[node.lineno - 1]
        at = line.find(name, node.col_offset)
        if at == -1:
            at = line.rfind(name)
        return (node.lineno, at) if at != -1 else None

    tree = ast.parse(src)
    inside_fstring: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            for sub in ast.walk(node):
                if sub is not node:
                    inside_fstring.add(id(sub))

    hits = []
    for node in ast.walk(tree):
        if id(node) in inside_fstring:
            # Identifiers inside an f-string's `{...}` are left alone. CPython
            # reports them with the whole literal's span, so a positional edit
            # lands in the wrong place and splices text over the braces, giving
            # "f-string: expecting '='". Renaming would need the literal fully
            # rebuilt, which is more risk than the obfuscation is worth here.
            continue
        if isinstance(node, ast.Name) and node.id in renames:
            hits.append((node.lineno, node.col_offset, node.end_lineno,
                         node.end_col_offset, renames[node.id]))
        elif isinstance(node, ast.arg) and node.arg in renames:
            hits.append((node.lineno, node.col_offset, node.end_lineno,
                         node.end_col_offset, renames[node.arg]))
        elif isinstance(node, ast.Attribute):
            # Two cases need the attribute renamed to match a renamed definition:
            #   * `config_mod.load(...)` -- the module is one of ours, so the
            #     name was renamed in the defining module;
            #   * `self._config_page(...)` -- a method on our own class.
            # Without this the Worker raises "module 'config' has no attribute
            # 'load'" or "'Default' object has no attribute '_config_page'" at
            # request time, which no local test catches.
            target = node.attr
            owner = node.value
            is_local_module = (
                isinstance(owner, ast.Name) and owner.id in modules
            )
            is_self = isinstance(owner, ast.Name) and owner.id in ("self", "cls")
            if target in renames and (is_local_module or is_self):
                line = lines[node.lineno - 1]
                at = line.find("." + target, node.col_offset)
                if at != -1:
                    hits.append((node.lineno, at + 1, node.lineno,
                                 at + 1 + len(target), renames[target]))
        elif isinstance(node, ast.alias):
            # `from vless import is_valid_uuid` / `import config as cfg`.
            # These carry plain strings rather than Name nodes, so they need
            # handling of their own -- without it the definition is renamed in
            # one module while the importer still asks for the old name, and the
            # Worker fails to start with
            # "cannot import name 'is_valid_uuid' from 'vless'".
            target = node.asname or node.name
            if target in renames:
                spot = locate(node, target)
                if spot:
                    hits.append((spot[0], spot[1], spot[0],
                                 spot[1] + len(target), renames[target]))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            # A def/class node's col_offset points at the `def`/`class` keyword,
            # not at the name, so the name's own span has to be computed. Using
            # col_offset directly replaced only the first len(name) characters
            # and turned `class ConfigError` into `YgError`.
            if node.name in renames:
                after_keyword = node.col_offset + 4
                start = lines[node.lineno - 1].find(node.name, after_keyword)
                if start == -1:
                    start = lines[node.lineno - 1].find(node.name)
                if start != -1:
                    hits.append((node.lineno, start, node.lineno,
                                 start + len(node.name), renames[node.name]))
        elif isinstance(node, ast.ExceptHandler) and node.name in renames:
            hits.append((node.lineno, node.col_offset, node.end_lineno,
                         node.end_col_offset, renames[node.name]))

    # Apply back to front so earlier offsets stay valid.
    for lineno, start, end_lineno, end, new in sorted(hits, reverse=True):
        if lineno == end_lineno:
            replace(lineno, start, end, new)
    return chr(10).join(lines)


def insert_noop(src: str) -> str:
    """Add a no-op statement, but only where it cannot change semantics.

    It must come after the module docstring (or the docstring stops being one)
    and after any `from __future__` imports, which have to be the first
    statements in the file.

    `ast.parse` does not enforce the `__future__` rule -- only `compile` does --
    so inserting blindly produced a Worker that failed to load with "from
    __future__ imports must occur at the beginning of the file" while every
    syntax check passed. verify() below compiles for that reason.
    """
    lines = src.split("\n")
    at = 0
    idx = 0
    n = len(lines)

    # Skip a module docstring.
    while idx < n and not lines[idx].strip():
        idx += 1
    if idx < n and lines[idx].lstrip()[:1] in ("'", '"'):
        quote = lines[idx].lstrip()[:3]
        if quote in ('"""', "'''"):
            for j in range(idx + 1, n):
                if lines[j].rstrip().endswith(quote):
                    idx = j + 1
                    break
        else:
            idx += 1
        at = idx

    # Skip any __future__ imports.
    for j in range(idx, n):
        stripped = lines[j].strip()
        if not stripped:
            continue
        if stripped.startswith("from __future__"):
            at = j + 1
            continue
        break

    lines.insert(at, _NOOP)
    return "\n".join(lines)


def collect_module_names(source: str) -> set[str]:
    """Names that are safe to rename: module-level defs, classes and constants.

    This is deliberately a whitelist rather than "everything bound". Renaming
    broadly breaks in ways that only show up at request time:

      * a dataclass field or `__init__` parameter is matched to `Foo(host=...)`
        by name, giving "unexpected keyword argument 'host'";
      * a method reached as `self._config_page(...)` needs the attribute renamed
        too, giving "'Default' object has no attribute '_config_page'".

    Both passed every local test, because the tests call the modules through
    their public API and never through `self` or a keyword argument on a renamed
    dataclass. Restricting to module-level names removes the whole class of
    failure while still hiding the function names, which is what a fingerprint
    rule keys on.
    """
    bound: set[str] = set()
    tree = ast.parse(source)

    # Only functions and classes. Module-level constants are left alone on
    # purpose: they are referenced from inside f-strings, and an f-string's
    # `{...}` expressions cannot be edited positionally without corrupting the
    # braces. Renaming `HTTP_PORTS` while the f-string still said `HTTP_PORTS`
    # produced "NameError: name 'HTTP_PORTS' is not defined" at request time.
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)

    # Names imported from a sibling module are already collected from that
    # module, and the shared table renames both sides.
    return bound


#: Names that belong to the *entrypoint contract*: the runtime reaches into the
#: entry module for these, so they can never be renamed. Kept separate from
#: KEEP_NAMES because the entry module is the only place they matter.
ENTRY_EXPORTS = frozenset({"Default", "fetch", "handler"})


def keyword_arg_names(sources: dict[str, str]) -> set[str]:
    """Every name used as a keyword argument, plus every parameter name.

    These must never be renamed. A keyword argument is matched to a parameter by
    name at call time, so renaming the parameter while the call site still says
    `host=` raises

        TypeError: ProxyIP.__init__() got an unexpected keyword argument 'host'

    which is exactly how the first obfuscated deployment failed. Dataclass fields
    and `__init__` parameters are the main source of these, and they cannot be
    renamed without rewriting every call site too.
    """
    names: set[str] = set()
    for source in sources.values():
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.keyword) and node.arg:
                names.add(node.arg)
            elif isinstance(node, ast.arg):
                names.add(node.arg)
            elif isinstance(node, ast.ClassDef):
                for sub in node.body:
                    # Dataclass-style annotations: the field name is the
                    # parameter name.
                    if isinstance(sub, ast.AnnAssign) and isinstance(
                        sub.target, ast.Name
                    ):
                        names.add(sub.target.id)
    return names


def build_rename_table(sources: dict[str, str]) -> dict[str, str]:
    """One shared table: the union of names bound anywhere in the package."""
    names: set[str] = set()
    for source in sources.values():
        names |= collect_module_names(source)

    # Anything reachable by keyword cannot be renamed safely.
    protected = keyword_arg_names(sources)

    table: dict[str, str] = {}
    counter = 0
    for name in sorted(names):
        if name in KEEP_NAMES or name in ENTRY_EXPORTS:
            continue
        if name in protected:
            continue
        if keyword.iskeyword(name):
            continue
        if name.startswith("__") and name.endswith("__"):
            continue
        counter += 1
        table[name] = "%s%04x" % (RENAME_PREFIX, counter)
    return table


def local_module_names(sources: dict[str, str]) -> set[str]:
    """Module names this package defines, plus any alias bound to them.

    `entry.py` does `import config as config_mod` and then `config_mod.load(...)`.
    The definition of `load` gets renamed inside config.py, so the attribute
    access has to be renamed as well or the Worker dies at request time with
    "module 'config' has no attribute 'load'". Local tests miss this entirely,
    because they import each module directly rather than through another module
    of the package.
    """
    modules = {name[:-3] for name in sources if name.endswith(".py")}
    aliases = set(modules)
    for source in sources.values():
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    base = alias.name.split(".")[0]
                    if base in modules and alias.asname:
                        aliases.add(alias.asname)
    return aliases


def attribute_renames(
    sources: dict[str, str], renames: dict[str, str]
) -> list[tuple[int, int, int, int, str]]:
    """(file-agnostic) spans of `module.name` accesses to rewrite."""
    modules = local_module_names(sources)
    hits: list[tuple[int, int, int, int, str]] = []
    for source in sources.values():
        lines = source.split(chr(10))
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Attribute):
                continue
            if not isinstance(node.value, ast.Name):
                continue
            if node.value.id not in modules:
                continue
            if node.attr not in renames:
                continue
            line = lines[node.lineno - 1]
            # The attribute name sits after the dot on the node's own line.
            at = line.find("." + node.attr, node.col_offset)
            if at == -1:
                continue
            hits.append((node.lineno, at + 1, node.lineno,
                         at + 1 + len(node.attr), renames[node.attr]))
    return hits


def verify(out_dir: str) -> None:
    """Fail loudly if the generated sources are not loadable.

    `compile` is used rather than `ast.parse` because only compile enforces
    ordering rules such as `from __future__` having to come first, and an
    ordering mistake surfaces at deploy time index as a Worker that will not
    start.
    """
    for name in sorted(os.listdir(out_dir)):
        if not name.endswith(".py"):
            continue
        path = os.path.join(out_dir, name)
        source = io.open(path, encoding="utf-8").read()
        try:
            compile(source, name, "exec")
        except SyntaxError as exc:
            raise SystemExit("obfuscated %s does not compile: %s" % (name, exc))


def obfuscate_dir(src_dir: str, out_dir: str) -> list[tuple[str, int, int]]:
    os.makedirs(out_dir, exist_ok=True)

    sources: dict[str, str] = {}
    for name in sorted(os.listdir(src_dir)):
        if not name.endswith(".py") or name.startswith("__"):
            continue
        sources[name] = io.open(
            os.path.join(src_dir, name), encoding="utf-8"
        ).read()

    # One shared table across the whole package, so cross-module imports keep
    # resolving.
    renames = build_rename_table(sources)

    rows = []
    for name, source in sources.items():
        result = Obfuscator(source, renames, frozenset(local_module_names(sources))).run()
        io.open(os.path.join(out_dir, name), "w", encoding="utf-8").write(result)
        rows.append((name, len(source), len(result)))
    verify(out_dir)
    return rows


if __name__ == "__main__":
    src_dir = sys.argv[1] if len(sys.argv) > 1 else "src"
    out_dir = sys.argv[2] if len(sys.argv) > 2 else "build"
    total_in = total_out = 0
    for name, before, after in obfuscate_dir(src_dir, out_dir):
        total_in += before
        total_out += after
        print("  %-14s %7d -> %7d  x%.2f" % (name, before, after, after / max(before, 1)))
    print("  %-14s %7d -> %7d  x%.2f"
          % ("TOTAL", total_in, total_out, total_out / max(total_in, 1)))
