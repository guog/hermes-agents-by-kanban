---
name: sdd-dispatch-kanban
description: Start and reconcile a serial PRD-to-code run with Hermes Kanban and GitLab gates
version: 0.1.0
---

# SDD Kanban dispatcher

Use only for formal run start, deterministic stage gates, recovery, checked-head merge, status and notifications.

## Start

1. Accept `实现 PRD <merged GitLab MR URL>` or explicit project/file/40-char commit.
2. Verify allowlisted sender and GitLab host/group, merged MR, file existence, target-branch reachability, no blocking PRD question, and no active run for the same project+commit.
3. Derive `run_key = sdd-<base32(sha256(host|project_id|prd_commit))[0:20]>` and use the Feishu message ID in the card idempotency key.
4. Create exactly one `RUN-INIT` card on the project board with `assignee=dispatcher`, `tenant=run_key`, explicit worktree workspace and the card template at `/opt/fleet/templates/kanban-card.md`.
5. Acknowledge with run key and links; future progress must not depend on the chat session.

## Gate/reconcile

1. Call `kanban_show()` and validate run, project, spec, stage, iteration, parent and completion metadata against `/opt/fleet/schemas/card-completion.schema.json`.
2. Query GitLab live state before every write. Reuse an existing branch, MR, comment, merge or notification on replay.
3. Accept an `SDD-GATE` only from the allowed role identity when run, stage, task and current 40-char head all match. A later fail/scope_gap for the same head wins.
4. On professional `fail`, complete the gate and create the bounded rework chain. Do not count it as a worker crash.
5. On `scope_gap`, route to TASKS convergence; never let coder silently change approved intent.
6. Before merge require non-draft, mergeable current MR, successful required pipeline, resolved blocking discussions and all required gates on the same head.
7. Merge with GitLab API `sha=<checked_head>`. A mismatch blocks and causes fresh gates; never retry with an unchecked SHA.
8. Create the next typed card with a stable idempotency key and current gate as parent, then complete the gate card.

Every created card must set the exact assignee and `skills` list; never rely on semantic auto-selection:

| Assignee | skills |
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

## Serial completion

- Do not activate another SPEC before the current code MR is merged.
- Design rework limit is 3; code rework limit is 5.
- Finish only after every frozen SPEC has a merged code MR and traceable artifact commits.
- Send only the events defined in `/opt/fleet/templates/feishu-messages.md`, with stable idempotency keys. Hermes gateway is the only inbound consumer; never run `lark-cli event consume im.message.receive_v1`.
- Block for needs_input/capability/transient with a concrete human action. Never implement another role's work.
