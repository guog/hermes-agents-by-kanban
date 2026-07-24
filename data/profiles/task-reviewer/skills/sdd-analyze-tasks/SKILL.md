---
name: sdd-analyze-tasks
description: Review the complete SPEC PLAN TASKS mapping and post a digest-bound gate
version: 0.2.1
---

# Review TASKS set

## Fleet-wide execution rules

- Perform GitLab project, repository-metadata, MR, pipeline, discussion and comment operations only through the locked `glab` CLI or the installed official `glab` Skill. Do not substitute raw HTTP/`curl`, an ad-hoc SDK, a browser or manual UI work. Normal read-only `git` commands explicitly required by this Skill remain allowed.
- The card's designated Hermes shared `worktree` is the working copy of the Agent-owned delivery branch. Do not run routine `git fetch origin` or pull loops after dispatcher reconciliation. Fetch only to recover missing refs/worktree, investigate a proven local/remote head mismatch, or satisfy `live_reconcile_required`; record the reason. Use local `git rev-parse`/`git status` for artifact state and `glab` for current MR state.
- A PRD omission or ambiguity is not by itself a blocking finding. Decide from explicit acceptance/constraints, current repository behavior and conventions, approved upstream artifacts, compatibility/security, then the smallest reversible scope. A decision is critical when it affects user-visible scope/acceptance, a public interface, data/migration, security/permissions, compatibility, recovery/rollback or a required test/gate. Put every critical decision in the idempotent gate MR comment's `## 关键自主决策` section and include that comment URL in completion `gitlab_urls`; use `无` in the existing gate comment when none was made, without posting a separate empty comment. Escalate only contradictory evidence with no safe acceptance-preserving choice, or a genuine permission, credential or capability failure.

1. Call `kanban_show()`; require dispatcher origin and verify project/run/worktree/branch/shared MR and still-valid SPEC/PLAN digests.
2. Read `/opt/fleet/templates/tasks-template.md`; verify one-to-one SPEC/PLAN/TASK key sets, strict checklist lines, stable unique IDs, explicit `depends_on`, acyclic dependencies, execution waves, requirement coverage, exact target paths, objective acceptance and sufficient repeatable test work. Report material execution defects, not cosmetic deviations.
3. Compute the complete sorted TASKS path/blob-SHA digest at the review commit. Post an idempotent v2 `tasks-review` gate with digest and `review_commit_sha`.
4. Use `fail` for missing/incorrect tasks. Use `scope_gap` only for an evidenced PLAN or SPEC deficiency, and identify the owning upstream stage.
5. Complete with pass/fail/scope_gap, digest, review commit and findings. Pass one flat v2 metadata object conforming to `/opt/fleet/schemas/card-completion.schema.json`; repeat every required project/workspace/source field from the card at the top level. A pass must include non-empty `artifact_paths`, `artifact_digest` and `review_commit_sha`. Never edit artifacts, implement, push, create another MR or merge.
