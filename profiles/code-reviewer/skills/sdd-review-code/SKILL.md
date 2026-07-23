---
name: sdd-review-code
description: Review the exact tested head of the one PRD delivery MR and post a correctness gate
version: 0.2.0
---

# Review delivery head

## Fleet-wide execution rules

- Perform GitLab project, repository-metadata, MR, pipeline, discussion and comment operations only through the locked `glab` CLI or the installed official `glab` Skill. Do not substitute raw HTTP/`curl`, an ad-hoc SDK, a browser or manual UI work. Normal read-only `git` commands explicitly required by this Skill remain allowed.
- The card's designated Hermes shared `worktree` is the working copy of the Agent-owned delivery branch. Do not run routine `git fetch origin` or pull loops after dispatcher reconciliation. Fetch only to recover missing refs/worktree, investigate a proven local/remote head mismatch, or satisfy `live_reconcile_required`; record the reason. Use local `git rev-parse`/`git status` for reviewed code state and `glab` for current MR/pipeline/discussion state.
- A PRD omission or ambiguity is not by itself a blocking finding. Decide from explicit acceptance/constraints, current repository behavior and conventions, approved upstream artifacts, compatibility/security, then the smallest reversible scope. A decision is critical when it affects user-visible scope/acceptance, a public interface, data/migration, security/permissions, compatibility, recovery/rollback or a required test/gate. Put every critical decision in the idempotent gate MR comment's `## 关键自主决策` section and include that comment URL in completion `gitlab_urls`; use `无` in the existing gate comment when none was made, without posting a separate empty comment. Escalate only contradictory evidence with no safe acceptance-preserving interpretation, or a genuine permission, credential or capability failure.

1. Call `kanban_show()`; require dispatcher origin and verify project/run/worktree/branch/MR, approved artifacts, current MR head and tester pass all target the same full SHA.
2. Review the complete diff for correctness, requirement/task coverage, regression, error paths, security, transactions/concurrency, migration, compatibility and maintainability. Query pipeline and unresolved discussions.
3. Use inline discussions for actionable findings. Use `fail` for code defects and `scope_gap` only for an evidenced upstream artifact deficiency.
4. Re-read MR head immediately before posting. If it changed or no matching tester pass exists, post no pass and block for fresh test/review.
5. Post one idempotent v2 `code-review` gate bound to the exact `head_sha`, with precise locations, evidence and residual risk.
6. Complete pass/fail/scope_gap with one flat v2 metadata object conforming to `/opt/fleet/schemas/card-completion.schema.json`; repeat every required project/workspace/source field from the card at the top level. A pass must bind `head_sha`, `mr_iid` and `mr_url` to the reviewed current head. Never edit code, push, resolve your own blocking findings, create another MR or merge.
