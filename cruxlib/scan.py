"""Phase 0: the zero-token static pass. Produces everything the LLM phases need."""
from __future__ import annotations

import os
import time
from collections import Counter
from dataclasses import dataclass

from . import inventory as inv
from . import symbols as sy
from .graph import RepoGraph
from .util import est_tokens, log


@dataclass
class ScanResult:
    repo: str
    files: list            # [FileInfo]
    texts: dict            # path -> text
    graph: RepoGraph
    skipped: Counter
    context: dict
    seconds: float

    @property
    def symbols(self):
        return self.graph.symbols

    def callables(self):
        return [s for s in self.graph.symbols if s.callable]

    def source_tokens(self) -> int:
        return sum(est_tokens(t) for t in self.texts.values())

    def summary(self) -> dict:
        cal = self.callables()
        langs = Counter()
        for f in self.files:
            langs[f.lang] += f.lines
        return {
            "files": len(self.files),
            "lines": sum(f.lines for f in self.files),
            "source_tokens_est": self.source_tokens(),
            "languages_by_lines": dict(langs.most_common()),
            "symbols": len(self.graph.symbols),
            "callables": len(cal),
            "trivial_callables": sum(1 for s in cal if s.trivial),
            "entry_candidates": len(self.graph.entries),
            "entry_kinds": dict(Counter(e["kind"] for e in self.graph.entries).most_common()),
            "extractors": dict(Counter(f.extractor for f in self.files)),
            "skipped": dict(self.skipped),
            "tree_sitter": sy.has_tree_sitter(),
            "seconds": round(self.seconds, 2),
        }

    def to_json(self) -> dict:
        g = self.graph
        return {
            "repo": self.repo,
            "summary": self.summary(),
            "context": self.context,
            "files": [f.to_json() for f in self.files],
            "symbols": [s.to_json() for s in g.symbols],
            "entries": g.entries,
            "edges": [[a, b, round(w, 2)] for a, outs in g.edges.items() for b, w in outs.items()],
        }


def scan(repo: str, include_tests=False, include_migrations=False, include_globs=None, exclude_globs=None,
         max_files=None, use_tree_sitter=True, library_fallback=True) -> ScanResult:
    t0 = time.time()
    repo = os.path.abspath(repo)
    files, texts, skipped, context = inv.inventory(repo, include_tests, include_migrations,
                                                   include_globs, exclude_globs, max_files)
    syms_by_file, imports = {}, {}
    for f in files:
        syms, imps, names, extractor = sy.extract(f.path, texts[f.path], f.lang, use_tree_sitter)
        f.extractor = extractor
        f.imports, f.imported_names = imps, names
        syms_by_file[f.path] = syms
        imports[f.path] = (imps, names)
    all_syms = [s for p in syms_by_file for s in syms_by_file[p]]
    sy.assign_ids(all_syms)
    log(f"symbols: {len(all_syms)} ({sum(s.callable for s in all_syms)} callables) "
        f"via {dict(Counter(f.extractor for f in files))}")
    g = RepoGraph(files, texts, syms_by_file, imports)
    g.build()
    g.compute_signals(sy.trivial_reason)
    g.detect_entries(library_fallback=library_fallback)
    g.entry_reach()
    g.priorities()
    log(f"graph: {sum(len(v) for v in g.edges.values())} edges, {len(g.entries)} entry candidates, "
        f"{time.time() - t0:.1f}s")
    return ScanResult(repo, files, texts, g, skipped, context, time.time() - t0)
