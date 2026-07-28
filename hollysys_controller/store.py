from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS event_cursor (
    board TEXT PRIMARY KEY,
    event_id INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS managed_cards (
    board TEXT NOT NULL,
    card_id TEXT NOT NULL,
    run_key TEXT NOT NULL,
    stage TEXT NOT NULL,
    iteration INTEGER NOT NULL,
    idempotency_key TEXT NOT NULL,
    parent_card_id TEXT,
    purpose TEXT NOT NULL DEFAULT 'work',
    created_at INTEGER NOT NULL,
    PRIMARY KEY (board, card_id),
    UNIQUE (board, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_managed_run
    ON managed_cards(run_key, created_at, card_id);

CREATE TABLE IF NOT EXISTS requests (
    request_key TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL,
    response TEXT,
    error TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS operations (
    operation_key TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL,
    result TEXT,
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS outbox (
    outbox_key TEXT PRIMARY KEY,
    run_key TEXT NOT NULL,
    event TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
"""


@dataclass(frozen=True)
class ManagedCard:
    board: str
    card_id: str
    run_key: str
    stage: str
    iteration: int
    idempotency_key: str
    parent_card_id: str | None
    purpose: str
    created_at: int


class ControllerStore:
    def __init__(self, path: Path, *, read_only: bool = False):
        self.path = path
        self.read_only = read_only
        if read_only:
            if not path.is_file():
                raise FileNotFoundError(path)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        if self.read_only:
            conn = sqlite3.connect(
                f"file:{self.path}?mode=ro",
                uri=True,
                timeout=10,
            )
        else:
            conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            if not self.read_only:
                conn.commit()
        finally:
            conn.close()

    def cursor(self, board: str) -> int:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT event_id FROM event_cursor WHERE board = ?", (board,)
            ).fetchone()
            return int(row["event_id"]) if row else 0

    def set_cursor(self, board: str, event_id: int) -> None:
        now = int(time.time())
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO event_cursor(board, event_id, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(board) DO UPDATE SET
                    event_id=MAX(event_cursor.event_id, excluded.event_id),
                    updated_at=excluded.updated_at
                """,
                (board, event_id, now),
            )

    def add_managed_card(
        self,
        *,
        board: str,
        card_id: str,
        run_key: str,
        stage: str,
        iteration: int,
        idempotency_key: str,
        parent_card_id: str | None,
        purpose: str = "work",
        created_at: int | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO managed_cards(
                    board, card_id, run_key, stage, iteration, idempotency_key,
                    parent_card_id, purpose, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(board, card_id) DO UPDATE SET
                    run_key=excluded.run_key,
                    stage=excluded.stage,
                    iteration=excluded.iteration,
                    idempotency_key=excluded.idempotency_key,
                    parent_card_id=excluded.parent_card_id,
                    purpose=excluded.purpose
                """,
                (
                    board,
                    card_id,
                    run_key,
                    stage,
                    iteration,
                    idempotency_key,
                    parent_card_id,
                    purpose,
                    created_at or int(time.time()),
                ),
            )

    def managed_card(self, board: str, card_id: str) -> ManagedCard | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM managed_cards WHERE board=? AND card_id=?",
                (board, card_id),
            ).fetchone()
            return ManagedCard(**dict(row)) if row else None

    def cards_for_run(self, run_key: str) -> list[ManagedCard]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM managed_cards WHERE run_key=?
                ORDER BY created_at, rowid
                """,
                (run_key,),
            ).fetchall()
            return [ManagedCard(**dict(row)) for row in rows]

    def run_keys(self) -> list[str]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT run_key FROM managed_cards ORDER BY run_key"
            ).fetchall()
            return [str(row[0]) for row in rows]

    def begin_request(self, key: str, kind: str, payload: dict) -> dict | None:
        now = int(time.time())
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with self.connect() as conn:
            row = conn.execute(
                "SELECT payload, status, response, error FROM requests WHERE request_key=?",
                (key,),
            ).fetchone()
            if row:
                if row["payload"] != encoded:
                    raise ValueError(
                        f"idempotency key {key} was reused with new payload"
                    )
                if row["status"] == "done" and row["response"]:
                    return json.loads(row["response"])
                if row["status"] == "failed":
                    raise RuntimeError(row["error"] or f"request {key} failed")
                return None
            conn.execute(
                """
                INSERT INTO requests(
                    request_key, kind, payload, status, created_at, updated_at
                ) VALUES (?, ?, ?, 'running', ?, ?)
                """,
                (key, kind, encoded, now, now),
            )
        return None

    def finish_request(self, key: str, response: dict) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE requests SET status='done', response=?, error=NULL, updated_at=?
                WHERE request_key=?
                """,
                (json.dumps(response, ensure_ascii=False), int(time.time()), key),
            )

    def fail_request(self, key: str, error: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE requests SET status='failed', error=?, updated_at=?
                WHERE request_key=?
                """,
                (error[:2000], int(time.time()), key),
            )

    def running_requests(self) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT request_key, kind, payload FROM requests
                WHERE status='running' ORDER BY created_at
                """
            ).fetchall()
        return [
            {
                "request_key": row["request_key"],
                "kind": row["kind"],
                "payload": json.loads(row["payload"]),
            }
            for row in rows
        ]

    def operation_result(self, key: str, kind: str, payload: dict) -> dict | None:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        now = int(time.time())
        with self.connect() as conn:
            row = conn.execute(
                "SELECT kind, payload, status, result FROM operations WHERE operation_key=?",
                (key,),
            ).fetchone()
            if row:
                if row["kind"] != kind or row["payload"] != encoded:
                    raise ValueError(f"operation key {key} was reused")
                if row["status"] == "done" and row["result"]:
                    return json.loads(row["result"])
                conn.execute(
                    """
                    UPDATE operations SET status='running', attempts=attempts+1,
                        updated_at=? WHERE operation_key=?
                    """,
                    (now, key),
                )
                return None
            conn.execute(
                """
                INSERT INTO operations(
                    operation_key, kind, payload, status, attempts, created_at, updated_at
                ) VALUES (?, ?, ?, 'running', 1, ?, ?)
                """,
                (key, kind, encoded, now, now),
            )
        return None

    def finish_operation(self, key: str, result: dict) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE operations SET status='done', result=?, error=NULL, updated_at=?
                WHERE operation_key=?
                """,
                (json.dumps(result, ensure_ascii=False), int(time.time()), key),
            )

    def fail_operation(self, key: str, error: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE operations SET status='failed', error=?, updated_at=?
                WHERE operation_key=?
                """,
                (error[:2000], int(time.time()), key),
            )

    def enqueue(self, key: str, run_key: str, event: str, payload: dict) -> None:
        now = int(time.time())
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO outbox(
                    outbox_key, run_key, event, payload, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    key,
                    run_key,
                    event,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    now,
                    now,
                ),
            )

    def pending_outbox(self, limit: int = 20) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM outbox WHERE status IN ('pending', 'failed')
                ORDER BY created_at LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    def finish_outbox(self, key: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE outbox SET status='done', attempts=attempts+1,
                    last_error=NULL, updated_at=? WHERE outbox_key=?
                """,
                (int(time.time()), key),
            )

    def fail_outbox(self, key: str, error: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE outbox SET status='failed', attempts=attempts+1,
                    last_error=?, updated_at=? WHERE outbox_key=?
                """,
                (error[:2000], int(time.time()), key),
            )

    def health(self) -> dict:
        with self.connect() as conn:
            cursors = {
                row["board"]: row["event_id"]
                for row in conn.execute(
                    "SELECT board, event_id FROM event_cursor ORDER BY board"
                )
            }
            pending = conn.execute(
                "SELECT COUNT(*) FROM outbox WHERE status != 'done'"
            ).fetchone()[0]
            failed_ops = conn.execute(
                "SELECT COUNT(*) FROM operations WHERE status='failed'"
            ).fetchone()[0]
        return {
            "event_cursors": cursors,
            "outbox_pending": pending,
            "failed_operations": failed_ops,
        }
