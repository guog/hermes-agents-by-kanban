---
name: sdd-create-tasks
description: Write the complete one-to-one TASKS set on the shared delivery branch
version: 0.2.1
---

# Create TASKS set

## Fleet-wide execution rules

- Perform GitLab project, repository-metadata, MR, pipeline, discussion and comment operations only through the locked `glab` CLI or the installed official `glab` Skill. Do not substitute raw HTTP/`curl`, an ad-hoc SDK, a browser or manual UI work. Normal `git` commands explicitly required by this Skill remain allowed for worktree inspection, commit and push.
- The card's designated Hermes shared `worktree` is the only editable working copy of the Agent-owned delivery branch. Do not run routine `git fetch origin` or pull loops after dispatcher reconciliation. Fetch only to recover missing refs/worktree, investigate a proven local/remote head mismatch or rejected push, or satisfy `live_reconcile_required`; record the reason. Use local `git rev-parse`/`git status` for worktree state and `glab` for current MR state.
- A PRD omission or ambiguity is not by itself `scope_gap` or `needs_input`. Decide from explicit acceptance/constraints, current repository behavior and conventions, approved upstream artifacts, compatibility/security, then the smallest reversible scope. A decision is critical when it affects user-visible scope/acceptance, a public interface, data/migration, security/permissions, compatibility, recovery/rollback or a required test/gate. Before completion, reconcile one idempotent delivery-MR comment containing every critical decision made by this card using `/opt/fleet/templates/decision-comment.md` and include its URL in completion `gitlab_urls`; do not post an empty comment when none was made. Record non-critical assumptions in TASKS or completion evidence. Escalate only contradictory evidence with no safe acceptance-preserving choice, or a genuine permission, credential or capability failure.

1. Call `kanban_show()`; require dispatcher origin and verify project/run/shared worktree/branch/MR plus still-valid SPEC and PLAN digests.
2. For every matching SPEC/PLAN key, write exactly `docs/prds/<prd-basename>/tasks/task-<key>.md` from the Chinese `/opt/fleet/templates/tasks-template.md`. Replace every placeholder and preserve stable IDs on rework.
3. Keep the strict `- [ ] T001 [P?] [US?] ...` line format and allocate IDs uniquely across the complete PRD TASKS set; never restart at T001 in each file. Each task defines SPEC/requirement source, exact target files, `depends_on`, objective acceptance and a repeatable test or verification. Provide an acyclic cross-file DAG, execution waves and complete coverage matrix.
4. Repair task defects only. If PLAN or SPEC is insufficient, complete `scope_gap`; do not silently rewrite approved requirements/design or renumber unrelated work.
5. Commit minimal coherent TASKS changes and push the same branch/MR. Never create GitLab Issues, Task work items or a TASKS MR.
6. Complete with sorted TASKS paths/blob SHAs, graph verification, commit/head, MR and residual risk. Pass one flat v2 metadata object conforming to `/opt/fleet/schemas/card-completion.schema.json`; repeat every required project/workspace/source field from the card at the top level, using schema-allowed nulls/empty arrays without omitting keys. Do not implement, review or merge.
