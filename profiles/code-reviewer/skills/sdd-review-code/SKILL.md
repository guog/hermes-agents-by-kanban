---
name: sdd-review-code
description: Review the exact tested head of the one PRD delivery MR and post a correctness gate
version: 0.2.0
---

# Review delivery head

1. Call `kanban_show()`; require dispatcher origin and verify project/run/worktree/branch/MR, approved artifacts, current MR head and tester pass all target the same full SHA.
2. Review the complete diff for correctness, requirement/task coverage, regression, error paths, security, transactions/concurrency, migration, compatibility and maintainability. Query pipeline and unresolved discussions.
3. Use inline discussions for actionable findings. Use `fail` for code defects and `scope_gap` only for an evidenced upstream artifact deficiency.
4. Re-read MR head immediately before posting. If it changed or no matching tester pass exists, post no pass and block for fresh test/review.
5. Post one idempotent v2 `code-review` gate bound to the exact `head_sha`, with precise locations, evidence and residual risk.
6. Complete pass/fail/scope_gap metadata. Never edit code, push, resolve your own blocking findings, create another MR or merge.
