#!/usr/bin/env python3
"""repo-crux: find entry points, critical flows and cruxes in any repository, then score every
file and method 1-10 by importance to those flows. Output: one JSON document.

  python crux.py scan     REPO                  # static pass only (free): files, methods, graph, entry candidates
  python crux.py estimate REPO                  # token + cost estimate for a full run, no model calls
  python crux.py run      REPO -o analysis.json # full analysis
  python crux.py auto     NAME|PATH|GIT-URL     # unattended: find or clone, pick a backend, fit a cost cap, run
  python crux.py validate analysis.json         # check an output file against schemas/analysis.schema.json
  python crux.py summary  analysis.json         # human-readable digest of an output file

Backends (--backend): anthropic (default; ANTHROPIC_API_KEY), claude-cli (uses your Claude Code
login), files (write prompts to disk; any agent answers them; re-run to continue), mock.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cruxlib import scan as scanmod  # noqa: E402
from cruxlib.llm import PendingRequests, cost_usd, make_backend, price_for  # noqa: E402
from cruxlib.pipeline import Config, Pipeline  # noqa: E402
from cruxlib.util import log, set_verbose, write_json  # noqa: E402
from cruxlib.validate import validate_file  # noqa: E402


def add_scan_args(p, positional=True):
    if positional:
        p.add_argument("repo", help="path to the repository (git or plain directory)")
    p.add_argument("--include", action="append", default=[], metavar="GLOB", help="only analyze matching paths (repeatable)")
    p.add_argument("--exclude", action="append", default=[], metavar="GLOB", help="skip matching paths (repeatable)")
    p.add_argument("--include-tests", action="store_true", help="also score test files")
    p.add_argument("--include-migrations", action="store_true", help="also score DB migration files")
    p.add_argument("--max-files", type=int, help="cap analyzed files (keeps most-churned)")
    p.add_argument("--no-tree-sitter", action="store_true", help="force the built-in heuristic extractor")
    p.add_argument("--work", help="work dir for cache/intermediates (default ./.crux/<repo-name>)")
    p.add_argument("-q", "--quiet", action="store_true")


def add_model_args(p):
    d = Config()
    p.add_argument("--model-map", default=os.environ.get("CRUX_MODEL_MAP", d.model_map))
    p.add_argument("--model-trace", default=os.environ.get("CRUX_MODEL_TRACE", d.model_trace))
    p.add_argument("--model-score", default=os.environ.get("CRUX_MODEL_SCORE", d.model_score))
    p.add_argument("--model-calibrate", default=os.environ.get("CRUX_MODEL_CALIBRATE", d.model_calibrate))
    p.add_argument("--effort-map", default=d.effort_map)
    p.add_argument("--effort-trace", default=d.effort_trace)
    p.add_argument("--effort-calibrate", default=d.effort_calibrate)
    p.add_argument("--map-budget", type=int, default=d.map_budget, help="tokens for the repo brief (default %(default)s)")
    p.add_argument("--full-source-threshold", type=int, default=d.full_source_threshold,
                   help="repos smaller than this many tokens: map sees all code, trace skipped (default %(default)s)")
    p.add_argument("--trace", choices=["auto", "always", "never"], default="auto",
                   help="per-flow tracing against code; auto skips it when the map already saw all source")
    p.add_argument("--no-trace", action="store_true", help="same as --trace never")
    p.add_argument("--max-trace-flows", type=int, default=d.max_trace_flows)
    p.add_argument("--trace-budget", type=int, default=d.trace_budget)
    p.add_argument("--chunk-tokens", type=int, default=d.score_chunk_tokens, help="file material per scoring call")
    p.add_argument("--no-calibrate", action="store_true", help="skip the cross-file calibration call")
    p.add_argument("--batch", action="store_true", help="anthropic backend: Message Batches API (50%% cheaper, slower)")


def config_from(a) -> Config:
    return Config(
        backend=getattr(a, "backend", "mock"), model_map=a.model_map, model_trace=a.model_trace,
        model_score=a.model_score, model_calibrate=a.model_calibrate, effort_map=a.effort_map or None,
        effort_trace=a.effort_trace or None, effort_calibrate=a.effort_calibrate or None,
        map_budget=a.map_budget, full_source_threshold=a.full_source_threshold, trace="never" if a.no_trace else a.trace,
        max_trace_flows=a.max_trace_flows, trace_budget=a.trace_budget, score_chunk_tokens=a.chunk_tokens,
        calibrate=not a.no_calibrate, batch=a.batch, concurrency=getattr(a, "concurrency", None),
        max_cost=getattr(a, "max_cost", None), reuse_context=getattr(a, "reuse_context", False))


def scan_kwargs(a) -> dict:
    return dict(include_tests=a.include_tests, include_migrations=a.include_migrations,
                include_globs=a.include or None, exclude_globs=a.exclude or None, max_files=a.max_files,
                use_tree_sitter=not a.no_tree_sitter)


def do_scan(a):
    return scanmod.scan(a.repo, **scan_kwargs(a))


def work_dir(a) -> str:
    return os.path.abspath(a.work or os.path.join(".crux", os.path.basename(os.path.abspath(a.repo).rstrip("/"))))


def cmd_scan(a):
    sc = do_scan(a)
    out = a.out or os.path.join(work_dir(a), "scan.json")
    write_json(out, sc.to_json())
    print(json.dumps(sc.summary(), indent=1))
    print(f"scan written to {out}")


def cmd_estimate(a):
    sc = do_scan(a)
    cfg = config_from(a)
    from cruxlib.llm import MockBackend
    wd = os.path.join(work_dir(a), "estimate")
    pipe = Pipeline(sc, cfg, MockBackend(), wd, use_cache=False)
    pipe.run()
    rows, total = [], 0.0
    for phase in ("map", "trace", "score", "calibrate"):
        reqs = pipe.requests_log.get(phase, [])
        if not reqs:
            continue
        inp = sum(r.input_tokens_est() for r in reqs)
        c = pipe.estimate_cost(reqs)
        total += c
        rows.append((phase, len(reqs), inp, reqs[0].model, c))
    sm = sc.summary()
    print(f"\nRepository: {sm['files']} files, {sm['lines']} lines, ~{sm['source_tokens_est']} source tokens, "
          f"{sm['callables']} methods ({sm['trivial_callables']} rule-scored), {sm['entry_candidates']} entry candidates")
    print(f"{'phase':10} {'calls':>6} {'input tok':>11}  {'model':28} {'est. cost':>10}")
    for phase, n, inp, model, c in rows:
        print(f"{phase:10} {n:>6} {inp:>11}  {model:28} ${c:>9.2f}")
    print(f"{'total':10} {'':>6} {'':>11}  {'':28} ${total:>9.2f}" + ("  (batch pricing)" if cfg.batch else ""))
    print("Rough estimate: assumes moderate thinking on Opus/Sonnet calls and prompt-cache reads for the shared scoring prefix.")


def cmd_run(a):
    sc = do_scan(a)
    cfg = config_from(a)
    wd = work_dir(a)
    os.makedirs(wd, exist_ok=True)
    write_json(os.path.join(wd, "scan.json"), sc.to_json())
    backend = make_backend(a.backend, wd, a.concurrency, a.batch, a.claude_bin)
    pipe = Pipeline(sc, cfg, backend, wd, use_cache=not a.no_cache, dump_prompts=a.dump_prompts)
    try:
        result = pipe.run()
    except PendingRequests as p:
        print(f"\n{len(p.paths)} request(s) are waiting for answers. Instructions: {p.index}")
        print("Answer each requests/<id>.md into responses/<id>.json, then re-run this command.")
        sys.exit(3)
    out = a.output or os.path.join(wd, "analysis.json")
    write_json(out, result)
    errs = validate_file(out)
    if errs:
        print(f"warning: output failed schema validation ({len(errs)} issue(s)): {errs[:3]}")
    print_summary(result)
    print(f"\nanalysis written to {out}")


def print_summary(r: dict, n: int = 10):
    run = r.get("run", {})
    print(f"\n{r['repo']['name']}: {r['repo'].get('kind')} - {r['repo'].get('summary', '')}")
    print(f"entry points: {len(r['entry_points'])} · flows: {len(r['critical_flows'])} · cruxes: {len(r['cruxes'])} · "
          f"files: {len(r['files'])} · methods: {len(r['method_analysis'])}")
    print("score distribution (10..1): " + " ".join(f"{k}:{v}" for k, v in r["score_distribution"].items()))
    print("\nCRITICAL FLOWS")
    for f in r["critical_flows"][:n]:
        print(f"  {f['id']} [{f['criticality']}] {f['name']}  ({len(f['steps'])} steps, cruxes {','.join(f['cruxes']) or '-'})")
    print("\nCRUXES")
    for c in r["cruxes"][:n]:
        print(f"  {c['id']} {c['score']} {c['name']}  ({c['file']}:{c['line']}) [{c['kind']}] {c['why'][:90]}")
    print("\nTOP METHODS")
    for m in r["method_analysis"][:n]:
        print(f"  {m['score']:>2} {m['qualified_name'][:60]:60} {m['role']:15} {m['file']}:{m['start_line']}")
    if run:
        print(f"\nrun: backend {run.get('backend')}, cost ${run.get('total_cost_usd', 0):.2f}, {run.get('seconds')}s, "
              + ", ".join(f"{k}: {v['calls']} calls ({v['cached']} cached)" for k, v in run.get("phases", {}).items()))
        if run.get("warnings"):
            print(f"warnings: {len(run['warnings'])} (see run.warnings in the JSON)")


def cmd_auto(a):
    from cruxlib.auto import run_auto
    cfg = config_from(a)
    sys.exit(run_auto(a.target, scan_kwargs(a), cfg, a.results_dir, no_cost_limit=a.no_cost_limit))


def cmd_validate(a):
    errs = validate_file(a.file)
    if errs:
        print(f"{len(errs)} problem(s):")
        for e in errs[:50]:
            print("  " + e)
        sys.exit(1)
    print("valid")


def cmd_summary(a):
    with open(a.file, encoding="utf-8") as f:
        print_summary(json.load(f), a.n)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="crux", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("scan", help="static pass only (no model calls)")
    add_scan_args(p)
    p.add_argument("--out", help="where to write scan.json")
    p.set_defaults(fn=cmd_scan)
    p = sub.add_parser("estimate", help="estimate tokens and cost of a run (no model calls)")
    add_scan_args(p)
    add_model_args(p)
    p.set_defaults(fn=cmd_estimate)
    p = sub.add_parser("run", help="full analysis")
    add_scan_args(p)
    add_model_args(p)
    p.add_argument("-o", "--output", help="output JSON path (default <work>/analysis.json)")
    p.add_argument("--backend", default=os.environ.get("CRUX_BACKEND", "anthropic"),
                   choices=["anthropic", "claude-cli", "files", "mock"])
    p.add_argument("--concurrency", type=int, help="parallel requests (default 8 api / 4 claude-cli)")
    p.add_argument("--max-cost", type=float, help="abort before scoring if the estimated total exceeds this (USD)")
    p.add_argument("--reuse-context", action="store_true", help="reuse previous map/trace; only re-score changed files")
    p.add_argument("--no-cache", action="store_true", help="ignore cached model answers")
    p.add_argument("--claude-bin", help="path to the claude CLI (claude-cli backend)")
    p.add_argument("--dump-prompts", action="store_true", help="write every prompt sent to <work>/prompts/ for inspection")
    p.set_defaults(fn=cmd_run)
    p = sub.add_parser("auto", help="unattended run for a local repo name/path or a git/Bitbucket URL (never prompts)")
    p.add_argument("target", help="local repo path or folder name, or a Bitbucket/git URL")
    add_scan_args(p, positional=False)
    add_model_args(p)
    p.add_argument("--max-cost", type=float, help="cost cap in USD (default $CRUX_MAX_COST or 15); larger repos are trimmed to fit")
    p.add_argument("--no-cost-limit", action="store_true",
                   help="no spend cap: analyze every file with every phase, no trimming "
                        "(same as CRUX_MAX_COST=none or --max-cost 0)")
    p.add_argument("--concurrency", type=int, help="parallel requests")
    p.add_argument("--results-dir", help="where analysis.json and summary.md go (default ~/repo-crux-results/<repo>)")
    p.set_defaults(fn=cmd_auto)
    p = sub.add_parser("validate", help="validate an analysis.json")
    p.add_argument("file")
    p.set_defaults(fn=cmd_validate)
    p = sub.add_parser("summary", help="print a digest of an analysis.json")
    p.add_argument("file")
    p.add_argument("-n", type=int, default=10)
    p.set_defaults(fn=cmd_summary)
    a = ap.parse_args(argv)
    set_verbose(not getattr(a, "quiet", False))
    a.fn(a)


if __name__ == "__main__":
    main()
