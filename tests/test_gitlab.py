from __future__ import annotations

import base64
import os
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired
from types import SimpleNamespace
from unittest.mock import patch

from hollysys_controller.errors import (
    ControllerFatalError,
    DependencyAuthError,
    DependencyContractError,
    DependencyRateLimitedError,
    DependencyTransientError,
    ErrorContext,
    MergeBlocked,
)
from hollysys_controller.gitlab import CheckedHeadConflict, GitLabClient
from hollysys_controller.models import Stage
from tests.helpers import completion, config, run_record, write_profile_env


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
            "source_branch": "feature/example-aaaaaaaa",
            "target_branch": "main",
        }
        self.pipeline_status = "success"
        self.unresolved = False
        self.notes = []
        self.merge_fields = None
        self.merge_error = False
        self.run_branch = "feature/example-aaaaaaaa"
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
        if "/notes/" in endpoint and method == "GET":
            note_id = int(endpoint.rsplit("/", 1)[1])
            return next(
                note for note in self.notes if int(note.get("id") or 0) == note_id
            )
        if endpoint.endswith("/notes") and method == "POST":
            self.notes.append({"body": fields["body"]})
            return {"id": 99, "body": fields["body"]}
        if endpoint.endswith("/merge_requests/2") and method == "PUT":
            self.mr["state"] = "closed"
            return dict(self.mr)
        if "/refs?" in endpoint:
            return self.refs
        if "/pipelines?" in endpoint:
            return [
                {
                    "status": self.pipeline_status,
                    "sha": self.mr["sha"],
                    "ref": self.run_branch,
                }
            ]
        if "/discussions?" in endpoint:
            return (
                [
                    {
                        "notes": [
                            {
                                "id": 31,
                                "resolvable": True,
                                "resolved": False,
                                "author": {"username": "review-owner"},
                                "updated_at": "2026-07-29T09:30:00Z",
                            }
                        ]
                    }
                ]
                if self.unresolved
                else []
            )
        if endpoint.endswith("/merge"):
            if self.merge_error:
                raise DependencyContractError(
                    "409 Conflict",
                    context=ErrorContext(
                        dependency="gitlab",
                        endpoint="merge",
                        status_code=409,
                    ),
                )
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
                "id": 21,
                "author": {"id": 9, "username": "tester", "name": "Tester"},
                "body": (
                    "HOLLYSYS-GATE: v=5 run=hollysys-abcdefghijklmnopqrst "
                    "stage=test result=pass digest=na artifact=na "
                    f"head={'d' * 40} test=executed task=t_abc"
                ),
            },
            {
                "id": 22,
                "author": {
                    "id": 10,
                    "username": "code-reviewer",
                    "name": "Code Reviewer",
                },
                "body": (
                    "HOLLYSYS-GATE: v=5 run=hollysys-abcdefghijklmnopqrst "
                    "stage=code-review result=pass digest=na artifact=na "
                    f"head={'d' * 40} test=na task=t_abc"
                    "\nHOLLYSYS-SEMANTIC-GATE: v=1 "
                    "run=hollysys-abcdefghijklmnopqrst "
                    "phase=implementation_completion decision=approved "
                    f"artifact={'c' * 40} digest={'b' * 64}"
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

    def test_start_rejects_noncanonical_urls_before_api_or_persistence(
        self,
    ) -> None:
        sha = "a" * 40
        valid_blob = (
            "https://green-git.hollysys.net/group/project/-/blob/"
            f"{sha}/docs/prds/feature.md"
        )
        valid_mr = (
            "https://green-git.hollysys.net/group/project/-/merge_requests/2"
        )
        cases = (
            (
                valid_blob.replace(
                    "https://",
                    "https://oauth2:secret@",
                ),
                valid_mr,
            ),
            (
                valid_blob.replace(
                    "green-git.hollysys.net",
                    "green-git.hollysys.net:8443",
                ),
                valid_mr,
            ),
            (valid_blob, valid_mr + "?private_token=secret"),
            (
                valid_blob.replace(
                    "group/project",
                    "group/%2e%2e/other",
                ),
                valid_mr.replace(
                    "group/project",
                    "group/%2e%2e/other",
                ),
            ),
            (
                valid_blob.replace(
                    "docs/prds/feature.md",
                    "docs/%2e%2e/secret.md",
                ),
                valid_mr,
            ),
        )
        for blob_url, mr_url in cases:
            with self.subTest(blob_url=blob_url, mr_url=mr_url), patch.object(
                self.client,
                "api",
            ) as api:
                with self.assertRaises(ValueError):
                    self.client.validate_start(
                        prd_blob_url=blob_url,
                        prd_mr_url=mr_url,
                        origin=self.run.origin,
                    )
                api.assert_not_called()

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

    def test_authoring_pass_must_match_shared_delivery_mr_head(self) -> None:
        metadata = completion(self.root, Stage.IMPLEMENT)
        self.client.validate_author_completion(self.run, metadata)

        self.client.mr["sha"] = "e" * 40
        with self.assertRaisesRegex(ValueError, "current shared delivery"):
            self.client.validate_author_completion(self.run, metadata)

    def test_explicit_delivery_mr_must_belong_to_run_branch(self) -> None:
        self.client.mr["source_branch"] = "feature/another-run"
        with patch.object(
            self.client,
            "api",
            return_value=dict(self.client.mr),
        ), self.assertRaisesRegex(ValueError, "run branch/target"):
            GitLabClient.delivery_mr(self.client, self.run, 2)

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
        with self.assertRaises(MergeBlocked) as pipeline:
            self.client.validate_merge(
                self.run,
                mr_iid=2,
                test=self.test_meta,
                code_review=self.review_meta,
            )
        self.assertEqual(pipeline.exception.kind, "pipeline_failed")
        self.assertTrue(pipeline.exception.immediate_exception)
        self.client.pipeline_status = "success"
        self.client.unresolved = True
        self.client.mr["detailed_merge_status"] = "discussions_not_resolved"
        with self.assertRaises(MergeBlocked) as discussion:
            self.client.validate_merge(
                self.run,
                mr_iid=2,
                test=self.test_meta,
                code_review=self.review_meta,
            )
        self.assertEqual(discussion.exception.kind, "discussion_unresolved")
        self.assertTrue(str(discussion.exception.url).endswith("#note_31"))
        self.assertEqual(discussion.exception.owner, "review-owner")
        self.assertEqual(
            discussion.exception.updated_at,
            "2026-07-29T09:30:00Z",
        )

    def test_pipeline_skipped_is_an_immediate_exception(self) -> None:
        self.client.pipeline_status = "skipped"
        with self.assertRaises(MergeBlocked) as failure:
            self.client.validate_merge(
                self.run,
                mr_iid=2,
                test=self.test_meta,
                code_review=self.review_meta,
            )
        self.assertEqual(failure.exception.kind, "pipeline_skipped")
        self.assertTrue(failure.exception.immediate_exception)

    def test_pipeline_must_match_delivery_head_and_branch(self) -> None:
        self.client.run_branch = "feature/another-run"
        with self.assertRaisesRegex(
            DependencyContractError,
            "delivery head/ref",
        ):
            self.client.validate_merge(
                self.run,
                mr_iid=2,
                test=self.test_meta,
                code_review=self.review_meta,
            )

    def test_http_failures_have_stable_recovery_classes(self) -> None:
        cases = (
            ("HTTP 401 unauthorized", DependencyAuthError),
            (
                "HTTP 429 rate limited Retry-After: 45",
                DependencyRateLimitedError,
            ),
            ("HTTP 422 invalid request", DependencyContractError),
            ("HTTP 503 unavailable", DependencyTransientError),
        )
        for stderr, expected in cases:
            with self.subTest(stderr=stderr):
                error = self.client._api_error(
                    "projects/12",
                    CompletedProcess(["glab"], 1, "", stderr),
                )
                self.assertIsInstance(error, expected)
        limited = self.client._api_error(
            "projects/12",
            CompletedProcess(
                ["glab"],
                1,
                "",
                "HTTP 429 rate limited Retry-After: 45",
            ),
        )
        self.assertEqual(limited.context.retry_after_seconds, 45)

    def test_api_errors_redact_controller_token(self) -> None:
        token = "controller-secret-token"
        write_profile_env(self.client.config, token=token)
        error = self.client._api_error(
            "user",
            CompletedProcess(
                ["glab"],
                1,
                "",
                f"HTTP 401 Authorization: Bearer {token}",
            ),
        )
        self.assertNotIn(token, str(error))
        self.assertIn("[REDACTED]", str(error))

    def test_invalid_dispatcher_profile_env_is_local_fatal(self) -> None:
        write_profile_env(
            self.client.config,
            token="controller-token",
            mode=0o644,
        )
        with self.assertRaisesRegex(
            ControllerFatalError,
            "controller_gitlab_token_invalid",
        ), patch("hollysys_controller.gitlab.subprocess.run") as run:
            self.client._run(["glab", "api", "user"])
        run.assert_not_called()

    def test_controller_git_environment_ignores_user_git_configuration(self) -> None:
        write_profile_env(self.client.config, token="controller-token")
        inherited = {
            "GLAB_TOKEN": "wrong-token",
            "PRIVATE_TOKEN": "wrong-private-token",
            "GIT_ASKPASS": "/tmp/untrusted-askpass",
            "GIT_CONFIG_GLOBAL": "/tmp/untrusted-gitconfig",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "url.ssh://attacker/.insteadOf",
            "GIT_CONFIG_VALUE_0": "https://green-git.hollysys.net/",
            "GIT_SSH_COMMAND": "ssh -F /tmp/untrusted",
        }
        with patch.dict(os.environ, inherited, clear=False):
            env = self.client._env()

        self.assertEqual(env["GITLAB_TOKEN"], "controller-token")
        for key in inherited:
            self.assertNotIn(key, env)

    def test_existing_checkout_origin_must_be_https(self) -> None:
        checkout = Path(self.run.workspace.checkout)
        (checkout / ".git").mkdir(parents=True)
        for origin in (
            "http://green-git.hollysys.net/group/project.git",
            "https://green-git.hollysys.net/other/group/project.git",
            "https://green-git.hollysys.net:8443/group/project.git",
        ):
            with self.subTest(origin=origin):
                self.client._git = lambda *args, origin=origin, **kwargs: CompletedProcess(
                    ["git"],
                    0,
                    f"{origin}\n",
                    "",
                )
                with self.assertRaisesRegex(
                    ValueError,
                    "token-free project URL",
                ):
                    self.client.ensure_workspace(
                        self.run,
                    self.run.workspace.repository_base_sha,
                )

    def test_existing_worktree_must_belong_to_validated_checkout(self) -> None:
        checkout = Path(self.run.workspace.checkout)
        worktree = Path(self.run.workspace.worktree)
        (checkout / ".git").mkdir(parents=True)
        worktree.mkdir(parents=True)

        def git_result(cwd, args, tolerate=False):
            if args == ["remote", "get-url", "origin"]:
                stdout = (
                    "https://green-git.hollysys.net/"
                    f"{self.run.project.project_path}.git\n"
                )
            elif args[:2] == ["cat-file", "-e"]:
                stdout = ""
            elif args == ["branch", "--show-current"]:
                stdout = f"{self.run.workspace.branch}\n"
            elif args == ["rev-parse", "--git-common-dir"]:
                stdout = f"{self.root / 'other-checkout' / '.git'}\n"
            else:
                self.fail(f"unexpected git invocation: {args}")
            return CompletedProcess(["git", *args], 0, stdout, "")

        self.client._git = git_result

        with self.assertRaisesRegex(ValueError, "validated checkout"):
            self.client.ensure_workspace(
                self.run,
                self.run.workspace.repository_base_sha,
            )

    def test_api_command_timeout_is_a_transient_dependency_error(self) -> None:
        write_profile_env(self.client.config, token="controller-token")
        client = GitLabClient(self.client.config)
        with patch(
            "hollysys_controller.gitlab.subprocess.run",
            side_effect=TimeoutExpired(["glab"], 10),
        ), self.assertRaises(DependencyTransientError) as raised:
            client.api("user")
        self.assertEqual(raised.exception.context.error_code, "timeout")

    def test_semantic_gate_requires_frozen_requirement_and_contract(self) -> None:
        self.client.artifact_digest_result = "b" * 64
        document = (
            "# TASKS\n"
            "- [ ] T001 在 `src/order.py` 实现 OP-001 和 PLAN-BLK-001\n"
            "  - 动作：modify\n"
            "  - depends_on：[]\n"
            "  - 验收：行为可验证\n"
            "  - 测试：`python -m unittest`\n"
            "evidence: docs/evidence/deployment-entry.md\n"
        )
        self.client.file = lambda project_id, path, ref: {
            "encoding": "base64",
            "content": base64.b64encode(document.encode()).decode(),
            "blob_id": "f" * 40,
        }
        metadata = completion(
            self.root,
            Stage.IMPLEMENT,
            gate_phase="deployment_entry",
            gate_decision="approved",
            gate_reviewer="id:42",
            gate_reviewed_at="2026-07-29T12:00:00+08:00",
            gate_reason="approved in the isolated deployment environment",
            gate_evidence_refs=[
                (
                    "https://green-git.hollysys.net/group/project/"
                    "-/merge_requests/2#note_42"
                ),
                "docs/evidence/deployment-entry.md",
            ],
            gate_artifact_paths=["docs/tasks/feature/tasks.md"],
            gate_artifact_commit_sha="c" * 40,
            gate_artifact_digest="b" * 64,
            contract_refs=["PLAN-BLK-001"],
            requirement_ids=["OP-001"],
        )
        self.client.notes = [
            {
                "id": 42,
                "author": {"id": 42, "username": "release-reviewer"},
                "body": (
                    "HOLLYSYS-SEMANTIC-GATE: v=1 "
                    "run=hollysys-abcdefghijklmnopqrst "
                    "phase=deployment_entry decision=approved "
                    f"artifact={'c' * 40} digest={'b' * 64}"
                ),
            }
        ]
        self.client.validate_semantic_gate(self.run, metadata)

        self.client.notes[0]["author"]["id"] = 41
        with self.assertRaisesRegex(ValueError, "author or frozen identity"):
            self.client.validate_semantic_gate(self.run, metadata)
        self.client.notes[0]["author"]["id"] = 42

        unverified_url = metadata.model_copy(
            update={
                "gate_evidence_refs": [
                    (
                        "https://green-git.hollysys.net/group/project/"
                        "-/merge_requests/2"
                    )
                ]
            }
        )
        with self.assertRaisesRegex(ValueError, "exact delivery MR note"):
            self.client.validate_semantic_gate(self.run, unverified_url)

        missing = metadata.model_copy(
            update={"requirement_ids": ["OP-MISSING"]}
        )
        with self.assertRaisesRegex(ValueError, "does not exist"):
            self.client.validate_semantic_gate(self.run, missing)

        false_prefix = document.replace("OP-001", "OP-0010")
        self.client.file = lambda project_id, path, ref: {
            "encoding": "base64",
            "content": base64.b64encode(false_prefix.encode()).decode(),
            "blob_id": "f" * 40,
        }
        with self.assertRaisesRegex(ValueError, "does not exist"):
            self.client.validate_semantic_gate(self.run, metadata)

    def test_implementation_entry_rejects_task_cycles_and_upstream_rewrites(
        self,
    ) -> None:
        cyclic = (
            "- [ ] T001 在 `src/a.py` 实现 OP-001 PLAN-BLK-001\n"
            "  - 动作：modify\n"
            "  - depends_on：[T002]\n"
            "- [ ] T002 在 `src/b.py` 实现辅助行为\n"
            "  - 动作：extend\n"
            "  - depends_on：[T001]\n"
        )
        with self.assertRaisesRegex(ValueError, "cycle"):
            self.client._validate_task_graph([cyclic])

        inversion = (
            "- [ ] T001 在 `docs/prds/order/specs/spec-order.md` 补写需求\n"
            "  - 动作：modify\n"
            "  - depends_on：[]\n"
        )
        with self.assertRaisesRegex(ValueError, "frozen"):
            self.client._validate_task_graph([inversion])

    def test_merge_always_sends_checked_head(self) -> None:
        result = self.client.merge(self.run, 2, "d" * 40)
        self.assertEqual(result["state"], "merged")
        self.assertTrue(result["_hollysys_controller_merge_submitted_v3"])
        self.assertEqual(self.client.merge_fields, {"sha": "d" * 40})

    def test_merge_readback_before_submission_is_not_claimed(self) -> None:
        self.client.mr["state"] = "merged"
        result = self.client.merge(self.run, 2, "d" * 40)
        self.assertFalse(result["_hollysys_controller_merge_submitted_v3"])
        self.assertIsNone(self.client.merge_fields)

    def test_merge_rejects_a_merged_response_for_another_head(self) -> None:
        self.client.mr["sha"] = "f" * 40
        self.client.mr["state"] = "merged"
        with self.assertRaises(CheckedHeadConflict):
            self.client.merge(self.run, 2, "d" * 40)

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
