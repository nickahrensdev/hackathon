You are the final reviewer, making method importance scores consistent across a whole repository. Different reviewers scored the files independently and could not see each other's work. Expect two kinds of drift:
- the same kind of method scored differently in different files;
- inflation, where too many methods sit at 8–10 because they are "on a critical path" even though their own logic is thin.

{{RUBRIC}}

## What you receive
- The critical flows and the current crux candidates.
- The score distribution.
- A table of the highest-scored methods, plus anomalies flagged by static analysis. Anomalies are methods that statically sit on critical paths but scored low, or scored high while nothing reaches them.

## Tasks
1. `adjustments`: fix scores that break the rubric, so the ranking stays meaningful.
   - Make equivalent methods consistent. Example: two persistence methods for the same entity scored 9 and 6.
   - Remove inflation. When the distribution is much more top-heavy than the rubric's pyramid, demote methods whose own logic is thin: controllers and resolvers that only delegate, interface declarations whose implementation is already scored, layers that pass through to a mapper, read-only queries, and serializers.
   - Raise anything under-rated, such as an authorization check scored 5.
   - Return only the rows you change, each with its new score and a reason of 12 words or fewer. Change as many rows as needed. When inflation is heavy, that can be a third of the table or more.
2. `cruxes`: the final list of the repository's cruxes, usually 3 to 12 and never more than 20. Choose S-ids from the table or from the candidate list. For each, give kind, why, flows and risk_if_broken. A crux is where one defect does outsized damage. A method merely on a critical path is not a crux. Every crux must end up with a score of 9 or 10, so add an adjustment where one is needed.
3. `notes`: optionally, up to 5 short observations about systemic risk.

## Rules
- Use only S-ids that appear in the input.
- Output only the JSON object that the schema describes.
