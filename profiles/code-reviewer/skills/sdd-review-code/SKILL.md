---
name: sdd-review-code
description: Review one tested code MR head and post a correctness gate
version: 0.1.0
---

# Review code

1. Call `kanban_show()`; query the MR and verify current head equals expected head and tester evidence targets the same SHA.
2. Read approved SPEC/PLAN/TASKS, diff, pipeline and unresolved discussions.
3. Review correctness, requirement/task coverage, regression, error paths, security, transactions/concurrency, migrations, compatibility and maintainability.
4. Use inline discussions for actionable findings. Use `fail` for code defects and `scope_gap` only for an upstream artifact deficiency.
5. Post an idempotent `SDD-GATE` for `code-review`, bound to run/task/head, with concise evidence and residual risk.
6. Complete pass/fail/scope_gap metadata. Do not edit code or merge.

