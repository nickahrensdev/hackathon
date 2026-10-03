IMPORTANCE SCALE: how much THIS repository's critical flows depend on the method's OWN logic. This is not a measure of code quality, size or complexity.
10 Crux. Its own logic is the single point where a top flow can fail catastrophically: the auth or permission check, token signing or verification, money calculation, the commit of the core entity. Expect only a handful per repository.
 9 The core rule or state change of a top flow (criticality 8 or higher), with no fallback.
 8 Its own code decides or writes something a top flow depends on: validation of critical input, persistence of a core entity, a query that guards a write.
 7 On a top flow's path with modest logic of its own (mapping, enrichment, orchestration with real branching), or the core logic of a secondary flow.
 6 A shared helper or read path that critical flows use. A defect degrades results but doesn't corrupt state.
 5 Secondary feature logic. A defect is noticeable but contained.
 4 Peripheral: admin, reporting, rarely used paths, non-critical integrations.
 3 Minor helper: formatting, logging, simple conversions, configuration getters.
 2 Trivial plumbing: accessors, DTO boilerplate, constants, generic wiring (DI modules, re-exports).
 1 Dead, debug-only, test-only or generated code.

Calibration
- Score the method's own logic. Calling important code does not make a method important. A pass-through (a controller, resolver, route handler or service method that only delegates) scores 1–2 below its most important callee. Don't go lower than that: it is still the door the flow comes through.
- Layers. When an interface, its implementation and a mapper or DAO all serve one operation, the layer holding the real logic (query, transaction, branching) carries the score, and the other layers are pass-throughs. If that logic lives outside code (MyBatis XML, ORM-derived queries, Feign or Retrofit clients), score the declaration that stands for it, and treat the rest as pass-throughs.
- Writes, and checks that guard state, outrank reads. A read-only query on a critical flow is usually 5–7.
- Expect a pyramid across the repository: roughly 1% of methods at 10, 3% at 9, 7% at 8 and 10% at 7, with most methods between 2 and 6. If more than a quarter of the methods in a batch of ordinary code are 8 or higher, you are inflating.
- A method that no critical flow reaches rarely deserves more than 6.
- Consequence beats size. A 3-line permission check can be a 10; a 300-line report formatter can be a 3.
