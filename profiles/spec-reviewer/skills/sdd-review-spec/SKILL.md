---
name: sdd-review-spec
description: Review the complete SPEC set and post a digest-bound gate on the shared MR
version: 0.2.0
---

# Review SPEC set

1. Call `kanban_show()`; require a dispatcher-created card and verify project/run/worktree/branch/PRD/shared MR identity. Read live GitLab state but do not require the whole MR head to remain frozen afterward.
2. Enumerate the complete sorted `spec-<key>.md` set. Read the exact PRD and check full coverage, scope fidelity, independence, testability, boundaries, success criteria, dependencies, contradictions and implementation leakage.
3. Compute the artifact digest from sorted `<path>\0<git-blob-sha>\n` rows at the review commit. Post one idempotent v2 `spec-review` gate using `/opt/fleet/templates/gate-comment.md`, with paths, digest and `review_commit_sha`.
4. Use `fail` for SPEC defects. Record a genuine unresolved PRD conflict as a blocking scope finding; do not invent business decisions.
5. Complete with pass/fail, digest, review commit, MR and findings. Never edit artifacts, push, create another MR or merge.
