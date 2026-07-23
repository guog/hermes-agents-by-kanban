---
name: sdd-dispatch-kanban
description: Start, recover and merge one-PRD one-MR delivery runs across allowlisted GitLab projects
version: 0.3.0
---

# SDD Kanban dispatcher

Use this Skill only for formal run intake, deterministic stage transitions, recovery, checked-head merge, status and original-channel notification. Never write professional SPEC/PLAN/TASKS/code or their reviews.

## Fleet-wide execution rules

- Perform GitLab project, repository-metadata, MR, pipeline, discussion, comment and merge operations only through the locked `glab` CLI or the installed official `glab` Skill. Do not substitute raw HTTP/`curl`, an ad-hoc SDK, a browser or manual UI work. Normal `git` commands explicitly required by this Skill remain allowed for checkout/worktree preparation, inspection, commit and push.
- The card's designated Hermes shared `worktree` is the only editable working copy of the Agent-owned delivery branch. After initial preparation/recovery, do not run routine `git fetch origin` or pull loops between stages. Fetch only to create/recover missing refs or a worktree, investigate a proven local/remote head mismatch or rejected push, or satisfy `live_reconcile_required`; record the reason. Use local `git rev-parse`/`git status` for worktree state and `glab` for current MR, pipeline and discussion state.
- A PRD omission or ambiguity is not by itself `needs_input`. Decide from explicit acceptance/constraints, current repository behavior and conventions, approved upstream artifacts, compatibility/security, then the smallest reversible scope. A decision is critical when it affects user-visible scope/acceptance, a public interface, data/migration, security/permissions, compatibility, recovery/rollback or a required test/gate. If the delivery MR exists, reconcile one idempotent MR comment containing all critical decisions made by this card using `/opt/fleet/templates/decision-comment.md` and include its URL in completion `gitlab_urls`. Before the first MR exists, carry those decisions explicitly into the spec-writer card so it can put them in the initial MR description. Do not post an empty comment when no critical decision was made. Ask a human only when evidence is contradictory and no safe choice preserves acceptance, or a permission, credential or capability is genuinely missing.

## Intake

1. Accept only `实现 PRD <exact blob/raw URL> <merged PRD MR URL>`. If either URL is missing, their host/project IDs differ, or repository identity is ambiguous, ask only for the missing/correct value in the original Feishu channel and do not create a card.
2. In order, query GitLab for: allowed host/group; readable project; PRD path; `archived=false`; MR merged to current `default_branch`; PRD included in that merged revision; current default-branch PRD still equals that effective version. Do not call an unreadable project “文件不存在”.
3. Use `project_id` and `path_with_namespace` as identity. Project description Chinese text is display-only.
4. Derive `run_key = sdd-<base32(sha256(host|project_id|prd_path|prd_commit_sha))[0:20]>`. Query Kanban and GitLab before creating anything: resume an active run, return a merged result, or start only a new PRD commit.
5. Read the current `default_branch` HEAD as the run base SHA. Select the repository's branch convention; otherwise use `feature/<prd-basename>-<prd_sha8>`. Derive:
   - checkout: `/workspace/projects/p<project_id>-<repo-slug>`
   - worktree: `/workspace/projects/worktrees/p<project_id>/<run_key>`
   - board: `gitlab-p<project_id>`
6. Prepare the workspace with standard `git` and `hermes` commands, without a fleet helper script:
   - Require a positive numeric project ID, a lowercase filesystem-safe repo slug, a valid `run_key`, a full lowercase base SHA and a branch accepted by `git check-ref-format --branch`.
   - Require the token-free clone URL and any existing `origin` to equal the validated `https://<GITLAB_HOST>/<path_with_namespace>[.git]`; require the project to remain inside `GITLAB_ALLOWED_GROUPS`.
   - Create the parent directories. Clone only when the checkout is absent. Fetch only for first preparation, missing refs, proven divergence, rejected push or explicit live reconciliation.
   - Verify `base_sha^{commit}`. Reuse an existing worktree only when its branch is exactly the expected branch; otherwise create it from the existing local branch, tracked remote branch or base SHA, in that order.
   - Create or reconcile the board with `hermes -p dispatcher kanban boards create <board>`, using the project display name and checkout as its default workdir.
7. Create one `run-init` card with a stable message/run idempotency key, `tenant=run_key`, `created_by=dispatcher`, exact assignee/skills and the v2 card template. Preserve `message_id`, `chat_id`, `thread_id` and initiator for recovery.

## Card and gate reconciliation

1. Call `kanban_show()`; validate the single flat completion metadata object against `/opt/fleet/schemas/card-completion.schema.json`. Card-body `identity`/`workspace`/`source`/`delivery` sections and comments do not fill missing completion fields. Refuse any next card whose stored or returned `created_by` is not exactly `dispatcher`.
2. Every card must repeat project ID/path/display name, checkout/worktree, shared branch/target, PRD path/commit/MR, run, delivery MR and expected head. Reconcile GitLab live state before every write.
3. Advance only through the worker/continuation pair protocol below. Fleet policy reserves `kanban_create` and `kanban_link` for dispatcher and forbids delegating graph shaping. The unmodified official Hermes runtime does not enforce this policy in a patched handler, so always re-check `created_by=dispatcher` before trusting a card.
4. Artifact gates use sorted path/blob-SHA digest plus reviewer identity and `review_commit_sha`. Recompute the stage path set at every transition. A changed approved artifact invalidates that gate and every downstream gate; later PLAN/code additions do not invalidate an unchanged SPEC digest.
5. `fail` returns to the owning producer. `scope_gap` routes by evidence to TASKS, PLAN or SPEC; never allow coder to expand scope. Design rework is limited to 3 iterations per stage; code rework is limited to 5 total.
6. Tester and code-reviewer gates must be separate comments by allowed identities and bind the same current MR `head_sha`. Any push invalidates both.

### Worker/continuation pair protocol

Every dispatcher gate, including `run-init` and every resumed dispatcher continuation, creates one logical pair before it completes:

1. Derive `transition_key = <run_key>:<next-stage>:<iteration>`. Reconcile cards by exact `idempotency_key`; never infer identity from title text.
2. Create or reuse work card `W` with key `<transition_key>:work`, exact stage/assignee/Skills, `created_by=dispatcher`, and parent equal to the current dispatcher gate.
3. Create or reuse dispatcher continuation card `C` with key `<transition_key>:continue`, assignee `dispatcher`, and parent equal to `W`. `C` records only the expected parent card/stage and `live_reconcile_required=true`.
4. Verify both cards and both parent relationships. Only then complete the current gate with `next_card_ids=[W,C]`. If either create/link fails, leave the current gate running or blocked and retry the same keys; do not complete it.
5. Completing the current gate promotes only `W` to `ready`. Completing `W` promotes `C` to `ready`. `C` must read `W` completion metadata, validate it against the schema, then re-read GitLab project/branch/MR/comments/pipeline/discussions/current head before deciding the next pair. For a historical invalid handoff, create nothing and accept only an audited `kanban edit` backfill or a fresh review/work pair; never infer missing fields or approve a schema exception.
6. Never put a predicted `head_sha`, `artifact_digest`, review result, pipeline result or merge result into `C`. Unknown live facts stay null until `C` runs. Duplicate messages, dispatcher restarts and worker retries must reuse the same pair.

Use the same protocol for every transition: write, independent review, any design/code rework, test, code review and merge. The merge work card is assigned to `dispatcher` and is followed by a `run-complete` continuation; `run-complete` becomes ready only after the checked-head merge card completes.

## Single MR lifecycle and merge

1. spec-writer creates exactly one `Draft: [PRD] <prd-basename>.md` MR after the first valid SPEC commit. Every later producer updates that branch/MR; every reviewer/tester comments there. Coder marks it ready after implementation and self-test.
2. Before merge require ready, mergeable, required pipeline successful, blocking discussions resolved, current artifact digests approved, and tester/code-reviewer pass on one `checked_head`.
3. The merge work card and its completion metadata must carry `head_sha=<checked_head>` and `checked_head=<checked_head>`. Merge through GitLab with `sha=<checked_head>`. On SHA mismatch create fresh test/review pairs; never retry with an unchecked SHA.
4. Use returned `merge_commit_sha` to post one idempotent MR comment with permanent PRD/SPEC/PLAN/TASKS blob links.
5. Clean up without a fleet helper script: derive the checkout/worktree again only from the validated project ID, repo slug and run key; require the checkout to remain the expected Git repository; require the delivery MR to be merged and the worktree to have no uncommitted changes; then run `git -C <checkout> worktree remove <worktree>` followed by `git -C <checkout> worktree prune`. Never recursively delete a derived path.
6. Complete the run and notify the original channel/thread; @ the initiator in group/topic replies.

Every created card sets the exact assignee and Skill list:

| Assignee | Skills |
| --- | --- |
| dispatcher | `sdd-dispatch-kanban`, `glab`, `lark-shared`, `lark-im` |
| spec-writer | `sdd-write-spec`, `glab` |
| spec-reviewer | `sdd-review-spec`, `glab` |
| planner | `sdd-write-plan`, `glab` |
| plan-reviewer | `sdd-review-plan`, `glab` |
| tasker | `sdd-create-tasks`, `glab` |
| task-reviewer | `sdd-analyze-tasks`, `glab` |
| coder | `sdd-implement`, `glab` |
| tester | `sdd-test`, `glab` |
| code-reviewer | `sdd-review-code`, `glab` |

Block with a concrete human action only for missing required intake identity, contradictory requirements with no safe acceptance-preserving choice, `capability`, credential/environment failure or exhausted budget. PRD ambiguity alone is not a reason to block. Never create a GitLab Task work item for formal delivery and never run a second Feishu inbound consumer.
