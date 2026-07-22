---
name: sdd-analyze-tasks
description: Analyze SPEC, PLAN and TASKS consistency and post a head-bound gate
version: 0.1.0
---

# Analyze TASKS

1. Call `kanban_show()` and confirm the TASKS MR current head equals expected head.
2. Read exact SPEC, PLAN and TASKS revisions.
3. Validate stable unique IDs, acyclic dependencies, execution waves, requirement coverage, correct files/modules, objective acceptance and test work.
4. Classify missing/incorrect tasks as `fail`; classify an upstream requirement/design gap as `scope_gap` only when TASKS cannot safely resolve it.
5. Post an idempotent `SDD-GATE` for `tasks-analyze`, bound to run/task/head, using the gate template.
6. Complete pass/fail/scope_gap metadata. Do not edit artifacts, implement or merge.

