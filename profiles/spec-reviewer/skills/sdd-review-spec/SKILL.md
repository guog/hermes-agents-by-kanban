---
name: sdd-review-spec
description: Review a SPEC MR against its PRD and post an SDD-GATE
version: 0.1.0
---

# Review SPEC

1. Call `kanban_show()`; query the MR and verify its current head equals the card expected head.
2. Read the exact PRD and SPEC. Check scope, story independence, requirement testability, edge cases, success criteria, dependencies and full coverage.
3. Flag implementation leakage, invented rules, contradictions and missing acceptance as blocking findings.
4. Post one idempotent review using `/opt/fleet/templates/gate-comment.md` and the official `glab` skill. Bind run, stage `spec-review`, task and 40-char head.
5. Complete with `outcome=pass|fail`, MR/head, findings summary and verification. Do not edit or merge the SPEC.

