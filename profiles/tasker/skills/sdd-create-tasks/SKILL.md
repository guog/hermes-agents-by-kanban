---
name: sdd-create-tasks
description: Compile SPEC and PLAN into stable tasks.md or append convergence tasks
version: 0.1.0
---

# Create TASKS

1. Call `kanban_show()` and verify merged SPEC/PLAN commits or explicit convergence findings.
2. Read artifact and repository task conventions. Preserve existing stable IDs on rework.
3. Write tasks with ID, requirement source, target files/modules, dependencies, acceptance and tests. Produce an acyclic DAG, execution waves and coverage matrix.
4. Compute a deterministic normalized digest if the project contract requires it.
5. For convergence, append or clarify only the missing work; do not rewrite approved requirements or silently renumber unrelated tasks.
6. Reconcile branch/MR, validate graph and coverage, and complete with MR/head/paths/verification. Do not create GitLab Issues, code, review or merge.

