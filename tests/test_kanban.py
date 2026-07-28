from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from hollysys_controller.kanban import (
    KanbanReader,
    parse_card_body,
    parse_run_body,
    render_card_body,
    render_run_body,
)
from hollysys_controller.models import CardRecord, Stage
from tests.helpers import origin, run_record

DB_SCHEMA = """
CREATE TABLE tasks (
  id TEXT PRIMARY KEY, title TEXT, body TEXT, assignee TEXT, status TEXT,
  created_by TEXT, created_at INTEGER, completed_at INTEGER,
  idempotency_key TEXT, tenant TEXT, workspace_path TEXT, branch_name TEXT,
  skills TEXT, current_run_id INTEGER
);
CREATE TABLE task_events (
  id INTEGER PRIMARY KEY, task_id TEXT, run_id INTEGER, kind TEXT,
  payload TEXT, created_at INTEGER
);
CREATE TABLE task_runs (
  id INTEGER PRIMARY KEY, task_id TEXT, summary TEXT, metadata TEXT, outcome TEXT
);
CREATE TABLE task_links (parent_id TEXT, child_id TEXT);
CREATE TABLE task_comments (
  id INTEGER PRIMARY KEY, task_id TEXT, author TEXT, body TEXT, created_at INTEGER
);
CREATE TABLE kanban_notify_subs (
  task_id TEXT, platform TEXT, chat_id TEXT, thread_id TEXT, user_id TEXT,
  notifier_profile TEXT
);
"""


class KanbanReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        db = self.root / "kanban.db"
        with closing(sqlite3.connect(db)) as conn:
            conn.executescript(DB_SCHEMA)
            conn.execute(
                """
                INSERT INTO tasks VALUES(
                  't_a','title','body','worker','done','hollysys-controller',
                  1,2,'key','tenant','/work',NULL,'["skill"]',NULL
                )
                """
            )
            conn.execute("INSERT INTO task_events VALUES(1,'t_a',4,'completed','{}',2)")
            conn.execute("INSERT INTO task_events VALUES(2,'t_a',4,'gave_up','{}',3)")
            conn.execute(
                """
                INSERT INTO task_runs VALUES(
                  4,'t_a','ok','{"protocol_version":"bad"}','completed'
                )
                """
            )
            conn.execute("INSERT INTO task_links VALUES('t_root','t_a')")
            conn.execute("INSERT INTO task_comments VALUES(1,'t_a','worker','note',2)")
            conn.execute(
                """
                INSERT INTO kanban_notify_subs
                VALUES('t_a','feishu','oc_abc','omt_abc','ou_abc','dispatcher')
                """
            )
            conn.commit()
        self.reader = KanbanReader(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_reads_events_runs_links_and_subscription_without_writes(self) -> None:
        events = self.reader.events_after("default", 0)
        self.assertEqual(events[0].run_id, 4)
        self.assertEqual([event.id for event in events], [1, 2])
        self.assertEqual(
            [event.id for event in self.reader.events_after("default", 1)],
            [2],
        )
        task = self.reader.task("default", "t_a")
        assert task
        self.assertEqual(task.latest_metadata, {"protocol_version": "bad"})
        self.assertEqual(task.parents, ["t_root"])
        self.assertEqual(task.event_kinds, ["completed", "gave_up"])
        self.assertTrue(self.reader.subscription_exists("default", "t_a", origin()))

    def test_card_and_root_bodies_round_trip(self) -> None:
        run = run_record(self.root)
        card = CardRecord(
            run=run,
            stage=Stage.SPEC_WRITE,
            iteration=1,
            idempotency_key=f"{run.run_key}:spec-write:1:work",
            parent_card_id="t_root",
            assignee="spec-writer",
            skills=["hollysys-write-spec", "glab"],
            resume_answer="Use this fenced example:\n```text\nvalue\n```",
        )
        self.assertEqual(parse_run_body(render_run_body(run)), run)
        self.assertEqual(parse_card_body(render_card_body(card)), card)


if __name__ == "__main__":
    unittest.main()
