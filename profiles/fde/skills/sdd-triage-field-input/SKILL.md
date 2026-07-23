---
name: sdd-triage-field-input
description: Collaborate in Feishu to classify, sanitize and record field evidence as a normal GitLab Issue
version: 0.2.0
---

# Triage field input

## Fleet-wide execution rules

- Perform GitLab project, repository-metadata, file, Issue, discussion and comment operations only through the locked `glab` CLI or the installed official `glab` Skill. Do not substitute raw HTTP/`curl`, an ad-hoc SDK, a browser or manual UI work.
- Ambiguity is not by itself a reason to interrupt the human. Classify and choose the next reversible action from supplied evidence, repository conventions, compatibility/security and the smallest reversible sufficient scope; record assumptions and uncertainty in the Issue. If the intake explicitly references a PRD MR and FDE makes a critical decision affecting user-visible scope/acceptance, an interface, data, security, compatibility, recovery or testing, reconcile an idempotent MR comment using `/opt/fleet/templates/decision-comment.md` and return its URL; otherwise FDE does not own formal delivery decisions. Ask only for evidence required to make the Issue useful, an irreversible/product-scope choice with contradictory evidence, authorization, or a genuine permission, credential or capability failure.

1. Reply in the original Feishu chat/topic; @ the initiator in group/topic replies. Confirm the allowlisted project, product version and environment; use project ID/path as identity and never guess from a Chinese display name or acronym.
2. Classify the input as defect, feature request, configuration/environment issue or insufficient evidence.
3. Record minimum reproduction, expected/actual result, impact, frequency, evidence and uncertainty. Redact credentials, personal data and sensitive raw logs before any upload.
4. Read repository content only. Reconcile an existing intake Issue before creating a normal GitLab Issue; FDE has no Git commit identity and never pushes.
5. Reply with the Issue URL, classification and precise next action; ask for missing evidence or authorization when required.
6. Never create a formal SDD run or a GitLab Task work item and never implement code.
