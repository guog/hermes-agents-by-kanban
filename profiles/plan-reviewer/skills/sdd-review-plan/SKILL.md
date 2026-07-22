---
name: sdd-review-plan
description: Review a PLAN MR against its SPEC and repository constraints
version: 0.1.0
---

# Review PLAN

1. Call `kanban_show()` and confirm current MR head matches the card.
2. Read approved SPEC, PLAN artifacts and relevant repository code/rules.
3. Check traceability, feasibility, architecture fit, interfaces, data/migration, security, observability, tests, rollback and unresolved decisions.
4. Treat requirement drift, missing failure handling and non-testable design as blocking; avoid cosmetic-only findings.
5. Post an idempotent `SDD-GATE` for stage `plan-review` using the gate template; bind run/task/head.
6. Complete pass/fail metadata. Do not edit PLAN, create TASKS or merge.

