"""Build the LLM requests for each phase from scan data, under token budgets."""
from __future__ import annotations

import json
import os
from collections import defaultdict

from . import render
from .llm import Request
from .util import est_tokens, one_line, sha1

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_prompts() -> dict:
    out = {}
    for name in ("rubric", "map", "trace", "score", "calibrate"):
        with open(os.path.join(HERE, "prompts", f"{name}.md"), encoding="utf-8") as f:
            out[name] = f.read().strip()
    for name in ("score", "calibrate"):
        out[name] = out[name].replace("{{RUBRIC}}", out["rubric"])
    return out


def load_schemas() -> dict:
    out = {}
    for name in ("map", "trace", "score", "calibrate"):
        with open(os.path.join(HERE, "schemas", f"{name}.json"), encoding="utf-8") as f:
            out[name] = json.load(f)
    return out


# ----------------------------------------------------------------------------------------
# MAP
def map_request(scan, cfg, prompts, schemas) -> Request:
    g = scan.graph
    overview = render.render_overview(scan)
    entries = render.render_entries(g, g.entries)
    full = scan.source_tokens() <= cfg.full_source_threshold
    fixed = est_tokens(overview) + est_tokens(entries) + 1500
    parts = [overview, entries]
    if full:
        sk, _, _ = render.render_skeleton(g, 10 ** 9)
        parts.append(sk)
        src = ["SOURCE (complete; «S-id» marks each function):"]
        for p in sorted(scan.texts):
            src.append(f"=== {p}\n" + render.render_source(scan.texts[p], g.syms_by_file.get(p, [])))
        parts.append("\n".join(src))
    else:
        sk, inc, om = render.render_skeleton(g, max(4000, cfg.map_budget - fixed))
        parts.append(sk)
    parts.append("Return the JSON object now.")
    top = sorted(scan.callables(), key=lambda s: -s.prio)
    meta = {
        "full_source": full,
        "entries": [{"id": e["id"], "symbol": e["symbol"] or "", "trigger": e.get("trigger", ""),
                     "reach": [sid for sid, _ in sorted(((sid, d) for sid, d in
                               (g.reach_from([e["symbol"]], 3).items() if e["symbol"] else [])
                               if sid in g.by_id and g.by_id[sid].callable and sid != e["symbol"]),
                               key=lambda x: (x[1], -g.by_id[x[0]].prio))][:6]}
                    for e in sorted(g.entries, key=lambda e: -(g.by_id[e["symbol"]].prio if e["symbol"] else 0))[:40]],
        "top_symbols": [s.id for s in top[:10]],
    }
    r = Request(id="", phase="map", model=cfg.model_map, system=[prompts["map"]],
                user="\n\n".join(parts), schema=schemas["map"], max_tokens=32000,
                effort=cfg.effort_map, meta=meta)
    r.id = f"map-{r.key()[:6]}"
    return r


# ----------------------------------------------------------------------------------------
# shared context for trace/score/calibrate
def flows_brief(g, flows: list[dict], entries_by_id: dict, with_steps: bool = True, max_steps: int = 12) -> str:
    out = []
    for f in flows:
        out.append(f"{f['id']} [criticality {f.get('criticality', '?')}] {f['name']}"
                   + (f" ({f.get('confidence')} confidence)" if f.get("confidence") else ""))
        eps = []
        for eid in f.get("entry_points", [])[:6]:
            e = entries_by_id.get(eid)
            if e:
                s = g.by_id.get(e.get("symbol") or "")
                eps.append(f"{eid} {e.get('trigger') or e['kind']}" + (f" -> {s.id} {s.qualname}" if s else ""))
        if eps:
            out.append("   entry: " + "; ".join(eps))
        if with_steps and f.get("steps"):
            names = []
            for st in f["steps"][:max_steps]:
                sid = st["symbol"] if isinstance(st, dict) else st
                s = g.by_id.get(sid)
                if s:
                    names.append(f"{s.id} {s.qualname}")
            if names:
                out.append("   path: " + " -> ".join(names))
        if f.get("business_impact"):
            out.append("   impact: " + one_line(f["business_impact"], 200))
    return "\n".join(out)


def cruxes_brief(g, cruxes: list[dict]) -> str:
    out = []
    for c in cruxes:
        s = g.by_id.get(c["symbol"])
        if not s:
            continue
        out.append(f"{s.id} {s.qualname} ({s.file}) [{c.get('kind', 'other')}] flows {','.join(c.get('flows', []))}: "
                   + one_line(c.get("why", ""), 160))
    return "\n".join(out)


def score_context(scan, summary: str, repo_kind: str, flows, cruxes, entries_by_id, eps_annot: dict) -> str:
    g = scan.graph
    out = [f"REPOSITORY: {scan.repo.rstrip('/').split('/')[-1]} ({repo_kind})", f"SUMMARY: {summary}", "",
           "CRITICAL FLOWS (most critical first):", flows_brief(g, flows, entries_by_id), "",
           "CRUX CANDIDATES (expected to score 9-10 if the code confirms them):", cruxes_brief(g, cruxes) or "(none)"]
    top_eps = sorted(eps_annot.values(), key=lambda e: -(e.get("criticality") or 0))[:40]
    if top_eps:
        out += ["", "KEY ENTRY POINTS:"]
        for e in top_eps:
            s = g.by_id.get(e.get("symbol") or "")
            out.append(f"{e['id']} {e.get('trigger') or e.get('kind')} [criticality {e.get('criticality', '?')}]"
                       + (f" -> {s.id} {s.qualname}" if s else "") + (f" flows {','.join(e.get('flows', []))}" if e.get("flows") else ""))
    return "\n".join(out)


# ----------------------------------------------------------------------------------------
# TRACE
def _module_code(scan, path: str, max_lines: int = 80) -> str:
    g = scan.graph
    lines = scan.texts[path].split("\n")
    covered = set()
    for s in g.syms_by_file.get(path, []):
        if s.callable:
            covered.update(range(s.deco_start, s.end + 1))
    keep = [f"{i + 1:>5} {l}" for i, l in enumerate(lines) if (i + 1) not in covered and l.strip()]
    if len(keep) > max_lines:
        keep = keep[:max_lines] + [f"    … ({len(keep) - max_lines} more top-level lines)"]
    return "\n".join(keep)


def trace_requests(scan, cfg, flows, summary, entries_by_id, prompts, schemas) -> list[Request]:
    g = scan.graph
    reqs = []
    all_flows = "\n".join(f"{f['id']} {f['name']}" for f in flows)
    for f in flows[: cfg.max_trace_flows]:
        seeds, seed_set = [], set()
        for eid in f.get("entry_points", []):
            e = entries_by_id.get(eid)
            if e:
                sid = e.get("symbol") or g.module_ids.get(e["file"])
                if sid and sid not in seed_set:
                    seeds.append(sid)
                    seed_set.add(sid)
        for sid in f.get("steps", []):
            if sid in g.by_id and sid not in seed_set:
                seeds.append(sid)
                seed_set.add(sid)
        if not seeds:
            continue
        dist = g.reach_from(seeds, max_depth=3)
        cands = [sid for sid in dist if sid in g.by_id and g.by_id[sid].callable and sid not in seed_set]
        cands.sort(key=lambda sid: (dist[sid], -g.by_id[sid].prio))
        order = seeds + cands
        budget = cfg.trace_budget
        used, shown, blocks = 0, [], defaultdict(list)
        for sid in order:
            if sid.startswith("M"):
                path = sid[1:]
                code = _module_code(scan, path)
                key = path
                txt = f"(top-level code of {path})\n{code}"
            else:
                s = g.by_id[sid]
                lines = scan.texts[s.file].split("\n")
                txt = render.render_symbol_code(lines, s, max_lines=140 if sid in seed_set else 90)
                key = s.file
            t = est_tokens(txt)
            if used + t > budget and shown:
                continue
            used += t
            blocks[key].append((0 if sid.startswith("M") else g.by_id[sid].start, txt))
            shown.append(sid)
        not_shown = [sid for sid in order if sid not in shown and not sid.startswith("M")][:80]
        code_txt = []
        for path in sorted(blocks):
            code_txt.append(f"### {path}")
            code_txt.extend(t for _, t in sorted(blocks[path], key=lambda x: x[0]))
        ns_txt = "\n".join(f"{sid} {g.by_id[sid].qualname} ({g.by_id[sid].file})" for sid in not_shown) or "(none)"
        flow_txt = flows_brief(g, [f], entries_by_id)
        extra = []
        for k in ("failure_modes", "observable_outcomes", "preconditions"):
            if f.get(k):
                extra.append(f"   {k}: " + "; ".join(one_line(x, 120) for x in f[k][:5]))
        user = "\n".join([
            f"REPO SUMMARY: {summary}", "", "ALL FLOWS:", all_flows, "",
            "THE FLOW UNDER STUDY:", flow_txt, *extra, "",
            "CODE:", *code_txt, "", "NOT SHOWN (related symbols):", ns_txt, "", "Return the JSON object now."])
        r = Request(id="", phase="trace", model=cfg.model_trace, system=[prompts["trace"]],
                    user=user, schema=schemas["trace"], max_tokens=16000, effort=cfg.effort_trace,
                    meta={"flow": f["id"], "shown": [s for s in shown if not s.startswith("M")]})
        r.id = f"trace-{f['id']}-{r.key()[:6]}"
        reqs.append(r)
    return reqs


# ----------------------------------------------------------------------------------------
# SCORE
def file_fan_in(g) -> dict:
    files = defaultdict(set)
    for src, outs in g.edges.items():
        sfile = src[1:] if src.startswith("M") else g.by_id[src].file
        for dst in outs:
            d = g.by_id.get(dst)
            if d and d.file != sfile:
                files[d.file].add(sfile)
    return {k: len(v) for k, v in files.items()}


def _method_row(s, flow_reach: dict, on_path: dict, crux_ids: set, entries_by_id: dict) -> str:
    parts = [f"{s.id} {s.qualname} L{s.start}-{s.end}"]
    if s.fan_in:
        parts.append(f"in:{s.fan_in}")
    fr = flow_reach.get(s.id)
    if fr:
        parts.append("reach " + ",".join(f"{k}@{v}" for k, v in sorted(fr.items(), key=lambda x: (x[1], x[0]))[:4]))
    if on_path.get(s.id):
        parts.append("on path of " + ",".join(sorted(on_path[s.id])))
    if s.tags:
        parts.append("[" + " ".join(s.tags) + "]")
    for eid in s.entries[:2]:
        e = entries_by_id.get(eid)
        if e:
            parts.append(f"{eid} {e.get('trigger') or e['kind']}")
    if s.id in crux_ids:
        parts.append("CRUX CANDIDATE")
    return " · ".join(parts)


def _static_estimate(s, pct: dict) -> int:
    if s.trivial:
        return 1 if s.trivial == "empty" else 2
    p = pct.get(s.id, 0.5)
    return 8 if p >= 0.98 else 7 if p >= 0.9 else 6 if p >= 0.75 else 5 if p >= 0.5 else 4 if p >= 0.2 else 3


def static_percentiles(scan) -> dict:
    cal = sorted((s for s in scan.callables() if not s.trivial), key=lambda s: s.prio)
    n = max(1, len(cal) - 1)
    return {s.id: i / n for i, s in enumerate(cal)}


def _file_units(scan, path, cfg, flow_reach, on_path, crux_ids, entries_by_id, ffi, only_ids=None):
    """Render one file (or several chunks of a large one) for scoring. Returns list of (text, meta)."""
    g = scan.graph
    f = g.files[path]
    text = scan.texts[path]
    syms = g.syms_by_file.get(path, [])
    callables = sorted((s for s in syms if s.callable), key=lambda s: s.start)
    if only_ids is not None:
        callables = [s for s in callables if s.id in only_ids]
        if not callables:
            return []
    to_score = [s for s in callables if not s.trivial or only_ids is not None]
    pre = [s for s in callables if s.trivial and only_ids is None]
    discover = f.extractor == "none" and only_ids is None
    file_reach = defaultdict(lambda: 99)
    for s in callables:
        for fl, d in flow_reach.get(s.id, {}).items():
            file_reach[fl] = min(file_reach[fl], d)
    hints = [f"used from {ffi.get(path, 0)} other files"]
    if f.churn:
        hints.append(f"churn {f.churn} commits ({f.fix_churn} fixes)")
    if file_reach:
        hints.append("flows reaching: " + ", ".join(f"{k}(d{v})" for k, v in sorted(file_reach.items(), key=lambda x: (x[1], x[0]))[:6]))

    def header(part=""):
        h = f"=== FILE {path} ({f.lang}, {f.lines} lines){part}"
        if discover:
            h += " · DISCOVER: functions were not extracted; list them in `extra`"
        elif not callables:
            h += " · no functions: give the file score and summary only (methods: [])"
        return h

    pre_txt = ""
    if pre:
        pre_txt = "PRE-SCORED (rule; include only to override): " + "; ".join(
            f"{s.id} {s.name}={1 if s.trivial == 'empty' else 2} {s.trivial}" for s in pre[:60])

    def meta_for(ms):
        return {"path": path, "methods": [(s.id, None, sorted(flow_reach.get(s.id, {}))[:2]) for s in ms]}

    rows = [_method_row(s, flow_reach, on_path, crux_ids, entries_by_id) for s in to_score]
    max_lines = 400 if not callables else None
    if only_ids is not None:   # retry round: send just the methods that were missed
        lines_ = text.split("\n")
        src = "\n…\n".join(render.render_symbol_code(lines_, s, max_lines=300) for s in to_score)
    else:
        src = render.render_source(text, syms, line_numbers=discover,
                                   line_range=(1, max_lines) if max_lines else None)
    block = "\n".join([header(), "hints: " + " · ".join(hints),
                       f"METHODS TO SCORE ({len(to_score)}):" if to_score else "METHODS TO SCORE: none",
                       *rows, *([pre_txt] if pre_txt else []), "SOURCE:", src])
    if est_tokens(block) <= cfg.score_chunk_tokens or len(to_score) <= 1:
        if est_tokens(block) > cfg.score_chunk_tokens and to_score:
            # one enormous function: keep head/tail of its body
            s = to_score[0]
            lines = text.split("\n")
            src = render.render_symbol_code(lines, s, max_lines=int(cfg.score_chunk_tokens * 3.5 / 60))
            block = "\n".join([header(" (function body truncated)"), "hints: " + " · ".join(hints),
                               "METHODS TO SCORE (1):", rows[0], "SOURCE:", src])
        return [(block, meta_for(to_score))]
    # split large files into chunks of consecutive methods, each with the file header for context
    first = callables[0].deco_start if callables else 1
    head_src = render.render_source(text, [], line_range=(1, min(first - 1, 60))) if first > 1 else ""
    units, cur, cur_tok = [], [], 0
    budget = int(cfg.score_chunk_tokens * 0.85)
    chunks = []
    for s in to_score:
        body = render.render_symbol_code(text.split("\n"), s, max_lines=400)
        t = est_tokens(body) + 40
        if cur and cur_tok + t > budget:
            chunks.append(cur)
            cur, cur_tok = [], 0
        cur.append(s)
        cur_tok += t
    if cur:
        chunks.append(cur)
    for i, ch in enumerate(chunks, 1):
        lo, hi = min(s.deco_start for s in ch), max(s.end for s in ch)
        body = render.render_source(text, syms, line_range=(lo, hi))
        if est_tokens(body) > budget * 1.3:  # gaps between methods were large: render methods only
            body = "\n…\n".join(render.render_symbol_code(text.split("\n"), s, max_lines=300) for s in ch)
        ch_rows = [_method_row(s, flow_reach, on_path, crux_ids, entries_by_id) for s in ch]
        parts = [header(f" · part {i}/{len(chunks)}"), "hints: " + " · ".join(hints),
                 f"METHODS TO SCORE ({len(ch)}):", *ch_rows]
        if i == 1 and pre_txt:
            parts.append(pre_txt)
        parts += ["FILE HEADER:", head_src or "(none)", f"SOURCE (lines {lo}-{hi}):", body]
        units.append(("\n".join(parts), meta_for(ch)))
    return units


def score_requests(scan, cfg, context_text, flow_reach, on_path, crux_ids, entries_by_id, prompts, schemas,
                   only_ids=None, id_prefix="score") -> list[Request]:
    g = scan.graph
    ffi = file_fan_in(g)
    pct = static_percentiles(scan)
    units = []
    for path in sorted(g.files):
        units.extend(_file_units(scan, path, cfg, flow_reach, on_path, crux_ids, entries_by_id, ffi, only_ids))
    system = [prompts["score"], context_text]
    reqs, cur, cur_meta, cur_tok = [], [], [], 0

    def flush():
        if not cur:
            return
        n = len(reqs) + 1
        meta = {"files": []}
        for m in cur_meta:
            meta["files"].append({"path": m["path"], "methods": [(mid, _static_estimate(g.by_id[mid], pct), fl)
                                                                  for mid, _, fl in m["methods"]]})
        expected = [mid for m in cur_meta for mid, _, _ in m["methods"]]
        n_methods = len(expected)
        r = Request(id="", phase="score", model=cfg.model_score, system=system,
                    user="\n\n".join(cur) + "\n\nReturn the JSON object now: one entry in `files` per FILE above.",
                    schema=schemas["score"], max_tokens=min(64000, 2500 + 160 * n_methods),
                    effort=cfg.effort_score, meta={**meta, "expected": expected})
        r.id = f"{id_prefix}-{n:04d}-{r.key()[:6]}"   # content-tagged: a stale answer can never match
        reqs.append(r)
        cur.clear()
        cur_meta.clear()

    for text, meta in units:
        t = est_tokens(text)
        # Pack boundaries are content-defined (path hash) as well as size-defined, so editing one
        # file only changes the pack it lives in and cached answers for other packs stay valid.
        boundary = int(sha1(meta["path"])[:4], 16) % 6 == 0 and cur_tok >= cfg.score_chunk_tokens * 0.5
        if cur and (cur_tok + t > cfg.score_chunk_tokens or boundary):
            flush()
            cur_tok = 0
        cur.append(text)
        cur_meta.append(meta)
        cur_tok += t
    flush()
    return reqs


# ----------------------------------------------------------------------------------------
# CALIBRATE
def calibrate_request(scan, cfg, summary, flows, cruxes, methods: dict, entries_by_id, flow_reach, on_path,
                      prompts, schemas) -> Request:
    g = scan.graph
    hist = defaultdict(int)
    for m in methods.values():
        hist[m["score"]] += 1
    high = sorted((m for m in methods.values() if m["score"] >= 7), key=lambda m: (-m["score"], -g.by_id[m["id"]].prio))
    high = high[: cfg.calibrate_rows]
    high_ids = {m["id"] for m in high}
    crit = {f["id"]: f.get("criticality", 5) for f in flows}
    anomalies = []
    for m in methods.values():
        if m["id"] in high_ids:
            continue
        s = g.by_id[m["id"]]
        fr = flow_reach.get(s.id, {})
        near_critical = any(d <= 1 and crit.get(fl, 0) >= 8 for fl, d in fr.items()) or on_path.get(s.id)
        if m["score"] <= 4 and near_critical and not s.trivial:
            anomalies.append(m)
    for m in high:
        s = g.by_id[m["id"]]
        if m["score"] >= 8 and not m.get("flows") and not flow_reach.get(s.id) and s.fan_in == 0 and not s.entries:
            anomalies.append(m)
    anomalies = anomalies[:150]

    def row(m):
        s = g.by_id[m["id"]]
        shape = f"{s.end - s.start + 1}L" + (" " + " ".join(t for t in s.tags if t in ("decl-only", "db", "tx", "auth")) if s.tags else "")
        return (f"{s.id} | {s.file}:{s.start} | {s.qualname} | {shape} | {m['score']} | {m.get('role', '')} | "
                f"{','.join(m.get('flows', [])) or '-'} | {one_line(m.get('why', ''), 140)}")

    dist = ", ".join(f"{k}: {hist[k]}" for k in range(10, 0, -1))
    user = "\n".join([
        f"SUMMARY: {summary}", "", "CRITICAL FLOWS:", flows_brief(g, flows, entries_by_id, with_steps=True, max_steps=8), "",
        "CURRENT CRUX CANDIDATES:", cruxes_brief(g, cruxes) or "(none)", "",
        f"SCORE DISTRIBUTION ({len(methods)} methods): {dist}", "",
        "HIGH-SCORED METHODS (id | location | name | size+tags | score | role | flows | why):", *[row(m) for m in high], "",
        "ANOMALIES FLAGGED BY STATIC ANALYSIS (on/near a critical path but scored low, or scored high with no path):",
        *([row(m) for m in anomalies] or ["(none)"]), "", "Return the JSON object now."])
    r = Request(id="", phase="calibrate", model=cfg.model_calibrate, system=[prompts["calibrate"]],
                user=user, schema=schemas["calibrate"], max_tokens=32000, effort=cfg.effort_calibrate,
                meta={"rows": [m["id"] for m in high[:10]]})
    r.id = f"calibrate-{r.key()[:6]}"
    return r
