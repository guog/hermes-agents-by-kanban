from __future__ import annotations

import hashlib
import tempfile
import time
import unittest
from pathlib import Path

from hollysys_controller.errors import ControllerFatalError
from hollysys_controller.store import ControllerStore


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = ControllerStore(Path(self.temp.name) / "controller.db")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_event_cursor_is_monotonic_in_storage(self) -> None:
        self.assertEqual(self.store.cursor("b"), 0)
        self.store.set_cursor("b", 9)
        self.store.set_cursor("b", 3)
        self.assertEqual(self.store.cursor("b"), 9)

    def test_read_only_store_can_query_but_cannot_mutate(self) -> None:
        self.store.set_cursor("b", 9)
        read_only = ControllerStore(self.store.path, read_only=True)

        self.assertEqual(read_only.cursor("b"), 9)
        with self.assertRaisesRegex(
            ControllerFatalError,
            "controller_store_database_error",
        ):
            read_only.set_cursor("b", 10)

    def test_managed_card_upsert_does_not_create_business_state(self) -> None:
        self.store.add_managed_card(
            board="b",
            card_id="t_a",
            run_key="hollysys-abcdefghijklmnopqrst",
            stage="spec-write",
            iteration=1,
            idempotency_key="key",
            parent_card_id="t_root",
        )
        rows = self.store.cards_for_run("hollysys-abcdefghijklmnopqrst")
        self.assertEqual(len(rows), 1)
        with self.store.connect() as conn:
            columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(managed_cards)")
            }
        self.assertNotIn("current_stage", columns)
        self.assertNotIn("head_sha", columns)
        self.assertNotIn("gate_result", columns)

    def test_completed_operation_replays_result_without_action(self) -> None:
        payload = {"card": "t_a"}
        self.assertIsNone(self.store.operation_result("op", "create", payload))
        self.store.finish_operation("op", {"card_id": "t_a"})
        self.assertEqual(
            self.store.operation_result("op", "create", payload),
            {"card_id": "t_a"},
        )

    def test_failed_or_interrupted_operation_can_be_replayed(self) -> None:
        payload = {"checked_head": "a" * 40}
        self.assertIsNone(self.store.operation_result("merge", "merge", payload))
        self.store.fail_operation("merge", "connection lost")
        self.assertIsNone(self.store.operation_result("merge", "merge", payload))
        self.store.finish_operation("merge", {"state": "merged"})
        self.assertEqual(
            self.store.operation_result("merge", "merge", payload),
            {"state": "merged"},
        )

    def test_outbox_is_idempotent(self) -> None:
        self.store.enqueue("k", "run", "merged", {"x": 1})
        self.store.enqueue("k", "run", "merged", {"x": 2})
        pending = self.store.pending_outbox()
        self.assertEqual(len(pending), 1)
        self.assertIn('"x": 1', pending[0]["payload"])

    def test_abort_confirmation_is_bound_to_requester_and_channel(self) -> None:
        run_key = "hollysys-abcdefghijklmnopqrst"
        token_hash = hashlib.sha256(b"SAFE2345").hexdigest()
        self.store.ensure_run_control(run_key)
        self.store.create_abort_request(
            request_id="abort-request:om_request",
            run_key=run_key,
            token_hash=token_hash,
            sender="ou_owner",
            chat_id="oc_origin",
            thread_id="omt_origin",
            reason="human stopped the delivery",
            expires_at=int(time.time()) + 600,
        )

        with self.assertRaisesRegex(
            PermissionError,
            "requester, and channel",
        ):
            self.store.confirm_abort_request(
                run_key=run_key,
                token_hash=token_hash,
                sender="ou_other",
                chat_id="oc_origin",
                thread_id="omt_origin",
                message_id="om_wrong",
            )

        control = self.store.confirm_abort_request(
            run_key=run_key,
            token_hash=token_hash,
            sender="ou_owner",
            chat_id="oc_origin",
            thread_id="omt_origin",
            message_id="om_confirm",
        )
        self.assertEqual(control["state"], "abort_requested")
        self.store.mark_aborting(run_key)
        self.store.finish_abort(run_key)
        self.assertEqual(self.store.run_control(run_key)["state"], "aborted")

    def test_abort_confirmation_token_expires(self) -> None:
        run_key = "hollysys-abcdefghijklmnopqrst"
        token_hash = hashlib.sha256(b"SAFE2345").hexdigest()
        self.store.ensure_run_control(run_key)
        self.store.create_abort_request(
            request_id="abort-request:expired",
            run_key=run_key,
            token_hash=token_hash,
            sender="ou_owner",
            chat_id="oc_origin",
            thread_id=None,
            reason="stop",
            expires_at=int(time.time()) - 1,
        )
        with self.assertRaisesRegex(ValueError, "expired"):
            self.store.confirm_abort_request(
                run_key=run_key,
                token_hash=token_hash,
                sender="ou_owner",
                chat_id="oc_origin",
                thread_id=None,
                message_id="om_confirm",
            )

    def test_dependency_outage_backoff_is_persisted_and_recovers(self) -> None:
        first = self.store.record_dependency_failure(
            "gitlab",
            "503 unavailable",
            initial_backoff_seconds=5,
            maximum_backoff_seconds=20,
        )
        second = self.store.record_dependency_failure(
            "gitlab",
            "503 unavailable",
            initial_backoff_seconds=5,
            maximum_backoff_seconds=20,
        )
        self.assertEqual(first["failures"], 1)
        self.assertEqual(second["failures"], 2)
        self.assertEqual(first["outage_id"], second["outage_id"])
        self.assertGreaterEqual(
            second["next_retry_at"] - second["updated_at"],
            10,
        )
        self.assertEqual(len(self.store.open_dependency_outages()), 1)
        self.store.associate_outage_run(
            "gitlab",
            first["outage_id"],
            "hollysys-abcdefghijklmnopqrst",
        )
        self.assertEqual(
            self.store.outage_run_keys("gitlab", first["outage_id"]),
            ["hollysys-abcdefghijklmnopqrst"],
        )
        recovered = self.store.recover_dependency("gitlab")
        self.assertEqual(recovered["outage_id"], first["outage_id"])
        self.assertEqual(self.store.open_dependency_outages(), [])

    def test_merge_wait_is_reconstructable_and_clearable(self) -> None:
        run_key = "hollysys-abcdefghijklmnopqrst"
        first = self.store.set_merge_wait(
            run_key,
            mr_iid=2,
            head_sha="a" * 40,
            blocker_kind="pipeline_pending",
            blocker="pipeline is running",
            retry_seconds=30,
        )
        changed = self.store.set_merge_wait(
            run_key,
            mr_iid=2,
            head_sha="a" * 40,
            blocker_kind="pipeline_pending",
            blocker="pipeline is still running",
            blocker_updated_at="2026-07-29T12:00:00Z",
            retry_seconds=30,
        )
        waiting = self.store.merge_wait(run_key)
        self.assertEqual(waiting["mr_iid"], 2)
        self.assertEqual(waiting["head_sha"], "a" * 40)
        self.assertEqual(waiting["blocker_kind"], "pipeline_pending")
        self.assertEqual(waiting["blocker"], "pipeline is still running")
        self.assertEqual(changed["first_seen_at"], first["first_seen_at"])
        self.assertTrue(changed["changed"])
        self.store.clear_merge_wait(run_key)
        self.assertIsNone(self.store.merge_wait(run_key))

    def test_worker_runtime_envelope_tracks_session_and_progress_lease(self) -> None:
        run_key = "hollysys-abcdefghijklmnopqrst"
        self.store.add_managed_card(
            board="gitlab-p12",
            card_id="t_work",
            run_key=run_key,
            stage="implement",
            iteration=1,
            idempotency_key="work",
            parent_card_id="t_root",
        )
        self.store.record_card_runtime_event(
            board="gitlab-p12",
            card_id="t_work",
            kind="claimed",
            created_at=100,
            worker_session_id="session-1",
            lease_seconds=600,
        )
        self.store.record_card_runtime_event(
            board="gitlab-p12",
            card_id="t_work",
            kind="heartbeat",
            created_at=200,
            worker_session_id="session-1",
            lease_seconds=600,
        )
        runtime = self.store.card_runtime("gitlab-p12", "t_work")
        self.assertEqual(runtime["worker_started_at"], 100)
        self.assertEqual(runtime["worker_session_id"], "session-1")
        self.assertEqual(runtime["last_heartbeat_at"], 200)
        # Heartbeats refresh liveness only; they do not extend progress.
        self.assertEqual(runtime["deadline_at"], 700)


if __name__ == "__main__":
    unittest.main()
