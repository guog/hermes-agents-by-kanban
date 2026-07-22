---
name: sdd-test
description: Test one exact code MR head and post a reproducible SDD-GATE
version: 0.1.0
---

# Test code

1. Call `kanban_show()`; query GitLab and ensure the current MR head equals the expected 40-char SHA.
2. Read approved SPEC, PLAN and TASKS plus the diff. Build a requirement/task-to-test coverage list.
3. Run the smallest complete reproducible suite: changed-area tests, required integration/contract tests, static checks and pipeline status.
4. Use `fail` for implementation defects; use `scope_gap` only when approved artifacts omit work needed for correct acceptance.
5. Post one idempotent `SDD-GATE` for stage `test` with exact commands/results, coverage, residual risk, run/task/head.
6. Complete pass/fail/scope_gap metadata. Do not edit code, resolve findings yourself or merge.

