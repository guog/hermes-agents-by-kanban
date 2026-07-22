---
name: sdd-write-plan
description: Write the complete PLAN set for approved SPECs on the shared delivery branch
version: 0.2.0
---

# Write PLAN set

1. Call `kanban_show()`; require dispatcher origin and verify exact project, shared worktree/branch, PRD, delivery MR and approved SPEC digest.
2. Read repository rules, architecture and relevant code. For each `spec-<key>.md`, create exactly `docs/prds/<prd-basename>/plans/plan-<key>.md`; do not rename existing docs.
3. Cover architecture, interfaces, data/migration, compatibility, observability, security, testing, rollback and risks. Trace decisions to SPEC requirements without changing intent or defining final Task IDs.
4. On rework, preserve the one-to-one keys and change only affected PLANs. Report `scope_gap` when SPEC lacks a material decision.
5. Commit minimal coherent PLAN changes and push the same branch/MR. Never create a PLAN MR.
6. Complete with sorted PLAN paths/blob SHAs, commit/head, MR, verification and residual risk. Do not write TASKS/code, review or merge.
