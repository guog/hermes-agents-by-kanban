---
name: sdd-dispatch-kanban
description: Start, recover and merge one-PRD one-MR delivery runs across allowlisted GitLab projects
version: 0.3.0
---

# SDD Kanban dispatcher

Use this Skill only for formal run intake, deterministic stage transitions, recovery, checked-head merge, status and original-channel notification. Never write professional SPEC/PLAN/TASKS/code or their reviews.

## Intake

1. Accept only `实现 PRD <exact blob/raw URL> <merged PRD MR URL>`. If either URL is missing, their host/project IDs differ, or repository identity is ambiguous, ask only for the missing/correct value in the original Feishu channel and do not create a card.
2. In order, query GitLab for: allowed host/group; readable project; PRD path; `archived=false`; MR merged to current `default_branch`; PRD included in that merged revision; current default-branch PRD still equals that effective version. Do not call an unreadable project “文件不存在”.
3. Use `project_id` and `path_with_namespace` as identity. Project description Chinese text is display-only.
4. Derive `run_key = sdd-<base32(sha256(host|project_id|prd_path|prd_commit_sha))[0:20]>`. Query Kanban and GitLab before creating anything: resume an active run, return a merged result, or start only a new PRD commit.
5. Read the current `default_branch` HEAD as the run base SHA. Select the repository's branch convention; otherwise use `feature/<prd-basename>-<prd_sha8>`. Run `/opt/fleet/scripts/prepare-run-workspace.sh` with that base and the validated token-free HTTPS URL. Use board `gitlab-p<project_id>` and the returned shared worktree.
6. Create one `run-init` card with a stable message/run idempotency key, `tenant=run_key`, `created_by=dispatcher`, exact assignee/skills and the v2 card template. Preserve `message_id`, `chat_id`, `thread_id` and initiator for recovery.

## Card and gate reconciliation

1. Call `kanban_show()`; validate completion metadata against `/opt/fleet/schemas/card-completion.schema.json`. Refuse any next card whose stored or returned `created_by` is not exactly `dispatcher`.
2. Every card must repeat project ID/path/display name, checkout/worktree, shared branch/target, PRD path/commit/MR, run, delivery MR and expected head. Reconcile GitLab live state before every write.
3. Advance only through the worker/continuation pair protocol below. `kanban_create` and `kanban_link` are dispatcher-only; never delegate graph shaping.
4. Artifact gates use sorted path/blob-SHA digest plus reviewer identity and `review_commit_sha`. Recompute the stage path set at every transition. A changed approved artifact invalidates that gate and every downstream gate; later PLAN/code additions do not invalidate an unchanged SPEC digest.
5. `fail` returns to the owning producer. `scope_gap` routes by evidence to TASKS, PLAN or SPEC; never allow coder to expand scope. Design rework is limited to 3 iterations per stage; code rework is limited to 5 total.
6. Tester and code-reviewer gates must be separate comments by allowed identities and bind the same current MR `head_sha`. Any push invalidates both.

### Worker/continuation pair protocol

Every dispatcher gate, including `run-init` and every resumed dispatcher continuation, creates one logical pair before it completes:

1. Derive `transition_key = <run_key>:<next-stage>:<iteration>`. Reconcile cards by exact `idempotency_key`; never infer identity from title text.
2. Create or reuse work card `W` with key `<transition_key>:work`, exact stage/assignee/Skills, `created_by=dispatcher`, and parent equal to the current dispatcher gate.
3. Create or reuse dispatcher continuation card `C` with key `<transition_key>:continue`, assignee `dispatcher`, and parent equal to `W`. `C` records only the expected parent card/stage and `live_reconcile_required=true`.
4. Verify both cards and both parent relationships. Only then complete the current gate with `next_card_ids=[W,C]`. If either create/link fails, leave the current gate running or blocked and retry the same keys; do not complete it.
5. Completing the current gate promotes only `W` to `ready`. Completing `W` promotes `C` to `ready`. `C` must read `W` completion metadata, validate it against the schema, then re-read GitLab project/branch/MR/comments/pipeline/discussions/current head before deciding the next pair.
6. Never put a predicted `head_sha`, `artifact_digest`, review result, pipeline result or merge result into `C`. Unknown live facts stay null until `C` runs. Duplicate messages, dispatcher restarts and worker retries must reuse the same pair.

Use the same protocol for every transition: write, independent review, any design/code rework, test, code review and merge. The merge work card is assigned to `dispatcher` and is followed by a `run-complete` continuation; `run-complete` becomes ready only after the checked-head merge card completes.

## Single MR lifecycle and merge

1. spec-writer creates exactly one `Draft: [PRD] <prd-basename>.md` MR after the first valid SPEC commit. Every later producer updates that branch/MR; every reviewer/tester comments there. Coder marks it ready after implementation and self-test.
2. Before merge require ready, mergeable, required pipeline successful, blocking discussions resolved, current artifact digests approved, and tester/code-reviewer pass on one `checked_head`.
3. The merge work card and its completion metadata must carry `head_sha=<checked_head>` and `checked_head=<checked_head>`. Merge through GitLab with `sha=<checked_head>`. On SHA mismatch create fresh test/review pairs; never retry with an unchecked SHA.
4. Use returned `merge_commit_sha` to post one idempotent MR comment with permanent PRD/SPEC/PLAN/TASKS blob links. Run `/opt/fleet/scripts/cleanup-run-worktree.sh`, complete the run, and notify the original channel/thread; @ the initiator in group/topic replies.

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

Block with a concrete human action only for `needs_input`, `capability`, credential/environment failure or exhausted budget. Never create a GitLab Task work item for formal delivery and never run a second Feishu inbound consumer.
