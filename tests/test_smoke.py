"""Offline smoke tests: python3 -m pytest tests/ -q   (or: python3 tests/test_smoke.py)

Covers: static scan on a synthetic multi-language repo, the full pipeline with the mock backend,
output schema validation, the files backend round-trip, and the anthropic backend's request /
response handling (single + batch) against a fake HTTP transport. No network or API key needed.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from cruxlib import scan as scanmod  # noqa: E402
from cruxlib.llm import FilesBackend, MockBackend, PendingRequests  # noqa: E402
from cruxlib.pipeline import Config, Pipeline  # noqa: E402
from cruxlib.util import set_verbose  # noqa: E402
from cruxlib.validate import validate  # noqa: E402

set_verbose(False)

FILES = {
    "app/main.py": '''
from flask import Flask, request, jsonify
from app.services.orders import place_order, refund_order
from app.auth import require_user

app = Flask(__name__)


@app.route("/orders", methods=["POST"])
def create_order():
    user = require_user(request.headers.get("Authorization"))
    order = place_order(user, request.json["items"], request.json["card_token"])
    return jsonify(order), 201


@app.post("/orders/<order_id>/refund")
def refund(order_id):
    user = require_user(request.headers.get("Authorization"))
    return jsonify(refund_order(user, order_id))


if __name__ == "__main__":
    app.run()
''',
    "app/auth.py": '''
import jwt

SECRET = "change-me"


def require_user(header):
    if not header or not header.startswith("Bearer "):
        raise PermissionError("missing token")
    claims = jwt.decode(header[7:], SECRET, algorithms=["HS256"])
    return claims["sub"]
''',
    "app/services/orders.py": '''
from app.db import session, Order
from app.payments import charge_card, refund_charge


def place_order(user, items, card_token):
    total = sum(i["price"] * i["qty"] for i in items)
    if total <= 0:
        raise ValueError("empty order")
    charge_id = charge_card(card_token, total)
    order = Order(user=user, total=total, charge_id=charge_id, status="paid")
    session.add(order)
    session.commit()
    return order.to_dict()


def refund_order(user, order_id):
    order = session.get(Order, order_id)
    if order.user != user:
        raise PermissionError("not your order")
    refund_charge(order.charge_id, order.total)
    order.status = "refunded"
    session.commit()
    return order.to_dict()


def format_total(total):
    return f"${total:.2f}"
''',
    "app/payments.py": '''
import requests


def charge_card(token, amount):
    r = requests.post("https://pay.example/charges", json={"token": token, "amount": amount})
    r.raise_for_status()
    return r.json()["id"]


def refund_charge(charge_id, amount):
    r = requests.post(f"https://pay.example/charges/{charge_id}/refund", json={"amount": amount})
    r.raise_for_status()
''',
    "app/db.py": '''
class Order:
    def __init__(self, user, total, charge_id, status):
        self.user = user
        self.total = total
        self.charge_id = charge_id
        self.status = status

    def to_dict(self):
        return {"user": self.user, "total": self.total, "status": self.status}

    def get_status(self):
        return self.status


class _Session:
    def add(self, obj):
        pass

    def commit(self):
        pass

    def get(self, cls, key):
        return None


session = _Session()
''',
    "web/src/api.ts": '''
import axios from "axios";

export const Orders = {
  create: (items: any[], cardToken: string) => axios.post("/orders", { items, card_token: cardToken }),
  refund: (id: string) => axios.post(`/orders/${id}/refund`),
};

export async function checkout(cart: any[], cardToken: string) {
  if (!cart.length) {
    throw new Error("empty cart");
  }
  const res = await Orders.create(cart, cardToken);
  return res.data;
}
''',
    "web/src/server.ts": '''
import express from "express";
import { checkout } from "./api";

const router = express.Router();

router.post("/checkout", async (req, res, next) => {
  try {
    res.json(await checkout(req.body.cart, req.body.token));
  } catch (e) {
    next(e);
  }
});

export default router;
''',
    "svc/src/main/java/com/acme/OrderController.java": '''
package com.acme;

import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api/orders")
public class OrderController {
  private final OrderService orderService;

  public OrderController(OrderService orderService) {
    this.orderService = orderService;
  }

  @PostMapping("/{id}/cancel")
  public String cancel(@PathVariable String id) {
    return orderService.cancel(id);
  }

  public String getName() {
    return name;
  }
}
''',
    "svc/src/main/java/com/acme/OrderService.java": '''
package com.acme;

public interface OrderService {
  String cancel(String id);
}
''',
    "svc/src/main/java/com/acme/OrderServiceImpl.java": '''
package com.acme;

public class OrderServiceImpl implements OrderService {
  @Override
  public String cancel(String id) {
    if (id == null) {
      throw new IllegalArgumentException("id");
    }
    return "cancelled " + id;
  }
}
''',
    "tests/test_orders.py": "def test_x():\n    assert True\n",
}


def make_repo() -> str:
    d = tempfile.mkdtemp(prefix="crux-test-")
    for rel, text in FILES.items():
        p = os.path.join(d, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text.lstrip("\n"))
    return d


def _scan(repo):
    return scanmod.scan(repo)


def test_scan_finds_symbols_entries_and_edges():
    repo = make_repo()
    try:
        sc = _scan(repo)
        names = {s.qualname for s in sc.callables()}
        for expected in ("create_order", "refund", "place_order", "charge_card", "require_user", "Order.to_dict",
                         "checkout", "Orders.create", "OrderController.cancel", "OrderServiceImpl.cancel"):
            assert expected in names, (expected, sorted(names))
        assert "tests/test_orders.py" not in sc.graph.files            # tests skipped by default
        triggers = {e["trigger"] for e in sc.graph.entries}
        assert "POST /orders" in triggers and "POST /checkout" in triggers, triggers
        assert any(t.startswith("POST /api/orders/") for t in triggers), triggers
        by = {s.qualname: s for s in sc.callables()}
        assert by["Order.get_status"].trivial == "accessor"
        assert by["OrderController.getName"].trivial == "accessor"
        assert "money" in by["place_order"].tags and "db" in by["place_order"].tags
        g = sc.graph
        assert by["place_order"].id in g.edges[by["create_order"].id]           # create_order -> place_order
        assert by["charge_card"].id in g.edges[by["place_order"].id]            # place_order -> charge_card
        # interface -> implementation link lets reachability cross DI
        reach = g.reach_from([by["OrderController.cancel"].id], 4)
        assert by["OrderServiceImpl.cancel"].id in reach
        assert "format_total" in names and by["format_total"].fan_in == 0
    finally:
        shutil.rmtree(repo)


def test_mock_pipeline_end_to_end_valid_json():
    repo = make_repo()
    work = tempfile.mkdtemp(prefix="crux-work-")
    try:
        sc = _scan(repo)
        res = Pipeline(sc, Config(backend="mock"), MockBackend(), work, use_cache=False).run()
        assert validate(res) == [], validate(res)[:5]
        assert len(res["method_analysis"]) == len(sc.callables())
        assert all(1 <= m["score"] <= 10 for m in res["method_analysis"])
        assert res["entry_points"] and res["critical_flows"] and res["cruxes"]
    finally:
        shutil.rmtree(repo)
        shutil.rmtree(work)


def test_files_backend_round_trip():
    repo = make_repo()
    work = tempfile.mkdtemp(prefix="crux-work-")
    try:
        sc = _scan(repo)
        mock = MockBackend()
        rounds = 0
        while True:
            rounds += 1
            assert rounds < 10
            pipe = Pipeline(sc, Config(backend="files"), FilesBackend(work), work)
            try:
                res = pipe.run()
                break
            except PendingRequests as p:
                # act as "the agent": answer every pending request file
                reqs = {r.id: r for phase in pipe.requests_log.values() for r in phase}
                for path in p.paths:
                    rid = os.path.basename(path)[:-3]
                    r = reqs[rid]
                    out = {}
                    mock.run([r], lambda req, result: out.setdefault("d", result["data"]))
                    with open(os.path.join(work, "responses", rid + ".json"), "w") as f:
                        json.dump(out["d"], f)
        assert validate(res) == []
        assert rounds >= 3   # map, then score, then calibrate (trace skipped: full-source map)
    finally:
        shutil.rmtree(repo)
        shutil.rmtree(work)


def test_anthropic_backend_against_fake_transport():
    try:
        import anthropic  # noqa: F401
    except ImportError:
        print("skip: anthropic not installed")
        return
    try:  # newer SDKs ship their own httpx fork
        import httpx2 as httpx
    except ImportError:
        import httpx
    from cruxlib.llm import AnthropicBackend
    repo = make_repo()
    work = tempfile.mkdtemp(prefix="crux-work-")
    seen = []
    mock = MockBackend()
    pending = {}

    def answer(body):
        # reconstruct which phase from the system prompt and synthesize a schema-valid answer
        from cruxlib.llm import Request
        sys_text = body["system"][0]["text"]
        phase = ("map" if "reconnaissance" in sys_text else "trace" if "tracing ONE" in sys_text
                 else "calibrate" if "final reviewer" in sys_text else "score")
        r = Request(id="x", phase=phase, model=body["model"], system=[], user="", schema={}, meta=pending[body["messages"][0]["content"][:200]])
        out = {}
        mock.run([r], lambda req, res: out.setdefault("d", res["data"]))
        return out["d"]

    def handler(req: httpx.Request):
        body = json.loads(req.content) if req.content else {}
        if req.url.path == "/v1/messages":
            seen.append(body)
            assert body["output_config"]["format"]["type"] == "json_schema"
            assert body["system"][-1]["cache_control"]["type"] == "ephemeral"
            if "haiku" in body["model"]:
                assert "effort" not in body["output_config"]
            assert body.get("stream") is True
            text = json.dumps(answer(body))
            events = [
                ("message_start", {"type": "message_start", "message": {
                    "id": "msg_1", "type": "message", "role": "assistant", "model": body["model"], "content": [],
                    "stop_reason": None, "stop_sequence": None,
                    "usage": {"input_tokens": 100, "output_tokens": 1, "cache_read_input_tokens": 10,
                              "cache_creation_input_tokens": 0}}}),
                ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
                ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}}),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                                   "usage": {"output_tokens": 50}}),
                ("message_stop", {"type": "message_stop"}),
            ]
            sse = "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in events)
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse.encode())
        return httpx.Response(404, json={"error": "unexpected " + req.url.path})

    try:
        sc = _scan(repo)
        be = AnthropicBackend(concurrency=2)
        be.client = anthropic.Anthropic(api_key="test", base_url="http://fake", max_retries=0,
                                        http_client=httpx.Client(transport=httpx.MockTransport(handler)))
        pipe = Pipeline(sc, Config(), be, work)
        orig = pipe._execute

        def spy(reqs):
            for r in reqs:
                pending[r.user[:200]] = r.meta
            return orig(reqs)
        pipe._execute = spy
        res = pipe.run()
        assert validate(res) == []
        assert seen and res["run"]["total_cost_usd"] > 0
        assert res["run"]["phases"]["score"]["calls"] >= 1
    finally:
        shutil.rmtree(repo)
        shutil.rmtree(work)



def test_anthropic_batch_against_fake_transport():
    try:
        import anthropic  # noqa: F401
    except ImportError:
        print("skip: anthropic not installed")
        return
    try:
        import httpx2 as httpx
    except ImportError:
        import httpx
    from cruxlib.llm import AnthropicBackend, MockBackend as MB, Request
    repo = make_repo()
    work = tempfile.mkdtemp(prefix="crux-work-")
    state = {"batches": {}}
    metas = {}

    def answer(params):
        sys_text = params["system"][0]["text"]
        phase = ("map" if "reconnaissance" in sys_text else "trace" if "tracing ONE" in sys_text
                 else "calibrate" if "final reviewer" in sys_text else "score")
        r = Request(id="x", phase=phase, model=params["model"], system=[], user="", schema={},
                    meta=metas[params["messages"][0]["content"][:200]])
        out = {}
        MB().run([r], lambda req, res: out.setdefault("d", res["data"]))
        return out["d"]

    def batch_obj(bid, ended):
        return {"id": bid, "type": "message_batch", "processing_status": "ended" if ended else "in_progress",
                "request_counts": {"processing": 0, "succeeded": 1, "errored": 0, "canceled": 0, "expired": 0},
                "created_at": "2026-01-01T00:00:00Z", "expires_at": "2026-01-02T00:00:00Z", "ended_at": None,
                "archived_at": None, "cancel_initiated_at": None,
                "results_url": f"http://fake/v1/messages/batches/{bid}/results" if ended else None}

    def handler(req):
        path = req.url.path
        if path == "/v1/messages/batches" and req.method == "POST":
            body = json.loads(req.content)
            bid = f"msgbatch_{len(state['batches'])}"
            state["batches"][bid] = body["requests"]
            for q in body["requests"]:
                assert q["params"]["system"][-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
            return httpx.Response(200, json=batch_obj(bid, False))
        if path.endswith("/results"):
            bid = path.split("/")[-2]
            lines = []
            for q in state["batches"][bid]:
                msg = {"id": "msg", "type": "message", "role": "assistant", "model": q["params"]["model"],
                       "content": [{"type": "text", "text": json.dumps(answer(q["params"]))}], "stop_reason": "end_turn",
                       "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 5}}
                lines.append(json.dumps({"custom_id": q["custom_id"], "result": {"type": "succeeded", "message": msg}}))
            return httpx.Response(200, content=("\n".join(lines) + "\n").encode(), headers={"content-type": "application/x-jsonl"})
        if path.startswith("/v1/messages/batches/"):
            return httpx.Response(200, json=batch_obj(path.split("/")[-1], True))
        if path == "/v1/messages":   # single requests (map/calibrate are single, so not batched)
            body = json.loads(req.content)
            text = json.dumps(answer(body))
            events = [("message_start", {"type": "message_start", "message": {"id": "m", "type": "message", "role": "assistant",
                       "model": body["model"], "content": [], "stop_reason": None, "stop_sequence": None,
                       "usage": {"input_tokens": 10, "output_tokens": 1}}}),
                      ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
                      ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}}),
                      ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                      ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 5}}),
                      ("message_stop", {"type": "message_stop"})]
            sse = "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in events)
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse.encode())
        return httpx.Response(404, json={"error": path})

    try:
        sc = _scan(repo)
        be = AnthropicBackend(concurrency=2, batch=True, poll_seconds=0)
        be.client = anthropic.Anthropic(api_key="test", base_url="http://fake", max_retries=0,
                                        http_client=httpx.Client(transport=httpx.MockTransport(handler)))
        cfg = Config(batch=True, score_chunk_tokens=600)   # small chunks -> several score requests -> batch path
        pipe = Pipeline(sc, cfg, be, work)
        orig = pipe._execute

        def spy(reqs):
            for r in reqs:
                metas[r.user[:200]] = r.meta
            return orig(reqs)
        pipe._execute = spy
        res = pipe.run()
        assert validate(res) == []
        assert state["batches"], "score phase should have used the batch API"
        assert res["run"]["batch"] is True
    finally:
        shutil.rmtree(repo)
        shutil.rmtree(work)


# ---------------------------------------------------------------------------------------- auto / skill path
def test_auto_url_parsing():
    from cruxlib.auto import parse_git_url, is_url, _mask
    s = parse_git_url("https://bitbucket.org/acme/payments-api")
    assert s.clone_urls == ["https://bitbucket.org/acme/payments-api.git", "git@bitbucket.org:acme/payments-api.git"]
    assert s.name == "acme-payments-api" and s.branch == ""
    assert parse_git_url("https://bitbucket.org/acme/payments-api/src/develop/").branch == "develop"
    assert parse_git_url("https://bitbucket.org/acme/payments-api/src/develop/app/main.py").branch == ""
    assert parse_git_url("https://bitbucket.org/acme/payments-api/branch/feature/x").branch == "feature/x"
    assert parse_git_url("https://nick@bitbucket.org/acme/payments-api.git").clone_urls[0] == "https://nick@bitbucket.org/acme/payments-api.git"
    s = parse_git_url("git@bitbucket.org:acme/payments-api.git")
    assert s.clone_urls == ["git@bitbucket.org:acme/payments-api.git", "https://bitbucket.org/acme/payments-api.git"]
    s = parse_git_url("https://bitbucket.acme.com/projects/PAY/repos/payments-api/browse?at=refs%2Fheads%2Frelease")
    assert s.server and s.branch == "release", s
    assert s.clone_urls == ["https://bitbucket.acme.com/scm/pay/payments-api.git", "ssh://git@bitbucket.acme.com:7999/pay/payments-api.git"]
    assert parse_git_url("https://bitbucket.acme.com/scm/pay/payments-api.git").server
    assert parse_git_url("https://github.com/o/r/tree/main").branch == "main"
    g = parse_git_url("https://gitlab.com/group/sub/repo/-/tree/main")
    assert g.clone_urls[0] == "https://gitlab.com/group/sub/repo.git" and g.branch == "main", g
    assert is_url("git@bitbucket.org:a/b.git") and is_url("https://bitbucket.org/a/b") and not is_url("payments-api")
    assert _mask("https://x-token-auth:abc@bitbucket.org/a/b.git") == "https://***@bitbucket.org/a/b.git"


def test_auto_local_lookup():
    import subprocess as sp
    from cruxlib import auto
    home = tempfile.mkdtemp(prefix="crux-home-")
    old_home, old_cwd = os.environ.get("HOME"), os.getcwd()
    try:
        os.makedirs(os.path.join(home, "Documents", "old", "MyRepo", "src"))
        good = os.path.join(home, "code", "team", "myrepo")
        os.makedirs(os.path.join(good, "src"))
        sp.run(["git", "init", "-q", good], check=True)
        os.makedirs(os.path.join(home, "elsewhere"))
        os.environ["HOME"] = home
        os.chdir(os.path.join(home, "elsewhere"))
        assert auto.find_local_repo("myrepo") == os.path.realpath(good)
        assert auto.find_local_repo(good) == good                    # explicit path
        assert auto.find_local_repo("does-not-exist") is None
    finally:
        os.chdir(old_cwd)
        if old_home is not None:
            os.environ["HOME"] = old_home
        shutil.rmtree(home)


def _auto(args, env_extra, timeout=600):
    import subprocess as sp
    env = dict(os.environ, **env_extra)
    r = sp.run([sys.executable, os.path.join(os.path.dirname(HERE), "crux.py"), "auto", *args, "-q"],
               capture_output=True, text=True, env=env, timeout=timeout, stdin=sp.DEVNULL)
    return r.returncode, r.stdout, r.stderr


def test_auto_end_to_end_local_mock_budget_and_pending():
    repo = make_repo()
    home = tempfile.mkdtemp(prefix="crux-auto-")
    try:
        env = {"CRUX_HOME": os.path.join(home, "h"), "CRUX_BACKEND": "mock"}
        code, out, err = _auto([repo, "--results-dir", os.path.join(home, "res")], env)
        assert code == 0, (out, err[-2000:])
        line = [l for l in out.splitlines() if l.startswith("CRUX_RESULT ")][0]
        res = json.loads(line[len("CRUX_RESULT "):])
        assert os.path.exists(res["analysis"]) and os.path.exists(res["summary"]) and not res["schema_problems"]
        # a cap below the estimate -> fewer files / phases instead of asking anyone; an impossible cap -> exit 4
        big = os.path.join(home, "bigrepo")
        for i in range(60):
            os.makedirs(os.path.join(big, "pkg"), exist_ok=True)
            with open(os.path.join(big, "pkg", f"mod{i}.py"), "w") as fh:
                fh.write("".join(f"def f{i}_{j}(x):\n    y = x * {j}\n    if y > 3:\n        return y\n    return -y\n\n" for j in range(40)))
        from cruxlib import scan as scanmod
        from cruxlib.auto import estimate_total
        from cruxlib.pipeline import Config
        full = estimate_total(scanmod.scan(big), Config(), os.path.join(home, "est"))
        code, out, err = _auto([big, "--results-dir", os.path.join(home, "res2")], {**env, "CRUX_MAX_COST": f"{full * 0.6:.4f}"})
        assert code == 0, (out, err[-2000:])
        res = json.loads([l for l in out.splitlines() if l.startswith("CRUX_RESULT ")][0][len("CRUX_RESULT "):])
        assert res["trimmed_to_files"] or res["skipped_phases"], res
        code, out, err = _auto([big, "--results-dir", os.path.join(home, "res2b")], {**env, "CRUX_MAX_COST": "0.0001"})
        assert code == 4 and "CRUX_ERROR" in out, (code, out)
        # no spend limit: the same impossible env cap is overridden; every file and phase runs, nothing trimmed
        n_files = len(scanmod.scan(big).files)
        for args, extra in ((["--no-cost-limit"], {"CRUX_MAX_COST": "0.0001"}),
                            (["--max-cost", "0"], {}),
                            ([], {"CRUX_MAX_COST": "none"})):
            code, out, err = _auto([big, "--results-dir", os.path.join(home, "res2c"), *args], {**env, **extra})
            assert code == 0, (args, extra, out, err[-2000:])
            res = json.loads([l for l in out.splitlines() if l.startswith("CRUX_RESULT ")][0][len("CRUX_RESULT "):])
            assert res["cost_cap_usd"] is None and not res["trimmed_to_files"] and not res["skipped_phases"], res
            assert res["files"] == n_files, (res["files"], n_files)
        # files backend -> exit 3 with the pending index for the agent to answer
        code, out, err = _auto([repo, "--results-dir", os.path.join(home, "res3")], {**env, "CRUX_BACKEND": "files"})
        assert code == 3 and "CRUX_PENDING " in out, (code, out, err[-1500:])
        # missing repo -> exit 2 with a message, no prompt
        code, out, err = _auto(["no-such-repo-anywhere-xyz"], env, timeout=120)
        assert code == 2 and "CRUX_ERROR" in out
    finally:
        shutil.rmtree(repo)
        shutil.rmtree(home)


def test_auto_git_clone_and_no_credential_prompt():
    """Online: clones a public repo; an unknown private-looking URL must fail fast instead of prompting."""
    import subprocess as sp
    if sp.run(["git", "ls-remote", "https://github.com/gothinkster/golang-gin-realworld-example-app", "HEAD"],
              capture_output=True, timeout=30, env=dict(os.environ, GIT_TERMINAL_PROMPT="0")).returncode != 0:
        print("skip: no network for git")
        return
    home = tempfile.mkdtemp(prefix="crux-auto-")
    try:
        env = {"CRUX_HOME": os.path.join(home, "h"), "CRUX_BACKEND": "mock", "CRUX_CLONE_DEPTH": "20"}
        code, out, err = _auto(["https://github.com/gothinkster/golang-gin-realworld-example-app/tree/master",
                                "--results-dir", os.path.join(home, "res")], env)
        assert code == 0, (out, err[-2000:])
        assert os.path.isdir(os.path.join(home, "h", "repos", "gothinkster-golang-gin-realworld-example-app", ".git"))
        # second run reuses the clone (fetch), still unattended
        code, out, err = _auto(["https://github.com/gothinkster/golang-gin-realworld-example-app",
                                "--results-dir", os.path.join(home, "res")], env)
        assert code == 0, (out, err[-2000:])
        t0 = time.time()
        code, out, err = _auto(["https://github.com/this-org-does-not-exist-crux/private-repo"], env, timeout=180)
        assert code == 2 and "CRUX_ERROR" in out and time.time() - t0 < 150, (code, out, err[-800:])
    finally:
        shutil.rmtree(home)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
