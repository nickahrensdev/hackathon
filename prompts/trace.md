You are tracing ONE critical flow through real code. Your job is to confirm its actual path and pinpoint its cruxes. A later step scores every method with your trace as context, so state what the code actually shows.

## What you receive
- REPO SUMMARY and the list of all flows, for context.
- THE FLOW under study, as hypothesized from a skeleton: entry points, ordered steps, impact.
- CODE: the entry handler plus the functions statically reachable from it. Each function's first line is prefixed with its «S-id». Some may be irrelevant, because linking is name-based. Some real steps may be missing, because of dynamic dispatch, dependency injection or framework callbacks.
- NOT SHOWN: other related symbols, listed by S-id and name only.

## Tasks
1. `steps`: the actual ordered path, as S-ids. For each step, say what it does in this flow in 15 words or fewer. Use NOT SHOWN symbols only when the shown code clearly calls them. Set `certain` to true only when the code shows the call.
2. `cruxes`: the 1–5 symbols in this flow where a defect does the most damage, with kind, why and risk.
3. `off_path`: S-ids in CODE that are NOT part of this flow, so later scoring does not over-rate them.
4. Refine the flow from what the code actually does: criticality 1–10, business impact, observable outcomes (status codes, records written, events published), preconditions (data or auth state required) and failure modes (concrete ways it breaks, e.g. "slug collision overwrites another article").
5. `gaps`: up to 5 things the path depends on that you could not see, e.g. "JWT filter registered in framework config".

## Rules
- Use only S-ids that appear in this packet. Never invent IDs.
- Be terse. Keep every free-text field under 25 words.
- Output only the JSON object that the schema describes.
