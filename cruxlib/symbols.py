"""Phase 0b: extract functions/methods/classes with line ranges, for any language.

Strategy, per file:
  1. tree-sitter-language-pack (pip install tree-sitter-language-pack): ~300 grammars,
     accurate names and spans.
  2. Built-in heuristics (regex + brace/indent/`end` matching) for the common language
     families when tree-sitter is unavailable or fails.
  3. If both find nothing, the file is marked extractor='none' and the scoring prompt asks
     the model to discover functions itself.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .langs import FAMILY
from .util import b36, one_line, sha1, warn

try:  # optional dependency
    import tree_sitter_language_pack as _tslp  # type: ignore
    _HAS_TS = True
except Exception:  # pragma: no cover - depends on environment
    _tslp = None
    _HAS_TS = False

CALLABLE_KINDS = {"function", "method", "constructor"}
CONTAINER_KINDS = {"class", "interface", "struct", "enum", "trait", "impl", "module",
                   "namespace", "object", "record", "protocol", "extension"}
KEYWORDS = {
    "if", "for", "while", "switch", "catch", "return", "new", "else", "do", "try", "synchronized",
    "using", "foreach", "lock", "when", "throw", "function", "typeof", "sizeof", "await", "yield",
    "super", "this", "import", "export", "require", "elif", "with", "match", "case", "defer", "go",
    "fixed", "checked", "unchecked", "nameof", "default", "delete", "void", "in", "of", "assert",
}


@dataclass
class Symbol:
    id: str = ""
    key: str = ""
    file: str = ""
    name: str = ""
    qualname: str = ""
    kind: str = ""
    callable: bool = False
    anonymous: bool = False
    start: int = 0          # declaration line, 1-based
    end: int = 0
    deco_start: int = 0     # first decorator/annotation line (== start when none)
    container: str = ""
    signature: str = ""
    decorators: list = field(default_factory=list)
    # filled by graph.py
    loc: int = 0
    fan_in: int = 0
    fan_in_files: int = 0
    fan_out: int = 0
    tags: list = field(default_factory=list)
    complexity: int = 0
    trivial: str | None = None
    entries: list = field(default_factory=list)
    reach: dict = field(default_factory=dict)
    prio: float = 0.0

    def to_json(self) -> dict:
        return dict(self.__dict__)


def has_tree_sitter() -> bool:
    return _HAS_TS


# ----------------------------------------------------------------------------------------
# helpers shared by both extractors
DECORATOR_LINE = re.compile(r"^(@[A-Za-z_$][\w$.]*|#\[|\[\s*[A-Z][\w.]*\s*(\(|\]|,))")
ROUTE_CALL = re.compile(
    r"\b([A-Za-z_$][\w$]*)\s*\.\s*(get|post|put|patch|delete|del|all|use|options|head|route|on|handle|"
    r"subscribe|command|action|task|GET|POST|PUT|PATCH|DELETE|Get|Post|Put|Patch|Delete|Any|HandleFunc|Handle)"
    r"\s*\(\s*(['\"`])([^'\"`]{0,80})\3")
ROUTE_VERBS = {"get", "post", "put", "patch", "delete", "del", "all", "options", "head", "route",
               "any", "handle", "handlefunc"}


def _indent(s: str) -> int:
    return len(s) - len(s.lstrip(" \t"))


def find_decorators(lines: list[str], s0: int, decl: int) -> int:
    """Return the 0-based first line of the decorator block attached to a declaration.

    s0 is the span start reported by the parser (may already include annotations), decl the
    declaration line. Handles multi-line decorators such as FastAPI's @router.get(\n ...\n).
    """
    top = min(s0, decl)
    for i in range(min(s0, decl), decl):
        if DECORATOR_LINE.match(lines[i].lstrip()):
            top = min(top, i)
    j = top - 1
    while j >= 0 and top - j <= 40:
        st = lines[j].strip()
        if DECORATOR_LINE.match(st):
            top = j
            j -= 1
            continue
        if st and (st.startswith((")", "]")) or st.endswith((",", "(", "["))):
            bal, k, found = 0, j, None
            while k >= 0 and j - k <= 20:
                bal += lines[k].count(")") + lines[k].count("]") - lines[k].count("(") - lines[k].count("[")
                if DECORATOR_LINE.match(lines[k].strip()) and bal <= 0:
                    found = k
                    break
                k -= 1
            if found is not None:
                top = found
                j = found - 1
                continue
        break
    return top


def compact_signature(lines: list[str], decl: int, end: int, limit: int = 150) -> str:
    buf = []
    for i in range(decl, min(end, decl + 6) + 1):
        ln = lines[i]
        buf.append(ln.strip())
        joined = " ".join(buf)
        if re.search(r"(\{|=>|:)\s*$", ln.rstrip()) or ("(" in joined and joined.count("(") <= joined.count(")") and not ln.rstrip().endswith(",")):
            break
    sig = one_line(" ".join(buf), 400)
    sig = re.sub(r"\s*\{\s*$", "", sig)
    return sig if len(sig) <= limit else sig[: limit - 1] + "…"


OBJ_KEY_FN = re.compile(r"^\s*['\"]?([A-Za-z_$][\w$]*)['\"]?\s*:\s*(?:async\s+)?(?:function\b|\(|[A-Za-z_$][\w$]*\s*=>)")
OBJ_DECL = re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*\{")


def _enclosing_object(lines: list[str], s0: int, max_up: int = 200) -> str:
    """Name of the object literal (`const Auth = {`) that a `key: () => ...` member lives in."""
    depth = 0
    for j in range(s0 - 1, max(-1, s0 - max_up), -1):
        ln = lines[j]
        depth += ln.count("}") - ln.count("{")
        if depth < 0:
            m = OBJ_DECL.match(ln)
            return m.group(1) if m else ""
    return ""


def synth_anon_name(lines: list[str], s0: int) -> tuple[str, bool, str]:
    """Name an anonymous callable from its registration context.
    Returns (name, is_route_handler, container_hint)."""
    lo = max(0, s0 - 5)
    window = " ".join(l.strip() for l in lines[lo: s0 + 1])
    best = None
    for m in ROUTE_CALL.finditer(window):
        tail = window[m.start():]
        if tail.count("(") > tail.count(")"):   # registration call still open -> we are its argument
            best = m
    if best:
        recv, verb, _, rpath = best.groups()
        return f"{recv}.{verb}('{rpath}')", verb.lower() in ROUTE_VERBS, ""
    km = OBJ_KEY_FN.match(lines[s0])
    if km:
        return km.group(1), False, _enclosing_object(lines, s0)
    if re.match(r"^\s*export\s+default\b", lines[s0]):
        return "default", False, ""
    m = re.search(r"([A-Za-z_$][\w$.]*)\s*[:=]\s*(async\s+)?(function\b|\(|[A-Za-z_$][\w$]*\s*=>)", lines[s0])
    if m:
        return m.group(1), False, ""
    callee = None
    for m in re.finditer(r"([A-Za-z_$][\w$.]*)\s*\(", window):
        tail = window[m.end() - 1:]
        if tail.count("(") > tail.count(")") and m.group(1).split(".")[-1] not in KEYWORDS:
            callee = m.group(1)            # innermost call still open at the function's start
    if callee:
        return f"<callback of {callee}() @L{s0 + 1}>", False, ""
    return f"<anonymous @L{s0 + 1}>", False, ""


def _decl_line(lines: list[str], s0: int, e0: int, name: str) -> int:
    if not name:
        return s0
    pat = re.compile(r"(?<![\w$])" + re.escape(name) + r"(?![\w$])")
    for i in range(s0, min(e0, s0 + 25) + 1):
        st = lines[i].lstrip()
        if DECORATOR_LINE.match(st) or st.startswith(("//", "#", "*", "/*")):
            continue
        if pat.search(lines[i]):
            return i
    for i in range(s0, min(e0, s0 + 25) + 1):  # first non-decorator line
        if not DECORATOR_LINE.match(lines[i].lstrip()) and lines[i].strip():
            return i
    return s0


def _norm_kind(k) -> str:
    s = str(k).split(".")[-1].lower()
    return {"func": "function", "fn": "function"}.get(s, s)


# ----------------------------------------------------------------------------------------
# tree-sitter extraction
_TS_FAILED_LANGS: set = set()


def _ts_extract(path: str, text: str, lang: str):
    if not _HAS_TS or lang in _TS_FAILED_LANGS:
        return None
    try:
        cfg = _tslp.ProcessConfig(language=lang, structure=True, imports=True)
        res = _tslp.process(text, cfg)
    except Exception as e:  # language missing, download blocked, parse failure
        msg = str(e)
        if "not found" in msg.lower() or "download" in msg.lower() or "unknown" in msg.lower():
            _TS_FAILED_LANGS.add(lang)
            warn(f"tree-sitter unavailable for {lang} ({one_line(msg, 120)}); using heuristics")
        return None
    lines = text.split("\n")
    nlines = len(lines)
    out: list[Symbol] = []

    def emit_callable(it, container: str, name: str, anonymous: bool, kind_hint: str):
        s0 = max(0, min(it.span.start_line, nlines - 1))
        e0 = max(s0, min(it.span.end_line, nlines - 1))
        decl = _decl_line(lines, s0, e0, "" if anonymous else name)
        deco = find_decorators(lines, s0, decl)
        cont = container
        if lang == "go" and not cont:
            m = re.match(r"\s*func\s*\(\s*\w*\s*\*?\s*([A-Za-z_]\w*)", lines[decl])
            if m:
                cont = m.group(1)
        kind = kind_hint
        simple_cont = cont.split(".")[-1] if cont else ""
        if name in ("constructor", "__init__", "__construct", "initialize", "init", "new") and cont:
            kind = "constructor"
        elif simple_cont and name == simple_cont:
            kind = "constructor"
        elif cont and kind == "function":
            kind = "method"
        out.append(Symbol(
            file=path, name=name, qualname=f"{cont}.{name}" if cont else name, kind=kind,
            callable=True, anonymous=anonymous, start=decl + 1, end=e0 + 1, deco_start=deco + 1,
            container=cont, signature=compact_signature(lines, decl, e0),
            decorators=[one_line(lines[i], 120) for i in range(deco, decl) if DECORATOR_LINE.match(lines[i].lstrip())][:6],
        ))

    def visit(items, container: str, inside_callable: bool):
        for it in items:
            kind = _norm_kind(it.kind)
            name = it.name or ""
            if kind in ("function", "method"):
                span_len = it.span.end_line - it.span.start_line + 1
                if not name:
                    # anonymous: keep top-level handlers (router.post('/x', async (req, res) => {...}),
                    # object-literal members (const Auth = { login: (u, p) => ... }), default exports
                    if span_len > 0.6 * nlines and it.children and not inside_callable:
                        visit(it.children, container, False)   # transparent wrapper (IIFE/AMD)
                        continue
                    sname, is_route, hint = synth_anon_name(lines, it.span.start_line)
                    keyed = bool(hint) or (not sname.startswith("<") and not is_route)
                    if inside_callable and not is_route:
                        continue
                    if span_len < 3 and not (keyed or is_route):
                        continue
                    emit_callable(it, hint or container, sname, not keyed, "function")
                    continue
                if inside_callable:
                    continue  # named nested helper: part of its parent
                emit_callable(it, container, name, False, kind)
                visit(it.children, container, True)
            elif kind in CONTAINER_KINDS:
                if inside_callable:
                    continue
                if kind == "module" and it.span.end_line - it.span.start_line < 1:
                    continue  # e.g. Java package / Rust `mod x;` declarations
                qual = f"{container}.{name}" if container and name else (name or container)
                s0 = max(0, min(it.span.start_line, nlines - 1))
                e0 = max(s0, min(it.span.end_line, nlines - 1))
                decl = _decl_line(lines, s0, e0, name)
                out.append(Symbol(
                    file=path, name=name or "<anonymous>", qualname=qual or "<anonymous>", kind=kind,
                    callable=False, start=decl + 1, end=e0 + 1, deco_start=find_decorators(lines, s0, decl) + 1,
                    container=container, signature=compact_signature(lines, decl, e0),
                ))
                visit(it.children, qual, False)
            else:
                visit(it.children, container, inside_callable)

    try:
        visit(res.structure, "", False)
        _supplement_constructors(path, lines, out, lang)
    except Exception as e:  # defensive: unexpected structure shapes
        warn(f"tree-sitter walk failed for {path}: {e}")
        return None
    out.sort(key=lambda x: (x.start, -x.end))
    imports, names = [], set()
    for imp in res.imports or []:
        src = (imp.source or "").strip()
        if src:
            imports.append(one_line(src, 200))
        for n in imp.items or []:
            if n:
                names.add(str(n).split(".")[-1].split(" as ")[-1].strip())
        if imp.alias:
            names.add(str(imp.alias))
    if not out and lang in ("vue", "svelte", "html"):
        return None  # let the script-block path handle SFCs
    return out, imports, names


CTOR_LANGS = {"java", "csharp", "groovy", "dart", "apex", "cpp", "kotlin", "scala"}


def _supplement_constructors(path: str, lines: list[str], syms: list, lang: str) -> None:
    """tree-sitter-language-pack's structure view omits some constructors (e.g. Java). Add them."""
    if lang not in CTOR_LANGS:
        return
    taken = [(s.deco_start, s.end) for s in syms if s.callable]
    for c in [s for s in syms if not s.callable and s.kind in ("class", "struct", "record", "object")]:
        name = c.name
        if not name or name.startswith("<"):
            continue
        rx = re.compile(r"^\s*(?:(?:public|private|protected|internal|static|inline|explicit|constexpr)\s+)*"
                        + (r"(constructor)\s*\(" if lang == "kotlin" else r"(" + re.escape(name) + r")\s*\("))
        for i in range(c.start, min(c.end - 1, len(lines))):
            if any(a - 1 <= i <= b - 1 for a, b in taken):
                continue
            ln = LEADING_ANNOT.sub(r"\1", lines[i])
            if not rx.match(ln) or ln.rstrip().endswith(";"):
                continue
            end = _brace_end(lines, i)
            deco = find_decorators(lines, i, i)
            syms.append(Symbol(file=path, name=name, qualname=f"{c.qualname}.{name}", kind="constructor",
                               callable=True, start=i + 1, end=end + 1, deco_start=deco + 1, container=c.qualname,
                               signature=compact_signature(lines, i, end)))
            taken.append((deco + 1, end + 1))


# ----------------------------------------------------------------------------------------
# heuristic extraction (no dependencies)
JS_LANGS = {"javascript", "typescript", "tsx", "vue", "svelte"}
JAVA_LIKE = {"java", "csharp", "kotlin", "scala", "groovy", "dart", "apex", "cpp", "c", "objc"}
MOD = r"(?:(?:public|private|protected|internal|static|final|abstract|synchronized|native|override|virtual|sealed|async|open|suspend|inline|operator|default|partial|extern|unsafe|new|transient|strictfp|readonly|export|declare)\s+)*"
HEUR_PATTERNS = {
    "js": [
        (re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)\s*[<(]"), "function"),
        (re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*(?:async\s+)?(?:function\b|\([^)]*\)\s*(?::[^=]+)?=>|[A-Za-z_$][\w$]*\s*=>|\(\s*$)"), "function"),
        (re.compile(r"^\s*" + MOD + r"(?:get\s+|set\s+)?\*?\s*([A-Za-z_$][\w$]*)\s*(?:<[^>]*>)?\s*\([^;]*\)\s*(?::\s*[^{;=]+)?\{\s*$"), "method"),
        (re.compile(r"^\s*([A-Za-z_$][\w$]*)\s*:\s*(?:async\s+)?(?:function\b|\([^)]*\)\s*=>)"), "method"),
        (re.compile(r"^\s*" + MOD + r"([A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?\([^)]*\)\s*(?::[^=]+)?=>"), "method"),
    ],
    "javalike": [
        (re.compile(r"^\s*" + MOD + r"(?:<[^>]+>\s*)?(?:[\w\[\]<>,.?*&]+\s+)+\**(?:\w+::)*([A-Za-z_~]\w*)\s*\((?!.*;\s*$)"), "method"),
        (re.compile(r"^\s*" + MOD + r"(?:tailrec\s+|infix\s+)*fun\s+(?:<[^>]+>\s*)?(?:[\w.]+\.)?([A-Za-z_]\w*)\s*\("), "function"),
        (re.compile(r"^\s*(?:(?:override|private|protected|final|implicit|lazy)\s+)*def\s+([A-Za-z_]\w*)"), "function"),
        (re.compile(r"^\s*" + MOD + r"([A-Z]\w*)\s*\([^;]*\)\s*(?::\s*(?:base|this)\s*\([^)]*\)\s*)?\{?\s*$"), "constructor"),
    ],
    "go": [(re.compile(r"^func\s*(?:\(\s*\w*\s*\*?\s*([A-Za-z_]\w*)[^)]*\)\s*)?([A-Za-z_]\w*)\s*[\[(]"), "function")],
    "rust": [(re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:default\s+)?(?:async\s+)?(?:const\s+)?(?:unsafe\s+)?(?:extern\s+\"[^\"]*\"\s+)?fn\s+([A-Za-z_]\w*)"), "function")],
    "php": [(re.compile(r"^\s*(?:(?:public|private|protected|static|final|abstract)\s+)*function\s+&?\s*([A-Za-z_]\w*)\s*\("), "function")],
    "swift": [(re.compile(r"^\s*(?:(?:public|private|internal|fileprivate|open|static|class|override|final|mutating|nonisolated|@\w+)\s+)*func\s+([A-Za-z_]\w*)"), "function"),
              (re.compile(r"^\s*(?:(?:public|private|internal|convenience|required|override)\s+)*(init)\s*[?!]?\s*\("), "constructor")],
    "solidity": [(re.compile(r"^\s*(function|modifier)\s+([A-Za-z_]\w*)\s*\("), "function")],
}
CLASS_RE = re.compile(
    r"^(\s*)(?:(?:export|default|public|private|protected|internal|abstract|final|sealed|static|partial|data|open|"
    r"annotation|inner|case|pub|declare)\s+)*(class|interface|struct|enum|trait|object|record|protocol|extension|impl)\s+"
    r"(?:<[^>]*>\s*)?([A-Za-z_]\w*)")
PY_DEF = re.compile(r"^(\s*)(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(")
PY_CLASS = re.compile(r"^(\s*)class\s+([A-Za-z_]\w*)")
END_DEF = {
    "ruby": re.compile(r"^(\s*)def\s+(?:self\.)?([\w?!=\[\]+\-*/<>%]+)"),
    "crystal": re.compile(r"^(\s*)(?:private\s+|protected\s+)?def\s+(?:self\.)?([\w?!=]+)"),
    "lua": re.compile(r"^(\s*)(?:local\s+)?function\s+([\w.:]+)"),
    "elixir": re.compile(r"^(\s*)defp?\s+([\w?!]+)"),
    "julia": re.compile(r"^(\s*)function\s+([\w.!]+)"),
}
END_CLASS = re.compile(r"^(\s*)(class|module|defmodule)\s+([\w:.]+)")
LEADING_ANNOT = re.compile(r"^(\s*)(?:(?:@[\w.]+(?:\([^()]*\))?|\[[A-Z][\w.]*(?:\([^()]*\))?\])\s+)+(?=\S)")


def _strip_strings_comments(line: str, state: dict) -> str:
    """Remove string/comment content from a C-like line, carrying block-comment state."""
    out, i, n = [], 0, len(line)
    while i < n:
        if state.get("block"):
            j = line.find("*/", i)
            if j < 0:
                return "".join(out)
            state["block"] = False
            i = j + 2
            continue
        ch = line[i]
        if ch == "/" and i + 1 < n and line[i + 1] == "/":
            break
        if ch == "#" and state.get("hash"):
            break
        if ch == "/" and i + 1 < n and line[i + 1] == "*":
            state["block"] = True
            i += 2
            continue
        if ch in "\"'`":
            q, i = ch, i + 1
            while i < n and line[i] != q:
                i += 2 if line[i] == "\\" else 1
            i += 1
            out.append('""')
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _brace_end(lines: list[str], start: int, hash_comments=False, max_scan=4000) -> int:
    state = {"hash": hash_comments}
    depth, opened = 0, False
    for i in range(start, min(len(lines), start + max_scan)):
        code = _strip_strings_comments(lines[i], state)
        if not opened:
            semi, brace = code.find(";"), code.find("{")
            arrow = code.find("=>")
            if brace < 0 and semi >= 0:
                return i            # declaration only / one-liner
            if brace < 0 and arrow >= 0 and not code.rstrip().endswith(("=>", "(")) and i > start:
                return i
        for ch in code:
            if ch == "{":
                depth += 1
                opened = True
            elif ch == "}":
                depth -= 1
                if opened and depth <= 0:
                    return i
        if not opened and i - start > 12:
            return start
    return min(len(lines) - 1, start + max_scan)


def _python_end(lines: list[str], start: int) -> int:
    ind = _indent(lines[start])
    i, bal = start, 0
    while i < len(lines):  # find end of signature
        bal += lines[i].count("(") + lines[i].count("[") - lines[i].count(")") - lines[i].count("]")
        if bal <= 0 and lines[i].rstrip().endswith(":"):
            break
        if bal <= 0 and i > start + 30:
            break
        i += 1
    last = i
    for j in range(i + 1, len(lines)):
        st = lines[j].strip()
        if not st or st.startswith("#"):
            continue
        if _indent(lines[j]) <= ind:
            break
        last = j
    return last


def _end_keyword_end(lines: list[str], start: int, lang: str) -> int:
    ind = _indent(lines[start])
    first = lines[start]
    if re.search(r"\bend\s*$", first) or re.search(r",\s*do:", first) or re.search(r"\)\s*=\s*[^=]", first):
        return start
    for j in range(start + 1, min(len(lines), start + 3000)):
        st = lines[j].strip()
        if not st:
            continue
        if _indent(lines[j]) <= ind and re.match(r"end\b", st):
            return j
        if _indent(lines[j]) < ind and lang != "lua":
            return j - 1
    return start


def _heuristic_extract(path: str, text: str, lang: str):
    fam = FAMILY.get(lang)
    if not fam:
        return None
    lines = text.split("\n")
    out: list[Symbol] = []
    if lang in ("vue", "svelte"):
        return _sfc_extract(path, text, lang)
    if fam == "indent":
        classes = []  # (indent, qualname, end)
        for i, ln in enumerate(lines):
            m = PY_CLASS.match(ln)
            if m:
                ind = len(m.group(1))
                while classes and (classes[-1][0] >= ind or classes[-1][2] < i):
                    classes.pop()
                end = _python_end(lines, i)
                qual = f"{classes[-1][1]}.{m.group(2)}" if classes else m.group(2)
                out.append(Symbol(file=path, name=m.group(2), qualname=qual, kind="class", start=i + 1,
                                  end=end + 1, deco_start=find_decorators(lines, i, i) + 1,
                                  container=classes[-1][1] if classes else "",
                                  signature=compact_signature(lines, i, end)))
                classes.append((ind, qual, end))
                continue
            m = PY_DEF.match(ln)
            if m:
                ind = len(m.group(1))
                while classes and (classes[-1][0] >= ind or classes[-1][2] < i):
                    classes.pop()
                # skip nested functions
                if any(s.callable and s.start - 1 < i <= s.end - 1 for s in out[-30:]):
                    continue
                end = _python_end(lines, i)
                cont = classes[-1][1] if classes and classes[-1][0] < ind else ""
                name = m.group(2)
                kind = "constructor" if name == "__init__" and cont else ("method" if cont else "function")
                deco = find_decorators(lines, i, i)
                out.append(Symbol(file=path, name=name, qualname=f"{cont}.{name}" if cont else name, kind=kind,
                                  callable=True, start=i + 1, end=end + 1, deco_start=deco + 1, container=cont,
                                  signature=compact_signature(lines, i, end),
                                  decorators=[one_line(lines[k], 120) for k in range(deco, i) if DECORATOR_LINE.match(lines[k].lstrip())][:6]))
        return out, [], set()
    if fam == "end":
        dre = END_DEF.get(lang)
        stack = []
        for i, ln in enumerate(lines):
            m = END_CLASS.match(ln)
            if m:
                end = _end_keyword_end(lines, i, lang)
                ind = len(m.group(1))
                while stack and (stack[-1][0] >= ind or stack[-1][2] < i):
                    stack.pop()
                qual = f"{stack[-1][1]}.{m.group(3)}" if stack else m.group(3)
                out.append(Symbol(file=path, name=m.group(3), qualname=qual, kind="class", start=i + 1, end=end + 1,
                                  deco_start=i + 1, container=stack[-1][1] if stack else "",
                                  signature=one_line(ln, 150)))
                stack.append((ind, qual, end))
                continue
            m = dre.match(ln) if dre else None
            if m:
                ind = len(m.group(1))
                while stack and (stack[-1][0] >= ind or stack[-1][2] < i):
                    stack.pop()
                if any(s.callable and s.start - 1 < i <= s.end - 1 for s in out[-30:]):
                    continue
                end = _end_keyword_end(lines, i, lang)
                cont = stack[-1][1] if stack else ""
                name = m.group(2)
                kind = "constructor" if name in ("initialize", "init") and cont else ("method" if cont else "function")
                out.append(Symbol(file=path, name=name, qualname=f"{cont}.{name}" if cont else name, kind=kind,
                                  callable=True, start=i + 1, end=end + 1, deco_start=i + 1, container=cont,
                                  signature=one_line(ln, 150)))
        return out, [], set()
    # brace family
    if lang in JS_LANGS:
        pats = HEUR_PATTERNS["js"]
    elif lang in JAVA_LIKE:
        pats = HEUR_PATTERNS["javalike"]
    else:
        pats = HEUR_PATTERNS.get(lang, HEUR_PATTERNS["javalike"])
    hash_comments = lang == "php"
    containers = []  # (qualname, end)
    covered_until = -1
    i = 0
    while i < len(lines):
        raw = lines[i]
        # drop leading annotations/attributes on the same line: "@Override public void x() {"
        ln = LEADING_ANNOT.sub(r"\1", raw)
        st = ln.strip()
        if not st or st.startswith(("//", "*", "/*", "#", "@", "[")) and not st.startswith("#["):
            i += 1
            continue
        while containers and containers[-1][1] < i:
            containers.pop()
        m = CLASS_RE.match(ln)
        if m and not st.endswith(";"):
            end = _brace_end(lines, i, hash_comments)
            name = m.group(3)
            cont = containers[-1][0] if containers else ""
            qual = f"{cont}.{name}" if cont else name
            out.append(Symbol(file=path, name=name, qualname=qual, kind=m.group(2), start=i + 1, end=end + 1,
                              deco_start=find_decorators(lines, i, i) + 1, container=cont,
                              signature=one_line(st, 150)))
            containers.append((qual, end))
            i += 1
            continue
        if i <= covered_until:
            i += 1
            continue
        if lang in JS_LANGS and re.search(r"(=>|\bfunction\b[^(]*\([^)]*\))\s*\{\s*$", ln):
            name, is_route, _ = synth_anon_name(lines, i)
            if is_route:
                end = _brace_end(lines, i, hash_comments)
                if end - i >= 2:
                    out.append(Symbol(file=path, name=name, qualname=name, kind="function", callable=True,
                                      anonymous=True, start=i + 1, end=end + 1, deco_start=i + 1,
                                      signature=one_line(st, 150)))
                    covered_until = end
                    i += 1
                    continue
        for rx, kind in pats:
            m = rx.match(ln)
            if not m:
                continue
            groups = [g for g in m.groups() if g]
            if lang == "go":
                recv, name = m.group(1), m.group(2)
            elif lang == "solidity":
                recv, name = None, m.group(2)
            else:
                recv, name = None, groups[-1] if groups else None
            if not name or name in KEYWORDS or name[0].isdigit():
                continue
            if lang in JAVA_LIKE and re.match(r"^\s*(return|throw|new|else|await|yield|case|goto)\b", ln):
                continue
            end = _brace_end(lines, i, hash_comments)
            cont = recv or (containers[-1][0] if containers else "")
            k = kind
            if cont and k == "function":
                k = "method"
            if cont and name == cont.split(".")[-1]:
                k = "constructor"
            deco = find_decorators(lines, i, i)
            out.append(Symbol(file=path, name=name, qualname=f"{cont}.{name}" if cont else name, kind=k,
                              callable=True, start=i + 1, end=end + 1, deco_start=deco + 1, container=cont,
                              signature=compact_signature(lines, i, end),
                              decorators=[one_line(lines[q], 120) for q in range(deco, i) if DECORATOR_LINE.match(lines[q].lstrip())][:6]))
            covered_until = end
            break
        i += 1
    return out, [], set()


def _sfc_extract(path: str, text: str, lang: str):
    """Vue/Svelte single-file components: analyze the <script> block as TS/JS."""
    m = re.search(r"<script([^>]*)>(.*?)</script>", text, re.S)
    if not m:
        return [], [], set()
    attrs, body = m.group(1), m.group(2)
    sub_lang = "typescript" if re.search(r"lang=[\"']ts", attrs) else "javascript"
    offset = text[: m.start(2)].count("\n")
    res = _ts_extract(path, body, sub_lang) if _HAS_TS else None
    if res is None:
        res = _heuristic_extract(path, body, sub_lang)
    if res is None:
        return [], [], set()
    syms, imps, names = res
    for s in syms:
        s.start += offset
        s.end += offset
        s.deco_start += offset
    return syms, imps, names


# ----------------------------------------------------------------------------------------
def extract(path: str, text: str, lang: str, use_tree_sitter: bool = True):
    """Return (symbols, imports, imported_names, extractor_name)."""
    res = None
    extractor = "none"
    if lang in ("vue", "svelte"):
        res = _sfc_extract(path, text, lang)
        extractor = "tree-sitter" if _HAS_TS and use_tree_sitter else "heuristic"
    else:
        if use_tree_sitter and _HAS_TS:
            res = _ts_extract(path, text, lang)
            extractor = "tree-sitter"
        if res is None:
            res = _heuristic_extract(path, text, lang)
            extractor = "heuristic" if res is not None else "none"
    if res is None:
        return [], [], set(), "none"
    syms, imports, names = res
    if not any(s.callable for s in syms) and extractor != "none":
        # tree-sitter found nothing callable: try heuristics before giving up
        if extractor == "tree-sitter" and FAMILY.get(lang):
            alt = _heuristic_extract(path, text, lang)
            if alt and any(s.callable for s in alt[0]):
                return alt[0], imports, names, "heuristic"
    return syms, imports, names, extractor


BOILERPLATE = {
    "toString", "hashCode", "equals", "canEqual", "__repr__", "__str__", "__eq__", "__hash__",
    "__ne__", "__lt__", "__le__", "__gt__", "__ge__", "__unicode__", "String", "GoString",
    "ToString", "GetHashCode", "Equals", "compareTo", "clone", "inspect", "to_s", "to_str",
    "toJSON", "__len__", "__iter__", "__contains__", "__getitem__", "__bool__",
}
FIELD_ONLY = re.compile(
    r"^(return\s+)?((this|self|me)\s*(\.|->)\s*|@|\$this->)?_?[A-Za-z_]\w*\s*;?$"
    r"|^((this|self|me)\s*(\.|->)\s*|@|\$this->)_?[A-Za-z_]\w*\s*=\s*\$?[A-Za-z_]\w*\s*;?$"
    r"|^(super|base)\s*\(.*\)\s*;?$")
ABSTRACT_BODY = re.compile(
    r"^(pass|\.\.\.|raise NotImplementedError.*|throw new (UnsupportedOperation|NotImplemented|NotSupported)\w*\(.*\);?"
    r"|unimplemented!\(.*\);?|todo!\(.*\);?|panic\(\"not implemented\"\))$")


def body_statements(lines: list[str], sym: Symbol) -> list[str]:
    out = []
    for i in range(sym.start, min(sym.end, len(lines))):  # lines after the declaration line
        st = lines[i].strip()
        if not st or st in ("{", "}", "};", "end", ")", "});", "})") or st.startswith(("//", "#", "*", "/*", '"""', "\'\'\'")):
            continue
        out.append(st.rstrip("{").strip())
    decl = lines[sym.start - 1] if 0 < sym.start <= len(lines) else ""
    if not out:  # one-liners: body on the declaration line
        m = (re.search(r"\{(.*)\}\s*;?\s*$", decl) or re.search(r"=>\s*(.+?);?\s*$", decl)
             or re.search(r":\s*(return\s+.+|pass|\.\.\.)\s*$", decl) or re.search(r"\)\s*(?::\s*[\w<>?,. ]+)?\s*=\s*(.+)$", decl))
        if m and m.group(1).strip():
            out.append(m.group(1).strip())
    return [s for s in out if s]


def is_declaration_only(lines: list[str], sym: Symbol, lang: str) -> bool:
    """Interface/abstract method signatures without a body (Java/C#/TS/Kotlin...)."""
    if lang in ("python", "ruby", "elixir", "lua", "crystal", "julia"):
        return False
    span = "\n".join(lines[sym.start - 1: sym.end])
    return "{" not in span and "=>" not in span and bool(re.search(r";\s*$", lines[sym.end - 1] if sym.end - 1 < len(lines) else ""))


def trivial_reason(lines: list[str], sym: Symbol, lang: str = "") -> str | None:
    """Rule-score obvious boilerplate so no tokens are spent on it. Declaration-only methods are
    NOT trivial: for Spring Data / MyBatis / Feign / Retrofit the signature *is* the logic."""
    if not sym.callable or sym.anonymous or is_declaration_only(lines, sym, lang):
        return None
    body = body_statements(lines, sym)
    if not body or all(ABSTRACT_BODY.match(b) for b in body):
        return "field-init" if sym.kind == "constructor" else "empty"
    if sym.name in BOILERPLATE and len(body) <= 8:
        return "boilerplate"
    if all(FIELD_ONLY.match(b) for b in body):
        if sym.kind == "constructor" and len(body) <= 10:
            return "field-init"
        if len(body) <= 2:
            return "accessor"
    return None


def assign_ids(symbols: list[Symbol]) -> None:
    """Stable short IDs from path+qualname (+occurrence), so re-runs and incremental runs reuse
    cached LLM answers for unchanged files."""
    seen_keys: dict = {}
    for s in symbols:
        base = f"{s.file}::{s.qualname}"
        n = seen_keys.get(base, 0) + 1
        seen_keys[base] = n
        s.key = base if n == 1 else f"{base}#{n}"
    used: set = set()
    for s in sorted(symbols, key=lambda x: x.key):
        h = bytes.fromhex(sha1(s.key))
        for length in (5, 6, 7, 8, 10):
            cand = "S" + b36(h, length)
            if cand not in used:
                break
        used.add(cand)
        s.id = cand
