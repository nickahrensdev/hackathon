"""`crux.py auto INPUT`: the unattended path behind the repo-crux skill. It never prompts.

INPUT is either
  * a local repository: an existing path, or just a folder name that is searched for in the
    usual code folders (cwd, its parents, ~/code, ~/src, ~/projects, ~/IdeaProjects, ...), or
  * a git URL: Bitbucket Cloud (https://bitbucket.org/ws/repo[/src/branch|/branch/name]),
    Bitbucket Server/Data Center (https://host/projects/P/repos/r[/browse][?at=refs/heads/b]),
    or any other https/ssh git URL (GitHub, GitLab, ...).

Steps: resolve or clone (non-interactive git) -> pick a model backend -> estimate and fit the run
under a cost cap by trimming files -> full run -> write analysis.json + summary.md -> print a
`CRUX_RESULT {...}` line. Exit codes: 0 ok, 2 repo not found / clone failed, 3 requests waiting
for an agent to answer (files backend), 4 other failure.

Environment knobs (all optional): CRUX_BACKEND, CRUX_MAX_COST (USD, default 15), CRUX_HOME
(default ~/.repo-crux), CRUX_RESULTS_DIR (default ~/repo-crux-results/<repo>), CRUX_SEARCH_ROOTS
(colon-separated extra folders to search), BITBUCKET_TOKEN or BITBUCKET_USERNAME +
BITBUCKET_APP_PASSWORD (private clones without stored git credentials), CRUX_CLONE_DEPTH.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import urllib.parse
from dataclasses import dataclass, field

from .util import log, warn, write_json

DEFAULT_MAX_COST = 15.0
SKIP_DIRS = {"node_modules", ".git", "Library", ".Trash", "venv", ".venv", "dist", "build", "target", "out",
             "Applications", "Movies", "Music", "Pictures", ".cache", ".npm", ".gradle", ".m2", ".cargo", ".rustup",
             "Pods", "DerivedData", "__pycache__", ".idea", ".vscode", "site-packages", ".sdkman", ".local"}
CODE_ROOTS = ["code", "Code", "src", "dev", "Dev", "projects", "Projects", "repos", "Repos", "git", "Git",
              "workspace", "Workspace", "workspaces", "IdeaProjects", "Developer", "work", "Work", "go/src",
              "source", "Source", "sources", "github", "GitHub", "bitbucket", "Bitbucket", "Sites"]
SLOW_ROOTS = ["Documents", "Desktop", "Downloads"]   # macOS may guard these; searched last


# ----------------------------------------------------------------------------------------
@dataclass
class RepoSpec:
    kind: str                       # "local" | "git"
    raw: str
    path: str = ""                  # local path (kind=local) or clone destination (kind=git)
    name: str = ""
    clone_urls: list = field(default_factory=list)   # candidates, tried in order
    branch: str = ""
    host: str = ""
    server: bool = False            # Bitbucket Server / Data Center


def _clean(s: str) -> str:
    return s.strip().strip("'\"").strip()


def _slug(*parts: str) -> str:
    s = "-".join(p for p in parts if p)
    return re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-")[:120] or "repo"


def is_url(s: str) -> bool:
    s = _clean(s)
    return bool(re.match(r"^(https?://|ssh://|git@|git://)", s)) or (s.endswith(".git") and ":" in s)


def parse_git_url(raw: str) -> RepoSpec:
    """Turn a browser or clone URL into ordered clone candidates (+ branch when the URL names one)."""
    s = _clean(raw)
    spec = RepoSpec(kind="git", raw=s)
    m = re.match(r"^git@([^:]+):(.+?)(\.git)?/?$", s)
    if m:  # scp-like ssh
        host, path = m.group(1), m.group(2)
        spec.host = host
        parts = [p for p in path.split("/") if p]
        spec.name = _slug(*parts[-2:])
        spec.clone_urls = [s if s.endswith(".git") else s + ".git"]
        if host == "bitbucket.org" and len(parts) >= 2:
            spec.clone_urls.append(f"https://bitbucket.org/{parts[0]}/{parts[1]}.git")
        return spec
    if s.startswith("ssh://") or s.startswith("git://"):
        u = urllib.parse.urlparse(s)
        parts = [p for p in u.path.split("/") if p]
        spec.host = u.hostname or ""
        spec.name = _slug(*[re.sub(r"\.git$", "", p) for p in parts[-2:]])
        spec.clone_urls = [s]
        return spec
    u = urllib.parse.urlparse(s)
    host = (u.hostname or "").lower()
    spec.host = host
    userinfo = (u.username + "@") if u.username else ""
    port = f":{u.port}" if u.port else ""
    parts = [urllib.parse.unquote(p) for p in u.path.split("/") if p]
    query = urllib.parse.parse_qs(u.query)
    # --- Bitbucket Server / Data Center: /projects/P/repos/r/...  or /users/u/repos/r/...  or /scm/p/r.git
    if len(parts) >= 4 and parts[0] in ("projects", "users") and parts[2] == "repos":
        owner = parts[1] if parts[0] == "projects" else "~" + parts[1]
        repo = re.sub(r"\.git$", "", parts[3])
        spec.server = True
        spec.name = _slug(parts[1], repo)
        spec.clone_urls = [f"{u.scheme}://{userinfo}{host}{port}/scm/{owner.lower()}/{repo}.git",
                           f"ssh://git@{host}:7999/{owner.lower()}/{repo}.git"]
        at = (query.get("at") or [""])[0]
        if at:
            spec.branch = re.sub(r"^refs/heads/", "", at)
        return spec
    if len(parts) >= 3 and parts[0] == "scm":
        spec.server = True
        repo = re.sub(r"\.git$", "", parts[2])
        spec.name = _slug(parts[1].lstrip("~"), repo)
        spec.clone_urls = [f"{u.scheme}://{userinfo}{host}{port}/scm/{parts[1]}/{repo}.git",
                           f"ssh://git@{host}:7999/{parts[1]}/{repo}.git"]
        return spec
    if len(parts) < 2:
        raise ValueError(f"not a repository URL: {raw}")
    owner, repo = parts[0], re.sub(r"\.git$", "", parts[1])
    if host == "gitlab.com" and len(parts) > 2 and "-" in parts:      # gitlab group/subgroup/repo/-/tree/b
        dash = parts.index("-")
        owner, repo = "/".join(parts[:dash - 1]), parts[dash - 1]
    spec.name = _slug(owner.split("/")[-1], repo)
    rest = parts[2:]
    # branch hints in browser URLs
    if host.endswith("bitbucket.org"):
        if len(rest) >= 2 and rest[0] == "branch":
            spec.branch = "/".join(rest[1:])
        elif len(rest) == 2 and rest[0] == "src":
            spec.branch = rest[1]      # only when nothing follows, otherwise branch vs path is ambiguous
        at = (query.get("at") or [""])[0]
        if at and not spec.branch:
            spec.branch = re.sub(r"^refs/heads/", "", at)
    elif "tree" in rest:
        i = rest.index("tree")
        if i + 1 < len(rest) and len(rest) == i + 2:
            spec.branch = rest[i + 1]
    spec.clone_urls = [f"https://{userinfo}{host}{port}/{owner}/{repo}.git"]
    if host == "bitbucket.org":
        spec.clone_urls.append(f"git@bitbucket.org:{owner}/{repo}.git")
    elif host in ("github.com", "gitlab.com"):
        spec.clone_urls.append(f"git@{host}:{owner}/{repo}.git")
    return spec


def _mask(s: str) -> str:
    return re.sub(r"://[^/@\s]+@", "://***@", s)


def _git_env() -> dict:
    env = dict(os.environ)
    env.update({
        "GIT_TERMINAL_PROMPT": "0",          # never ask for a username/password on the terminal
        "GIT_ASKPASS": "echo",               # ...or through a GUI askpass helper
        "SSH_ASKPASS": "echo",
        "GCM_INTERACTIVE": "never",          # Git Credential Manager: no dialogs
        "GIT_SSH_COMMAND": "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20",
    })
    return env


def _token_variants(spec: RepoSpec) -> list[tuple[str, list[str]]]:
    """Extra (url, git -c args) attempts using tokens from the environment, never printed."""
    out = []
    https = [u for u in spec.clone_urls if u.startswith("https://")]
    if not https or "bitbucket" not in spec.host:
        return out
    base = re.sub(r"^https://[^/@]+@", "https://", https[0])
    tok = os.environ.get("BITBUCKET_TOKEN") or os.environ.get("BITBUCKET_ACCESS_TOKEN")
    user = os.environ.get("BITBUCKET_USERNAME") or os.environ.get("BITBUCKET_USER")
    pw = os.environ.get("BITBUCKET_APP_PASSWORD") or os.environ.get("BITBUCKET_API_TOKEN")
    if tok:
        if spec.server:
            out.append((base, ["-c", f"http.extraHeader=Authorization: Bearer {tok}"]))
        else:
            out.append((base.replace("https://", f"https://x-token-auth:{urllib.parse.quote(tok, safe='')}@"), []))
    if user and pw:
        out.append((base.replace("https://", f"https://{urllib.parse.quote(user, safe='')}:{urllib.parse.quote(pw, safe='')}@"), []))
    return out


def _run_git(args: list[str], timeout: int = 900) -> tuple[int, str]:
    try:
        r = subprocess.run(["git", *args], capture_output=True, text=True, timeout=timeout, env=_git_env(),
                           stdin=subprocess.DEVNULL)
        return r.returncode, (r.stderr or r.stdout or "").strip()
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    except FileNotFoundError:
        return 127, "git is not installed"


def clone_or_update(spec: RepoSpec, repos_dir: str) -> str:
    depth = os.environ.get("CRUX_CLONE_DEPTH", "300")
    dest = os.path.join(repos_dir, spec.name)
    if os.path.isdir(os.path.join(dest, ".git")):
        log(f"repo cache hit: {dest}; fetching latest")
        fetch = ["-C", dest, "fetch", "--prune", f"--depth={depth}", "origin"] + ([spec.branch] if spec.branch else [])
        code, msg = _run_git(fetch)
        if code == 0:
            code2, msg2 = _run_git(["-C", dest, "checkout", "--force", "--detach", "FETCH_HEAD"])
            if code2 != 0:
                warn(f"could not move to the fetched commit ({msg2[:200]}); using the cached checkout")
        else:
            warn(f"fetch failed ({_mask(msg)[:200]}); analyzing the cached checkout")
        return dest
    os.makedirs(repos_dir, exist_ok=True)
    attempts = [(u, []) for u in spec.clone_urls] + _token_variants(spec)
    errors = []
    branch = spec.branch
    for url, extra in attempts:
        for use_branch in ([branch, ""] if branch else [""]):
            tmp = f"{dest}.partial-{int(time.time() * 1000)}"
            args = [*extra, "clone", "--quiet", f"--depth={depth}", "--no-tags"]
            if use_branch:
                args += ["--branch", use_branch, "--single-branch"]
            args += [url, tmp]
            log(f"cloning {_mask(url)}" + (f" (branch {use_branch})" if use_branch else ""))
            code, msg = _run_git(args)
            if code == 0:
                os.replace(tmp, dest)
                if branch and not use_branch:
                    warn(f"branch '{branch}' was not found; analyzed the default branch instead")
                spec.branch = use_branch
                return dest
            last = _mask(msg).splitlines()[-1] if msg else str(code)
            if use_branch and re.search(r"not found in upstream|Could not find remote branch|couldn't find remote ref", msg, re.I):
                continue   # the URL named a branch that doesn't exist: fall back to the default branch
            break
        errors.append(f"{_mask(url)}: {last}")
        if re.search(r"not found in upstream|Could not find remote branch", last, re.I):
            continue
    raise RuntimeError("could not clone the repository without credentials prompts:\n  " + "\n  ".join(errors)
                       + "\nPrivate repo? Store git credentials (keychain/SSH key) or set BITBUCKET_TOKEN, or "
                         "BITBUCKET_USERNAME + BITBUCKET_APP_PASSWORD, then run again.")


# ----------------------------------------------------------------------------------------
def _search_roots() -> list[tuple[str, int]]:
    home = os.path.expanduser("~")
    cwd = os.getcwd()
    roots: list[tuple[str, int]] = []
    for extra in filter(None, os.environ.get("CRUX_SEARCH_ROOTS", "").split(":")):
        roots.append((os.path.expanduser(extra), 4))
    roots += [(cwd, 2), (os.path.dirname(cwd), 2), (os.path.dirname(os.path.dirname(cwd)), 2)]
    roots += [(os.path.join(home, r), 4) for r in CODE_ROOTS]
    roots += [(home, 2)]
    roots += [(os.path.join(home, r), 4) for r in SLOW_ROOTS]
    seen, out = set(), []
    for r, d in roots:
        rp = os.path.realpath(r)
        if rp not in seen and os.path.isdir(rp):
            seen.add(rp)
            out.append((rp, d))
    return out


def _looks_like_repo(path: str) -> int:
    if os.path.isdir(os.path.join(path, ".git")):
        return 2
    try:
        names = os.listdir(path)
    except OSError:
        return 0
    markers = {"package.json", "pom.xml", "build.gradle", "build.gradle.kts", "pyproject.toml", "setup.py", "go.mod",
               "Cargo.toml", "Gemfile", "composer.json", "requirements.txt", "src", "app", "lib"}
    return 1 if markers & set(names) else 0


def find_local_repo(name: str, time_budget: float = 25.0) -> str | None:
    """Exact folder-name search (case-insensitive) across common code roots; best = git repo, newest."""
    raw = _clean(name)
    p = os.path.expanduser(raw)
    if os.path.isdir(p):
        return os.path.abspath(p)
    target = os.path.basename(raw.rstrip("/")).lower()
    target = re.sub(r"\.git$", "", target)
    if not target:
        return None
    t0 = time.time()
    hits = []
    for root, max_depth in _search_roots():
        stack = [(root, 0)]
        while stack:
            if time.time() - t0 > time_budget:
                break
            d, depth = stack.pop()
            try:
                with os.scandir(d) as it:
                    entries = [e for e in it if e.is_dir(follow_symlinks=False)]
            except (PermissionError, FileNotFoundError, NotADirectoryError, OSError):
                continue
            for e in entries:
                if e.name in SKIP_DIRS or (e.name.startswith(".") and e.name != ".git"):
                    continue
                if e.name.lower() == target:
                    score = _looks_like_repo(e.path)
                    if score:
                        try:
                            mt = os.stat(os.path.join(e.path, ".git") if score == 2 else e.path).st_mtime
                        except OSError:
                            mt = 0
                        hits.append((score, mt, e.path))
                    continue
                if depth + 1 < max_depth:
                    stack.append((e.path, depth + 1))
        if hits and any(h[0] == 2 for h in hits):
            break   # a git repo found in an earlier (preferred) root wins
    if not hits:
        return None
    hits.sort(key=lambda h: (-h[0], -h[1]))
    if len(hits) > 1:
        log("several matches; using the most recently active git repo: " + hits[0][2])
    return hits[0][2]


# ----------------------------------------------------------------------------------------
def find_claude_bin() -> str | None:
    """`claude` on PATH, else the usual install locations (PATH can be thin inside tool shells)."""
    cands = [os.environ.get("CLAUDE_BIN") or "", shutil.which("claude") or ""]
    home = os.path.expanduser("~")
    cands += [os.path.join(home, ".claude", "local", "claude"), os.path.join(home, ".local", "bin", "claude"),
              "/opt/homebrew/bin/claude", "/usr/local/bin/claude", os.path.join(home, ".npm-global", "bin", "claude")]
    for c in cands:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def choose_backend() -> tuple[str, str]:
    forced = os.environ.get("CRUX_BACKEND")
    if forced:
        return forced, "CRUX_BACKEND"
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            import anthropic  # noqa: F401
            return "anthropic", "ANTHROPIC_API_KEY is set"
        except ImportError:
            warn("ANTHROPIC_API_KEY is set but the anthropic package is missing; trying the claude CLI")
    claude = find_claude_bin()
    if claude:
        try:
            r = subprocess.run([claude, "auth", "status"], capture_output=True, text=True, timeout=30,
                               stdin=subprocess.DEVNULL)
            logged_in = False
            try:
                logged_in = bool(json.loads(r.stdout).get("loggedIn"))
            except ValueError:
                low = r.stdout.lower()
                logged_in = r.returncode == 0 and "logged in" in low and "not logged in" not in low
            if logged_in:
                os.environ["CRUX_CLAUDE_BIN"] = claude
                return "claude-cli", "Claude Code CLI is logged in"
        except (OSError, subprocess.TimeoutExpired):
            pass
    return "files", "no API key or logged-in claude CLI: the calling agent answers the prompts"


# ----------------------------------------------------------------------------------------
def write_summary(result: dict, path: str, extra: dict) -> None:
    r = result
    lines = [f"# repo-crux: {r['repo']['name']}", "",
             f"{r['repo'].get('summary', '')}", "",
             f"- Kind: {r['repo'].get('kind')} · files {r['repo'].get('files_analyzed')} · methods {r['repo'].get('methods_analyzed')}",
             f"- Backend: {extra.get('backend')} ({extra.get('why')}) · cost ${r['run'].get('total_cost_usd', 0):.2f}"
             + (f" · trimmed to the {extra['max_files']} most-active files to stay under ${extra['cap']:.2f}" if extra.get("max_files") else "")
             + (f" · skipped {', '.join(extra['dropped'])} to fit the cap" if extra.get("dropped") else "")
             + (" · no spend limit" if extra.get("cap", 0) is None else ""),
             "- Score distribution (10→1): " + " ".join(f"{k}:{v}" for k, v in r["score_distribution"].items()), "",
             "## Critical flows", ""]
    for f in r["critical_flows"]:
        lines.append(f"- **{f['id']}** [{f['criticality']}] {f['name']} ({len(f['steps'])} steps"
                     + (", traced" if f.get("traced") else "") + ")")
    lines += ["", "## Cruxes", "", "| Score | Method | Where | Why |", "|---|---|---|---|"]
    for c in r["cruxes"]:
        lines.append(f"| {c['score']} | `{c['name']}` | {c['file']}:{c['line']} | {c['why'].replace('|', '/')} |")
    lines += ["", "## Top methods", "", "| Score | Method | Role | Why |", "|---|---|---|---|"]
    for m in r["method_analysis"][:20]:
        lines.append(f"| {m['score']} | `{m['qualified_name']}` | {m['role']} | {m['why'].replace('|', '/')} |")
    lines += ["", "## Top files", ""]
    for fo in r["files"][:15]:
        lines.append(f"- {fo['score']} `{fo['path']}` — {fo.get('summary', '')}")
    if r.get("notes"):
        lines += ["", "## Notes", ""] + [f"- {n}" for n in r["notes"][:10]]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


UNLIMITED_WORDS = {"none", "unlimited", "off", "no", "inf", "infinity", "0"}


def resolve_cap(cfg, no_cost_limit: bool = False) -> float | None:
    """The run's spend cap in USD, or None for no limit.
    Unlimited when --no-cost-limit, --max-cost 0 (or negative), or CRUX_MAX_COST is none/unlimited/off/0."""
    if no_cost_limit:
        return None
    env = (os.environ.get("CRUX_MAX_COST") or "").strip().lower()
    if cfg.max_cost is not None:
        return None if cfg.max_cost <= 0 else float(cfg.max_cost)
    if env:
        if env in UNLIMITED_WORDS:
            return None
        try:
            v = float(env)
        except ValueError:
            log(f"ignoring CRUX_MAX_COST={env!r}; using the ${DEFAULT_MAX_COST:.2f} default")
            return DEFAULT_MAX_COST
        return None if v <= 0 else v
    return DEFAULT_MAX_COST


def run_auto(target: str, scan_kwargs: dict, cfg, repo_out: str | None = None, no_cost_limit: bool = False) -> int:
    from . import scan as scanmod
    from .llm import PendingRequests, make_backend
    from .pipeline import Pipeline
    from .validate import validate_file

    home = os.path.expanduser(os.environ.get("CRUX_HOME", "~/.repo-crux"))
    # 1. repository
    try:
        if is_url(target):
            spec = parse_git_url(target)
            repo = clone_or_update(spec, os.path.join(home, "repos"))
            name = spec.name
        else:
            repo = find_local_repo(target)
            if not repo:
                print(f"CRUX_ERROR repository '{target}' not found. Looked for a folder with that name under: "
                      + ", ".join(r for r, _ in _search_roots()[:12]) + " ... Pass a full path or set CRUX_SEARCH_ROOTS.")
                return 2
            name = _slug(os.path.basename(repo.rstrip("/")))
    except (ValueError, RuntimeError) as e:
        print(f"CRUX_ERROR {e}")
        return 2
    log(f"repository: {repo}")
    work = os.path.join(home, "work", name)
    out_dir = os.path.expanduser(repo_out or os.environ.get("CRUX_RESULTS_DIR") or os.path.join("~", "repo-crux-results", name))
    os.makedirs(out_dir, exist_ok=True)
    # 2. backend
    backend_name, why = choose_backend()
    cfg.backend = backend_name
    log(f"backend: {backend_name} ({why})")
    # 3. budget: estimate with mock answers, then fit under the cap without asking anyone:
    #    trim to the most-active files, then drop tracing, then calibration; stop only if nothing fits.
    #    With no cap, everything runs and the estimate is only logged.
    cap = resolve_cap(cfg, no_cost_limit)
    sc = scanmod.scan(repo, **scan_kwargs)
    parts = estimate_parts(sc, cfg, work)
    est = sum(parts.values())
    max_files, dropped = None, []
    for _ in range(5):
        if cap is None or est <= cap:
            break
        fixed = parts.get("map", 0) + parts.get("trace", 0) + parts.get("calibrate", 0)
        score = parts.get("score", 0)
        room = cap * 0.9 - fixed
        if room <= 0 or len(sc.files) <= 10:
            if cfg.trace != "never" and parts.get("trace"):
                cfg.trace = "never"
                dropped.append("trace")
            elif cfg.calibrate and parts.get("calibrate"):
                cfg.calibrate = False
                dropped.append("calibrate")
            else:
                break
        else:
            n = max(10, int(len(sc.files) * room / max(score, 1e-9)))
            if n >= len(sc.files):
                n = max(10, len(sc.files) // 2)
            max_files = n
            sc = scanmod.scan(repo, **{**scan_kwargs, "max_files": max_files})
        parts = estimate_parts(sc, cfg, work)
        est = sum(parts.values())
        log(f"fitting the ${cap:.2f} cap: {len(sc.files)} files" + (f", skipping {', '.join(dropped)}" if dropped else "")
            + f" -> estimate ${est:.2f}")
    if cap is None:
        log(f"estimated cost ${est:.2f} (no spend limit: all {len(sc.files)} files, every phase)")
        cfg.max_cost = None        # the pipeline never aborts on cost
    else:
        if est > cap:
            print(f"CRUX_ERROR the smallest useful run is estimated at ${est:.2f}, above the ${cap:.2f} cap. "
                  "Raise CRUX_MAX_COST, pass --max-cost, or pass --no-cost-limit.")
            return 4
        log(f"estimated cost ${est:.2f} (cap ${cap:.2f})")
        cfg.max_cost = cap * 1.3   # hard stop inside the pipeline if reality runs well past the estimate
    # 4. run
    write_json(os.path.join(work, "scan.json"), sc.to_json())
    backend = make_backend(backend_name, work, cfg.concurrency, cfg.batch, os.environ.get("CRUX_CLAUDE_BIN"))
    pipe = Pipeline(sc, cfg, backend, work)
    try:
        result = pipe.run()
    except PendingRequests as p:
        print(f"CRUX_PENDING {json.dumps({'index': p.index, 'count': len(p.paths), 'work': work, 'responses_dir': os.path.join(work, 'responses')})}")
        return 3
    except SystemExit as e:
        print(f"CRUX_ERROR {e}")
        return 4
    out = os.path.join(out_dir, "analysis.json")
    if os.path.exists(out):
        os.replace(out, os.path.join(out_dir, "analysis.prev.json"))
    write_json(out, result)
    problems = validate_file(out)
    summary = os.path.join(out_dir, "summary.md")
    extra = {"backend": backend_name, "why": why, "max_files": max_files, "cap": cap, "dropped": dropped}
    write_summary(result, summary, extra)
    print("CRUX_RESULT " + json.dumps({
        "repo": repo, "analysis": out, "summary": summary, "backend": backend_name,
        "cost_usd": result["run"].get("total_cost_usd"), "files": len(result["files"]),
        "methods": len(result["method_analysis"]), "flows": len(result["critical_flows"]),
        "cruxes": len(result["cruxes"]), "cost_cap_usd": cap, "estimated_cost_usd": round(est, 2),
        "trimmed_to_files": max_files, "skipped_phases": dropped,
        "schema_problems": problems[:5]}))
    return 0


def estimate_parts(sc, cfg, work: str) -> dict:
    """Per-phase cost estimate for a full run, using mock answers (no model calls)."""
    import copy
    from . import util
    from .llm import MockBackend
    from .pipeline import Pipeline
    c = copy.copy(cfg)
    c.max_cost = None
    pipe = Pipeline(sc, c, MockBackend(), os.path.join(work, "estimate"), use_cache=False)
    was = util._VERBOSE
    util.set_verbose(False)
    try:
        pipe.run()
    finally:
        util.set_verbose(was)
    return {ph: pipe.estimate_cost(pipe.requests_log.get(ph, [])) for ph in ("map", "trace", "score", "calibrate")}


def estimate_total(sc, cfg, work: str) -> float:
    return sum(estimate_parts(sc, cfg, work).values())
