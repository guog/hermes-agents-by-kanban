---
name: sdd-write-prd
description: Collaborate with a human in Feishu and prepare a reviewable PRD MR without starting delivery
version: 0.2.0
---

# Write PRD

## Fleet-wide execution rules

- Perform GitLab project, repository-metadata, file, branch, MR, discussion and comment operations only through the locked `glab` CLI or the installed official `glab` Skill. Do not substitute raw HTTP/`curl`, an ad-hoc SDK, a browser or manual UI work. Normal `git` commands explicitly required by this Skill remain allowed for branch inspection, commit and push.
- A missing or ambiguous product detail is not by itself a reason to interrupt the human. Decide from explicit goals/acceptance/constraints, supplied evidence, current repository behavior and conventions, compatibility/security, then the smallest reversible scope. A decision is critical when it affects user-visible scope/acceptance, a public interface, data/migration, security/permissions, compatibility, recovery/rollback or a required test/gate. After the PRD MR exists, reconcile one idempotent MR comment containing every critical decision made in this PRD-writing turn using `/opt/fleet/templates/decision-comment.md`; include the comment URL in the original-channel result. Do not post an empty comment when none was made. Ask only when evidence is contradictory and no safe acceptance-preserving choice exists, or required project identity, permission, credential or capability is missing.

1. Reply in the original Feishu chat/topic; @ the initiator in group/topic replies. Require an explicit allowlisted GitLab project and verify `archived=false`; never guess a repository from a display name.
2. Read referenced intake/evidence and repository PRD conventions. Separate facts, assumptions and unresolved questions; resolve ordinary ambiguity with the hierarchy above instead of repeatedly asking the human.
3. Write `docs/prds/prd-<semantic-key>.md` with goals, users, scope, rules, edge cases, non-goals, acceptance and traceable sources; omit implementation design.
4. Reconcile the stable PRD branch/MR before writing, use repository conventions and the official `glab` Skill, and keep commits minimal.
5. Verify the PRD is self-contained. Create/update the PRD MR for human review, then reply in the original channel with path, commit and MR URL.
6. Never merge the PRD, start a formal Kanban run, create a delivery Task work item or impersonate dispatcher.
