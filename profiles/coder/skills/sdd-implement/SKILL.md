---
name: sdd-implement
description: Implement or rework all approved TASKS on the one shared PRD delivery MR
version: 0.2.0
---

# Implement PRD

1. Call `kanban_show()`; require dispatcher origin and verify exact project, shared worktree/branch, PRD, delivery MR and approved SPEC/PLAN/TASKS digests. Never create another checkout, branch or MR.
2. Reconcile current branch, commits, MR comments, pipeline and existing partial implementation before writing. Execute the complete task DAG in dependency order and preserve unrelated repository changes.
3. Add or update required tests. Commit/push minimal coherent implementation or rework units using repository conventions.
4. Run proportionate formatting, static, unit, integration/contract checks and record exact commands/results in the same MR description.
5. If approved artifacts cannot support correct acceptance, stop with `scope_gap` evidence and owning upstream stage; never change requirement intent.
6. After every TASK is covered and self-tests pass, update `/opt/fleet/templates/mr-description.md` metadata and mark the existing Draft MR ready. On code rework, keep it ready unless GitLab policy requires draft while changing.
7. Complete with task coverage, changed files, commands, MR/current head and residual risk. Do not self-review or merge.
