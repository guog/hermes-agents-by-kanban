from __future__ import annotations

import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from hollysys_controller.kanban import (
    KanbanCLI,
    KanbanReader,
    TaskRecord,
    parse_card_body,
    parse_run_body,
    render_card_body,
    render_run_body,
)
from hollysys_controller.models import CardRecord, Stage
from tests.helpers import config, run_record

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
            conn.commit()
        self.reader = KanbanReader(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_reads_events_runs_and_links_without_writes(self) -> None:
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

    def test_prepares_triage_for_completion_with_hermes_transitions(self) -> None:
        states = iter(["triage", "todo", "ready"])

        def task_with_status(status: str) -> TaskRecord:
            return TaskRecord(
                id="t_triage",
                title="triage",
                body="body",
                assignee="coder",
                status=status,
                created_by="hollysys-controller",
                created_at=1,
                completed_at=None,
                idempotency_key="key",
                tenant="run",
                workspace_path=None,
                branch_name=None,
                skills=[],
                current_run_id=None,
                latest_summary=None,
                latest_metadata=None,
                latest_outcome="blocked",
                parents=[],
                comments=[],
                event_kinds=[],
            )

        reader = type(
            "StateReader",
            (),
            {"task": staticmethod(lambda board, task_id: task_with_status(next(states)))},
        )()
        cli = KanbanCLI(config(self.root), reader)
        completed = subprocess.CompletedProcess([], 0, "", "")

        with patch("hollysys_controller.kanban.subprocess.run", return_value=completed) as run:
            cli.prepare_human_block_for_completion("gitlab-p12", "t_triage")

        self.assertEqual(run.call_count, 2)
        first = run.call_args_list[0]
        self.assertIn("specify_triage_task", first.args[0][2])
        self.assertEqual(first.kwargs["env"]["HERMES_KANBAN_BOARD"], "gitlab-p12")
        second_command = run.call_args_list[1].args[0]
        self.assertIn("promote", second_command)
        self.assertIn("t_triage", second_command)

    def test_abort_running_task_reclaims_then_archives(self) -> None:
        statuses = iter(["running", "ready", "archived"])

        def current_task(board: str, task_id: str) -> TaskRecord:
            return TaskRecord(
                id=task_id,
                title="work",
                body="body",
                assignee="coder",
                status=next(statuses),
                created_by="hollysys-controller",
                created_at=1,
                completed_at=None,
                idempotency_key="key",
                tenant="run",
                workspace_path="/worktree",
                branch_name="feature/run",
                skills=[],
                current_run_id=4,
                latest_summary=None,
                latest_metadata=None,
                latest_outcome=None,
                parents=["t_root"],
                comments=[],
                event_kinds=[],
            )

        reader = type("AbortReader", (), {"task": staticmethod(current_task)})()
        cli = KanbanCLI(config(self.root), reader)
        completed = subprocess.CompletedProcess([], 0, "", "")
        with patch(
            "hollysys_controller.kanban.subprocess.run",
            return_value=completed,
        ) as run:
            cli.abort_task("gitlab-p12", "t_work", "human abort")

        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn("reclaim", commands[0])
        self.assertIn("t_work", commands[0])
        self.assertIn("archive", commands[1])
        self.assertIn("t_work", commands[1])


if __name__ == "__main__":
    unittest.main()
