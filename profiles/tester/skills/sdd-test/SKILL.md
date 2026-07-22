---
name: sdd-test
description: Test the exact current head of the one PRD delivery MR and post reproducible evidence
version: 0.2.0
---

# Test delivery head

1. Call `kanban_show()`; require dispatcher origin and verify project/run/worktree/branch/MR, approved artifact digests and current MR head equals the card's full 40-character expected SHA.
2. Read all approved SPEC/PLAN/TASKS plus the complete diff. Build a requirement/task-to-test coverage list.
3. Run the smallest complete reproducible suite: changed-area tests, required integration/contract tests, static checks and required pipeline state. Do not modify code or test files.
4. Use `fail` for implementation defects and `scope_gap` only for an evidenced artifact omission.
5. Re-read MR head immediately before posting. If it changed, post no pass and block for a fresh card. Otherwise post one idempotent v2 `test` gate bound to the exact `head_sha`, with commands/results, coverage and residual risk.
6. Complete pass/fail/scope_gap metadata. Never resolve findings, push, create another MR or merge.
