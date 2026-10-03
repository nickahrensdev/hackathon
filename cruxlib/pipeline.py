"""Orchestration: map -> trace -> score -> calibrate -> assemble, with caching and fallbacks."""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import threading
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass

from . import packets
from .llm import PendingRequests, Request, cost_usd, min_cacheable, price_for
from .util import clamp_int, est_tokens, log, one_line, read_json, sha1, warn, write_json

VERSION = "1.0.0"
ROLES = {"entry", "orchestration", "business_rule", "validation", "security", "persistence", "integration",
         "transformation", "error_handling", "configuration", "ui", "utility", "accessor", "dead_code", "other"}
CRUX_KINDS = {"auth_gate", "transaction_boundary", "core_domain_rule", "state_transition", "persistence",
              "integration", "dispatch", "validation", "configuration", "other"}
ENTRY_KINDS = {"http", "graphql", "grpc", "websocket", "cli", "main", "consumer", "job", "scheduled", "serverless",
               "ui-route", "ui-bootstrap", "bootstrap", "handler", "middleware", "public-api", "webhook", "other"}


@dataclass
class Config:
    backend: str = "anthropic"
    model_map: str = "claude-opus-5-5"
    model_trace: str = "claude-sonnet-5-5"
    model_score: str = "claude-haiku-4-5-20251001"
    model_calibrate: str = "claude-opus-5-5"
    effort_map: str | None = "high"
    effort_trace: str | None = "medium"
    effort_score: str | None = "low"
    effort_calibrate: str | None = "medium"
    map_budget: int = 60000              # tokens of repo brief sent to the map call
    full_source_threshold: int = 45000   # below this, map sees all source and trace is skipped
    trace: str = "auto"                  # auto: skip when the map already saw all source | always | never
    max_trace_flows: int = 8
    trace_budget: int = 24000            # tokens of code per traced flow
    score_chunk_tokens: int = 12000      # tokens of file material per scoring request
    calibrate: bool = True
    calibrate_rows: int = 700
    concurrency: int | None = None
    batch: bool = False
    max_cost: float | None = None
    reuse_context: bool = False          # keep previous map/trace (incremental re-scoring)


class Pipeline:
    def __init__(self, scan, cfg: Config, backend, work_dir: str, use_cache: bool = True, dump_prompts: bool = False):
        self.scan, self.g, self.cfg, self.backend = scan, scan.graph, cfg, backend
        self.work = work_dir
        self.cache_dir = os.path.join(work_dir, "cache")
        self.use_cache = use_cache
        os.makedirs(self.cache_dir, exist_ok=True)
        self.prompts = packets.load_prompts()
        self.schemas = packets.load_schemas()
        self.usage = defaultdict(lambda: {"calls": 0, "cached": 0, "failed": 0, "input_tokens": 0, "output_tokens": 0,
                                          "cache_read_tokens": 0, "cache_write_tokens": 0, "cost_usd": 0.0})
        self.requests_log = defaultdict(list)
        self.warnings: list[str] = []
        self._lock = threading.Lock()
        self.t0 = time.time()
        self.dump_dir = os.path.join(work_dir, "prompts") if dump_prompts else None

    # ------------------------------------------------------------------------------------
    def _warn(self, msg: str) -> None:
        if len(self.warnings) < 200:
            self.warnings.append(msg)

    def _execute(self, reqs: list[Request]) -> dict:
        results, todo = {}, []
        for r in reqs:
            self.requests_log[r.phase].append(r)
            if self.dump_dir:
                os.makedirs(self.dump_dir, exist_ok=True)
                with open(os.path.join(self.dump_dir, f"{r.id}.md"), "w", encoding="utf-8") as fh:
                    fh.write(f"<!-- {r.id} | model {r.model} | effort {r.effort} | ~{r.input_tokens_est()} input tokens -->\n")
                    fh.write("# SYSTEM\n\n" + "\n\n---\n\n".join(r.system) + "\n\n# USER\n\n" + r.user + "\n")
            cached = read_json(os.path.join(self.cache_dir, r.key() + ".json")) if self.use_cache else None
            if cached and isinstance(cached.get("data"), dict):
                results[r.id] = cached["data"]
                self.usage[r.phase]["cached"] += 1
            else:
                todo.append(r)

        def on_result(r: Request, res: dict):
            with self._lock:
                u = self.usage[r.phase]
                if "error" in res:
                    u["failed"] += 1
                    self._warn(f"{r.id}: {one_line(res['error'], 200)}")
                    results[r.id] = None
                    return
                u["calls"] += 1
                for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
                    u[k] += int(res.get("usage", {}).get(k, 0) or 0)
                u["cost_usd"] += float(res.get("cost_usd") or 0.0)
                results[r.id] = res["data"]
            if self.use_cache and self.backend.name != "mock":
                write_json(os.path.join(self.cache_dir, r.key() + ".json"),
                           {"id": r.id, "model": res.get("model"), "data": res["data"]}, indent=None)

        if todo:
            log(f"{todo[0].phase}: {len(todo)} request(s) via {self.backend.name}"
                + (f" ({len(reqs) - len(todo)} cached)" if len(reqs) > len(todo) else ""))
            self.backend.run(todo, on_result)
        return results

    def spent(self) -> float:
        return sum(u["cost_usd"] for u in self.usage.values())

    # ------------------------------------------------------------------------------------
    # MAP
    def phase_map(self) -> dict:
        req = packets.map_request(self.scan, self.cfg, self.prompts, self.schemas)
        self.full_source_map = req.meta.get("full_source", False)
        ctx_path = os.path.join(self.work, "context.json")
        if self.cfg.reuse_context and os.path.exists(ctx_path):
            prev = read_json(ctx_path)
            if prev and prev.get("map"):
                log("map: reusing previous context (--reuse-context)")
                return self._clean_map(prev["map"])
        log(f"map: brief ~{req.input_tokens_est()} tokens"
            + (" (full source)" if self.full_source_map else " (skeleton)"))
        data = self._execute([req]).get(req.id)
        if not data:
            raise SystemExit("map phase failed; see warnings above")
        write_json(os.path.join(self.work, "map.raw.json"), data)
        return self._clean_map(data)

    def _resolve_symbol(self, sid: str, file: str = "", name: str = "") -> str | None:
        if sid in self.g.by_id:
            return sid
        if name:
            cands = [s for s in self.g.symbols if s.callable and (s.name == name or s.qualname == name or s.qualname.endswith("." + name))]
            if file:
                cands = [s for s in cands if s.file == file or s.file.endswith(file)] or cands
            if len(cands) == 1:
                return cands[0].id
        return None

    def _clean_map(self, m: dict) -> dict:
        g = self.g
        eids = {e["id"] for e in g.entries}
        rename, flows, seen = {}, [], set()
        for i, f in enumerate(m.get("critical_flows") or [], 1):
            fid = str(f.get("id") or "")
            new = fid if re.fullmatch(r"F\d+", fid) and fid not in seen else f"F{i}"
            while new in seen:
                new = f"F{int(new[1:]) + 1}"
            seen.add(new)
            rename[fid] = new
            bad = [s for s in f.get("steps") or [] if s not in g.by_id]
            if bad:
                self._warn(f"map: flow {new} referenced unknown symbols {bad[:5]}")
            flows.append({
                "id": new, "name": one_line(f.get("name") or new, 120),
                "criticality": clamp_int(f.get("criticality"), 1, 10, 5),
                "confidence": f.get("confidence") if f.get("confidence") in ("high", "medium", "low") else "medium",
                "entry_points": [e for e in f.get("entry_points") or [] if e in eids],
                "steps": [s for s in f.get("steps") or [] if s in g.by_id],
                "business_impact": f.get("business_impact") or "",
                "failure_modes": list(f.get("failure_modes") or []),
                "observable_outcomes": list(f.get("observable_outcomes") or []),
                "preconditions": list(f.get("preconditions") or []),
            })
        flows.sort(key=lambda f: -f["criticality"])
        fids = {f["id"] for f in flows}
        fl = lambda xs: [rename.get(x, x) for x in xs or [] if rename.get(x, x) in fids]  # noqa: E731
        cruxes = []
        for c in m.get("cruxes") or []:
            sid = self._resolve_symbol(str(c.get("symbol") or ""))
            if not sid:
                self._warn(f"map: crux with unknown symbol {c.get('symbol')}")
                continue
            cruxes.append({"symbol": sid, "kind": c.get("kind") if c.get("kind") in CRUX_KINDS else "other",
                           "why": c.get("why") or "", "flows": fl(c.get("flows")), "risk_if_broken": c.get("risk_if_broken") or "",
                           "confidence": c.get("confidence") or "medium", "sources": ["map"]})
        annot = {}
        for e in m.get("entry_points") or []:
            if e.get("id") in eids:
                annot[e["id"]] = {"trigger": e.get("trigger") or "", "criticality": clamp_int(e.get("criticality"), 1, 10, None),
                                  "flows": fl(e.get("flows")), "description": e.get("description") or ""}
        rejected = set(m.get("rejected_entry_candidates") or []) & eids
        added = []
        for a in m.get("added_entry_points") or []:
            sid = self._resolve_symbol(str(a.get("symbol") or ""), a.get("file") or "", a.get("name") or "")
            added.append({"symbol": sid, "file": a.get("file") or (g.by_id[sid].file if sid else ""),
                          "name": a.get("name") or "", "kind": a.get("kind") if a.get("kind") in ENTRY_KINDS else "other",
                          "trigger": a.get("trigger") or "", "criticality": clamp_int(a.get("criticality"), 1, 10, None),
                          "flows": fl(a.get("flows")), "description": a.get("description") or ""})
        return {"system_summary": m.get("system_summary") or "", "repo_kind": m.get("repo_kind") or "other",
                "flows": flows, "cruxes": cruxes, "entry_annotations": annot, "rejected": sorted(rejected),
                "added": added, "notes": list(m.get("notes") or [])}

    def _entries(self, mp: dict) -> list[dict]:
        """Static candidates minus rejected, plus model-added; annotated."""
        out = []
        for e in self.g.entries:
            if e["id"] in mp["rejected"]:
                continue
            a = mp["entry_annotations"].get(e["id"], {})
            out.append({**e, "trigger": a.get("trigger") or e.get("trigger") or "", "criticality": a.get("criticality"),
                        "flows": a.get("flows", []), "description": a.get("description", ""),
                        "source": "static+llm" if a else "static"})
        n = len(self.g.entries)
        for i, a in enumerate(mp["added"], 1):
            s = self.g.by_id.get(a["symbol"] or "")
            out.append({"id": f"E{n + i}", "kind": a["kind"], "file": s.file if s else a["file"], "line": s.start if s else None,
                        "symbol": a["symbol"], "module": False, "evidence": s.signature if s else "", "pattern": "llm",
                        "trigger": a["trigger"], "criticality": a["criticality"], "flows": a["flows"],
                        "description": a["description"], "source": "llm"})
        return out

    # ------------------------------------------------------------------------------------
    # TRACE
    def phase_trace(self, mp: dict, entries_by_id: dict) -> dict:
        if self.cfg.trace in (False, "never") or not mp["flows"]:
            return {}
        if self.full_source_map and self.cfg.trace != "always":
            log("trace: skipped (map already saw the full source; use --trace always to force)")
            return {}
        ctx_path = os.path.join(self.work, "context.json")
        if self.cfg.reuse_context and os.path.exists(ctx_path):
            prev = read_json(ctx_path) or {}
            if prev.get("traces") is not None:
                return prev["traces"]
        reqs = packets.trace_requests(self.scan, self.cfg, mp["flows"], mp["system_summary"], entries_by_id,
                                      self.prompts, self.schemas)
        res = self._execute(reqs)
        traces = {}
        for r in reqs:
            d = res.get(r.id)
            if not d:
                continue
            fid = r.meta["flow"]
            g = self.g
            traces[fid] = {
                "steps": [{"symbol": st["symbol"], "what": st.get("what") or "", "certain": bool(st.get("certain"))}
                          for st in d.get("steps") or [] if st.get("symbol") in g.by_id],
                "cruxes": [{"symbol": c["symbol"], "kind": c.get("kind") if c.get("kind") in CRUX_KINDS else "other",
                            "why": c.get("why") or "", "risk_if_broken": c.get("risk_if_broken") or ""}
                           for c in d.get("cruxes") or [] if c.get("symbol") in g.by_id],
                "off_path": [s for s in d.get("off_path") or [] if s in g.by_id],
                "criticality": clamp_int(d.get("criticality"), 1, 10, None),
                "business_impact": d.get("business_impact") or "",
                "observable_outcomes": list(d.get("observable_outcomes") or []),
                "preconditions": list(d.get("preconditions") or []),
                "failure_modes": list(d.get("failure_modes") or []),
                "gaps": list(d.get("gaps") or []),
            }
        return traces

    @staticmethod
    def merge_flows(mp: dict, traces: dict) -> tuple[list, list]:
        flows = []
        for f in mp["flows"]:
            t = traces.get(f["id"])
            nf = dict(f)
            if t:
                nf["steps"] = t["steps"] or [{"symbol": s, "what": "", "certain": False} for s in f["steps"]]
                for k in ("business_impact", "observable_outcomes", "preconditions", "failure_modes"):
                    if t.get(k):
                        nf[k] = t[k]
                if t.get("criticality"):
                    nf["criticality"] = t["criticality"]
                nf["gaps"], nf["off_path"], nf["traced"] = t["gaps"], t["off_path"], True
            else:
                nf["steps"] = [{"symbol": s, "what": "", "certain": False} for s in f["steps"]]
                nf["gaps"], nf["off_path"], nf["traced"] = [], [], False
            flows.append(nf)
        flows.sort(key=lambda f: -f["criticality"])
        cruxes = {c["symbol"]: dict(c) for c in mp["cruxes"]}
        for fid, t in traces.items():
            for c in t["cruxes"]:
                if c["symbol"] in cruxes:
                    cx = cruxes[c["symbol"]]
                    cx["flows"] = sorted(set(cx["flows"]) | {fid})
                    cx["sources"] = sorted(set(cx["sources"]) | {"trace"})
                else:
                    cruxes[c["symbol"]] = {**c, "flows": [fid], "confidence": "high", "sources": ["trace"]}
        return flows, list(cruxes.values())

    def flow_reach(self, flows: list, entries_by_id: dict) -> tuple[dict, dict]:
        g = self.g
        reach, on_path = defaultdict(dict), defaultdict(set)
        for f in flows:
            seeds = []
            for eid in f["entry_points"]:
                e = entries_by_id.get(eid)
                if e:
                    seeds.append(e.get("symbol") or g.module_ids.get(e["file"]))
            seeds += [st["symbol"] for st in f["steps"]]
            off = set(f.get("off_path") or [])
            for sid, d in g.reach_from([s for s in seeds if s], max_depth=4).items():
                if sid in g.by_id and sid not in off:
                    cur = reach[sid].get(f["id"])
                    reach[sid][f["id"]] = d if cur is None else min(cur, d)
            for st in f["steps"]:
                on_path[st["symbol"]].add(f["id"])
        return reach, on_path

    # ------------------------------------------------------------------------------------
    # SCORE
    def phase_score(self, context_text, flows, flow_reach, on_path, crux_ids, entries_by_id) -> tuple[dict, dict]:
        g = self.g
        fids = {f["id"] for f in flows}
        reqs = packets.score_requests(self.scan, self.cfg, context_text, flow_reach, on_path, crux_ids,
                                      entries_by_id, self.prompts, self.schemas)
        est = self.estimate_cost(reqs)
        log(f"score: {len(reqs)} request(s), ~{sum(r.input_tokens_est() for r in reqs)} input tokens, est ${est:.2f}")
        if self.cfg.max_cost is not None and self.spent() + est > self.cfg.max_cost:
            raise SystemExit(f"Estimated total ${self.spent() + est:.2f} exceeds --max-cost {self.cfg.max_cost}. "
                             "Use a cheaper --model-score, --batch, --include/--exclude, or raise the cap.")
        methods, files = {}, {}
        self._merge_scores(reqs, self._execute(reqs), methods, files, fids)
        expected = {mid for r in reqs for mid in r.meta["expected"]}
        missing = sorted(expected - set(methods))
        if missing:
            log(f"score: {len(missing)} method(s) missing from answers; re-asking once")
            retry = packets.score_requests(self.scan, self.cfg, context_text, flow_reach, on_path, crux_ids,
                                           entries_by_id, self.prompts, self.schemas, only_ids=set(missing),
                                           id_prefix="score-retry")
            self._merge_scores(retry, self._execute(retry), methods, files, fids)
        pct = packets.static_percentiles(self.scan)
        for s in self.scan.callables():
            if s.id in methods:
                continue
            if s.trivial:
                methods[s.id] = {"id": s.id, "score": 1 if s.trivial == "empty" else 2, "role": "accessor" if s.trivial != "empty" else "other",
                                 "flows": [], "why": f"rule: {s.trivial}", "confidence": "high", "source": "rule"}
            else:
                methods[s.id] = {"id": s.id, "score": packets._static_estimate(s, pct), "role": "other",
                                 "flows": sorted(flow_reach.get(s.id, {}))[:3], "why": "static estimate (no model answer)",
                                 "confidence": "low", "source": "static-fallback"}
                self._warn(f"score: static fallback for {s.id} {s.qualname}")
        return methods, files

    def _merge_scores(self, reqs, results, methods, files, fids) -> None:
        g = self.g
        for r in reqs:
            d = results.get(r.id)
            if not d:
                continue
            paths = [f["path"] for f in r.meta["files"]]
            expected = set(r.meta["expected"])
            for fo in d.get("files") or []:
                path = fo.get("path") or ""
                if path not in g.files:
                    alts = [p for p in paths if p.endswith(path) or path.endswith(p) or os.path.basename(p) == os.path.basename(path)]
                    path = alts[0] if len(alts) == 1 else path
                if path in g.files:
                    sc = clamp_int(fo.get("score"), 1, 10, None)
                    cur = files.get(path)
                    if cur is None or (sc or 0) > (cur.get("score") or 0):
                        files[path] = {"score": sc, "summary": fo.get("summary") or (cur or {}).get("summary", "")}
                for mo in fo.get("methods") or []:
                    mid = mo.get("id")
                    s = g.by_id.get(mid)
                    if not s or not s.callable:
                        continue
                    if mid not in expected and mid in methods:
                        continue
                    sc = clamp_int(mo.get("score"), 1, 10, None)
                    if sc is None:
                        continue
                    methods[mid] = {"id": mid, "score": sc,
                                    "role": mo.get("role") if mo.get("role") in ROLES else "other",
                                    "flows": [x for x in mo.get("flows") or [] if x in fids],
                                    "why": one_line(mo.get("why") or "", 300),
                                    "confidence": mo.get("confidence") if mo.get("confidence") in ("high", "medium", "low") else "medium",
                                    "source": "llm-override" if s.trivial else "llm"}
                for ex in fo.get("extra") or []:
                    if path not in g.files or not ex.get("name"):
                        continue
                    xid = "X" + sha1(f"{path}::{ex['name']}::{ex.get('line')}")[:6]
                    methods[xid] = {"id": xid, "score": clamp_int(ex.get("score"), 1, 10, 3), "role": ex.get("role") if ex.get("role") in ROLES else "other",
                                    "flows": [x for x in ex.get("flows") or [] if x in fids], "why": one_line(ex.get("why") or "", 300),
                                    "confidence": ex.get("confidence") or "low", "source": "llm-discovered",
                                    "discovered": {"file": path, "name": ex["name"], "line": clamp_int(ex.get("line"), 1, 10 ** 7, None)}}

    # ------------------------------------------------------------------------------------
    # CALIBRATE
    def phase_calibrate(self, summary, flows, cruxes, methods, entries_by_id, flow_reach, on_path):
        g = self.g
        fids = {f["id"] for f in flows}
        scorable = {k: v for k, v in methods.items() if k in g.by_id}
        if not self.cfg.calibrate or not scorable:
            return [], None, []
        req = packets.calibrate_request(self.scan, self.cfg, summary, flows, cruxes, scorable, entries_by_id,
                                        flow_reach, on_path, self.prompts, self.schemas)
        d = self._execute([req]).get(req.id)
        if not d:
            return [], None, []
        adjustments = []
        for a in d.get("adjustments") or []:
            mid = a.get("id")
            sc = clamp_int(a.get("score"), 1, 10, None)
            if mid in methods and sc is not None and sc != methods[mid]["score"]:
                adjustments.append({"id": mid, "from": methods[mid]["score"], "to": sc, "reason": a.get("reason") or ""})
        final = []
        for c in d.get("cruxes") or []:
            if c.get("symbol") in g.by_id and g.by_id[c["symbol"]].callable:
                final.append({"symbol": c["symbol"], "kind": c.get("kind") if c.get("kind") in CRUX_KINDS else "other",
                              "why": c.get("why") or "", "flows": [x for x in c.get("flows") or [] if x in fids],
                              "risk_if_broken": c.get("risk_if_broken") or "", "sources": ["calibrate"]})
        return adjustments, final, list(d.get("notes") or [])

    # ------------------------------------------------------------------------------------
    def estimate_cost(self, reqs: list[Request]) -> float:
        """Rough pre-flight cost: input from packet sizes, output from method counts, shared system
        prompt billed as a cache write once and cache reads afterwards (when long enough to cache)."""
        total, seen_prefix = 0.0, set()
        for r in reqs:
            n_methods = len(r.meta.get("expected", [])) if r.phase == "score" else 0
            out = 60 * n_methods + 300 if r.phase == "score" else 4000
            thinking = 0 if "haiku" in r.model else (3000 if r.phase != "score" else 800)
            sys_tok = sum(est_tokens(s) for s in r.system)
            usage = {"input_tokens": r.input_tokens_est() - sys_tok, "output_tokens": out + thinking}
            prefix = (r.model, sha1(json.dumps(r.system)))
            if sys_tok >= min_cacheable(r.model):
                usage["cache_read_tokens" if prefix in seen_prefix else "cache_write_tokens"] = sys_tok
            else:
                usage["input_tokens"] += sys_tok
            seen_prefix.add(prefix)
            total += cost_usd(r.model, usage, batch=self.cfg.batch, cache_ttl="1h" if self.cfg.batch else "5m")
        return total

    # ------------------------------------------------------------------------------------
    def run(self) -> dict:
        g, cfg = self.g, self.cfg
        mp = self.phase_map()
        entries = self._entries(mp)
        entries_by_id = {e["id"]: e for e in entries}
        by_flow = {f["id"]: f for f in mp["flows"]}
        for e in entries:   # entry points the model added know their flows; link them both ways
            for fid in e.get("flows") or []:
                if fid in by_flow and e["id"] not in by_flow[fid]["entry_points"]:
                    by_flow[fid]["entry_points"].append(e["id"])
        traces = self.phase_trace(mp, entries_by_id)
        write_json(os.path.join(self.work, "context.json"), {"map": self._raw_map_for_reuse(mp), "traces": traces})
        flows, cruxes = self.merge_flows(mp, traces)
        flow_reach, on_path = self.flow_reach(flows, entries_by_id)
        crux_ids = {c["symbol"] for c in cruxes}
        context_text = packets.score_context(self.scan, mp["system_summary"], mp["repo_kind"], flows, cruxes,
                                             entries_by_id, {e["id"]: e for e in entries if e.get("criticality")})
        methods, files = self.phase_score(context_text, flows, flow_reach, on_path, crux_ids, entries_by_id)
        adjustments, final_cruxes, notes = self.phase_calibrate(mp["system_summary"], flows, cruxes, methods,
                                                                entries_by_id, flow_reach, on_path)
        return self.assemble(mp, entries, flows, cruxes, final_cruxes, methods, files, adjustments,
                             notes, flow_reach, on_path)

    @staticmethod
    def _raw_map_for_reuse(mp: dict) -> dict:
        """Re-serialize the cleaned map in the model's output shape (so --reuse-context can reload it)."""
        return {"system_summary": mp["system_summary"], "repo_kind": mp["repo_kind"],
                "rejected_entry_candidates": mp["rejected"],
                "entry_points": [{"id": k, **v} for k, v in mp["entry_annotations"].items()],
                "added_entry_points": [{**a, "symbol": a["symbol"] or ""} for a in mp["added"]],
                "critical_flows": mp["flows"], "cruxes": mp["cruxes"], "notes": mp["notes"]}

    # ------------------------------------------------------------------------------------
    def assemble(self, mp, entries, flows, cruxes, final_cruxes, methods, files, adjustments, notes,
                 flow_reach, on_path) -> dict:
        g, scan = self.g, self.scan
        for a in adjustments:
            m = methods[a["id"]]
            m["calibrated_from"] = m["score"]
            m["score"] = a["to"]
            m["calibration_reason"] = a["reason"]
        # cruxes: calibrated list if available, else candidates confirmed by scores
        if final_cruxes is None:
            final_cruxes = [c for c in cruxes if methods.get(c["symbol"], {}).get("score", 0) >= 8]
        crux_out = []
        prior = {c["symbol"]: c for c in cruxes}
        for i, c in enumerate(final_cruxes, 1):
            s = g.by_id[c["symbol"]]
            m = methods.get(s.id)
            if m and m["score"] < 9:
                m["calibrated_from"] = m.get("calibrated_from", m["score"])
                m["score"] = 9
                m["calibration_reason"] = (m.get("calibration_reason") or "") + " [raised: selected as crux]"
            p = prior.get(s.id, {})
            crux_out.append({"id": f"C{i}", "symbol": s.id, "key": s.key, "name": s.qualname, "file": s.file, "line": s.start,
                             "kind": c.get("kind", "other"), "score": m["score"] if m else None,
                             "flows": sorted(set(c.get("flows", [])) | set(p.get("flows", []))),
                             "why": c.get("why") or p.get("why", ""), "risk_if_broken": c.get("risk_if_broken") or p.get("risk_if_broken", ""),
                             "sources": sorted(set(c.get("sources", [])) | set(p.get("sources", [])))})
        crux_ids = {c["symbol"] for c in crux_out}
        # methods
        method_out = []
        for mid, m in methods.items():
            s = g.by_id.get(mid)
            if s is None:
                dsc = m["discovered"]
                method_out.append({"id": mid, "key": f"{dsc['file']}::{dsc['name']}", "file": dsc["file"], "name": dsc["name"],
                                   "qualified_name": dsc["name"], "kind": "function", "start_line": dsc["line"], "end_line": None,
                                   "score": m["score"], "role": m["role"], "flows": m["flows"], "why": m["why"],
                                   "confidence": m["confidence"], "source": m["source"], "is_entry_point": False, "is_crux": False,
                                   "static": None})
                continue
            rec = {"id": s.id, "key": s.key, "file": s.file, "name": s.name, "qualified_name": s.qualname, "kind": s.kind,
                   "start_line": s.start, "end_line": s.end, "score": m["score"], "role": m["role"],
                   "flows": sorted(set(m["flows"]) | on_path.get(s.id, set())), "why": m["why"],
                   "confidence": m["confidence"], "source": m["source"], "is_entry_point": bool(s.entries),
                   "is_crux": s.id in crux_ids,
                   "static": {"fan_in": s.fan_in, "fan_in_files": s.fan_in_files, "loc": s.loc, "complexity": s.complexity,
                              "tags": s.tags, "flow_reach": dict(sorted(flow_reach.get(s.id, {}).items())),
                              "trivial": s.trivial, "priority": s.prio}}
            if "calibrated_from" in m:
                rec["calibrated_from"] = m["calibrated_from"]
                rec["calibration_reason"] = m.get("calibration_reason", "")
            method_out.append(rec)
        method_out.sort(key=lambda r: (-r["score"], r["file"], r["start_line"] or 0))
        # files
        by_file = defaultdict(list)
        for r in method_out:
            by_file[r["file"]].append(r["score"])
        file_out = []
        for path, f in g.files.items():
            scores = sorted(by_file.get(path, []), reverse=True)
            llm = files.get(path, {})
            sc = llm.get("score")
            mx = scores[0] if scores else None
            source = "llm"
            if sc is None:
                sc, source = (mx if mx is not None else 1), "derived"
            elif mx is not None and not (mx - 1 <= sc <= mx + 1):
                # keep file scores consistent with the (calibrated) method scores they contain
                sc, source = min(max(sc, mx - 1), mx + 1), "llm+clamped"
            file_out.append({"path": path, "language": f.lang, "lines": f.lines, "score": sc, "summary": llm.get("summary", ""),
                             "methods": len(scores), "max_method_score": mx,
                             "mean_top3": round(sum(scores[:3]) / len(scores[:3]), 2) if scores else None,
                             "churn": f.churn, "fix_churn": f.fix_churn, "extractor": f.extractor, "score_source": source})
        file_out.sort(key=lambda r: (-r["score"], -(r["max_method_score"] or 0), r["path"]))
        # entry points
        meth_by_id = {r["id"]: r for r in method_out}
        ep_out = []
        for e in entries:
            s = g.by_id.get(e.get("symbol") or "")
            m = meth_by_id.get(s.id) if s else None
            crit = e.get("criticality") or (m["score"] if m else None)
            ep_out.append({"id": e["id"], "kind": e["kind"], "trigger": e.get("trigger") or "", "file": e["file"],
                           "line": e.get("line"), "symbol": s.id if s else None, "name": s.qualname if s else "<top-level>",
                           "criticality": crit, "flows": sorted(set(e.get("flows") or []) | (set(m["flows"]) if m else set())),
                           "description": e.get("description") or (m["why"] if m else ""), "evidence": e.get("evidence", ""),
                           "source": e.get("source", "static"), "detected_by": e.get("pattern")})
        ep_out.sort(key=lambda r: (-(r["criticality"] or 0), r["file"], r["line"] or 0))
        # flows
        flow_out = []
        for f in flows:
            steps = []
            for st in f["steps"]:
                s = g.by_id.get(st["symbol"])
                if s:
                    steps.append({"symbol": s.id, "name": s.qualname, "file": s.file, "line": s.start,
                                  "what": st.get("what", ""), "certain": st.get("certain", False)})
            flow_out.append({"id": f["id"], "name": f["name"], "criticality": f["criticality"], "confidence": f["confidence"],
                             "traced": f.get("traced", False), "entry_points": f["entry_points"], "steps": steps,
                             "business_impact": f["business_impact"], "failure_modes": f["failure_modes"],
                             "observable_outcomes": f["observable_outcomes"], "preconditions": f["preconditions"],
                             "gaps": f.get("gaps", []), "cruxes": [c["id"] for c in crux_out if f["id"] in c["flows"]]})
        hist = Counter(r["score"] for r in method_out)
        sm = scan.summary()
        run = {"backend": self.backend.name, "batch": self.cfg.batch,
               "models": {"map": self.cfg.model_map, "trace": self.cfg.model_trace, "score": self.cfg.model_score,
                          "calibrate": self.cfg.model_calibrate},
               "phases": {k: dict(v, cost_usd=round(v["cost_usd"], 4)) for k, v in self.usage.items()},
               "total_cost_usd": round(self.spent(), 4), "seconds": round(time.time() - self.t0, 1),
               "scan_seconds": sm["seconds"], "tree_sitter": sm["tree_sitter"], "warnings": self.warnings[:100]}
        return {
            "schema_version": "1.0",
            "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
            "tool": {"name": "repo-crux", "version": VERSION},
            "repo": {"name": scan.repo.rstrip("/").split("/")[-1], "path": scan.repo, **(scan.context.get("git") or {}),
                     "kind": mp["repo_kind"], "summary": mp["system_summary"], "languages": sm["languages_by_lines"],
                     "files_analyzed": sm["files"], "lines_analyzed": sm["lines"], "methods_analyzed": len(method_out)},
            "entry_points": ep_out,
            "critical_flows": flow_out,
            "cruxes": crux_out,
            "files": file_out,
            "method_analysis": method_out,
            "score_distribution": {str(k): hist.get(k, 0) for k in range(10, 0, -1)},
            "skipped_files": {"counts": dict(scan.skipped), "examples": scan.context.get("skipped_examples", {})},
            "notes": list(mp.get("notes") or []) + list(notes or []),
            "run": run,
        }
