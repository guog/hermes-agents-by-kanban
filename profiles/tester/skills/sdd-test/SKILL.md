---
name: sdd-test
description: Test the exact current head of the one PRD delivery MR and post reproducible evidence
version: 0.2.0
---

# Test delivery head

## Fleet-wide execution rules

- Perform GitLab project, repository-metadata, MR, pipeline, discussion and comment operations only through the locked `glab` CLI or the installed official `glab` Skill. Do not substitute raw HTTP/`curl`, an ad-hoc SDK, a browser or manual UI work. Normal read-only `git` commands explicitly required by this Skill remain allowed.
- The card's designated Hermes shared `worktree` is the working copy of the Agent-owned delivery branch. Do not run routine `git fetch origin` or pull loops after dispatcher reconciliation. Fetch only to recover missing refs/worktree, investigate a proven local/remote head mismatch, or satisfy `live_reconcile_required`; record the reason. Use local `git rev-parse`/`git status` for tested code state and `glab` for current MR/pipeline state.
- A PRD omission or ambiguity is not by itself a blocking finding. Decide from explicit acceptance/constraints, current repository behavior and conventions, approved upstream artifacts, compatibility/security, then the smallest reversible scope. A decision is critical when it affects user-visible scope/acceptance, a public interface, data/migration, security/permissions, compatibility, recovery/rollback or a required test/gate. Put every critical decision in the idempotent gate MR comment's `## 关键自主决策` section and include that comment URL in completion `gitlab_urls`; use `无` in the existing gate comment when none was made, without posting a separate empty comment. Escalate only contradictory evidence with no safe acceptance-preserving test oracle, or a genuine permission, credential or capability failure.

1. Call `kanban_show()`; require dispatcher origin and verify project/run/worktree/branch/MR, approved artifact digests and current MR head equals the card's full 40-character expected SHA.
2. Read all approved SPEC/PLAN/TASKS plus the complete diff. Build a requirement/task-to-test coverage list.
3. Run the smallest complete reproducible suite: changed-area tests, required integration/contract tests, static checks and required pipeline state. Do not modify code or test files.
4. Use `fail` for implementation defects and `scope_gap` only for an evidenced artifact omission.
5. Re-read MR head immediately before posting. If it changed, post no pass and block for a fresh card. Otherwise post one idempotent v2 `test` gate bound to the exact `head_sha`, with commands/results, coverage and residual risk.
6. Complete pass/fail/scope_gap with one flat v2 metadata object conforming to `/opt/fleet/schemas/card-completion.schema.json`; repeat every required project/workspace/source field from the card at the top level. A pass must bind `head_sha`, `mr_iid` and `mr_url` to the tested current head. Never resolve findings, push, create another MR or merge.
