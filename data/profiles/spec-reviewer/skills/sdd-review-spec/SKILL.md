---
name: sdd-review-spec
description: Review the complete SPEC set and post a digest-bound gate on the shared MR
version: 0.2.1
---

# Review SPEC set

## Fleet-wide execution rules

- Perform GitLab project, repository-metadata, MR, pipeline, discussion and comment operations only through the locked `glab` CLI or the installed official `glab` Skill. Do not substitute raw HTTP/`curl`, an ad-hoc SDK, a browser or manual UI work. Normal read-only `git` commands explicitly required by this Skill remain allowed.
- The card's designated Hermes shared `worktree` is the working copy of the Agent-owned delivery branch. Do not run routine `git fetch origin` or pull loops after dispatcher reconciliation. Fetch only to recover missing refs/worktree, investigate a proven local/remote head mismatch, or satisfy `live_reconcile_required`; record the reason. Use local `git rev-parse`/`git status` for artifact state and `glab` for current MR state.
- A PRD omission or ambiguity is not by itself a blocking finding. Decide from explicit acceptance/constraints, current repository behavior and conventions, approved upstream artifacts, compatibility/security, then the smallest reversible scope. A decision is critical when it affects user-visible scope/acceptance, a public interface, data/migration, security/permissions, compatibility, recovery/rollback or a required test/gate. Put every critical decision in the idempotent gate MR comment's `## 关键自主决策` section and include that comment URL in completion `gitlab_urls`; use `无` in the existing gate comment when none was made, without posting a separate empty comment. Escalate only contradictory evidence with no safe acceptance-preserving choice, or a genuine permission, credential or capability failure.

1. Call `kanban_show()`; require a dispatcher-created card and verify project/run/worktree/branch/PRD/shared MR identity. Read live GitLab state but do not require the whole MR head to remain frozen afterward.
2. Enumerate the complete sorted `spec-<key>.md` set. Read the exact PRD and `/opt/fleet/templates/spec-template.md`; check all mandatory sections and the coverage matrix, plus full coverage, scope fidelity, independence, testability, boundaries, assumptions, success criteria, dependencies, contradictions and implementation leakage. Report material omissions, not cosmetic deviations.
3. Compute the artifact digest from sorted `<path>\0<git-blob-sha>\n` rows at the review commit. Post one idempotent v2 `spec-review` gate using `/opt/fleet/templates/gate-comment.md`, with paths, digest and `review_commit_sha`.
4. Use `fail` for SPEC defects. Resolve ordinary PRD omissions and ambiguity with the decision hierarchy above; use a blocking scope finding only for contradictory requirements with no safe acceptance-preserving interpretation.
5. Complete with pass/fail, digest, review commit, MR and findings. Pass one flat v2 metadata object conforming to `/opt/fleet/schemas/card-completion.schema.json`; repeat every required project/workspace/source field from the card at the top level. A pass must include non-empty `artifact_paths`, `artifact_digest` and `review_commit_sha`. Never edit artifacts, push, create another MR or merge.
