---
name: sdd-write-prd
description: Turn approved intake into a reviewable PRD merge request without starting delivery
version: 0.1.0
---

# Write PRD

1. Read the Kanban card, referenced source and repository PRD conventions.
2. Separate evidence, assumptions and unresolved questions. Block on a missing decision that changes scope or acceptance.
3. Write goal, users, scope, rules, edge cases, non-goals, acceptance and traceable sources; omit implementation design.
4. Reconcile the stable source branch and MR before writing. Use the project MR template and the official `glab` skill.
5. Verify the PRD is self-contained and has no hidden blocking question.
6. Complete with PRD path, commit, MR IID/head and verification. Never merge, create a formal run or notify another worker.

