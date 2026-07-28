from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

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

    def delivery_mr(self, run, mr_iid=None):
        return dict(self.mr)

    def api(self, endpoint, *, method="GET", fields=None):
        if "/notes?" in endpoint:
            return self.notes
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
                    "HOLLYSYS-GATE: v=3 run=hollysys-abcdefghijklmnopqrst "
                    "stage=test result=pass digest=na review=na "
                    f"head={'d' * 40} task=t_abc"
                ),
            },
            {
                "author": {
                    "id": 10,
                    "username": "code-reviewer",
                    "name": "Code Reviewer",
                },
                "body": (
                    "HOLLYSYS-GATE: v=3 run=hollysys-abcdefghijklmnopqrst "
                    "stage=code-review result=pass digest=na review=na "
                    f"head={'d' * 40} task=t_abc"
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
                    "HOLLYSYS-GATE: v=3 run=hollysys-abcdefghijklmnopqrst "
                    "stage=test result=pass digest=na review=na "
                    f"head={'d' * 40} task=t_abc"
                ),
            }
        ]
        self.client.validate_gate(self.run, self.test_meta)
        self.client.notes[0]["author"]["username"] = "wrong-user"
        with self.assertRaisesRegex(ValueError, "allowed GitLab identity"):
            self.client.validate_gate(self.run, self.test_meta)

    def test_document_gate_review_commit_must_be_on_delivery_branch(self) -> None:
        metadata = completion(
            self.root,
            Stage.SPEC_REVIEW,
            artifact_paths=self.client.artifact_path_result,
            artifact_digest=self.client.artifact_digest_result,
            review_commit_sha="c" * 40,
        )
        self.client.notes = [
            {
                "author": {
                    "id": 11,
                    "username": "spec-reviewer",
                    "name": "Spec Reviewer",
                },
                "body": (
                    "HOLLYSYS-GATE: v=3 run=hollysys-abcdefghijklmnopqrst "
                    "stage=spec-review result=pass "
                    f"digest={'b' * 64} review={'c' * 40} "
                    "head=na task=t_abc"
                ),
            }
        ]
        self.client.validate_gate(self.run, metadata)
        self.client.refs = [{"type": "branch", "name": "another-branch"}]
        with self.assertRaisesRegex(ValueError, "delivery branch"):
            self.client.validate_gate(self.run, metadata)

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
