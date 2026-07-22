---
name: sdd-write-prd
description: Collaborate with a human in Feishu and prepare a reviewable PRD MR without starting delivery
version: 0.2.0
---

# Write PRD

1. Reply in the original Feishu chat/topic; @ the initiator in group/topic replies. Require an explicit allowlisted GitLab project and verify `archived=false`; never guess a repository from a display name.
2. Read referenced intake/evidence and repository PRD conventions. Separate facts, assumptions and unresolved questions; ask before a decision changes scope or acceptance.
3. Write `docs/prds/prd-<semantic-key>.md` with goals, users, scope, rules, edge cases, non-goals, acceptance and traceable sources; omit implementation design.
4. Reconcile the stable PRD branch/MR before writing, use repository conventions and the official `glab` Skill, and keep commits minimal.
5. Verify the PRD is self-contained. Create/update the PRD MR for human review, then reply in the original channel with path, commit and MR URL.
6. Never merge the PRD, start a formal Kanban run, create a delivery Task work item or impersonate dispatcher.
