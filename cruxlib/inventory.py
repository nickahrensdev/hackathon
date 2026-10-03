"""Phase 0a: discover and classify files, collect git churn, manifests and README context."""
from __future__ import annotations

import json
import os
import re
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from . import langs
from .util import log, one_line, read_text

MAX_FILE_BYTES = 1_500_000


@dataclass
class FileInfo:
    path: str
    lang: str | None
    category: str            # 'source' or a skip reason
    lines: int = 0
    bytes: int = 0
    churn: int = 0
    fix_churn: int = 0
    extractor: str = ""      # 'tree-sitter' | 'heuristic' | 'none'
    imports: list = field(default_factory=list)
    imported_names: set = field(default_factory=set)

    def to_json(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k not in ("imports", "imported_names")}
        return d


def _git(repo: str, *args: str, timeout: int = 120) -> str | None:
    try:
        r = subprocess.run(["git", "-C", repo, *args], capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.decode("utf-8", "replace")


def list_files(repo: str) -> list[str]:
    out = _git(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    if out is not None:
        files = [f for f in out.split("\0") if f]
        return sorted(set(files))
    files = []
    skip = langs.VENDOR_DIRS | langs.BUILD_DIRS
    for root, dirs, names in os.walk(repo):
        dirs[:] = [d for d in dirs if d.lower() not in skip and not d.startswith(".")]
        for n in names:
            files.append(os.path.relpath(os.path.join(root, n), repo).replace(os.sep, "/"))
    return sorted(files)


def git_meta(repo: str) -> dict:
    head = (_git(repo, "rev-parse", "HEAD") or "").strip()
    branch = (_git(repo, "rev-parse", "--abbrev-ref", "HEAD") or "").strip()
    remote = (_git(repo, "config", "--get", "remote.origin.url") or "").strip()
    return {"commit": head or None, "branch": branch or None, "remote": remote or None}


FIX_RE = re.compile(r"\b(fix(e[sd])?|bug|hotfix|patch|regression|crash|broken|revert|incident|security|cve)\b", re.I)


def churn(repo: str, max_commits: int = 3000, since: str = "2 years ago") -> tuple[Counter, Counter]:
    out = _git(repo, "log", f"--since={since}", f"-n{max_commits}", "--no-merges",
               "--name-only", "--format=\x1e%s", timeout=180)
    total, fixes = Counter(), Counter()
    if not out:
        return total, fixes
    for block in out.split("\x1e"):
        if not block.strip():
            continue
        lines = block.strip("\n").split("\n")
        subject, paths = lines[0], [p for p in lines[1:] if p.strip()]
        is_fix = bool(FIX_RE.search(subject))
        for p in paths:
            total[p] += 1
            if is_fix:
                fixes[p] += 1
    return total, fixes


def inventory(repo: str, include_tests=False, include_migrations=False,
              include_globs=None, exclude_globs=None, max_files: int | None = None):
    """Returns (files: list[FileInfo], texts: dict path->text, skipped: Counter, context: dict)."""
    repo = os.path.abspath(repo)
    all_files = list_files(repo)
    log(f"inventory: {len(all_files)} files tracked")
    tot, fixes = churn(repo)
    files, texts, skipped = [], {}, Counter()
    skipped_examples = defaultdict(list)
    manifests = []
    for rel in all_files:
        if exclude_globs and langs.matches_any(rel, exclude_globs):
            skipped["excluded"] += 1
            continue
        if langs.is_manifest(rel) and rel.count("/") <= 3:
            manifests.append(rel)
        cat = langs.classify(rel, include_tests, include_migrations)
        abspath = os.path.join(repo, rel)
        try:
            size = os.path.getsize(abspath)
        except OSError:
            continue
        lang = langs.language_for(rel)
        if lang is None and cat == "source" and "." not in os.path.basename(rel) and size < 200_000:
            head = read_text(abspath, 200) or ""
            lang = langs.language_for(rel, head.split("\n", 1)[0])
        if include_globs and not langs.matches_any(rel, include_globs):
            skipped["not-included"] += 1
            continue
        if lang is None:
            skipped["non-code"] += 1
            continue
        if cat != "source":
            skipped[cat] += 1
            if len(skipped_examples[cat]) < 5:
                skipped_examples[cat].append(rel)
            continue
        if size > MAX_FILE_BYTES:
            skipped["too-large"] += 1
            skipped_examples["too-large"].append(rel)
            continue
        text = read_text(abspath)
        if text is None:
            skipped["binary"] += 1
            continue
        lines = text.count("\n") + (0 if text.endswith("\n") else 1)
        if lines and (len(text) / max(lines, 1) > 300 or (lines < 5 and len(text) > 20000)):
            skipped["minified"] += 1
            continue
        head = text[:1500]
        if langs.GENERATED_MARKER.search(head) and not re.search(r"copyright", head, re.I):
            skipped["generated"] += 1
            if len(skipped_examples["generated"]) < 5:
                skipped_examples["generated"].append(rel)
            continue
        if not text.strip():
            skipped["empty"] += 1
            continue
        fi = FileInfo(path=rel, lang=lang, category=cat, lines=lines, bytes=size,
                      churn=tot.get(rel, 0), fix_churn=fixes.get(rel, 0))
        files.append(fi)
        texts[rel] = text
    if max_files and len(files) > max_files:
        # keep the most-churned / largest files when the user caps the run
        files.sort(key=lambda f: (-(f.churn * 3 + f.fix_churn * 5), -f.lines))
        dropped = files[max_files:]
        files = sorted(files[:max_files], key=lambda f: f.path)
        for f in dropped:
            texts.pop(f.path, None)
        skipped["over-max-files"] += len(dropped)
    log(f"inventory: {len(files)} source files to analyze; skipped {dict(skipped)}")
    context = {
        "git": git_meta(repo),
        "manifests": collect_manifests(repo, manifests),
        "readme": collect_readme(repo, all_files),
        "tree": dir_tree(files),
        "skipped_examples": dict(skipped_examples),
    }
    return files, texts, skipped, context


def _summarize_package_json(text: str) -> str:
    try:
        d = json.loads(text)
    except Exception:
        return text[:2500]
    keep = {k: d[k] for k in ("name", "description", "main", "module", "bin", "type", "workspaces") if k in d}
    if "scripts" in d:
        keep["scripts"] = d["scripts"]
    for k in ("dependencies", "peerDependencies"):
        if k in d and isinstance(d[k], dict):
            keep[k] = sorted(d[k].keys())
    if "devDependencies" in d and isinstance(d["devDependencies"], dict):
        keep["devDependencies(names)"] = sorted(d["devDependencies"].keys())[:40]
    return json.dumps(keep, indent=0)[:4000]


def _summarize_pom(text: str) -> str:
    arts = re.findall(r"<artifactId>([^<]+)</artifactId>", text)
    groups = re.findall(r"<groupId>([^<]+)</groupId>", text)[:3]
    mods = re.findall(r"<module>([^<]+)</module>", text)
    parts = [f"groupId: {', '.join(groups)}", f"artifacts/dependencies: {', '.join(arts[:80])}"]
    if mods:
        parts.append(f"modules: {', '.join(mods)}")
    return "\n".join(parts)


def _summarize_makefile(text: str) -> str:
    targets = re.findall(r"^([A-Za-z0-9_.\-/]+):(?!=)", text, re.M)
    return "targets: " + ", ".join(dict.fromkeys(targets))[:1500]


def _summarize_openapi(text: str) -> str:
    ops = []
    if text.lstrip().startswith("{"):
        try:
            d = json.loads(text)
            for p, methods in (d.get("paths") or {}).items():
                for m in methods:
                    if m.lower() in ("get", "post", "put", "patch", "delete"):
                        ops.append(f"{m.upper()} {p}")
        except Exception:
            pass
    else:
        cur = None
        for line in text.splitlines():
            m = re.match(r"^  (/[^:]*):\s*$", line)
            if m:
                cur = m.group(1)
                continue
            m = re.match(r"^    (get|post|put|patch|delete):", line)
            if m and cur:
                ops.append(f"{m.group(1).upper()} {cur}")
    return "operations: " + "; ".join(ops[:300]) if ops else text[:2000]


def collect_manifests(repo: str, paths: list[str], per_file: int = 3500, total: int = 24000) -> list[dict]:
    out, used = [], 0
    # shallow ones first
    for rel in sorted(paths, key=lambda p: (p.count("/"), p)):
        text = read_text(os.path.join(repo, rel), 400_000)
        if not text:
            continue
        base = os.path.basename(rel)
        if base == "package.json":
            body = _summarize_package_json(text)
        elif base == "pom.xml":
            body = _summarize_pom(text)
        elif base == "Makefile":
            body = _summarize_makefile(text)
        elif langs.OPENAPI_HINT.search(base):
            body = _summarize_openapi(text)
        elif base.endswith(".sln"):
            body = "\n".join(re.findall(r'^Project\([^)]*\)\s*=\s*"([^"]+)"', text, re.M))
        else:
            body = text
        body = body.strip()[:per_file]
        if used + len(body) > total:
            break
        used += len(body)
        out.append({"path": rel, "content": body})
    return out


def collect_readme(repo: str, all_files: list[str], limit: int = 3500) -> str:
    cands = [f for f in all_files if "/" not in f and re.match(r"readme(\.|$)", f, re.I)]
    if not cands:
        cands = [f for f in all_files if f.count("/") == 1 and re.search(r"/readme(\.|$)", f, re.I)]
    if not cands:
        return ""
    text = read_text(os.path.join(repo, sorted(cands, key=len)[0]), 200_000) or ""
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)          # images
    text = re.sub(r"<img[^>]*>", "", text)
    text = re.sub(r"\[!\[.*?\]\(.*?\)\]\(.*?\)", "", text)    # badges
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()[:limit]


def dir_tree(files: list[FileInfo], max_lines: int = 120) -> str:
    """Directory summary: dir -> number of source files and lines, up to depth 4."""
    agg = defaultdict(lambda: [0, 0])
    for f in files:
        parts = f.path.split("/")[:-1]
        for depth in range(0, min(len(parts), 4) + 1):
            key = "/".join(parts[:depth]) or "."
            agg[key][0] += 1
            agg[key][1] += f.lines
    keys = sorted(agg)
    if len(keys) > max_lines:
        # keep the shallowest/biggest directories
        keys = sorted(keys, key=lambda k: (k.count("/"), -agg[k][1]))[:max_lines]
        keys.sort()
    lines = []
    for k in keys:
        depth = 0 if k == "." else k.count("/") + 1
        name = k if k == "." else k.split("/")[-1] + "/"
        lines.append(f"{'  ' * depth}{name} ({agg[k][0]} files, {agg[k][1]} lines)")
    return "\n".join(lines)


def summarize_skips(skipped: Counter) -> str:
    return ", ".join(f"{k}: {v}" for k, v in skipped.most_common())


__all__ = ["FileInfo", "inventory", "one_line", "summarize_skips"]
