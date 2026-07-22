---
name: sdd-write-spec
description: Write or rework SPEC artifacts from one merged PRD
version: 0.1.0
---

# Write SPEC

1. Call `kanban_show()` and verify run/project/PRD commit and repository remote.
2. Read the PRD at the exact commit. Split only on independently understandable, testable and deliverable business scope.
3. Write `specs/<feature-key>/spec.md` with user stories, requirements, edge cases, success criteria and PRD traceability; exclude implementation choices.
4. Freeze an ordered `spec_queue`, dependencies and PRD coverage matrix. Surface gaps instead of inventing rules.
5. Reconcile stable branches/MRs before writes; follow project templates and the official `glab` skill.
6. Verify paths, commits and coverage. Complete with MR IIDs/heads, queue and residual risk; never review or merge.

