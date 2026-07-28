from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

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
        with self.assertRaises(sqlite3.OperationalError):
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


if __name__ == "__main__":
    unittest.main()
