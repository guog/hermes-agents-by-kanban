---
name: sdd-write-spec
description: Write or rework the complete SPEC set for one PRD on its shared delivery branch
version: 0.2.0
---

# Write SPEC set

1. Call `kanban_show()` and require `created_by=dispatcher`; verify project ID/path, shared worktree/branch, exact PRD path/commit/MR and run key against GitLab. Never discover or clone another repository.
2. Read the PRD at the exact commit. Split only into independently understandable and testable business scopes; define ordered keys, dependencies and a full PRD coverage matrix.
3. Write every `docs/prds/<prd-basename>/specs/spec-<key>.md`. Keys are stable lowercase kebab-case. Include stories, requirements, boundaries, edge cases, success criteria and traceability; exclude implementation choices.
4. On rework, change only evidence-backed SPEC files and preserve keys unless the finding requires a set change. Do not write PLAN/TASKS/code.
5. Commit the smallest coherent SPEC change with the repository's conventional commit rules and push the shared branch. After the first valid SPEC commit, reconcile then create exactly one `Draft: [PRD] <prd-basename>.md` MR using `/opt/fleet/templates/mr-description.md`; otherwise update that MR.
6. Complete with sorted SPEC paths/blob SHAs, commit/head, MR IID/URL, coverage and residual risk. Do not review, mark ready or merge.
