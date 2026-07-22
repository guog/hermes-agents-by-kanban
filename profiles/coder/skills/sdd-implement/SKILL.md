---
name: sdd-implement
description: Implement or rework one approved TASKS set in its existing code MR
version: 0.1.0
---

# Implement

1. Call `kanban_show()`; verify run/project/spec/stage, worktree remote, artifact commits and expected code branch/MR.
2. Reconcile existing branch, MR, commits and comments before writing. Never create a second code MR for the same SPEC.
3. Execute tasks in dependency order, preserving repository rules and unrelated user changes. Add or update tests required by each task.
4. Run proportionate formatting, static checks and tests; record exact commands and results.
5. If approved artifacts cannot support a correct implementation, stop and complete with `scope_gap` evidence; do not change requirement intent.
6. Commit/push to the stable code branch and create or update the MR using `/opt/fleet/templates/mr-description.md` and official `glab` guidance.
7. Complete with task coverage, changed files, commands, MR/head and residual risk. Do not self-review or merge.

