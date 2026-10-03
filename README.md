# repo-crux

Point it at any repository. It finds the **entry points**, the **critical flows** and the **cruxes**, then scores **every file and method 1–10** by how important it is to those flows. The result is one JSON document.

```bash
pip install anthropic tree-sitter-language-pack        # both optional; see "Requirements"
python3 crux.py estimate /path/to/repo                 # free: static scan + token/cost estimate
python3 crux.py run /path/to/repo -o analysis.json     # full analysis (Claude API)
python3 crux.py run /path/to/repo --backend claude-cli # same, using your Claude Code login
python3 crux.py summary analysis.json                  # human-readable digest
```

## Unattended mode (used by the repo-crux skill)

```bash
python3 crux.py auto payments-api                                  # a folder name: searched in cwd, its parents, ~/code, ~/src, ~/projects, ~/IdeaProjects, ...
python3 crux.py auto ~/work/payments-api                           # a path
python3 crux.py auto https://bitbucket.org/acme/payments-api       # Bitbucket Cloud (…/src/<branch>/ or …/branch/<name> picks a branch)
python3 crux.py auto "https://bitbucket.acme.com/projects/PAY/repos/payments-api/browse?at=refs/heads/release"   # Bitbucket Server
```

`auto` never prompts. It runs these steps in order:
1. Finds or clones the repository. Cloning is shallow and non-interactive: credentials come only from your git keychain, SSH keys, or `BITBUCKET_TOKEN` / `BITBUCKET_USERNAME` + `BITBUCKET_APP_PASSWORD`.
2. Picks a backend. It uses `ANTHROPIC_API_KEY` if set, otherwise a logged-in `claude` CLI. With neither, it falls back to `files`, where the calling agent answers the prompts.
3. Fits the run under `CRUX_MAX_COST` (default $15). It keeps the most active files first, then skips tracing, then calibration. Pass `--no-cost-limit` (or `--max-cost 0`, or set `CRUX_MAX_COST=none`) to remove the cap: every file and every phase runs, and the estimate is only logged.
4. Runs the analysis and writes `analysis.json` and `summary.md` to `~/repo-crux-results/<repo>/`, then prints a `CRUX_RESULT {…}` line.

Exit codes:
- 2: the repo was not found or could not be cloned.
- 3: prompts are waiting in the files backend.
- 4: the run could not fit under the cap, or another failure.

Clones and caches live in `~/.repo-crux/`. Change the location with `CRUX_HOME`, and add search folders with `CRUX_SEARCH_ROOTS`.

## How it works

Plain code coordinates the work, and models are called only where judgment is needed. An agent loop would spend tokens re-reading the repo and deciding what to do next; this pipeline does neither.

```
Phase 0  scan        (no LLM)  files -> functions/methods/classes (tree-sitter, ~300 languages)
                               -> reference graph, fan-in, entry-point candidates, tags, git churn
Phase 1  map         1 call    strong model reads a compact brief (manifests, README, entry
         prompts/map.md        candidates, symbol skeleton; full source if the repo is small)
                               -> entry points, critical flows (ordered steps), crux candidates
Phase 2  trace       1/flow    for each flow, the code of the statically reachable functions
         prompts/trace.md      -> confirmed path, cruxes, observable outcomes, preconditions
Phase 3  score       N calls   fast model, files packed ~12k tokens per call, shared context in a
         prompts/score.md      prompt-cached prefix, parallel or Batch API
                               -> score/role/flows/why for every method, score + summary per file
Phase 4  calibrate   1 call    strong model sees the high scorers plus static anomalies from
         prompts/calibrate.md  every file, fixes cross-file inconsistencies, picks the final cruxes
         assemble    (no LLM)  merge, fallbacks, schema validation -> analysis.json
```

Every model call returns **structured output** that validates against `schemas/<phase>.json`. Symbols are referred to by short stable IDs (`S3kf9q`), so answers join back to the code exactly.

### Why it is efficient

| Technique | Effect |
|---|---|
| Static pass first (tree-sitter + reference graph) | Phases 1–2 read a skeleton and selected functions, not the whole repo |
| Model tiering: Opus for map/calibrate, Sonnet for trace, Haiku for bulk scoring | The bulk of the tokens go to the cheapest tier |
| Rule-scored boilerplate (accessors, empty bodies, `toString`, DI constructors) | No tokens spent on obvious 1s and 2s |
| Files packed into ~12k-token calls; imports and license headers collapsed | Fewer calls, 15–30% fewer input tokens |
| Shared rubric and flow context as a cached system prefix | Repeated context billed at 5–10% |
| `--batch` (Message Batches API) | Another 50% off when you can wait |
| Answers cached by content hash; stable IDs; content-defined packing | Re-runs are free. `--reuse-context` re-scores only the files you changed |
| `estimate` and `--max-cost` | You know the bill before spending it |

Measured runs with the default models (via `claude-cli`):

| Repo | Size | Calls | Cost | Time |
|---|---|---|---|---|
| `spring-boot-realworld` (Java/Spring) | 93 files, 242 methods | 1 map, 8 traces, 6 score, 1 calibrate | $1.35 | ~4 min |
| Express/Prisma RealWorld (TypeScript) | 27 files, 56 methods | 1 map (full source, no trace), 2 score, 1 calibrate | $0.57 | ~2 min |

An unchanged re-run costs $0, because every answer is cached. The estimate for Mastodon (2.4k files, 188k lines, 10k methods) is about $7.20, roughly 80% of it Haiku scoring; with `--batch`, about half that. The static scan of Mastodon takes about 20 seconds. Two examples ship in `examples/`.

### Keeping scores meaningful

Two things keep scores from drifting toward "everything on a critical path is an 8":
- **Shared rubric.** `prompts/rubric.md` is used by both the score and calibrate phases. It scores each method on its own logic. Pass-throughs sit 1–2 points below what they call. When interface, implementation and mapper serve one operation, only the layer that holds the real logic carries the score. Writes outrank reads. The rubric spells out the expected pyramid.
- **Calibration.** The calibrate pass sees every high scorer from every file at once. It demotes inflation, aligns equivalent methods, and raises under-rated checks.

On the Java example, an earlier version of the rubric put 28% of methods at 8 or higher. The current rubric plus calibration puts 9.5% there (3 at 10, 7 at 9, 13 at 8). The top ten are the JWT filter, the token service, the security config, the ownership checks and the login and registration rules.

## Backends: how the model calls are made

| `--backend` | Needs | Notes |
|---|---|---|
| `anthropic` (default) | `pip install anthropic`, `ANTHROPIC_API_KEY` | Parallel, streaming, prompt caching, structured outputs, `--batch` |
| `claude-cli` | Claude Code installed and logged in | Runs `claude -p --json-schema …` per request in an empty dir. No API key needed. |
| `files` | anything that can read and write files | Writes each prompt to `<work>/requests/<id>.md` and exits with code 3. Write the JSON answers to `<work>/responses/<id>.json` and run again. |
| `mock` | nothing | Fake answers, for testing the plumbing |

**Coordinating with an agent (the `files` backend).** This works in Claude Code, Cursor, or any agent with subagents. The script stays the coordinator; the agent only answers prompts. The agent's loop is:

1. Run `python3 crux.py run REPO --backend files`.
2. If it exits with code 3, open `<work>/REQUESTS.md`. Fan the listed request files out to subagents, one request or a few per subagent. Each subagent writes only the JSON answer to the path named in the request.
3. Run the same command again. Repeat until it prints `analysis written to …`. That is usually 3–4 rounds: map, then trace, then score, then calibrate.

## Output (`schemas/analysis.schema.json`)

```jsonc
{
  "repo": { "name", "kind", "summary", "languages", "files_analyzed", "methods_analyzed", "commit" },
  "entry_points": [ { "id": "E8", "kind": "http", "trigger": "POST /articles", "name": "ArticlesApi.createArticle",
                      "file", "line", "symbol", "criticality": 8, "flows": ["F4"], "description", "source" } ],
  "critical_flows": [ { "id": "F1", "name": "User login and token issuance", "criticality": 10, "traced": true,
                        "entry_points": ["E21"], "steps": [ { "symbol", "name", "file", "line", "what", "certain" } ],
                        "business_impact", "failure_modes": [], "observable_outcomes": [], "preconditions": [],
                        "gaps": [], "cruxes": ["C3"] } ],
  "cruxes": [ { "id": "C1", "name": "JwtTokenFilter.doFilterInternal", "file", "line", "kind": "auth_gate",
                "score": 10, "flows": ["F1","F4"], "why", "risk_if_broken" } ],
  "files": [ { "path", "score": 9, "summary", "methods", "max_method_score", "churn", "fix_churn" } ],
  "method_analysis": [ { "id", "key": "src/…/UserService.java::UserService.createUser", "file", "qualified_name",
                         "start_line", "end_line", "score": 9, "role": "business_rule", "flows": ["F3"],
                         "why": "consequence if wrong", "confidence", "source": "llm|rule|static-fallback",
                         "is_entry_point", "is_crux", "calibrated_from",
                         "static": { "fan_in", "fan_in_files", "complexity", "tags", "flow_reach" } } ],
  "score_distribution": { "10": 2, "9": 10, "...": 0 },
  "skipped_files": { "counts": { "test": 23, "vendor": 0 } },
  "notes": [ "systemic risks the models noticed" ],
  "run": { "backend", "models", "phases": { "score": { "calls", "cached", "input_tokens", "output_tokens", "cost_usd" } },
           "total_cost_usd", "warnings" }
}
```

Useful queries:

```bash
jq '.method_analysis[] | select(.score >= 8) | [.score, .qualified_name, .file] | @tsv' -r analysis.json
jq '.critical_flows[] | {name, preconditions, observable_outcomes, failure_modes}' analysis.json   # -> Given/Then
jq '.files[:20][] | "\(.score) \(.path)"' -r analysis.json
```

### Using it to drive test generation

Each critical flow carries:
- `preconditions` → **Given**
- the entry point `trigger` → **When**
- `observable_outcomes` → **Then**
- `failure_modes` → negative scenarios

Write scenarios for flows in criticality order, and use `method_analysis` scores to choose which steps get dedicated scenarios. Cruxes should get a scenario per failure mode.

## Options that matter

```
--model-map / --model-trace / --model-score / --model-calibrate   (env: CRUX_MODEL_*)
      defaults: claude-opus-5-5 / claude-sonnet-5-5 / claude-haiku-4-5-20251001 / claude-opus-5-5
--effort-map high --effort-trace medium --effort-calibrate medium   (Haiku has no effort setting)
--full-source-threshold 45000   repos below this many tokens: the map sees all code
--trace auto|always|never       auto skips tracing when the map already saw all code
--max-trace-flows 8  --trace-budget 24000  --map-budget 60000  --chunk-tokens 12000
--include 'src/**' --exclude 'legacy/**' --include-tests --include-migrations --max-files N
--batch   --max-cost 20   --reuse-context   --no-cache   --dump-prompts   --concurrency N
```

`--dump-prompts` writes every prompt to `<work>/prompts/`, so you can read exactly what each model saw. The prompts themselves live in `prompts/*.md` and can be edited without touching code.

## Requirements

- Python 3.9+. No other dependency is needed for `scan`, `estimate`, the `files` and `mock` backends, or validation.
- `tree-sitter-language-pack` (recommended): accurate functions and methods for about 300 languages. Grammars download on first use. Without it, built-in heuristics cover Python, JS/TS, Java, Kotlin, C#, Go, Rust, PHP, Ruby, Swift, C/C++, Scala, Dart, Lua and Elixir. Files nobody can parse are marked `DISCOVER`, and the scoring model lists their functions itself.
- `anthropic` for the default backend, or Claude Code for `claude-cli`.
- `git` (optional): respects `.gitignore` and provides the churn and bug-fix signals.

## Tests

```bash
python3 tests/test_smoke.py   # scan, mock pipeline, files round-trip, API backend vs a fake HTTP transport (incl. batch),
                              # auto: URL parsing, local lookup, cost-cap fitting, git clone (online, skipped offline)
```

## Limitations

- The reference graph is name-based. It handles receivers, imports, interface→implementation links and constructors, but dynamic dispatch, reflection, DI configured in XML/YAML and event buses are invisible to it. The trace phase and the models fill those gaps, and the static hints are labeled as approximate in every prompt.
- Entry-point detection is a pattern table over common frameworks (Spring, JAX-RS, DGS, NestJS, Express, Fastify, Next.js, Remix, SvelteKit, Flask, FastAPI, Django/DRF, Rails, Sinatra, Laravel, Symfony, ASP.NET incl. minimal APIs and MediatR, Gin/Echo/chi/net-http, Axum/Actix, Ktor, Phoenix, GraphQL, gRPC, Kafka/Rabbit/SQS listeners, Celery/Sidekiq/cron, Lambda/GCF/Azure Functions, CLIs, React/Angular/Vue routers, Android/iOS). The map phase adds what it misses and rejects false positives; `source` on each entry says which happened.
- Scores are relative to the repo's own flows: a 9 in a blog engine is not a 9 in a payments system.
- Non-code artifacts that hold logic (MyBatis XML, SQL in YAML, stored procedures outside `.sql`) are not scored. The models often flag them in `notes`.
