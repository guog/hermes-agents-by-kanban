from __future__ import annotations

import json
import sqlite3
import time
import uuid
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

CREATE TABLE IF NOT EXISTS card_runtime (
    board TEXT NOT NULL,
    card_id TEXT NOT NULL,
    worker_started_at INTEGER,
    worker_session_id TEXT,
    last_heartbeat_at INTEGER,
    last_progress_event_at INTEGER,
    deadline_at INTEGER,
    last_event_kind TEXT,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (board, card_id)
);

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

CREATE TABLE IF NOT EXISTS run_control (
    run_key TEXT PRIMARY KEY,
    state TEXT NOT NULL DEFAULT 'active',
    abort_requested_by TEXT,
    abort_reason TEXT,
    abort_requested_at INTEGER,
    aborted_at INTEGER,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS abort_requests (
    request_id TEXT PRIMARY KEY,
    run_key TEXT NOT NULL,
    token_hash TEXT NOT NULL,
    sender TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    thread_id TEXT,
    reason TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    confirm_message_id TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    FOREIGN KEY(run_key) REFERENCES run_control(run_key)
);
CREATE INDEX IF NOT EXISTS idx_abort_pending
    ON abort_requests(run_key, status, created_at);

CREATE TABLE IF NOT EXISTS dependency_outages (
    dependency TEXT PRIMARY KEY,
    outage_id TEXT NOT NULL,
    status TEXT NOT NULL,
    error_class TEXT NOT NULL,
    error_summary TEXT NOT NULL,
    failures INTEGER NOT NULL,
    started_at INTEGER NOT NULL,
    next_retry_at INTEGER NOT NULL,
    recovered_at INTEGER,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS dependency_outage_runs (
    dependency TEXT NOT NULL,
    outage_id TEXT NOT NULL,
    run_key TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (dependency, outage_id, run_key)
);

CREATE TABLE IF NOT EXISTS merge_wait (
    run_key TEXT PRIMARY KEY,
    mr_iid INTEGER,
    head_sha TEXT,
    blocker_kind TEXT NOT NULL,
    blocker TEXT NOT NULL,
    first_seen_at INTEGER NOT NULL,
    last_checked_at INTEGER NOT NULL,
    next_retry_at INTEGER NOT NULL
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

    def record_card_runtime_event(
        self,
        *,
        board: str,
        card_id: str,
        kind: str,
        created_at: int,
        worker_session_id: str | None,
        lease_seconds: int,
    ) -> None:
        started = kind in {"claimed", "started", "worker_started"}
        heartbeat = kind in {"heartbeat", "worker_heartbeat"}
        progress = started or heartbeat or kind in {
            "progress",
            "completed",
            "blocked",
            "crashed",
            "timed_out",
            "gave_up",
            "spawn_auto_blocked",
        }
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO card_runtime(
                    board, card_id, worker_started_at, worker_session_id,
                    last_heartbeat_at, last_progress_event_at, deadline_at,
                    last_event_kind, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(board, card_id) DO UPDATE SET
                    worker_started_at=COALESCE(
                        card_runtime.worker_started_at,
                        excluded.worker_started_at
                    ),
                    worker_session_id=COALESCE(
                        excluded.worker_session_id,
                        card_runtime.worker_session_id
                    ),
                    last_heartbeat_at=COALESCE(
                        excluded.last_heartbeat_at,
                        card_runtime.last_heartbeat_at
                    ),
                    last_progress_event_at=COALESCE(
                        excluded.last_progress_event_at,
                        card_runtime.last_progress_event_at
                    ),
                    deadline_at=COALESCE(
                        excluded.deadline_at,
                        card_runtime.deadline_at
                    ),
                    last_event_kind=excluded.last_event_kind,
                    updated_at=excluded.updated_at
                """,
                (
                    board,
                    card_id,
                    created_at if started else None,
                    worker_session_id,
                    created_at if heartbeat else None,
                    created_at if progress else None,
                    created_at + lease_seconds if progress else None,
                    kind,
                    int(time.time()),
                ),
            )

    def card_runtime(self, board: str, card_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM card_runtime WHERE board=? AND card_id=?",
                (board, card_id),
            ).fetchone()
        return dict(row) if row else None

    def runtime_for_run(self, run_key: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT runtime.*, cards.stage, cards.iteration
                FROM card_runtime AS runtime
                JOIN managed_cards AS cards
                  ON cards.board=runtime.board
                 AND cards.card_id=runtime.card_id
                WHERE cards.run_key=?
                ORDER BY runtime.updated_at, runtime.card_id
                """,
                (run_key,),
            ).fetchall()
        return [dict(row) for row in rows]

    def run_keys(self) -> list[str]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT run_key FROM managed_cards ORDER BY run_key"
            ).fetchall()
            return [str(row[0]) for row in rows]

    def ensure_run_control(self, run_key: str) -> None:
        now = int(time.time())
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO run_control(run_key, state, updated_at)
                VALUES (?, 'active', ?)
                """,
                (run_key, now),
            )

    def run_control(self, run_key: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM run_control WHERE run_key=?", (run_key,)
            ).fetchone()
        return dict(row) if row else None

    def create_abort_request(
        self,
        *,
        request_id: str,
        run_key: str,
        token_hash: str,
        sender: str,
        chat_id: str,
        thread_id: str | None,
        reason: str,
        expires_at: int,
    ) -> dict:
        now = int(time.time())
        self.ensure_run_control(run_key)
        with self.connect() as conn:
            control = conn.execute(
                "SELECT state FROM run_control WHERE run_key=?", (run_key,)
            ).fetchone()
            if control is None:
                raise ValueError(f"unknown run {run_key}")
            if control["state"] != "active":
                raise ValueError(
                    f"run {run_key} cannot request abort from {control['state']}"
                )
            conn.execute(
                """
                UPDATE abort_requests SET status='expired', updated_at=?
                WHERE run_key=? AND status='pending'
                """,
                (now, run_key),
            )
            conn.execute(
                """
                INSERT INTO abort_requests(
                    request_id, run_key, token_hash, sender, chat_id, thread_id,
                    reason, expires_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    run_key,
                    token_hash,
                    sender,
                    chat_id,
                    thread_id,
                    reason,
                    expires_at,
                    now,
                    now,
                ),
            )
        return {
            "request_id": request_id,
            "run_key": run_key,
            "expires_at": expires_at,
            "reason": reason,
        }

    def confirm_abort_request(
        self,
        *,
        run_key: str,
        token_hash: str,
        sender: str,
        chat_id: str,
        thread_id: str | None,
        message_id: str,
    ) -> dict:
        now = int(time.time())
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM abort_requests
                WHERE run_key=? AND status='pending'
                ORDER BY created_at DESC LIMIT 1
                """,
                (run_key,),
            ).fetchone()
            if row is None:
                raise ValueError("no pending abort request")
            if int(row["expires_at"]) < now:
                conn.execute(
                    """
                    UPDATE abort_requests SET status='expired', updated_at=?
                    WHERE request_id=?
                    """,
                    (now, row["request_id"]),
                )
                raise ValueError("abort confirmation token expired")
            if (
                row["token_hash"] != token_hash
                or row["sender"] != sender
                or row["chat_id"] != chat_id
                or (row["thread_id"] or None) != (thread_id or None)
            ):
                raise PermissionError(
                    "abort confirmation must match token, requester, and channel"
                )
            conn.execute(
                """
                UPDATE abort_requests
                SET status='confirmed', confirm_message_id=?, updated_at=?
                WHERE request_id=?
                """,
                (message_id, now, row["request_id"]),
            )
            conn.execute(
                """
                UPDATE run_control
                SET state='abort_requested', abort_requested_by=?,
                    abort_reason=?, abort_requested_at=?, updated_at=?
                WHERE run_key=? AND state='active'
                """,
                (sender, row["reason"], now, now, run_key),
            )
            control = conn.execute(
                "SELECT * FROM run_control WHERE run_key=?", (run_key,)
            ).fetchone()
        if control is None or control["state"] != "abort_requested":
            raise ValueError("run is no longer active")
        return dict(control)

    def mark_aborting(self, run_key: str) -> None:
        now = int(time.time())
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE run_control SET state='aborting', updated_at=?
                WHERE run_key=? AND state IN ('abort_requested', 'aborting')
                """,
                (now, run_key),
            )

    def finish_abort(self, run_key: str, state: str = "aborted") -> None:
        if state not in {"aborted", "completed_before_abort"}:
            raise ValueError(f"invalid abort terminal state {state}")
        now = int(time.time())
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE run_control SET state=?, aborted_at=?, updated_at=?
                WHERE run_key=? AND state IN ('abort_requested', 'aborting')
                """,
                (state, now, now, run_key),
            )

    def active_abort_run_keys(self) -> list[str]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT run_key FROM run_control
                WHERE state IN ('abort_requested', 'aborting')
                ORDER BY updated_at
                """
            ).fetchall()
        return [str(row["run_key"]) for row in rows]

    def record_dependency_failure(
        self,
        dependency: str,
        error: str,
        *,
        initial_backoff_seconds: float,
        maximum_backoff_seconds: float,
        error_class: str = "dependency_transient",
    ) -> dict:
        now = int(time.time())
        with self.connect() as conn:
            previous = conn.execute(
                "SELECT * FROM dependency_outages WHERE dependency=?",
                (dependency,),
            ).fetchone()
            continuing = previous is not None and previous["status"] == "open"
            failures = int(previous["failures"]) + 1 if continuing else 1
            outage_id = (
                str(previous["outage_id"])
                if continuing
                else uuid.uuid4().hex
            )
            delay = min(
                maximum_backoff_seconds,
                initial_backoff_seconds * (2 ** (failures - 1)),
            )
            next_retry_at = now + max(1, int(delay))
            started_at = (
                int(previous["started_at"]) if continuing else now
            )
            conn.execute(
                """
                INSERT INTO dependency_outages(
                    dependency, outage_id, status, error_class, error_summary,
                    failures, started_at, next_retry_at, recovered_at, updated_at
                ) VALUES (?, ?, 'open', ?, ?, ?, ?, ?, NULL, ?)
                ON CONFLICT(dependency) DO UPDATE SET
                    outage_id=excluded.outage_id,
                    status='open',
                    error_class=excluded.error_class,
                    error_summary=excluded.error_summary,
                    failures=excluded.failures,
                    started_at=excluded.started_at,
                    next_retry_at=excluded.next_retry_at,
                    recovered_at=NULL,
                    updated_at=excluded.updated_at
                """,
                (
                    dependency,
                    outage_id,
                    error_class,
                    error[:1000],
                    failures,
                    started_at,
                    next_retry_at,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM dependency_outages WHERE dependency=?",
                (dependency,),
            ).fetchone()
        return dict(row)

    def associate_outage_run(
        self,
        dependency: str,
        outage_id: str,
        run_key: str,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO dependency_outage_runs(
                    dependency, outage_id, run_key, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (dependency, outage_id, run_key, int(time.time())),
            )

    def outage_run_keys(
        self,
        dependency: str,
        outage_id: str,
    ) -> list[str]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT run_key FROM dependency_outage_runs
                WHERE dependency=? AND outage_id=?
                ORDER BY run_key
                """,
                (dependency, outage_id),
            ).fetchall()
        return [str(row["run_key"]) for row in rows]

    def recover_dependency(self, dependency: str) -> dict | None:
        now = int(time.time())
        with self.connect() as conn:
            previous = conn.execute(
                """
                SELECT * FROM dependency_outages
                WHERE dependency=? AND status='open'
                """,
                (dependency,),
            ).fetchone()
            if previous is None:
                return None
            conn.execute(
                """
                UPDATE dependency_outages
                SET status='recovered', recovered_at=?, updated_at=?
                WHERE dependency=?
                """,
                (now, now, dependency),
            )
        return dict(previous)

    def open_dependency_outages(self) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM dependency_outages
                WHERE status='open'
                ORDER BY started_at, dependency
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def set_merge_wait(
        self,
        run_key: str,
        *,
        mr_iid: int,
        head_sha: str | None,
        blocker_kind: str,
        blocker: str,
        retry_seconds: int,
    ) -> dict:
        now = int(time.time())
        with self.connect() as conn:
            previous = conn.execute(
                "SELECT * FROM merge_wait WHERE run_key=?",
                (run_key,),
            ).fetchone()
            same_blocker = (
                previous is not None
                and previous["head_sha"] == head_sha
                and previous["blocker_kind"] == blocker_kind
                and previous["blocker"] == blocker[:1000]
            )
            first_seen_at = (
                int(previous["first_seen_at"]) if same_blocker else now
            )
            conn.execute(
                """
                INSERT INTO merge_wait(
                    run_key, mr_iid, head_sha, blocker_kind, blocker,
                    first_seen_at, last_checked_at, next_retry_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_key) DO UPDATE SET
                    mr_iid=excluded.mr_iid,
                    head_sha=excluded.head_sha,
                    blocker_kind=excluded.blocker_kind,
                    blocker=excluded.blocker,
                    first_seen_at=excluded.first_seen_at,
                    last_checked_at=excluded.last_checked_at,
                    next_retry_at=excluded.next_retry_at
                """,
                (
                    run_key,
                    mr_iid,
                    head_sha,
                    blocker_kind,
                    blocker[:1000],
                    first_seen_at,
                    now,
                    now + retry_seconds,
                ),
            )
            row = conn.execute(
                "SELECT * FROM merge_wait WHERE run_key=?",
                (run_key,),
            ).fetchone()
        result = dict(row)
        result["changed"] = not same_blocker
        return result

    def clear_merge_wait(self, run_key: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM merge_wait WHERE run_key=?", (run_key,))

    def merge_wait(self, run_key: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM merge_wait WHERE run_key=?",
                (run_key,),
            ).fetchone()
        return dict(row) if row else None

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
            aborting = conn.execute(
                """
                SELECT COUNT(*) FROM run_control
                WHERE state IN ('abort_requested', 'aborting')
                """
            ).fetchone()[0]
        return {
            "event_cursors": cursors,
            "outbox_pending": pending,
            "failed_operations": failed_ops,
            "aborting_runs": aborting,
            "dependency_outages": self.open_dependency_outages(),
        }
