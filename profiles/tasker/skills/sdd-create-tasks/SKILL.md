---
name: sdd-create-tasks
description: Write the complete one-to-one TASKS set on the shared delivery branch
version: 0.2.0
---

# Create TASKS set

1. Call `kanban_show()`; require dispatcher origin and verify project/run/shared worktree/branch/MR plus still-valid SPEC and PLAN digests.
2. For every matching SPEC/PLAN key, write exactly `docs/prds/<prd-basename>/tasks/task-<key>.md`. Preserve stable IDs on rework.
3. Each task defines ID, SPEC/requirement source, target module/files, dependencies, objective acceptance and tests. Provide an acyclic cross-file DAG, execution waves and complete coverage matrix.
4. Repair task defects only. If PLAN or SPEC is insufficient, complete `scope_gap`; do not silently rewrite approved requirements/design or renumber unrelated work.
5. Commit minimal coherent TASKS changes and push the same branch/MR. Never create GitLab Issues, Task work items or a TASKS MR.
6. Complete with sorted TASKS paths/blob SHAs, graph verification, commit/head, MR and residual risk. Do not implement, review or merge.
