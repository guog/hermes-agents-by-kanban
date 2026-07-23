---
name: sdd-implement
description: Implement or rework all approved TASKS on the one shared PRD delivery MR
version: 0.2.0
---

# Implement PRD

## Fleet-wide execution rules

- Perform GitLab project, repository-metadata, MR, pipeline, discussion and comment operations only through the locked `glab` CLI or the installed official `glab` Skill. Do not substitute raw HTTP/`curl`, an ad-hoc SDK, a browser or manual UI work. Normal `git` commands explicitly required by this Skill remain allowed for worktree inspection, commit and push.
- The card's designated Hermes shared `worktree` is the only editable working copy of the Agent-owned delivery branch. Do not run routine `git fetch origin` or pull loops after dispatcher reconciliation. Fetch only to recover missing refs/worktree, investigate a proven local/remote head mismatch or rejected push, or satisfy `live_reconcile_required`; record the reason. Use local `git rev-parse`/`git status` for worktree state and `glab` for current MR state.
- A PRD omission or ambiguity is not by itself `scope_gap` or `needs_input`. Decide from explicit acceptance/constraints, current repository behavior and conventions, approved upstream artifacts, compatibility/security, then the smallest reversible scope. A decision is critical when it affects user-visible scope/acceptance, a public interface, data/migration, security/permissions, compatibility, recovery/rollback or a required test/gate. Before completion, reconcile one idempotent delivery-MR comment containing every critical decision made by this card using `/opt/fleet/templates/decision-comment.md` and include its URL in completion `gitlab_urls`; do not post an empty comment when none was made. Record non-critical assumptions in code comments only when needed or in completion evidence. Escalate only contradictory evidence with no safe acceptance-preserving choice, or a genuine permission, credential or capability failure.

1. Call `kanban_show()`; require dispatcher origin and verify exact project, shared worktree/branch, PRD, delivery MR and approved SPEC/PLAN/TASKS digests. Never create another checkout, branch or MR.
2. Reconcile current branch, commits, MR comments, pipeline and existing partial implementation before writing. Execute the complete task DAG in dependency order and preserve unrelated repository changes.
3. Add or update required tests. Commit/push minimal coherent implementation or rework units using repository conventions.
4. Run proportionate formatting, static, unit, integration/contract checks and record exact commands/results in the same MR description.
5. Resolve ordinary omissions and ambiguity with the decision hierarchy above. If approved artifacts are contradictory or cannot support any safe acceptance-preserving implementation, stop with `scope_gap` evidence and the owning upstream stage; never change explicit requirement intent.
6. After every TASK is covered and self-tests pass, update `/opt/fleet/templates/mr-description.md` metadata and mark the existing Draft MR ready. On code rework, keep it ready unless GitLab policy requires draft while changing.
7. Complete with task coverage, changed files, commands, MR/current head and residual risk. Pass one flat v2 metadata object conforming to `/opt/fleet/schemas/card-completion.schema.json`; repeat every required project/workspace/source field from the card at the top level, using schema-allowed nulls/empty arrays without omitting keys. Do not self-review or merge.
