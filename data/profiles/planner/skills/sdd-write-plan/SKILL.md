---
name: sdd-write-plan
description: Write the complete PLAN set for approved SPECs on the shared delivery branch
version: 0.2.1
---

# Write PLAN set

## Fleet-wide execution rules

- Perform GitLab project, repository-metadata, MR, pipeline, discussion and comment operations only through the locked `glab` CLI or the installed official `glab` Skill. Do not substitute raw HTTP/`curl`, an ad-hoc SDK, a browser or manual UI work. Normal `git` commands explicitly required by this Skill remain allowed for worktree inspection, commit and push.
- The card's designated Hermes shared `worktree` is the only editable working copy of the Agent-owned delivery branch. Do not run routine `git fetch origin` or pull loops after dispatcher reconciliation. Fetch only to recover missing refs/worktree, investigate a proven local/remote head mismatch or rejected push, or satisfy `live_reconcile_required`; record the reason. Use local `git rev-parse`/`git status` for worktree state and `glab` for current MR state.
- A PRD omission or ambiguity is not by itself `scope_gap` or `needs_input`. Decide from explicit acceptance/constraints, current repository behavior and conventions, approved upstream artifacts, compatibility/security, then the smallest reversible scope. A decision is critical when it affects user-visible scope/acceptance, a public interface, data/migration, security/permissions, compatibility, recovery/rollback or a required test/gate. Before completion, reconcile one idempotent delivery-MR comment containing every critical decision made by this card using `/opt/fleet/templates/decision-comment.md` and include its URL in completion `gitlab_urls`; do not post an empty comment when none was made. Record non-critical assumptions in the PLAN or completion evidence. Escalate only contradictory evidence with no safe acceptance-preserving choice, or a genuine permission, credential or capability failure.

1. Call `kanban_show()`; require dispatcher origin and verify exact project, shared worktree/branch, PRD, delivery MR and approved SPEC digest.
2. Read repository rules, architecture and relevant code. For each `spec-<key>.md`, create exactly `docs/prds/<prd-basename>/plans/plan-<key>.md` from the Chinese `/opt/fleet/templates/plan-template.md`; replace every placeholder and do not rename existing docs.
3. Retain the template's mandatory technical context, governance checks, decisions, architecture, interfaces, data/migration, compatibility, observability, security, testing, rollback, real project structure, traceability and risk sections. Trace decisions to SPEC requirements without changing intent or defining final Task IDs.
4. On rework, preserve the one-to-one keys and change only affected PLANs. Resolve missing design decisions with the hierarchy above; report `scope_gap` only when approved requirements are contradictory or cannot support any safe acceptance-preserving plan.
5. Commit minimal coherent PLAN changes and push the same branch/MR. Never create a PLAN MR.
6. Complete with sorted PLAN paths/blob SHAs, commit/head, MR, verification and residual risk. Pass one flat v2 metadata object conforming to `/opt/fleet/schemas/card-completion.schema.json`; repeat every required project/workspace/source field from the card at the top level, using schema-allowed nulls/empty arrays without omitting keys. Do not write TASKS/code, review or merge.
