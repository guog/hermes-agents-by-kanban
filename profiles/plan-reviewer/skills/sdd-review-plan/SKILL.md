---
name: sdd-review-plan
description: Review the complete PLAN set and post a digest-bound gate on the shared MR
version: 0.2.0
---

# Review PLAN set

1. Call `kanban_show()`; require dispatcher origin and verify project/run/worktree/branch/shared MR plus the still-valid SPEC digest.
2. Enumerate one `plan-<key>.md` for every SPEC key. Check traceability, feasibility against actual code, architecture fit, interfaces, data/migration, compatibility, security, observability, tests, rollback, unresolved decisions and over-design.
3. Compute the sorted path/blob-SHA digest at the review commit. Post an idempotent v2 `plan-review` gate with digest and `review_commit_sha`.
4. Use `fail` for PLAN defects and `scope_gap` only when SPEC cannot support an implementable plan. Avoid cosmetic-only blockers.
5. Complete with pass/fail/scope_gap metadata and evidence. Never edit PLAN, create TASKS, push, create another MR or merge.
