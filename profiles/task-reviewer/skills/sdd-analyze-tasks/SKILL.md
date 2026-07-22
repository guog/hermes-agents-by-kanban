---
name: sdd-analyze-tasks
description: Review the complete SPEC PLAN TASKS mapping and post a digest-bound gate
version: 0.2.0
---

# Review TASKS set

1. Call `kanban_show()`; require dispatcher origin and verify project/run/worktree/branch/shared MR and still-valid SPEC/PLAN digests.
2. Verify one-to-one SPEC/PLAN/TASK key sets, stable unique IDs, acyclic dependencies, execution order, requirement coverage, correct modules, objective acceptance and sufficient test work.
3. Compute the complete sorted TASKS path/blob-SHA digest at the review commit. Post an idempotent v2 `tasks-review` gate with digest and `review_commit_sha`.
4. Use `fail` for missing/incorrect tasks. Use `scope_gap` only for an evidenced PLAN or SPEC deficiency, and identify the owning upstream stage.
5. Complete with pass/fail/scope_gap, digest, review commit and findings. Never edit artifacts, implement, push, create another MR or merge.
