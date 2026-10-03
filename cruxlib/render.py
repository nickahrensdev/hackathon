"""Text renderers that turn scan data into compact, token-efficient prompt material."""
from __future__ import annotations

import re
from collections import defaultdict

from .util import est_tokens, one_line

KIND_LETTER = {"function": "f", "method": "m", "constructor": "c", "class": "C", "interface": "I",
               "struct": "S", "enum": "E", "trait": "T", "impl": "M", "module": "N", "namespace": "N",
               "object": "O", "record": "R", "protocol": "P", "extension": "X"}
MODIFIERS = re.compile(r"\b(public|private|protected|internal|static|final|override|readonly|export|default|"
                       r"virtual|sealed|abstract|inline|suspend|open|synchronized|native|partial|extern|unsafe|"
                       r"pub(\([^)]*\))?|def|func|fn|function|fun|async)\s+")
IMPORT_LINE = re.compile(
    r"^\s*(import\b|from\s+[\w.]+\s+import\b|using\s+(static\s+)?[\w.]+\s*;|#\s*include\b|package\s+[\w.]+\s*;?\s*$|"
    r"(const|let|var)\s+[\w{}\s,]+=\s*require\(|require(_relative)?\s*\(?['\"]|use\s+[\w:\\{}, ]+;|"
    r"@import\b|namespace\s+[\w.\\]+\s*;)")
LICENSE_HINT = re.compile(r"(copyright|licen[cs]e|spdx|all rights reserved)", re.I)


def short_sig(sym, limit: int = 100) -> str:
    sig = sym.signature or sym.name
    sig = re.sub(r"@\w+(\([^)]*\))?\s*", "", sig)          # inline annotations
    sig = re.sub(r"\[\w+(\([^)]*\))?\]\s*", "", sig)       # C# attributes
    sig = MODIFIERS.sub("", sig)
    sig = re.sub(r"\s+", " ", sig).strip()
    return sig if len(sig) <= limit else sig[: limit - 1] + "…"


def sym_flags(sym, entry_ids=None) -> str:
    parts = []
    if sym.fan_in:
        parts.append(f"in:{sym.fan_in}")
    if sym.reach:
        parts.append(f"ep:{len(sym.reach)}")
    if sym.tags:
        parts.append("[" + " ".join(sym.tags) + "]")
    eps = entry_ids if entry_ids is not None else sym.entries
    if eps:
        parts.append(" ".join(eps[:3]))
    return " ".join(parts)


# ----------------------------------------------------------------------------------------
def file_priority(graph, path: str) -> float:
    syms = [s for s in graph.syms_by_file.get(path, []) if s.callable]
    if not syms:
        return 0.0
    ps = sorted((s.prio for s in syms), reverse=True)
    return ps[0] + 0.3 * sum(ps[1:3]) + (1.5 if any(s.entries for s in syms) else 0.0)


def render_overview(scan, max_tree_lines: int = 80) -> str:
    sm = scan.summary()
    ctx = scan.context
    out = [f"REPOSITORY: {scan.repo.rstrip('/').split('/')[-1]}"]
    git = ctx.get("git") or {}
    if git.get("remote"):
        out.append(f"remote: {git['remote']}")
    langs = ", ".join(f"{k} {v}" for k, v in list(sm["languages_by_lines"].items())[:8])
    out.append(f"size: {sm['files']} source files, {sm['lines']} lines ({langs} lines)")
    out.append(f"symbols: {sm['callables']} functions/methods ({sm['trivial_callables']} trivial), "
               f"{sm['entry_candidates']} entry candidates")
    tree = ctx.get("tree", "").split("\n")
    out.append("\nDIRECTORY TREE (source files only):")
    out.extend(tree[:max_tree_lines])
    if len(tree) > max_tree_lines:
        out.append(f"... ({len(tree) - max_tree_lines} more directories)")
    if ctx.get("manifests"):
        out.append("\nMANIFESTS:")
        for m in ctx["manifests"]:
            out.append(f"--- {m['path']}\n{m['content']}")
    if ctx.get("readme"):
        out.append("\nREADME (excerpt):\n" + ctx["readme"])
    return "\n".join(out)


def render_entries(graph, entries: list[dict], limit: int = 400) -> str:
    out = ["ENTRY CANDIDATES (static detection; may include false positives):"]
    for e in entries[:limit]:
        s = graph.by_id.get(e["symbol"]) if e.get("symbol") else None
        target = f"{s.id} {s.qualname}" if s else "<top-level code>"
        trig = f" {e['trigger']}" if e.get("trigger") else ""
        out.append(f"{e['id']} [{e['kind']}]{trig} -> {target} @ {e['file']}:{e['line']}  | {one_line(e['evidence'], 110)}")
    if len(entries) > limit:
        kinds = defaultdict(int)
        for e in entries[limit:]:
            kinds[e["kind"]] += 1
        out.append(f"... {len(entries) - limit} more candidates not shown: {dict(kinds)}")
    return "\n".join(out)


def render_file_skeleton(graph, path: str) -> str:
    f = graph.files[path]
    syms = sorted(graph.syms_by_file.get(path, []), key=lambda s: (s.deco_start, -s.end))
    churn = f" churn:{f.churn}/{f.fix_churn}" if f.churn else ""
    head = f"## {path} ({f.lang}, {f.lines}L){churn}"
    if f.extractor == "none":
        head += " [functions not extracted]"
    lines = [head]
    trivial = []
    for s in syms:
        depth = s.qualname.count(".") if s.container else 0
        ind = "  " * (1 + min(depth, 3))
        if s.callable and s.trivial:
            trivial.append(s.name)
            continue
        if not s.callable:
            lines.append(f"{ind}{s.id} {KIND_LETTER.get(s.kind, s.kind[:1])} {short_sig(s, 110)} L{s.start}-{s.end}")
            continue
        flags = sym_flags(s)
        lines.append(f"{ind}{s.id} {KIND_LETTER.get(s.kind, 'f')} {short_sig(s)} L{s.start}-{s.end}"
                     + (f"  {flags}" if flags else ""))
    if trivial:
        names = ", ".join(trivial[:8]) + ("…" if len(trivial) > 8 else "")
        lines.append(f"    (+{len(trivial)} trivial: {names})")
    return "\n".join(lines)


def render_skeleton(graph, budget_tokens: int) -> tuple[str, list[str], list[str]]:
    """Files by priority until the budget is used; the rest summarized per directory.
    Returns (text, included_paths, omitted_paths)."""
    paths = sorted(graph.files, key=lambda p: (-file_priority(graph, p), p))
    parts, used, included, omitted = [], 0, [], []
    for p in paths:
        block = render_file_skeleton(graph, p)
        t = est_tokens(block)
        if used + t > budget_tokens and included:
            omitted.append(p)
            continue
        parts.append(block)
        used += t
        included.append(p)
    # keep original directory order for readability
    order = {p: i for i, p in enumerate(sorted(included))}
    parts = [b for _, b in sorted(zip(included, parts), key=lambda x: order[x[0]])]
    text = "SKELETON (S-ids; files ordered by path; highest-priority files included first under a token budget):\n" + "\n".join(parts)
    if omitted:
        by_dir = defaultdict(list)
        for p in omitted:
            by_dir[p.rsplit("/", 1)[0] if "/" in p else "."].append(p.rsplit("/", 1)[-1])
        text += f"\n\nOMITTED FOR BUDGET ({len(omitted)} lower-priority files):"
        for d in sorted(by_dir):
            names = by_dir[d]
            text += f"\n  {d}/: {', '.join(names[:12])}{' …' if len(names) > 12 else ''} ({len(names)} files)"
    return text, included, omitted


# ----------------------------------------------------------------------------------------
def _drop_license_header(lines: list[str]) -> int:
    """Return index of the first line after a leading license comment block (0 if none)."""
    i, n = 0, len(lines)
    while i < n and not lines[i].strip():
        i += 1
    if i >= n:
        return 0
    st = lines[i].lstrip()
    end = None
    if st.startswith("/*"):
        for j in range(i, min(n, i + 80)):
            if "*/" in lines[j]:
                end = j
                break
    elif st.startswith(("//", "#")) and not st.startswith(("#!", "#include", "#import", "#[", "#define")):
        j = i
        while j < n and lines[j].lstrip().startswith(("//", "#")) and not lines[j].lstrip().startswith("#!"):
            j += 1
        end = j - 1
    if end is None or end - i < 3:
        return 0
    if not LICENSE_HINT.search("\n".join(lines[i:end + 1])):
        return 0
    return end + 1


def render_source(text: str, syms, mark_ids=None, compact: bool = True, line_numbers: bool = False,
                  line_range: tuple[int, int] | None = None, max_line_len: int = 220) -> str:
    """Source with «S-id» markers on each function's declaration line.
    compact: drop license header, collapse import runs and blank runs, cut very long lines."""
    lines = text.split("\n")
    markers = {}
    for s in syms:
        if s.callable and (mark_ids is None or s.id in mark_ids):
            markers.setdefault(s.start, []).append(s.id)
    lo, hi = (1, len(lines)) if not line_range else line_range
    start_idx = max(lo - 1, _drop_license_header(lines) if compact and lo == 1 else lo - 1)
    out, blank, imports = [], False, []
    in_import_block = False

    def flush_imports():
        if not imports:
            return
        if len(imports) <= 3:
            out.extend(imports)
        else:
            out.extend(imports[:2])
            out.append(f"… (+{len(imports) - 2} import lines)")
        imports.clear()

    for idx in range(start_idx, min(hi, len(lines))):
        ln = lines[idx]
        lineno = idx + 1
        if len(ln) > max_line_len:
            ln = ln[:max_line_len] + " …"
        if compact and lineno not in markers:
            st = ln.strip()
            if in_import_block:
                imports.append(ln)
                if re.search(r"(\}|\))\s*(from\s+['\"].*['\"])?\s*;?\s*$", st):
                    in_import_block = False
                continue
            if IMPORT_LINE.match(ln):
                imports.append(ln)
                if re.match(r"^\s*import\s*(type\s*)?\{[^}]*$", ln) or re.match(r"^\s*(import|from)\s.*\($", ln) \
                        or re.match(r"^\s*import\s*\($", ln):
                    in_import_block = True
                continue
            if not st:
                if imports:
                    continue
                if blank:
                    continue
                blank = True
                out.append("")
                continue
        flush_imports()
        blank = False
        prefix = f"{lineno:>5} " if line_numbers else ""
        if lineno in markers:
            ln = "«" + "»«".join(markers[lineno]) + "» " + ln
        out.append(prefix + ln)
    flush_imports()
    return "\n".join(out).strip("\n")


def render_symbol_code(text_lines: list[str], sym, max_lines: int = 160) -> str:
    lo, hi = max(1, sym.deco_start), min(len(text_lines), sym.end)
    body = text_lines[lo - 1: hi]
    if len(body) > max_lines:
        keep_head, keep_tail = int(max_lines * 0.7), int(max_lines * 0.25)
        body = body[:keep_head] + [f"    … ({len(body) - keep_head - keep_tail} lines omitted) …"] + body[-keep_tail:]
    decl_off = sym.start - lo
    if 0 <= decl_off < len(body):
        body = list(body)
        body[decl_off] = f"«{sym.id}» " + body[decl_off]
    return "\n".join(l if len(l) <= 220 else l[:220] + " …" for l in body)
