from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from hollysys_controller.gitlab import CheckedHeadConflict, GitLabClient
from hollysys_controller.kanban import CommandError
from hollysys_controller.models import Stage
from tests.helpers import completion, config, run_record


class FakeGitLab(GitLabClient):
    def __init__(self, cfg):
        super().__init__(cfg)
        self.mr = {
            "iid": 2,
            "web_url": "https://gitlab.example.com/group/project/-/merge_requests/2",
            "state": "opened",
            "sha": "d" * 40,
            "draft": False,
            "work_in_progress": False,
            "detailed_merge_status": "mergeable",
        }
        self.pipeline_status = "success"
        self.unresolved = False
        self.notes = []
        self.merge_fields = None
        self.merge_error = False
        self.refs = [{"type": "branch", "name": "feature/example-aaaaaaaa"}]
        self.artifact_path_result = ["docs/specs/feature/spec.md"]
        self.artifact_digest_result = "b" * 64
        self.api_calls = []

    def delivery_mr(self, run, mr_iid=None):
        return dict(self.mr)

    def api(self, endpoint, *, method="GET", fields=None):
        self.api_calls.append((endpoint, method, fields))
        if "/notes?" in endpoint:
            return self.notes
        if endpoint.endswith("/notes") and method == "POST":
            self.notes.append({"body": fields["body"]})
            return {"id": 99, "body": fields["body"]}
        if endpoint.endswith("/merge_requests/2") and method == "PUT":
            self.mr["state"] = "closed"
            return dict(self.mr)
        if "/refs?" in endpoint:
            return self.refs
        if "/pipelines?" in endpoint:
            return [{"status": self.pipeline_status}]
        if "/discussions?" in endpoint:
            return (
                [{"notes": [{"resolvable": True, "resolved": False}]}]
                if self.unresolved
                else []
            )
        if endpoint.endswith("/merge"):
            if self.merge_error:
                raise CommandError(["glab", "api"], 1, "409 Conflict")
            self.merge_fields = fields
            return {
                **self.mr,
                "state": "merged",
                "merge_commit_sha": "e" * 40,
            }
        raise AssertionError(f"unexpected API endpoint {endpoint}")

    def artifact_paths(self, project_id, ref, patterns):
        return list(self.artifact_path_result)

    def artifact_digest(self, project_id, ref, paths):
        return self.artifact_digest_result


class GitLabGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.client = FakeGitLab(config(self.root))
        self.run = run_record(self.root)
        self.test_meta = completion(
            self.root,
            Stage.TEST,
            mr_iid=2,
            mr_url=self.client.mr["web_url"],
            head_sha=self.client.mr["sha"],
        )
        self.review_meta = completion(
            self.root,
            Stage.CODE_REVIEW,
            mr_iid=2,
            mr_url=self.client.mr["web_url"],
            head_sha=self.client.mr["sha"],
        )
        self.client.notes = [
            {
                "author": {"id": 9, "username": "tester", "name": "Tester"},
                "body": (
                    "HOLLYSYS-GATE: v=5 run=hollysys-abcdefghijklmnopqrst "
                    "stage=test result=pass digest=na artifact=na "
                    f"head={'d' * 40} test=executed task=t_abc"
                ),
            },
            {
                "author": {
                    "id": 10,
                    "username": "code-reviewer",
                    "name": "Code Reviewer",
                },
                "body": (
                    "HOLLYSYS-GATE: v=5 run=hollysys-abcdefghijklmnopqrst "
                    "stage=code-review result=pass digest=na artifact=na "
                    f"head={'d' * 40} test=na task=t_abc"
                ),
            },
        ]

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_gate_note_requires_configured_identity_and_exact_head(self) -> None:
        self.client.notes = [
            {
                "author": {"id": 9, "username": "tester", "name": "Tester"},
                "body": (
                    "HOLLYSYS-GATE: v=5 run=hollysys-abcdefghijklmnopqrst "
                    "stage=test result=pass digest=na artifact=na "
                    f"head={'d' * 40} test=executed task=t_abc"
                ),
            }
        ]
        self.client.validate_gate(self.run, self.test_meta)
        self.client.notes[0]["author"]["username"] = "wrong-user"
        with self.assertRaisesRegex(ValueError, "allowed GitLab identity"):
            self.client.validate_gate(self.run, self.test_meta)

    def test_abort_delivery_comments_once_and_closes_open_mr(self) -> None:
        first = self.client.abort_delivery(
            self.run,
            requested_by="ou_owner",
            reason="human requested stop",
        )
        second = self.client.abort_delivery(
            self.run,
            requested_by="ou_owner",
            reason="human requested stop",
        )

        self.assertEqual(first["state"], "closed")
        self.assertEqual(second["state"], "closed")
        abort_notes = [
            note
            for note in self.client.notes
            if "[hollysys-aborted:v3]" in str(note.get("body"))
        ]
        self.assertEqual(len(abort_notes), 1)
        close_calls = [
            call
            for call in self.client.api_calls
            if call[1] == "PUT"
            and call[2] == {"state_event": "close"}
        ]
        self.assertEqual(len(close_calls), 1)

    def test_skipped_unavailable_test_is_bound_in_gate_marker(self) -> None:
        metadata = completion(
            self.root,
            Stage.TEST,
            mr_iid=2,
            mr_url=self.client.mr["web_url"],
            head_sha=self.client.mr["sha"],
            test_disposition="skipped_unavailable",
            skip_reason="browser runtime unavailable",
            verification=["browser preflight failed", "unit tests passed"],
            residual_risk=["browser flow remains unverified"],
        )
        self.client.notes = [
            {
                "author": {"id": 9, "username": "tester", "name": "Tester"},
                "body": (
                    "HOLLYSYS-GATE: v=5 run=hollysys-abcdefghijklmnopqrst "
                    "stage=test result=pass digest=na artifact=na "
                    f"head={'d' * 40} test=skipped_unavailable task=t_abc"
                ),
            }
        ]
        self.client.validate_gate(self.run, metadata)

    def test_repository_evidence_paths_must_exist_at_run_base(self) -> None:
        metadata = completion(
            self.root,
            Stage.IMPLEMENT,
            repository_evidence={
                "repository_base_sha": self.run.workspace.repository_base_sha,
                "inspected_paths": [
                    "docs/architecture.md",
                    "src/existing-module",
                ],
                "existing_capabilities": ["existing MES framework"],
                "change_strategy": "extend_and_modify",
                "reuse_decisions": ["reuse existing service and UI conventions"],
            },
        )
        checked: list[str] = []

        def fake_git(cwd, args, tolerate=False):
            checked.append(args[-1])
            return SimpleNamespace(
                returncode=(
                    0
                    if args[-1].endswith(
                        (":docs/architecture.md", ":src/existing-module")
                    )
                    else 1
                )
            )

        self.client._git = fake_git
        self.client.validate_repository_evidence(self.run, metadata)
        self.assertEqual(
            checked,
            [
                f"{'9' * 40}:docs/architecture.md",
                f"{'9' * 40}:src/existing-module",
            ],
        )

        invalid = completion(
            self.root,
            Stage.IMPLEMENT,
            repository_evidence={
                "repository_base_sha": self.run.workspace.repository_base_sha,
                "inspected_paths": ["src/invented-module"],
                "existing_capabilities": ["claimed capability"],
                "change_strategy": "extend_existing",
                "reuse_decisions": ["claimed reuse"],
            },
        )
        with self.assertRaisesRegex(ValueError, "does not exist at base"):
            self.client.validate_repository_evidence(self.run, invalid)

    def test_document_gate_artifact_commit_must_be_on_delivery_branch(self) -> None:
        metadata = completion(
            self.root,
            Stage.SPEC_REVIEW,
            artifact_paths=self.client.artifact_path_result,
            artifact_digest=self.client.artifact_digest_result,
            artifact_commit_sha="c" * 40,
            baseline_disposition="reviewed",
        )
        self.client.notes = [
            {
                "author": {
                    "id": 11,
                    "username": "spec-reviewer",
                    "name": "Spec Reviewer",
                },
                "body": (
                    "HOLLYSYS-GATE: v=5 run=hollysys-abcdefghijklmnopqrst "
                    "stage=spec-review result=pass "
                    f"digest={'b' * 64} artifact={'c' * 40} "
                    "head=na test=na task=t_abc"
                ),
            }
        ]
        self.client.validate_gate(self.run, metadata)
        self.client.refs = [{"type": "branch", "name": "another-branch"}]
        with self.assertRaisesRegex(ValueError, "delivery branch"):
            self.client.validate_gate(self.run, metadata)

    def test_finalization_requires_third_review_and_one_decision_note(self) -> None:
        mr_url = self.client.mr["web_url"]
        final_review_url = f"{mr_url}#note_31"
        decision_url = f"{mr_url}#note_32"
        metadata = completion(
            self.root,
            Stage.SPEC_WRITE,
            mode="finalization",
            mr_iid=2,
            mr_url=mr_url,
            artifact_paths=self.client.artifact_path_result,
            artifact_digest=self.client.artifact_digest_result,
            artifact_commit_sha="c" * 40,
            baseline_disposition="forced_after_review_limit",
            forced_advance={
                "review_limit": 3,
                "final_review_card_id": "t_review",
                "final_review_url": final_review_url,
                "decision_url": decision_url,
                "baseline_commit_sha": "c" * 40,
                "artifact_paths": self.client.artifact_path_result,
                "artifact_digest": self.client.artifact_digest_result,
                "key_decisions": ["Apply the stricter acceptance rule"],
                "unresolved_findings": ["conflicting PRD rules"],
                "residual_risks": ["manual compatibility check"],
            },
            gitlab_urls=[decision_url],
            key_decisions=["Apply the stricter acceptance rule"],
            residual_risk=["manual compatibility check"],
        )
        self.client.notes = [
            {
                "id": 31,
                "author": {"id": 11, "username": "spec-reviewer"},
                "body": (
                    "HOLLYSYS-GATE: v=5 run=hollysys-abcdefghijklmnopqrst "
                    "stage=spec-review result=fail "
                    f"digest={'b' * 64} artifact={'c' * 40} "
                    "head=na test=na task=t_review"
                ),
            },
            {
                "id": 32,
                "author": {"id": 12, "username": "spec-writer"},
                "body": (
                    "HOLLYSYS-FORCED-ADVANCE: v=1 "
                    "run=hollysys-abcdefghijklmnopqrst phase=spec "
                    "review_limit=3 task=t_abc"
                ),
            },
        ]
        self.client.validate_artifact_completion(self.run, metadata)

        self.client.notes.append(
            {
                "id": 33,
                "author": {"id": 12, "username": "spec-writer"},
                "body": self.client.notes[1]["body"],
            }
        )
        with self.assertRaisesRegex(ValueError, "exactly one idempotent"):
            self.client.validate_artifact_completion(self.run, metadata)

    def test_merge_rejects_head_pipeline_and_discussion_failures(self) -> None:
        changed = self.review_meta.model_copy(update={"head_sha": "f" * 40})
        with self.assertRaisesRegex(ValueError, "current MR head"):
            self.client.validate_merge(
                self.run, mr_iid=2, test=self.test_meta, code_review=changed
            )
        self.client.pipeline_status = "failed"
        with self.assertRaisesRegex(ValueError, "pipeline"):
            self.client.validate_merge(
                self.run,
                mr_iid=2,
                test=self.test_meta,
                code_review=self.review_meta,
            )
        self.client.pipeline_status = "success"
        self.client.unresolved = True
        with self.assertRaisesRegex(ValueError, "unresolved"):
            self.client.validate_merge(
                self.run,
                mr_iid=2,
                test=self.test_meta,
                code_review=self.review_meta,
            )

    def test_merge_always_sends_checked_head(self) -> None:
        result = self.client.merge(self.run, 2, "d" * 40)
        self.assertEqual(result["state"], "merged")
        self.assertEqual(self.client.merge_fields, {"sha": "d" * 40})

    def test_merge_sha_conflict_is_distinguished_from_other_errors(self) -> None:
        self.client.merge_error = True
        self.client.mr["sha"] = "e" * 40
        with self.assertRaisesRegex(CheckedHeadConflict, "head changed"):
            self.client.merge(self.run, 2, "d" * 40)

    def test_merge_requires_independent_test_and_review_users(self) -> None:
        self.client.notes[1]["author"]["id"] = 9
        with self.assertRaisesRegex(ValueError, "independent GitLab users"):
            self.client.validate_merge(
                self.run,
                mr_iid=2,
                test=self.test_meta,
                code_review=self.review_meta,
            )


if __name__ == "__main__":
    unittest.main()
