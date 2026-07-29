from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import ControllerConfig
from .models import CardRecord, RunRecord

CARD_MARKER = "[hollysys-controller-card:v3]"
RUN_MARKER = "[hollysys-controller-run:v3]"


class CommandError(RuntimeError):
    def __init__(self, command: list[str], returncode: int, stderr: str):
        safe = [
            part if "token" not in part.lower() else "<redacted>" for part in command
        ]
        super().__init__(
            f"command failed ({returncode}): {' '.join(safe)}: {stderr.strip()[:1000]}"
        )
        self.returncode = returncode
        self.stderr = stderr


@dataclass(frozen=True)
class EventRecord:
    id: int
    task_id: str
    run_id: int | None
    kind: str
    payload: dict | None
    created_at: int


@dataclass(frozen=True)
class TaskRecord:
    id: str
    title: str
    body: str | None
    assignee: str | None
    status: str
    created_by: str | None
    created_at: int
    completed_at: int | None
    idempotency_key: str | None
    tenant: str | None
    workspace_path: str | None
    branch_name: str | None
    skills: list[str]
    current_run_id: int | None
    latest_summary: str | None
    latest_metadata: dict | None
    latest_outcome: str | None
    parents: list[str]
    comments: list[dict]
    event_kinds: list[str]


def render_run_body(run: RunRecord) -> str:
    payload = json.dumps(run.model_dump(mode="json"), ensure_ascii=False, indent=2)
    return (
        f"{RUN_MARKER}\n\n"
        "这是控制器生成的不可变运行根记录，不由 Agent 执行业务工作。\n\n"
        f"```json\n{payload}\n```\n"
    )


def render_card_body(card: CardRecord) -> str:
    payload = json.dumps(card.model_dump(mode="json"), ensure_ascii=False, indent=2)
    return (
        f"{CARD_MARKER}\n\n"
        f"执行阶段：`{card.stage}`，模式：`{card.mode}`，"
        f"迭代：`{card.iteration}`。\n\n"
        "开始前必须读取完整 JSON 输入和角色 Skill。所有工作写入指定共享 "
        "worktree/branch/MR；完成时提交符合 "
        "`/opt/fleet/schemas/card-completion.schema.json` 的严格 metadata，并先运行 "
        "`hollysysctl validate-completion --card-id <当前卡> --metadata <json>`。\n\n"
        "真正需要人类时先写 `[human-block:v1]` 评论再使用 "
        "`kanban_block`；Controller outbox 负责原渠道通知。不得创建、链接或推进 "
        "其他正式卡片。environment/destructive_approval 阻塞还必须写明 "
        "`gate_phase`、冻结 `requirement_ids` 和 `contract_refs`。\n\n"
        f"```json\n{payload}\n```\n"
    )


def _extract_json(body: str | None, marker: str) -> dict:
    if not body or marker not in body:
        raise ValueError(f"missing {marker}")
    start = body.find("```json", body.find(marker))
    end = body.rfind("```")
    if start < 0 or end < 0:
        raise ValueError("missing JSON code block")
    if body[end + len("```") :].strip():
        raise ValueError("unexpected content after JSON code block")
    return json.loads(body[start + len("```json") : end].strip())


def parse_run_body(body: str | None) -> RunRecord:
    return RunRecord.model_validate(_extract_json(body, RUN_MARKER))


def parse_run_protocol_version(body: str | None) -> str:
    return str(_extract_json(body, RUN_MARKER).get("protocol_version") or "")


def parse_card_body(body: str | None) -> CardRecord:
    return CardRecord.model_validate(_extract_json(body, CARD_MARKER))


class KanbanReader:
    def __init__(self, hermes_home: Path):
        self.hermes_home = hermes_home

    def board_db(self, board: str) -> Path:
        if board == "default":
            return self.hermes_home / "kanban.db"
        return self.hermes_home / "kanban" / "boards" / board / "kanban.db"

    def discover_boards(self) -> dict[str, Path]:
        result: dict[str, Path] = {}
        default = self.board_db("default")
        if default.is_file():
            result["default"] = default
        boards_root = self.hermes_home / "kanban" / "boards"
        if boards_root.is_dir():
            for path in sorted(boards_root.glob("*/kanban.db")):
                if path.parent.name != "_archived":
                    result[path.parent.name] = path
        return result

    def _connect(self, board: str) -> sqlite3.Connection:
        path = self.board_db(board)
        if not path.is_file():
            raise FileNotFoundError(path)
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def events_after(
        self, board: str, cursor: int, limit: int = 200
    ) -> list[EventRecord]:
        with closing(self._connect(board)) as conn:
            rows = conn.execute(
                """
                SELECT id, task_id, run_id, kind, payload, created_at
                FROM task_events WHERE id > ? ORDER BY id LIMIT ?
                """,
                (cursor, limit),
            ).fetchall()
        events: list[EventRecord] = []
        for row in rows:
            payload = None
            if row["payload"]:
                try:
                    payload = json.loads(row["payload"])
                except (TypeError, json.JSONDecodeError):
                    payload = None
            events.append(
                EventRecord(
                    id=int(row["id"]),
                    task_id=str(row["task_id"]),
                    run_id=(int(row["run_id"]) if row["run_id"] is not None else None),
                    kind=str(row["kind"]),
                    payload=payload,
                    created_at=int(row["created_at"]),
                )
            )
        return events

    def task(self, board: str, task_id: str) -> TaskRecord | None:
        with closing(self._connect(board)) as conn:
            task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if task is None:
                return None
            run = conn.execute(
                """
                SELECT * FROM task_runs WHERE task_id=?
                ORDER BY id DESC LIMIT 1
                """,
                (task_id,),
            ).fetchone()
            parents = [
                str(row[0])
                for row in conn.execute(
                    "SELECT parent_id FROM task_links WHERE child_id=? ORDER BY parent_id",
                    (task_id,),
                )
            ]
            comments = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT id, author, body, created_at FROM task_comments
                    WHERE task_id=? ORDER BY id
                    """,
                    (task_id,),
                )
            ]
            event_kinds = [
                str(row[0])
                for row in conn.execute(
                    "SELECT kind FROM task_events WHERE task_id=? ORDER BY id",
                    (task_id,),
                )
            ]
        keys = set(task.keys())
        skills: list[str] = []
        if "skills" in keys and task["skills"]:
            try:
                decoded = json.loads(task["skills"])
                if isinstance(decoded, list):
                    skills = [str(item) for item in decoded]
            except json.JSONDecodeError:
                pass
        metadata = None
        if run is not None and run["metadata"]:
            try:
                metadata = json.loads(run["metadata"])
            except (TypeError, json.JSONDecodeError):
                metadata = None
        return TaskRecord(
            id=str(task["id"]),
            title=str(task["title"]),
            body=task["body"],
            assignee=task["assignee"],
            status=str(task["status"]),
            created_by=task["created_by"],
            created_at=int(task["created_at"]),
            completed_at=(
                int(task["completed_at"]) if task["completed_at"] is not None else None
            ),
            idempotency_key=task["idempotency_key"],
            tenant=task["tenant"],
            workspace_path=task["workspace_path"],
            branch_name=task["branch_name"],
            skills=skills,
            current_run_id=(
                int(task["current_run_id"])
                if task["current_run_id"] is not None
                else None
            ),
            latest_summary=run["summary"] if run is not None else None,
            latest_metadata=metadata,
            latest_outcome=run["outcome"] if run is not None else None,
            parents=parents,
            comments=comments,
            event_kinds=event_kinds,
        )

    def task_by_idempotency(self, board: str, key: str) -> TaskRecord | None:
        with closing(self._connect(board)) as conn:
            row = conn.execute(
                """
                SELECT id FROM tasks WHERE idempotency_key=? AND status != 'archived'
                ORDER BY created_at DESC LIMIT 1
                """,
                (key,),
            ).fetchone()
        return self.task(board, str(row["id"])) if row else None

    def max_event_id(self, board: str) -> int:
        with closing(self._connect(board)) as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(id), 0) AS event_id FROM task_events"
            ).fetchone()
        return int(row["event_id"])


class KanbanCLI:
    def __init__(self, config: ControllerConfig, reader: KanbanReader):
        self.config = config
        self.reader = reader

    def _run(
        self,
        args: list[str],
        *,
        board: str | None = None,
        json_output: bool = False,
        tolerate: bool = False,
    ) -> Any:
        command = [
            self.config.hermes_command,
            "-p",
            self.config.controller_profile,
            "kanban",
        ]
        if board is not None:
            command.extend(["--board", board])
        command.extend(args)
        result = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=self.config.command_timeout_seconds,
            check=False,
        )
        if result.returncode != 0 and not tolerate:
            raise CommandError(command, result.returncode, result.stderr)
        if json_output and result.stdout.strip():
            return json.loads(result.stdout)
        return {
            "returncode": result.returncode,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        }

    def ensure_board(self, board: str, name: str, worktree: str) -> None:
        boards = self._run(["boards", "list", "--json"], json_output=True)
        slugs = {
            str(item.get("slug"))
            for item in boards
            if isinstance(item, dict) and item.get("slug")
        }
        if board not in slugs:
            self._run(["boards", "create", board, "--name", name])
        self._run(["boards", "set-default-workdir", board, worktree])

    def create_root(self, run: RunRecord) -> TaskRecord:
        key = f"{run.run_key}:run-init"
        existing = self.reader.task_by_idempotency(run.workspace.board, key)
        if existing:
            return existing
        payload = self._run(
            [
                "create",
                f"[{run.run_key}] RUN INIT",
                "--body",
                render_run_body(run),
                "--tenant",
                run.run_key,
                "--workspace",
                f"dir:{run.workspace.worktree}",
                "--idempotency-key",
                key,
                "--created-by",
                "hollysys-controller",
                "--initial-status",
                "blocked",
                "--json",
            ],
            board=run.workspace.board,
            json_output=True,
        )
        task = self.reader.task(run.workspace.board, str(payload["id"]))
        if task is None:
            raise RuntimeError("created run-init card was not readable")
        return task

    def complete_root(self, run: RunRecord, card_id: str) -> None:
        task = self.reader.task(run.workspace.board, card_id)
        if task and task.status == "done":
            return
        metadata = json.dumps(
            {
                "protocol_version": "hollysys-controller/v3",
                "kind": "run-init",
                "run_key": run.run_key,
            },
            separators=(",", ":"),
        )
        self._run(
            [
                "complete",
                card_id,
                "--summary",
                "Controller accepted and validated the immutable run inputs.",
                "--metadata",
                metadata,
            ],
            board=run.workspace.board,
        )

    def create_work(self, card: CardRecord) -> TaskRecord:
        board = card.run.workspace.board
        existing = self.reader.task_by_idempotency(board, card.idempotency_key)
        if existing:
            return existing
        args = [
            "create",
            f"[{card.run.run_key}] {card.stage.value.upper()} iteration {card.iteration}",
            "--body",
            render_card_body(card),
            "--assignee",
            card.assignee,
            "--parent",
            card.parent_card_id,
            "--tenant",
            card.run.run_key,
            "--workspace",
            f"dir:{card.run.workspace.worktree}",
            "--idempotency-key",
            card.idempotency_key,
            "--created-by",
            "hollysys-controller",
            "--initial-status",
            "blocked",
            "--max-retries",
            "3",
        ]
        for skill in card.skills:
            args.extend(["--skill", skill])
        args.append("--json")
        payload = self._run(args, board=board, json_output=True)
        task = self.reader.task(board, str(payload["id"]))
        if task is None:
            raise RuntimeError("created work card was not readable")
        return task

    def release(self, board: str, task_id: str) -> None:
        task = self.reader.task(board, task_id)
        if task is None:
            raise RuntimeError(f"unknown task {task_id}")
        if task.status in {"ready", "running", "done", "todo"}:
            return
        self._run(["promote", task_id], board=board)
        released = self.reader.task(board, task_id)
        if released is None or released.status not in {"ready", "running", "todo"}:
            raise RuntimeError(f"card {task_id} did not leave controller hold")

    def abort_task(self, board: str, task_id: str, reason: str) -> None:
        """Stop an active worker through Hermes, then archive its task.

        ``reclaim`` performs the host-local claim/PID validation and sends
        SIGTERM followed by SIGKILL when needed.  Archiving preserves the task,
        run, comments, log pointers, and worktree while preventing redispatch.
        """
        task = self.reader.task(board, task_id)
        if task is None or task.status == "archived":
            return
        if task.status == "running":
            self._run(
                ["reclaim", task_id, "--reason", reason[:500]],
                board=board,
            )
        task = self.reader.task(board, task_id)
        if task is not None and task.status != "archived":
            self._run(["archive", task_id], board=board)

    def prepare_human_block_for_completion(
        self, board: str, task_id: str
    ) -> None:
        """Move a Hermes triage card through audited states so it can complete.

        Hermes routes a repeatedly blocked worker to ``triage``. Its public
        ``complete`` command accepts ``ready|running|blocked`` but not
        ``triage``. Use Hermes' own ``specify_triage_task`` transition without
        changing the card fields, then promote the resulting ``todo`` card.
        This preserves task events and avoids direct SQLite writes.
        """
        task = self.reader.task(board, task_id)
        if task is None:
            raise RuntimeError(f"unknown task {task_id}")
        if task.status == "triage":
            script = "\n".join(
                [
                    "from hermes_cli import kanban_db as kb",
                    f"task_id = {task_id!r}",
                    "with kb.connect_closing() as conn:",
                    "    changed = kb.specify_triage_task(",
                    "        conn, task_id, author='hollysys-controller'",
                    "    )",
                    "raise SystemExit(0 if changed else 1)",
                ]
            )
            env = os.environ.copy()
            env["HERMES_KANBAN_BOARD"] = board
            command = [sys.executable, "-c", script]
            result = subprocess.run(
                command,
                env=env,
                text=True,
                capture_output=True,
                timeout=self.config.command_timeout_seconds,
                check=False,
            )
            if result.returncode != 0:
                raise CommandError(command, result.returncode, result.stderr)
            task = self.reader.task(board, task_id)
            if task is None:
                raise RuntimeError(f"triage card {task_id} disappeared")
        if task.status == "todo":
            self._run(
                [
                    "promote",
                    task_id,
                    "--reason",
                    "Controller accepted the matching human resolution.",
                ],
                board=board,
            )
            task = self.reader.task(board, task_id)
            if task is None:
                raise RuntimeError(f"promoted card {task_id} disappeared")
        if task.status not in {"ready", "blocked"}:
            raise RuntimeError(
                f"human-block card {task_id} cannot complete from {task.status}"
            )

    def comment(
        self, board: str, task_id: str, text: str, author: str = "hollysys-controller"
    ) -> None:
        self._run(
            ["comment", task_id, text, "--author", author],
            board=board,
        )

    def complete(
        self,
        board: str,
        task_id: str,
        summary: str,
        metadata: dict,
    ) -> None:
        task = self.reader.task(board, task_id)
        if task and task.status == "done":
            return
        self._run(
            [
                "complete",
                task_id,
                "--summary",
                summary,
                "--metadata",
                json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
            ],
            board=board,
        )

    def create_exception(
        self,
        run: RunRecord,
        parent_card_id: str,
        reason: str,
        key: str,
    ) -> TaskRecord:
        board = run.workspace.board
        existing = self.reader.task_by_idempotency(board, key)
        if existing:
            return existing
        body = (
            "[hollysys-controller-exception:v3]\n\n"
            f"run_key: {run.run_key}\n\n"
            f"reason: {reason[:1000]}\n\n"
            "该卡由 Dispatcher 作为异常入口处理；不得直接推进门禁或合并。"
        )
        payload = self._run(
            [
                "create",
                f"[{run.run_key}] CONTROLLER EXCEPTION",
                "--body",
                body,
                "--assignee",
                "dispatcher",
                "--parent",
                parent_card_id,
                "--tenant",
                run.run_key,
                "--workspace",
                f"dir:{run.workspace.worktree}",
                "--idempotency-key",
                key,
                "--created-by",
                "hollysys-controller",
                "--initial-status",
                "blocked",
                "--skill",
                "hollysys-dispatch-kanban",
                "--json",
            ],
            board=board,
            json_output=True,
        )
        task = self.reader.task(board, str(payload["id"]))
        if task is None:
            raise RuntimeError("created exception card was not readable")
        return task
