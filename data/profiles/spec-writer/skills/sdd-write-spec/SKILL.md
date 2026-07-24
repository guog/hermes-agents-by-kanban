---
name: sdd-write-spec
description: Write or rework the complete SPEC set for one PRD on its shared delivery branch
version: 0.2.1
---

# Write SPEC set

## Fleet-wide execution rules

- Perform GitLab project, repository-metadata, MR, pipeline, discussion and comment operations only through the locked `glab` CLI or the installed official `glab` Skill. Do not substitute raw HTTP/`curl`, an ad-hoc SDK, a browser or manual UI work. Normal `git` commands explicitly required by this Skill remain allowed for worktree inspection, commit and push.
- The card's designated Hermes shared `worktree` is the only editable working copy of the Agent-owned delivery branch. Do not run routine `git fetch origin` or pull loops after dispatcher reconciliation. Fetch only to recover missing refs/worktree, investigate a proven local/remote head mismatch or rejected push, or satisfy `live_reconcile_required`; record the reason. Use local `git rev-parse`/`git status` for worktree state and `glab` for current MR state.
- A PRD omission or ambiguity is not by itself `needs_input`. Decide from explicit acceptance/constraints, current repository behavior and conventions, approved upstream artifacts, compatibility/security, then the smallest reversible scope. A decision is critical when it affects user-visible scope/acceptance, a public interface, data/migration, security/permissions, compatibility, recovery/rollback or a required test/gate. Before first MR creation, put every handed-off pre-MR decision and every critical SPEC decision directly in the `## 关键自主决策` section of `/opt/fleet/templates/mr-description.md`; on SPEC rework update that same section instead of posting an initial decision comment. Record non-critical assumptions in the SPEC or completion evidence. Escalate only contradictory evidence with no safe acceptance-preserving choice, or a genuine permission, credential or capability failure.

1. Call `kanban_show()` and require `created_by=dispatcher`; verify project ID/path, shared worktree/branch, exact PRD path/commit/MR and run key against GitLab. Never discover or clone another repository.
2. Read the PRD at the exact commit. Split only into independently understandable and testable business scopes; define ordered keys, dependencies and a full PRD coverage matrix.
3. Write every `docs/prds/<prd-basename>/specs/spec-<key>.md` from the Chinese `/opt/fleet/templates/spec-template.md`. Keys are stable lowercase kebab-case. Replace every placeholder, retain all mandatory sections and the PRD coverage matrix, and exclude implementation choices.
4. On rework, change only evidence-backed SPEC files and preserve keys unless the finding requires a set change. Do not write PLAN/TASKS/code.
5. Commit the smallest coherent SPEC change with the repository's conventional commit rules and push the shared branch. After the first valid SPEC commit, reconcile, populate the MR description including `## 关键自主决策` (`无` when none), then create exactly one `Draft: [PRD] <prd-basename>.md` MR using `/opt/fleet/templates/mr-description.md`; otherwise update that MR and its decision section.
6. Complete with sorted SPEC paths/blob SHAs, commit/head, MR IID/URL, coverage and residual risk. Pass one flat v2 metadata object conforming to `/opt/fleet/schemas/card-completion.schema.json`; repeat every required project/workspace/source field from the card at the top level, using schema-allowed nulls/empty arrays without omitting keys. Include the MR URL in `gitlab_urls` as the critical-decision audit location. Do not review, mark ready or merge.
