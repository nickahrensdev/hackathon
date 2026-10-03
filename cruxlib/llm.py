"""LLM backends. Each one takes a list of Requests and returns structured JSON per request.

  anthropic   Claude API (pip install anthropic, ANTHROPIC_API_KEY). Parallel, prompt-cached,
              structured outputs; --batch uses the Message Batches API (50% cheaper).
  claude-cli  Shells out to `claude -p` (Claude Code). Uses your Claude Code login, no API key.
  files       Writes each request to disk and reads answers back. Lets any agent (Claude Code
              subagents, another LLM, a human) act as the model. Re-run to continue.
  mock        Deterministic fake answers; used by `estimate` and the tests.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from .util import est_tokens, extract_json_object, log, sha256, warn

# $ per million tokens: (input, output, cache-read multiplier). Cache writes: 1.25x (5m) / 2x (1h).
PRICES = {
    "claude-fable-5-1": (10.0, 50.0, 0.025),
    "claude-mythos-5-1": (10.0, 50.0, 0.025),
    "claude-opus-5-5": (4.0, 20.0, 0.05),
    "claude-sonnet-5-5": (2.0, 10.0, 0.1),
    "claude-haiku-4-5": (1.0, 5.0, 0.1),
}
MAX_OUTPUT = {"claude-haiku-4-5": 64000}


def price_for(model: str):
    for k, v in PRICES.items():
        if model.startswith(k):
            return v
    return None


def min_cacheable(model: str) -> int:
    """Shortest prompt prefix the API will cache, per model family (shorter prefixes just aren't cached)."""
    if model.startswith("claude-haiku-4-5"):
        return 4096
    if model.startswith(("claude-opus-5", "claude-sonnet-5-5", "claude-fable-5", "claude-mythos-5")):
        return 512
    return 1024


def supports_effort(model: str) -> bool:
    return "haiku" not in model


def max_output(model: str) -> int:
    for k, v in MAX_OUTPUT.items():
        if model.startswith(k):
            return v
    return 128000


def cost_usd(model: str, usage: dict, batch: bool = False, cache_ttl: str = "5m") -> float:
    p = price_for(model)
    if not p:
        return 0.0
    pin, pout, rmult = p
    wmult = 2.0 if cache_ttl == "1h" else 1.25
    c = (usage.get("input_tokens", 0) * pin + usage.get("output_tokens", 0) * pout
         + usage.get("cache_write_tokens", 0) * pin * wmult + usage.get("cache_read_tokens", 0) * pin * rmult) / 1e6
    return c * (0.5 if batch else 1.0)


@dataclass
class Request:
    id: str
    phase: str
    model: str
    system: list
    user: str
    schema: dict
    max_tokens: int = 16000
    effort: str | None = None
    meta: dict = field(default_factory=dict)

    def key(self) -> str:
        return sha256(json.dumps([self.model, self.system, self.user, self.schema, self.effort], sort_keys=True))

    def input_tokens_est(self) -> int:
        return sum(est_tokens(s) for s in self.system) + est_tokens(self.user) + est_tokens(json.dumps(self.schema))


class PendingRequests(Exception):
    def __init__(self, paths: list[str], index: str):
        super().__init__(f"{len(paths)} requests waiting for answers")
        self.paths = paths
        self.index = index


class LLMError(Exception):
    pass


def _norm_usage(u) -> dict:
    if u is None:
        return {}
    g = (lambda k: getattr(u, k, None)) if not isinstance(u, dict) else u.get
    return {
        "input_tokens": g("input_tokens") or 0,
        "output_tokens": g("output_tokens") or 0,
        "cache_read_tokens": g("cache_read_input_tokens") or 0,
        "cache_write_tokens": g("cache_creation_input_tokens") or 0,
    }


def schema_instructions(schema: dict) -> str:
    return ("\n\nRespond with ONLY a JSON object (no prose, no code fences) that validates against this JSON Schema:\n"
            + json.dumps(schema, separators=(",", ":")))


class Backend:
    name = "base"
    batch = False

    def run(self, requests: list[Request], on_result) -> None:
        raise NotImplementedError

    @staticmethod
    def _warm_then_parallel(requests, fn, on_result, concurrency):
        """Send the first request of each shared-prefix group alone so its prompt-cache entry
        exists before the fan-out (cache entries are only readable once a response has begun)."""
        groups = {}
        for r in requests:
            groups.setdefault(sha256(json.dumps(r.system)), []).append(r)
        first = [g[0] for g in groups.values() if len(g) > 1]
        first_ids = {id(r) for r in first}
        rest = [r for r in requests if id(r) not in first_ids]
        errors = []

        def do(r):
            try:
                on_result(r, fn(r))
            except Exception as e:  # keep going; the pipeline falls back per request
                errors.append((r.id, e))
                on_result(r, {"error": f"{type(e).__name__}: {e}"})

        with ThreadPoolExecutor(max_workers=max(1, concurrency)) as ex:
            list(ex.map(do, first))
            futs = [ex.submit(do, r) for r in rest]
            for f in as_completed(futs):
                f.result()
        for rid, e in errors[:5]:
            warn(f"request {rid} failed: {e}")


# ----------------------------------------------------------------------------------------
class AnthropicBackend(Backend):
    name = "anthropic"

    def __init__(self, concurrency: int = 8, batch: bool = False, poll_seconds: int = 20):
        try:
            import anthropic  # noqa: F401
        except ImportError as e:
            raise SystemExit("The anthropic backend needs `pip install anthropic` and ANTHROPIC_API_KEY.") from e
        import anthropic
        self.client = anthropic.Anthropic(max_retries=6)
        self.concurrency = concurrency
        self.batch = batch
        self.poll = poll_seconds
        self.structured = True
        self._lock = threading.Lock()

    def _params(self, r: Request) -> dict:
        system = [{"type": "text", "text": t} for t in r.system if t]
        if system:
            system[-1]["cache_control"] = {"type": "ephemeral", **({"ttl": "1h"} if self.batch else {})}
        oc = {}
        if self.structured:
            oc["format"] = {"type": "json_schema", "schema": r.schema}
        if r.effort and supports_effort(r.model):
            oc["effort"] = r.effort
        user = r.user if self.structured else r.user + schema_instructions(r.schema)
        p = {"model": r.model, "max_tokens": min(r.max_tokens, max_output(r.model)), "system": system,
             "messages": [{"role": "user", "content": user}]}
        if oc:
            p["output_config"] = oc
        return p

    def _parse(self, r: Request, msg) -> dict:
        text = "".join(getattr(b, "text", "") for b in msg.content if getattr(b, "type", "") == "text")
        stop = getattr(msg, "stop_reason", None)
        if stop == "max_tokens":
            raise LLMError("max_tokens")
        if stop == "refusal":
            raise LLMError("refusal")
        data = extract_json_object(text)   # plain json.loads first; tolerant of fences if structured outputs are off
        usage = _norm_usage(getattr(msg, "usage", None))
        return {"data": data, "usage": usage, "model": r.model, "stop_reason": stop,
                "cost_usd": cost_usd(r.model, usage, self.batch, "1h" if self.batch else "5m")}

    def _call(self, r: Request) -> dict:
        import anthropic
        p = self._params(r)
        for attempt in range(3):
            try:
                # Stream: long thinking/outputs exceed the SDK's 10-minute non-streaming guard, and
                # streaming keeps the connection alive. get_final_message() returns a normal Message.
                try:
                    with self.client.messages.stream(**p) as stream:
                        msg = stream.get_final_message()
                except TypeError:  # older SDK without native output_config
                    q = dict(p)
                    oc = q.pop("output_config", None)
                    with self.client.messages.stream(**q, extra_body={"output_config": oc} if oc else None) as stream:
                        msg = stream.get_final_message()
                return self._parse(r, msg)
            except LLMError as e:
                if str(e) == "max_tokens" and p["max_tokens"] < max_output(r.model):
                    p["max_tokens"] = min(max_output(r.model), p["max_tokens"] * 2)
                    continue
                raise
            except anthropic.BadRequestError as e:
                if self.structured and re.search(r"output_config|format|schema", str(e), re.I):
                    with self._lock:
                        if self.structured:
                            warn("structured outputs rejected for this model/schema; falling back to prompted JSON")
                            self.structured = False
                    p = self._params(r)
                    continue
                raise
        raise LLMError("retries exhausted")

    def run(self, requests, on_result):
        if not requests:
            return
        if self.batch and len(requests) > 1:
            return self._run_batch(requests, on_result)
        self._warm_then_parallel(requests, self._call, on_result, self.concurrency)

    def _run_batch(self, requests, on_result):
        ids = {}
        reqs = []
        for i, r in enumerate(requests):
            cid = re.sub(r"[^A-Za-z0-9_-]", "_", r.id)[:56] + f"_{i}"
            ids[cid] = r
            reqs.append({"custom_id": cid, "params": self._params(r)})
        batch = self.client.messages.batches.create(requests=reqs)
        log(f"batch {batch.id}: {len(reqs)} requests submitted; polling every {self.poll}s")
        while True:
            b = self.client.messages.batches.retrieve(batch.id)
            if b.processing_status == "ended":
                break
            c = b.request_counts
            log(f"batch {batch.id}: processing={c.processing} succeeded={c.succeeded} errored={c.errored}")
            time.sleep(self.poll)
        retry = []
        for res in self.client.messages.batches.results(batch.id):
            r = ids.get(res.custom_id)
            if r is None:
                continue
            if res.result.type == "succeeded":
                try:
                    on_result(r, self._parse(r, res.result.message))
                    continue
                except Exception as e:  # noqa: BLE001
                    warn(f"batch result {r.id} unparseable ({e}); retrying directly")
            retry.append(r)
        if retry:
            self._warm_then_parallel(retry, self._call, on_result, self.concurrency)


# ----------------------------------------------------------------------------------------
class ClaudeCliBackend(Backend):
    name = "claude-cli"

    def __init__(self, concurrency: int = 4, claude_bin: str | None = None, timeout: int = 1200, extra_args=None):
        self.bin = claude_bin or shutil.which("claude")
        if not self.bin:
            raise SystemExit("claude-cli backend: `claude` (Claude Code) not found on PATH.")
        self.concurrency = concurrency
        self.timeout = timeout
        self.extra = extra_args or []

    def _call(self, r: Request) -> dict:
        with tempfile.TemporaryDirectory(prefix="crux-") as d:
            sp = os.path.join(d, "system.md")
            with open(sp, "w", encoding="utf-8") as f:
                f.write("\n\n".join(s for s in r.system if s))
            cmd = [self.bin, "-p", "--output-format", "json", "--model", r.model, "--tools", "",
                   "--strict-mcp-config", "--no-session-persistence", "--system-prompt-file", sp,
                   "--json-schema", json.dumps(r.schema, separators=(",", ":"))]
            if r.effort and supports_effort(r.model):
                cmd += ["--effort", r.effort]
            cmd += self.extra
            env = dict(os.environ)
            if not supports_effort(r.model) and not os.environ.get("CRUX_HAIKU_THINKING"):
                env["MAX_THINKING_TOKENS"] = "0"   # same as the API backend: no thinking on the bulk scoring tier
            # run in an empty dir so the target repo's CLAUDE.md / hooks / MCP config are not loaded
            proc = subprocess.run(cmd, input=r.user, capture_output=True, text=True, cwd=d, timeout=self.timeout, env=env)
        if proc.returncode != 0 and not proc.stdout.strip():
            raise LLMError(f"claude exited {proc.returncode}: {proc.stderr.strip()[:300]}")
        out = json.loads(proc.stdout)
        if out.get("is_error"):
            raise LLMError(f"claude error: {str(out.get('result'))[:300]}")
        data = out.get("structured_output")
        if not isinstance(data, dict):
            data = extract_json_object(out.get("result") or "")
        usage = _norm_usage(out.get("usage"))
        return {"data": data, "usage": usage, "model": r.model, "cost_usd": float(out.get("total_cost_usd") or 0.0),
                "stop_reason": out.get("stop_reason")}

    def run(self, requests, on_result):
        if requests:
            self._warm_then_parallel(requests, self._call, on_result, self.concurrency)


# ----------------------------------------------------------------------------------------
FILES_README = """# repo-crux: requests waiting for a model

Each `requests/<id>.md` file is a complete, self-contained prompt. To fulfil one, act as the
model: read the SYSTEM and USER sections and write ONLY the JSON answer (validating against the
schema shown) to `responses/<id>.json`. Then re-run the same `crux.py run ...` command; it
picks up the answers and continues with the next phase.

Requests are independent, so they can be answered in parallel (e.g. one subagent per request,
or per batch of requests). Answer the `map` request before anything else exists; later phases
are only generated once earlier answers are in.

Pending:
"""


class FilesBackend(Backend):
    name = "files"

    def __init__(self, work_dir: str):
        self.req_dir = os.path.join(work_dir, "requests")
        self.resp_dir = os.path.join(work_dir, "responses")
        os.makedirs(self.req_dir, exist_ok=True)
        os.makedirs(self.resp_dir, exist_ok=True)

    def run(self, requests, on_result):
        pending = []
        for r in requests:
            resp = os.path.join(self.resp_dir, f"{r.id}.json")
            if os.path.exists(resp):
                try:
                    with open(resp, encoding="utf-8") as f:
                        data = extract_json_object(f.read())
                    usage = {"input_tokens": r.input_tokens_est(), "output_tokens": est_tokens(json.dumps(data))}
                    on_result(r, {"data": data, "usage": usage, "model": "external", "cost_usd": 0.0})
                    continue
                except Exception as e:  # noqa: BLE001
                    warn(f"{resp}: unreadable answer ({e}); re-requesting")
            path = os.path.join(self.req_dir, f"{r.id}.md")
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"<!-- repo-crux request {r.id} | phase {r.phase} | suggested model {r.model} -->\n")
                f.write("# SYSTEM\n\n" + "\n\n".join(s for s in r.system if s))
                f.write("\n\n# USER\n\n" + r.user)
                f.write("\n\n# RESPONSE FORMAT\n\nWrite ONLY a JSON object validating against this schema to "
                        f"`responses/{r.id}.json`:\n\n```json\n{json.dumps(r.schema, indent=1)}\n```\n")
            pending.append(path)
        if pending:
            index = os.path.join(os.path.dirname(self.req_dir), "REQUESTS.md")
            with open(index, "w", encoding="utf-8") as f:
                f.write(FILES_README + "\n".join(f"- {os.path.relpath(p, os.path.dirname(index))}" for p in pending) + "\n")
            raise PendingRequests(pending, index)


# ----------------------------------------------------------------------------------------
class MockBackend(Backend):
    """Plausible, schema-valid answers derived from request metadata (no model involved)."""
    name = "mock"

    def run(self, requests, on_result):
        for r in requests:
            data = getattr(self, f"_{r.phase}")(r)
            usage = {"input_tokens": r.input_tokens_est(), "output_tokens": est_tokens(json.dumps(data))}
            on_result(r, {"data": data, "usage": usage, "model": r.model, "cost_usd": cost_usd(r.model, usage)})

    @staticmethod
    def _filler(n=14):
        return " ".join(["consequence"] * n)

    def _map(self, r):
        m = r.meta
        flows, used = [], set()
        for i, ep in enumerate(m.get("entries", [])[:8]):
            if ep["symbol"] in used:
                continue
            used.add(ep["symbol"])
            steps = [ep["symbol"]] + [s for s in ep.get("reach", [])[:5]]
            flows.append({"id": f"F{len(flows) + 1}", "name": f"Flow via {ep['trigger'] or ep['id']}", "criticality": 9 - min(i, 5),
                          "entry_points": [ep["id"]], "steps": steps, "business_impact": self._filler(),
                          "failure_modes": [self._filler(8)], "observable_outcomes": [self._filler(8)],
                          "preconditions": [self._filler(6)], "confidence": "medium"})
            if len(flows) >= 5:
                break
        cruxes = [{"symbol": s, "kind": "core_domain_rule", "why": self._filler(), "flows": ["F1"],
                   "risk_if_broken": self._filler(), "confidence": "medium"} for s in m.get("top_symbols", [])[:6]]
        eps = [{"id": e["id"], "trigger": e["trigger"] or "", "criticality": 7, "flows": ["F1"], "description": self._filler(10)}
               for e in m.get("entries", [])[:40]]
        return {"system_summary": self._filler(30), "repo_kind": "other", "rejected_entry_candidates": [],
                "entry_points": eps, "added_entry_points": [], "critical_flows": flows, "cruxes": cruxes, "notes": []}

    def _trace(self, r):
        shown = r.meta.get("shown", [])
        return {"steps": [{"symbol": s, "what": self._filler(10), "certain": True} for s in shown[:8]],
                "cruxes": [{"symbol": s, "kind": "core_domain_rule", "why": self._filler(), "risk_if_broken": self._filler()}
                           for s in shown[:2]],
                "off_path": shown[10:14], "criticality": 8, "business_impact": self._filler(),
                "observable_outcomes": [self._filler(8)] * 2, "preconditions": [self._filler(6)],
                "failure_modes": [self._filler(8)] * 2, "gaps": []}

    def _score(self, r):
        files = []
        for f in r.meta.get("files", []):
            ms = [{"id": mid, "score": est, "role": "other", "flows": fl, "why": self._filler(), "confidence": "medium"}
                  for mid, est, fl in f["methods"]]
            files.append({"path": f["path"], "score": max([m["score"] for m in ms] or [3]), "summary": self._filler(12),
                          "methods": ms, "extra": []})
        return {"files": files}

    def _calibrate(self, r):
        rows = r.meta.get("rows", [])
        return {"adjustments": [], "notes": [],
                "cruxes": [{"symbol": sid, "kind": "core_domain_rule", "why": self._filler(), "flows": ["F1"],
                            "risk_if_broken": self._filler()} for sid in rows[:5]]}


def make_backend(name: str, work_dir: str, concurrency: int | None = None, batch: bool = False, claude_bin=None):
    if name == "anthropic":
        return AnthropicBackend(concurrency or 8, batch=batch)
    if name == "claude-cli":
        return ClaudeCliBackend(concurrency or 4, claude_bin=claude_bin)
    if name == "files":
        return FilesBackend(work_dir)
    if name == "mock":
        return MockBackend()
    raise SystemExit(f"unknown backend {name!r}")
