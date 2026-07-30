from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from hollysys_controller.errors import ControllerFatalError
from hollysys_controller.gitlab import GitLabClient
from hollysys_controller.models import CompletionMetadata, DeliveryBinding
from hollysys_controller.store import ControllerStore
from hollysys_controller.validators import validate_task_documents
from tests.helpers import completion, config, origin, run_record


class StartIdentityGitLab(GitLabClient):
    def api(self, endpoint, *, method="GET", fields=None):
        if endpoint == "projects/group%2Fproject":
            return {
                "id": 12,
                "default_branch": "main",
                "name_with_namespace": "Group / Project",
                "archived": False,
            }
        if endpoint == "projects/12/merge_requests/1":
            return {"state": "merged", "target_branch": "main"}
        if endpoint == "projects/12/merge_requests/1/changes":
            return {"changes": [{"new_path": "docs/prds/example.md"}]}
        if endpoint == "projects/12/repository/branches/main":
            return {"commit": {"id": "9" * 40}}
        raise AssertionError(endpoint)

    def file(self, project_id, path, ref):
        return {"blob_id": "f" * 40}


class V4RunIdentityTests(unittest.TestCase):
    def test_same_prd_has_stable_source_and_isolated_random_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            client = StartIdentityGitLab(config(Path(temporary)))
            blob_url = (
                "https://green-git.hollysys.net/group/project/-/blob/"
                + "a" * 40
                + "/docs/prds/example.md"
            )
            mr_url = (
                "https://green-git.hollysys.net/group/project/"
                "-/merge_requests/1"
            )
            first = client.validate_start(
                prd_blob_url=blob_url,
                prd_mr_url=mr_url,
                origin=origin(),
            ).run
            second = client.validate_start(
                prd_blob_url=blob_url,
                prd_mr_url=mr_url,
                origin=origin(),
            ).run

            self.assertEqual(first.source_key, second.source_key)
            self.assertNotEqual(first.run_key, second.run_key)
            self.assertNotEqual(first.run_generation, second.run_generation)
            self.assertNotEqual(first.workspace.branch, second.workspace.branch)
            self.assertNotEqual(
                first.workspace.worktree,
                second.workspace.worktree,
            )
            self.assertEqual(first.provenance, "fresh_v4")


class V4StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = ControllerStore(self.root / "controller.db")
        self.run = run_record(self.root)
        self.store.save_run(self.run)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def binding(*, iid: int = 2) -> DeliveryBinding:
        return DeliveryBinding(
            mr_iid=iid,
            mr_url=(
                "https://green-git.hollysys.net/group/project/"
                f"-/merge_requests/{iid}"
            ),
            creator="controller-bot",
            created_at=datetime(2026, 7, 30, tzinfo=timezone.utc),
            initial_head_sha="d" * 40,
            claim_note_id=91,
        )

    def test_delivery_binding_is_idempotent_and_immutable(self) -> None:
        binding = self.binding()
        self.store.bind_delivery(self.run.run_key, binding)
        self.store.bind_delivery(self.run.run_key, binding)
        self.assertEqual(
            self.store.delivery_binding(self.run.run_key),
            binding,
        )
        with self.assertRaisesRegex(
            ControllerFatalError,
            "delivery_binding_conflict",
        ):
            self.store.bind_delivery(
                self.run.run_key,
                self.binding(iid=53),
            )

    def test_reconcile_intent_coalesces_and_replays_after_lease(self) -> None:
        intent_id = self.store.enqueue_reconcile(
            self.run.run_key,
            reason="event",
            event_id=10,
        )
        first = self.store.claim_reconcile(
            lease_owner="worker-a",
            lease_seconds=60,
        )
        self.assertEqual(first["intent_id"], intent_id)
        self.assertIsNone(
            self.store.claim_reconcile(
                lease_owner="worker-b",
                lease_seconds=60,
            )
        )

        coalesced = self.store.enqueue_reconcile(
            self.run.run_key,
            reason="newer-event",
            event_id=12,
        )
        self.assertEqual(coalesced, intent_id)
        self.store.finish_reconcile(
            intent_id,
            lease_owner="worker-a",
        )
        second = self.store.claim_reconcile(
            lease_owner="worker-b",
            lease_seconds=60,
        )
        self.assertEqual(second["intent_id"], intent_id)
        self.assertEqual(second["event_id"], 12)
        self.store.finish_reconcile(
            intent_id,
            lease_owner="worker-b",
        )
        self.assertEqual(self.store.reconcile_intents(), [])


class V4CompletionAndValidatorTests(unittest.TestCase):
    def test_schema_is_v8_and_v7_protocol_is_rejected(self) -> None:
        schema_path = (
            Path(__file__).resolve().parent.parent
            / "schemas"
            / "card-completion.schema.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertTrue(schema["$id"].endswith("card-completion-v8.json"))
        for required in (
            "source_key",
            "run_generation",
            "context_digest",
            "head_before_sha",
            "deterministic_checks",
        ):
            self.assertIn(required, schema["required"])

        with tempfile.TemporaryDirectory() as temporary:
            payload = completion(Path(temporary)).model_dump(mode="json")
            payload["protocol_version"] = "hollysys-controller/v3"
            with self.assertRaises(ValidationError):
                CompletionMetadata.model_validate(payload)

    def test_shared_tasks_validator_has_stable_error_codes(self) -> None:
        valid = validate_task_documents(
            [
                (
                    "- [ ] T001 修改 `src/service.py`\n"
                    "  - 动作：modify\n"
                    "  - depends_on：[]\n"
                    "- [ ] T002 测试 `tests/test_service.py`\n"
                    "  - 动作：extend\n"
                    "  - depends_on：[T001]\n"
                )
            ]
        )
        self.assertTrue(valid.passed)
        self.assertEqual(valid.error_codes, ())

        invalid_document = (
            "- [ ] T001 修改 `docs/specs/spec-a.md`\n"
            "  - 动作：modify\n"
            "  - depends_on：[T002]\n"
            "- [ ] T002 修改 `src/b.py`\n"
            "  - 动作：modify\n"
            "  - depends_on：[T001, T999]\n"
            "- [ ] T002 重复 `src/c.py`\n"
            "  - 动作：modify\n"
            "  - depends_on：[T002]\n"
            "- [ ] T003 自依赖 `src/d.py`\n"
            "  - 动作：modify\n"
            "  - depends_on：[T003]\n"
        )
        first = validate_task_documents([invalid_document])
        second = validate_task_documents([invalid_document])
        self.assertFalse(first.passed)
        self.assertEqual(first, second)
        self.assertEqual(
            set(first.error_codes),
            {
                "dependency_cycle",
                "duplicate_id",
                "frozen_file_modified",
                "missing_dependency",
                "self_dependency",
            },
        )


if __name__ == "__main__":
    unittest.main()
