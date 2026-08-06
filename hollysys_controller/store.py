from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .errors import ControllerFatalError
from .models import DeliveryBinding, RunRecord

SCHEMA_VERSION = 4
TERMINAL_RUN_STATES = {
    "completed",
    "completed_ready",
    "completed_with_findings",
    "completed_test_failed",
    "aborted",
    "completed_before_abort",
}
RUN_STATES = {
    "active",
    "transition_pending",
    "human_blocked",
    "retry_wait",
    "dependency_degraded",
    "merge_wait",
    "abort_requested",
    "aborting",
    "exception",
    *TERMINAL_RUN_STATES,
}

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS event_cursor (
    board TEXT PRIMARY KEY,
    event_id INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_key TEXT PRIMARY KEY,
    source_key TEXT NOT NULL,
    run_generation TEXT NOT NULL UNIQUE,
    started_at TEXT NOT NULL,
    provenance TEXT NOT NULL CHECK(provenance = 'fresh_v4'),
    record_json TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_source
    ON runs(source_key, created_at, run_key);

CREATE TABLE IF NOT EXISTS delivery_bindings (
    run_key TEXT PRIMARY KEY,
    mr_iid INTEGER NOT NULL UNIQUE,
    mr_url TEXT NOT NULL,
    creator TEXT NOT NULL,
    created_at TEXT NOT NULL,
    initial_head_sha TEXT NOT NULL,
    claim_note_id INTEGER NOT NULL,
    binding_json TEXT NOT NULL,
    updated_at INTEGER NOT NULL,
    FOREIGN KEY(run_key) REFERENCES runs(run_key)
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
    profile TEXT,
    dispatch_key TEXT,
    worker_pid INTEGER,
    worker_started_at INTEGER,
    worker_session_id TEXT,
    last_heartbeat_at INTEGER,
    last_progress_event_at INTEGER,
    deadline_at INTEGER,
    last_event_kind TEXT,
    attempt INTEGER NOT NULL DEFAULT 1,
    redispatch_count INTEGER NOT NULL DEFAULT 0,
    worktree TEXT,
    branch TEXT,
    mr_iid INTEGER,
    head_sha TEXT,
    attempt_status TEXT NOT NULL DEFAULT 'created',
    finished_at INTEGER,
    terminal_reason TEXT,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (board, card_id)
);

CREATE TABLE IF NOT EXISTS card_attempts (
    run_id TEXT PRIMARY KEY,
    board TEXT NOT NULL,
    card_id TEXT NOT NULL,
    run_key TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    profile TEXT,
    dispatch_key TEXT,
    worker_pid INTEGER,
    worker_session_id TEXT,
    started_at INTEGER,
    blocked_at INTEGER,
    completion_at INTEGER,
    exited_at INTEGER,
    last_heartbeat_at INTEGER,
    last_progress_at INTEGER,
    status TEXT NOT NULL,
    terminal_reason TEXT,
    recovery_reason TEXT,
    redispatched_from_run_id TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE(board, card_id, attempt),
    FOREIGN KEY(run_key) REFERENCES runs(run_key)
);
CREATE INDEX IF NOT EXISTS idx_card_attempts_card
    ON card_attempts(board, card_id, attempt);
CREATE INDEX IF NOT EXISTS idx_card_attempts_run
    ON card_attempts(run_key, created_at, run_id);

CREATE TABLE IF NOT EXISTS reconcile_intents (
    intent_id TEXT PRIMARY KEY,
    run_key TEXT NOT NULL UNIQUE,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    event_id INTEGER,
    lease_owner TEXT,
    lease_until INTEGER,
    current_step TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    revision INTEGER NOT NULL DEFAULT 1,
    leased_revision INTEGER,
    last_error TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    FOREIGN KEY(run_key) REFERENCES runs(run_key)
);
CREATE INDEX IF NOT EXISTS idx_reconcile_pending
    ON reconcile_intents(status, lease_until, created_at);

CREATE TABLE IF NOT EXISTS validation_runs (
    validation_id TEXT PRIMARY KEY,
    run_key TEXT NOT NULL,
    card_id TEXT NOT NULL,
    validator TEXT NOT NULL,
    validator_version TEXT NOT NULL,
    input_digest TEXT NOT NULL,
    result_digest TEXT NOT NULL,
    passed INTEGER NOT NULL,
    error_codes TEXT NOT NULL,
    timing_json TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    FOREIGN KEY(run_key) REFERENCES runs(run_key)
);
CREATE INDEX IF NOT EXISTS idx_validation_card
    ON validation_runs(run_key, card_id, created_at);

CREATE TABLE IF NOT EXISTS attempt_metrics (
    run_id TEXT NOT NULL,
    metric TEXT NOT NULL,
    duration_ms INTEGER NOT NULL,
    summary TEXT,
    recorded_at INTEGER NOT NULL,
    PRIMARY KEY(run_id, metric),
    FOREIGN KEY(run_id) REFERENCES card_attempts(run_id)
);

CREATE TABLE IF NOT EXISTS requests (
    request_key TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    run_key TEXT,
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
    expected_state_version INTEGER,
    expected_head_sha TEXT,
    uncertain_at INTEGER,
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
    error_class TEXT,
    next_attempt_at INTEGER NOT NULL,
    dead_at INTEGER,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS run_control (
    run_key TEXT PRIMARY KEY,
    state TEXT NOT NULL DEFAULT 'active',
    state_version INTEGER NOT NULL DEFAULT 1,
    terminal_at INTEGER,
    last_transition_reason TEXT,
    next_retry_at INTEGER,
    compliance TEXT,
    completion_source TEXT,
    checked_head TEXT,
    merge_commit_sha TEXT,
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
    peak_failures INTEGER NOT NULL,
    started_at INTEGER NOT NULL,
    last_failure_at INTEGER NOT NULL,
    last_success_at INTEGER,
    endpoint TEXT,
    circuit_state TEXT NOT NULL DEFAULT 'open',
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
    blocker_url TEXT,
    blocker_owner TEXT,
    blocker_updated_at TEXT,
    first_seen_at INTEGER NOT NULL,
    last_checked_at INTEGER NOT NULL,
    next_retry_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS dependency_outage_history (
    dependency TEXT NOT NULL,
    outage_id TEXT NOT NULL,
    error_class TEXT NOT NULL,
    error_summary TEXT NOT NULL,
    peak_failures INTEGER NOT NULL,
    started_at INTEGER NOT NULL,
    recovered_at INTEGER NOT NULL,
    duration_seconds INTEGER NOT NULL,
    endpoint TEXT,
    PRIMARY KEY (dependency, outage_id)
);

CREATE TABLE IF NOT EXISTS controller_boots (
    boot_id TEXT PRIMARY KEY,
    started_at INTEGER NOT NULL,
    stopped_at INTEGER,
    exit_reason TEXT,
    fatal INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS profile_preflight (
    profile TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    api_ok INTEGER,
    repository_read_ok INTEGER,
    repository_write_ok TEXT,
    https_username_ok INTEGER,
    remote_protocol TEXT,
    error_code TEXT,
    deep INTEGER NOT NULL,
    wrapper_checked_at INTEGER,
    checked_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS deployment_preflight (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    ok INTEGER NOT NULL,
    deep INTEGER NOT NULL,
    credential_contract_digest TEXT NOT NULL,
    checked_at INTEGER NOT NULL
);

PRAGMA user_version=4;
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
            self._validate_existing()
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        existed = path.is_file() and path.stat().st_size > 0
        if existed:
            self._validate_existing()
        else:
            with self.connect() as conn:
                conn.executescript(SCHEMA)
            self._validate_existing()

    def _validate_existing(self) -> None:
        mode = "ro" if self.read_only else "rw"
        conn = sqlite3.connect(
            f"file:{self.path}?mode={mode}",
            uri=True,
            timeout=10,
        )
        try:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version != SCHEMA_VERSION:
                raise ControllerFatalError(
                    f"unsupported_controller_schema:{version}; "
                    f"expected:{SCHEMA_VERSION}; fresh v4 state is required"
                )
            result = str(conn.execute("PRAGMA quick_check").fetchone()[0])
            if result != "ok":
                raise ControllerFatalError(
                    f"controller_store_quick_check_failed:{result}"
                )
            required_columns = {
                "runs": {
                    "run_key",
                    "source_key",
                    "run_generation",
                    "started_at",
                    "provenance",
                    "record_json",
                },
                "delivery_bindings": {
                    "run_key",
                    "mr_iid",
                    "creator",
                    "initial_head_sha",
                    "claim_note_id",
                    "binding_json",
                },
                "card_attempts": {
                    "run_id",
                    "card_id",
                    "run_key",
                    "attempt",
                    "worker_pid",
                    "worker_session_id",
                    "last_heartbeat_at",
                    "last_progress_at",
                    "status",
                    "redispatched_from_run_id",
                },
                "reconcile_intents": {
                    "intent_id",
                    "run_key",
                    "status",
                    "lease_owner",
                    "lease_until",
                    "current_step",
                    "attempts",
                    "revision",
                    "leased_revision",
                },
                "validation_runs": {
                    "validation_id",
                    "run_key",
                    "card_id",
                    "validator_version",
                    "input_digest",
                    "result_digest",
                    "timing_json",
                },
                "attempt_metrics": {
                    "run_id",
                    "metric",
                    "duration_ms",
                },
                "run_control": {
                    "state",
                    "state_version",
                    "terminal_at",
                    "last_transition_reason",
                    "next_retry_at",
                    "compliance",
                    "completion_source",
                    "checked_head",
                    "merge_commit_sha",
                    "abort_requested_by",
                    "abort_reason",
                    "abort_requested_at",
                    "aborted_at",
                },
                "card_runtime": {
                    "profile",
                    "dispatch_key",
                    "worker_pid",
                    "worker_session_id",
                    "last_progress_event_at",
                    "deadline_at",
                    "attempt",
                    "redispatch_count",
                    "worktree",
                    "branch",
                    "mr_iid",
                    "head_sha",
                    "attempt_status",
                    "terminal_reason",
                },
                "operations": {
                    "expected_state_version",
                    "expected_head_sha",
                    "uncertain_at",
                    "attempts",
                },
                "requests": {
                    "run_key",
                },
                "outbox": {
                    "attempts",
                    "error_class",
                    "next_attempt_at",
                    "dead_at",
                },
                "merge_wait": {
                    "blocker_kind",
                    "blocker_url",
                    "blocker_owner",
                    "blocker_updated_at",
                    "first_seen_at",
                    "last_checked_at",
                    "next_retry_at",
                },
                "dependency_outages": {
                    "dependency",
                    "outage_id",
                    "error_class",
                    "circuit_state",
                    "next_retry_at",
                },
                "profile_preflight": {
                    "profile",
                    "role",
                    "api_ok",
                    "repository_read_ok",
                    "repository_write_ok",
                    "https_username_ok",
                    "error_code",
                    "deep",
                    "wrapper_checked_at",
                    "checked_at",
                },
                "deployment_preflight": {
                    "ok",
                    "deep",
                    "credential_contract_digest",
                    "checked_at",
                },
            }
            for table, expected in required_columns.items():
                actual = {
                    str(row[1])
                    for row in conn.execute(f"PRAGMA table_info({table})")
                }
                if not expected.issubset(actual):
                    raise ControllerFatalError(
                        f"controller_store_schema_contract_failed:{table}"
                    )
            invariant_checks = {
                "unknown_run_state": """
                    SELECT 1 FROM run_control
                    WHERE state NOT IN (
                        'active', 'transition_pending', 'human_blocked',
                        'retry_wait', 'dependency_degraded', 'merge_wait',
                        'abort_requested', 'aborting', 'exception',
                        'completed', 'completed_ready',
                        'completed_with_findings', 'completed_test_failed',
                        'aborted', 'completed_before_abort'
                    ) LIMIT 1
                """,
                "invalid_state_version": """
                    SELECT 1 FROM run_control
                    WHERE state_version < 1 LIMIT 1
                """,
                "terminal_timestamp_mismatch": """
                    SELECT 1 FROM run_control
                    WHERE (
                        state IN (
                            'completed', 'completed_ready',
                            'completed_with_findings', 'completed_test_failed',
                            'aborted', 'completed_before_abort'
                        ) AND terminal_at IS NULL
                    ) OR (
                        state NOT IN (
                            'completed', 'completed_ready',
                            'completed_with_findings', 'completed_test_failed',
                            'aborted', 'completed_before_abort'
                        ) AND terminal_at IS NOT NULL
                    ) LIMIT 1
                """,
                "retry_schedule_mismatch": """
                    SELECT 1 FROM run_control
                    WHERE (
                        state IN (
                            'dependency_degraded', 'merge_wait', 'retry_wait'
                        )
                        AND next_retry_at IS NULL
                    ) OR (
                        state NOT IN (
                            'dependency_degraded', 'merge_wait', 'retry_wait',
                            'abort_requested', 'aborting'
                        )
                        AND next_retry_at IS NOT NULL
                    ) LIMIT 1
                """,
                "completed_metadata_missing": """
                    SELECT 1 FROM run_control
                    WHERE state='completed' AND (
                        compliance NOT IN ('verified', 'unverified')
                        OR completion_source NOT IN ('controller', 'external')
                        OR checked_head IS NULL
                        OR length(checked_head) != 40
                        OR checked_head GLOB '*[^0-9a-f]*'
                    ) LIMIT 1
                """,
                "flow_completion_metadata_missing": """
                    SELECT 1 FROM run_control
                    WHERE state IN (
                        'completed_ready', 'completed_with_findings',
                        'completed_test_failed'
                    ) AND (
                        compliance NOT IN ('verified', 'unverified')
                        OR completion_source != 'controller'
                        OR checked_head IS NULL
                        OR length(checked_head) != 40
                        OR checked_head GLOB '*[^0-9a-f]*'
                        OR merge_commit_sha IS NOT NULL
                    ) LIMIT 1
                """,
                "abort_metadata_missing": """
                    SELECT 1 FROM run_control
                    WHERE state IN (
                        'abort_requested', 'aborting', 'aborted',
                        'completed_before_abort'
                    ) AND (
                        abort_requested_by IS NULL OR abort_requested_by=''
                        OR abort_reason IS NULL OR abort_reason=''
                        OR abort_requested_at IS NULL
                    ) LIMIT 1
                """,
                "abort_terminal_timestamp_missing": """
                    SELECT 1 FROM run_control
                    WHERE state IN ('aborted', 'completed_before_abort')
                      AND aborted_at IS NULL
                    LIMIT 1
                """,
                "completed_before_abort_head_missing": """
                    SELECT 1 FROM run_control
                    WHERE state='completed_before_abort' AND (
                        checked_head IS NULL
                        OR length(checked_head) != 40
                        OR checked_head GLOB '*[^0-9a-f]*'
                    ) LIMIT 1
                """,
                "invalid_merge_commit_sha": """
                    SELECT 1 FROM run_control
                    WHERE merge_commit_sha IS NOT NULL AND (
                        length(merge_commit_sha) != 40
                        OR merge_commit_sha GLOB '*[^0-9a-f]*'
                    ) LIMIT 1
                """,
            }
            for code, query in invariant_checks.items():
                if conn.execute(query).fetchone() is not None:
                    raise ControllerFatalError(
                        f"controller_store_invariant_failed:{code}"
                    )
        except sqlite3.DatabaseError as exc:
            raise ControllerFatalError("controller_store_unreadable") from exc
        finally:
            conn.close()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        try:
            if self.read_only:
                conn = sqlite3.connect(
                    f"file:{self.path}?mode=ro",
                    uri=True,
                    timeout=10,
                )
            else:
                conn = sqlite3.connect(self.path, timeout=10)
            try:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA busy_timeout=10000")
                conn.execute("PRAGMA foreign_keys=ON")
                if self.read_only:
                    conn.execute("PRAGMA query_only=ON")
                yield conn
                if not self.read_only:
                    conn.commit()
            finally:
                conn.close()
        except ControllerFatalError:
            raise
        except sqlite3.DatabaseError as exc:
            raise ControllerFatalError(
                "controller_store_database_error"
            ) from exc

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

    def save_run(self, run: RunRecord) -> None:
        now = int(time.time())
        payload = run.model_dump_json()
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT record_json FROM runs WHERE run_key=?",
                (run.run_key,),
            ).fetchone()
            if existing is not None:
                persisted = RunRecord.model_validate_json(existing["record_json"])
                if persisted != run:
                    raise ControllerFatalError(
                        f"run_identity_conflict:{run.run_key}"
                    )
                return
            conn.execute(
                """
                INSERT INTO runs(
                    run_key, source_key, run_generation, started_at,
                    provenance, record_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.run_key,
                    run.source_key,
                    run.run_generation,
                    run.started_at.isoformat(),
                    run.provenance,
                    payload,
                    now,
                ),
            )

    def run_record(self, run_key: str) -> RunRecord | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT record_json FROM runs WHERE run_key=?",
                (run_key,),
            ).fetchone()
        return (
            RunRecord.model_validate_json(row["record_json"])
            if row is not None
            else None
        )

    def runs_for_source(self, source_key: str) -> list[RunRecord]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT record_json FROM runs
                WHERE source_key=? ORDER BY created_at, run_key
                """,
                (source_key,),
            ).fetchall()
        return [
            RunRecord.model_validate_json(row["record_json"])
            for row in rows
        ]

    def bind_delivery(
        self,
        run_key: str,
        binding: DeliveryBinding,
    ) -> None:
        if self.run_record(run_key) is None:
            raise ValueError(f"unknown_run:{run_key}")
        now = int(time.time())
        payload = binding.model_dump_json()
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT binding_json FROM delivery_bindings WHERE run_key=?",
                (run_key,),
            ).fetchone()
            if existing is not None:
                persisted = DeliveryBinding.model_validate_json(
                    existing["binding_json"]
                )
                if persisted != binding:
                    raise ControllerFatalError(
                        f"delivery_binding_conflict:{run_key}"
                    )
                return
            conn.execute(
                """
                INSERT INTO delivery_bindings(
                    run_key, mr_iid, mr_url, creator, created_at,
                    initial_head_sha, claim_note_id, binding_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_key,
                    binding.mr_iid,
                    str(binding.mr_url),
                    binding.creator,
                    binding.created_at.isoformat(),
                    binding.initial_head_sha,
                    binding.claim_note_id,
                    payload,
                    now,
                ),
            )

    def delivery_binding(self, run_key: str) -> DeliveryBinding | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT binding_json FROM delivery_bindings WHERE run_key=?",
                (run_key,),
            ).fetchone()
        return (
            DeliveryBinding.model_validate_json(row["binding_json"])
            if row is not None
            else None
        )

    def enqueue_reconcile(
        self,
        run_key: str,
        *,
        reason: str,
        event_id: int | None = None,
    ) -> str:
        if self.run_record(run_key) is None:
            raise ValueError(f"unknown_run:{run_key}")
        now = int(time.time())
        intent_id = uuid.uuid4().hex
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO reconcile_intents(
                    intent_id, run_key, reason, status, event_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 'pending', ?, ?, ?)
                ON CONFLICT(run_key) DO UPDATE SET
                    reason=excluded.reason,
                    event_id=MAX(
                        COALESCE(reconcile_intents.event_id, 0),
                        COALESCE(excluded.event_id, 0)
                    ),
                    status=CASE
                        WHEN reconcile_intents.status='leased'
                        THEN 'leased' ELSE 'pending'
                    END,
                    revision=reconcile_intents.revision+1,
                    updated_at=excluded.updated_at
                """,
                (intent_id, run_key, reason[:1000], event_id, now, now),
            )
            row = conn.execute(
                "SELECT intent_id FROM reconcile_intents WHERE run_key=?",
                (run_key,),
            ).fetchone()
        return str(row["intent_id"])

    def claim_reconcile(
        self,
        *,
        lease_owner: str,
        lease_seconds: int,
    ) -> dict | None:
        now = int(time.time())
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT * FROM reconcile_intents
                WHERE status='pending'
                   OR (status='leased' AND lease_until < ?)
                ORDER BY created_at, intent_id
                LIMIT 1
                """,
                (now,),
            ).fetchone()
            if row is None:
                return None
            changed = conn.execute(
                """
                UPDATE reconcile_intents
                SET status='leased', lease_owner=?, lease_until=?,
                    leased_revision=revision, attempts=attempts+1,
                    current_step='claimed', updated_at=?
                WHERE intent_id=?
                  AND (
                    status='pending'
                    OR (status='leased' AND lease_until < ?)
                  )
                """,
                (
                    lease_owner,
                    now + lease_seconds,
                    now,
                    row["intent_id"],
                    now,
                ),
            )
            if changed.rowcount != 1:
                return None
            claimed = conn.execute(
                "SELECT * FROM reconcile_intents WHERE intent_id=?",
                (row["intent_id"],),
            ).fetchone()
        return dict(claimed)

    def update_reconcile_step(
        self,
        intent_id: str,
        *,
        lease_owner: str,
        step: str,
        lease_seconds: int,
    ) -> None:
        now = int(time.time())
        with self.connect() as conn:
            changed = conn.execute(
                """
                UPDATE reconcile_intents
                SET current_step=?, lease_until=?, updated_at=?
                WHERE intent_id=? AND status='leased' AND lease_owner=?
                """,
                (
                    step[:200],
                    now + lease_seconds,
                    now,
                    intent_id,
                    lease_owner,
                ),
            )
        if changed.rowcount != 1:
            raise ValueError(f"reconcile_lease_lost:{intent_id}")

    def finish_reconcile(
        self,
        intent_id: str,
        *,
        lease_owner: str,
        error: str | None = None,
    ) -> None:
        now = int(time.time())
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT revision, leased_revision FROM reconcile_intents
                WHERE intent_id=? AND status='leased' AND lease_owner=?
                """,
                (intent_id, lease_owner),
            ).fetchone()
            if row is None:
                raise ValueError(f"reconcile_lease_lost:{intent_id}")
            rerun = int(row["revision"]) != int(row["leased_revision"])
            conn.execute(
                """
                UPDATE reconcile_intents
                SET status=?, lease_owner=NULL, lease_until=NULL,
                    current_step=NULL, last_error=?, updated_at=?
                WHERE intent_id=?
                """,
                (
                    "pending" if rerun or error else "done",
                    error[:1000] if error else None,
                    now,
                    intent_id,
                ),
            )

    def reconcile_intents(self, run_key: str | None = None) -> list[dict]:
        with self.connect() as conn:
            if run_key is None:
                rows = conn.execute(
                    """
                    SELECT * FROM reconcile_intents
                    WHERE status != 'done'
                    ORDER BY created_at, intent_id
                    """
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM reconcile_intents
                    WHERE run_key=? AND status != 'done'
                    ORDER BY created_at, intent_id
                    """,
                    (run_key,),
                ).fetchall()
        return [dict(row) for row in rows]

    def record_validation(
        self,
        *,
        run_key: str,
        card_id: str,
        validator: str,
        validator_version: str,
        input_digest: str,
        result_digest: str,
        passed: bool,
        error_codes: list[str],
        timing: dict[str, int],
    ) -> str:
        validation_id = uuid.uuid4().hex
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO validation_runs(
                    validation_id, run_key, card_id, validator,
                    validator_version, input_digest, result_digest, passed,
                    error_codes, timing_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    validation_id,
                    run_key,
                    card_id,
                    validator,
                    validator_version,
                    input_digest,
                    result_digest,
                    int(passed),
                    json.dumps(error_codes, separators=(",", ":")),
                    json.dumps(timing, sort_keys=True, separators=(",", ":")),
                    int(time.time()),
                ),
            )
        return validation_id

    def validation_runs(
        self,
        run_key: str,
        *,
        card_id: str | None = None,
    ) -> list[dict]:
        with self.connect() as conn:
            if card_id is None:
                rows = conn.execute(
                    """
                    SELECT * FROM validation_runs
                    WHERE run_key=? ORDER BY created_at, validation_id
                    """,
                    (run_key,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM validation_runs
                    WHERE run_key=? AND card_id=?
                    ORDER BY created_at, validation_id
                    """,
                    (run_key, card_id),
                ).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            item["passed"] = bool(item["passed"])
            item["error_codes"] = json.loads(item["error_codes"])
            item["timing"] = json.loads(item.pop("timing_json"))
            results.append(item)
        return results

    def record_attempt_metric(
        self,
        run_id: str,
        *,
        metric: str,
        duration_ms: int,
        summary: str | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO attempt_metrics(
                    run_id, metric, duration_ms, summary, recorded_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(run_id, metric) DO UPDATE SET
                    duration_ms=excluded.duration_ms,
                    summary=excluded.summary,
                    recorded_at=excluded.recorded_at
                """,
                (
                    run_id,
                    metric,
                    duration_ms,
                    summary[:500] if summary else None,
                    int(time.time()),
                ),
            )

    @staticmethod
    def _supervisor_state(raw_summary: str | None) -> dict:
        if raw_summary:
            try:
                decoded = json.loads(raw_summary)
                if isinstance(decoded, dict):
                    return decoded
            except json.JSONDecodeError:
                pass
        return {"state": "unknown"}

    @classmethod
    def _decorate_attempt_runtime(
        cls,
        item: dict,
        metrics: list[dict],
        *,
        now: int | None = None,
    ) -> dict:
        observed_at = int(time.time()) if now is None else now
        heartbeat = item.get("last_heartbeat_at") or item.get("started_at")
        progress = item.get("last_progress_at") or item.get("started_at")
        item["heartbeat_age_seconds"] = (
            max(0, observed_at - int(heartbeat))
            if heartbeat is not None
            else None
        )
        item["progress_age_seconds"] = (
            max(0, observed_at - int(progress))
            if progress is not None
            else None
        )
        supervisor = next(
            (
                metric.get("summary")
                for metric in metrics
                if metric.get("metric") == "supervisor_probe"
            ),
            None,
        )
        item["supervisor_state"] = cls._supervisor_state(supervisor)
        return item

    def attempts_for_run(self, run_key: str) -> list[dict]:
        with self.connect() as conn:
            attempts = conn.execute(
                """
                SELECT * FROM card_attempts
                WHERE run_key=? ORDER BY created_at, run_id
                """,
                (run_key,),
            ).fetchall()
            metrics = conn.execute(
                """
                SELECT metrics.* FROM attempt_metrics AS metrics
                JOIN card_attempts AS attempts
                  ON attempts.run_id=metrics.run_id
                WHERE attempts.run_key=?
                ORDER BY metrics.run_id, metrics.metric
                """,
                (run_key,),
            ).fetchall()
        by_run_id: dict[str, list[dict]] = {}
        for row in metrics:
            item = dict(row)
            by_run_id.setdefault(str(item.pop("run_id")), []).append(item)
        results = []
        now = int(time.time())
        for row in attempts:
            item = dict(row)
            item["metrics"] = by_run_id.get(str(item["run_id"]), [])
            results.append(
                self._decorate_attempt_runtime(
                    item,
                    item["metrics"],
                    now=now,
                )
            )
        return results

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
        worker_pid: int | None = None,
        lease_seconds: int,
        run_id: str | None = None,
        runtime_metrics: dict[str, float] | None = None,
        progress_summary: dict | None = None,
    ) -> None:
        started = kind in {"claimed", "started", "worker_started"}
        heartbeat = kind in {"heartbeat", "worker_heartbeat"}
        progress = started or kind in {
            "progress",
            "completed",
            "blocked",
            "reclaimed",
            "crashed",
            "rate_limited",
            "timed_out",
            "gave_up",
            "spawn_auto_blocked",
        }
        terminal = kind in {
            "completed",
            "blocked",
            "reclaimed",
            "crashed",
            "rate_limited",
            "timed_out",
            "gave_up",
            "spawn_auto_blocked",
        }
        with self.connect() as conn:
            previous = conn.execute(
                "SELECT * FROM card_runtime WHERE board=? AND card_id=?",
                (board, card_id),
            ).fetchone()
            new_session = bool(
                started
                and previous is not None
                and worker_session_id
                and worker_session_id != previous["worker_session_id"]
            )
            preserve_finished_runtime = bool(
                previous is not None
                and previous["finished_at"] is not None
                and not terminal
                and not new_session
            )
            if preserve_finished_runtime and kind != "worker_exited":
                # Supervisor-confirmed and formal terminal states are
                # irreversible for an attempt. A heartbeat/progress event that
                # was queued before termination may be polled afterwards, but
                # it must not resurrect the reclaimed runtime.
                return
            if run_id:
                managed = conn.execute(
                    """
                    SELECT run_key FROM managed_cards
                    WHERE board=? AND card_id=?
                    """,
                    (board, card_id),
                ).fetchone()
                if managed is None:
                    raise ValueError(f"unknown_managed_card:{board}:{card_id}")
                attempt_row = conn.execute(
                    "SELECT * FROM card_attempts WHERE run_id=?",
                    (run_id,),
                ).fetchone()
                if attempt_row is not None and (
                    attempt_row["board"] != board
                    or attempt_row["card_id"] != card_id
                    or attempt_row["run_key"] != managed["run_key"]
                ):
                    raise ControllerFatalError(
                        f"attempt_run_id_conflict:{run_id}"
                    )
                if attempt_row is None:
                    next_attempt = int(
                        conn.execute(
                            """
                            SELECT COALESCE(MAX(attempt), 0) + 1
                            FROM card_attempts
                            WHERE board=? AND card_id=?
                            """,
                            (board, card_id),
                        ).fetchone()[0]
                    )
                    conn.execute(
                        """
                        INSERT INTO card_attempts(
                            run_id, board, card_id, run_key, attempt,
                            worker_pid, worker_session_id, started_at,
                            blocked_at, completion_at, exited_at,
                            last_heartbeat_at, last_progress_at, status,
                            terminal_reason, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            run_id,
                            board,
                            card_id,
                            managed["run_key"],
                            next_attempt,
                            worker_pid,
                            worker_session_id,
                            created_at if started else None,
                            created_at if kind == "blocked" else None,
                            created_at if kind == "completed" else None,
                            (
                                created_at
                                if kind in {"worker_exited", "reclaimed"}
                                else None
                            ),
                            created_at if heartbeat else None,
                            created_at if progress else None,
                            (
                                "exited"
                                if kind == "worker_exited"
                                else "reclaimed"
                                if kind == "reclaimed"
                                else "blocked"
                                if kind == "blocked"
                                else "completion_recorded"
                                if kind == "completed"
                                else kind
                                if terminal
                                else "running"
                            ),
                            kind if terminal else None,
                            created_at,
                            int(time.time()),
                        ),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE card_attempts
                        SET worker_pid=COALESCE(?, worker_pid),
                            worker_session_id=COALESCE(?, worker_session_id),
                            started_at=COALESCE(
                                started_at, CASE WHEN ? THEN ? END
                            ),
                            blocked_at=COALESCE(
                                blocked_at, CASE WHEN ?='blocked' THEN ? END
                            ),
                            completion_at=COALESCE(
                                completion_at, CASE WHEN ?='completed' THEN ? END
                            ),
                            exited_at=COALESCE(
                                exited_at,
                                CASE WHEN ? IN ('worker_exited','reclaimed')
                                     THEN ? END
                            ),
                            last_heartbeat_at=CASE
                                WHEN ? THEN ? ELSE last_heartbeat_at
                            END,
                            last_progress_at=CASE
                                WHEN ? THEN ? ELSE last_progress_at
                            END,
                            status=CASE
                                WHEN ?='worker_exited' AND status!='reclaimed'
                                    THEN 'exited'
                                WHEN ?='reclaimed' THEN 'reclaimed'
                                WHEN ?='blocked' THEN 'blocked'
                                WHEN ?='completed' THEN 'completion_recorded'
                                WHEN ? THEN ?
                                ELSE status
                            END,
                            terminal_reason=CASE
                                WHEN ? THEN ? ELSE terminal_reason
                            END,
                            updated_at=?
                        WHERE run_id=?
                        """,
                        (
                            worker_pid,
                            worker_session_id,
                            int(started),
                            created_at,
                            kind,
                            created_at,
                            kind,
                            created_at,
                            kind,
                            created_at,
                            int(heartbeat),
                            created_at,
                            int(progress),
                            created_at,
                            kind,
                            kind,
                            kind,
                            kind,
                            int(terminal),
                            kind,
                            int(terminal),
                            kind,
                            int(time.time()),
                            run_id,
                        ),
                    )
                if started:
                    reclaimed = conn.execute(
                        """
                        SELECT run_id FROM card_attempts
                        WHERE board=? AND card_id=? AND status='reclaimed'
                          AND run_id != ?
                        ORDER BY attempt DESC LIMIT 1
                        """,
                        (board, card_id, run_id),
                    ).fetchone()
                    if reclaimed is not None:
                        conn.execute(
                            """
                            UPDATE card_attempts
                            SET redispatched_from_run_id=COALESCE(
                                    redispatched_from_run_id, ?
                                ),
                                updated_at=?
                            WHERE run_id=?
                            """,
                            (
                                str(reclaimed["run_id"]),
                                int(time.time()),
                                run_id,
                            ),
                        )
                        conn.execute(
                            """
                            INSERT INTO attempt_metrics(
                                run_id, metric, duration_ms, summary,
                                recorded_at
                            ) VALUES (?, 'redispatched_from_run_id', 0, ?, ?)
                            ON CONFLICT(run_id, metric) DO UPDATE SET
                                summary=excluded.summary,
                                recorded_at=excluded.recorded_at
                            """,
                            (
                                run_id,
                                str(reclaimed["run_id"]),
                                int(time.time()),
                            ),
                        )
                if kind == "progress" and runtime_metrics:
                    safe_summary = None
                    if isinstance(progress_summary, dict):
                        categories = progress_summary.get("tool_categories")
                        safe_categories = {
                            str(key)[:32]: int(value)
                            for key, value in (
                                categories.items()
                                if isinstance(categories, dict)
                                else ()
                            )
                            if isinstance(value, int) and value >= 0
                        }
                        safe_summary = json.dumps(
                            {
                                "tool_categories": safe_categories,
                                "tool_count": max(
                                    0,
                                    int(progress_summary.get("tool_count", 0)),
                                ),
                                "elapsed_seconds": max(
                                    0,
                                    int(
                                        progress_summary.get(
                                            "elapsed_seconds", 0
                                        )
                                    ),
                                ),
                            },
                            ensure_ascii=True,
                            separators=(",", ":"),
                        )
                    for metric in (
                        "model_wait",
                        "tool_execution",
                        "delegation_wait",
                        "retry_wait",
                    ):
                        value = runtime_metrics.get(metric)
                        if not isinstance(value, (int, float)) or value < 0:
                            continue
                        conn.execute(
                            """
                            INSERT INTO attempt_metrics(
                                run_id, metric, duration_ms, summary,
                                recorded_at
                            ) VALUES (?, ?, ?, ?, ?)
                            ON CONFLICT(run_id, metric) DO UPDATE SET
                                duration_ms=excluded.duration_ms,
                                summary=excluded.summary,
                                recorded_at=excluded.recorded_at
                            """,
                            (
                                run_id,
                                metric,
                                max(0, int(float(value) * 1000)),
                                safe_summary,
                                created_at,
                            ),
                        )
            if preserve_finished_runtime:
                # Preserve the formal card terminal state while still allowing
                # a real waitpid event above to close the attempt timeline.
                return
            if (
                previous is not None
                and previous["worker_session_id"]
                and worker_session_id
                and worker_session_id != previous["worker_session_id"]
                and not started
            ):
                # A late event from an older worker attempt must not overwrite
                # the lease or terminal facts of the current attempt.
                return
            if (
                started
                and previous is not None
                and previous["worker_started_at"] is not None
                and worker_session_id
                and worker_session_id != previous["worker_session_id"]
                and created_at < int(previous["worker_started_at"])
            ):
                # Event polling is ordered by event id, but imported/replayed
                # history can still contain an older start after a new session
                # is already current. It must not resurrect the old attempt.
                return
            new_attempt = bool(
                started
                and previous is not None
                and previous["worker_started_at"] is not None
                and worker_session_id
                and worker_session_id != previous["worker_session_id"]
            )
            conn.execute(
                """
                INSERT INTO card_runtime(
                    board, card_id, worker_pid, worker_started_at,
                    worker_session_id,
                    last_heartbeat_at, last_progress_event_at, deadline_at,
                    last_event_kind, attempt, redispatch_count,
                    attempt_status, finished_at, terminal_reason, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(board, card_id) DO UPDATE SET
                    worker_pid=COALESCE(
                        excluded.worker_pid,
                        card_runtime.worker_pid
                    ),
                    worker_started_at=CASE
                        WHEN ? THEN excluded.worker_started_at
                        ELSE COALESCE(
                            card_runtime.worker_started_at,
                            excluded.worker_started_at
                        )
                    END,
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
                    attempt=card_runtime.attempt + ?,
                    redispatch_count=card_runtime.redispatch_count + ?,
                    attempt_status=excluded.attempt_status,
                    finished_at=excluded.finished_at,
                    terminal_reason=excluded.terminal_reason,
                    updated_at=excluded.updated_at
                """,
                (
                    board,
                    card_id,
                    worker_pid,
                    created_at if started else None,
                    worker_session_id,
                    created_at if heartbeat else None,
                    created_at if progress else None,
                    created_at + lease_seconds if progress else None,
                    kind,
                    1,
                    0,
                    (
                        "finished"
                        if terminal
                        else "running"
                        if progress or heartbeat
                        else "created"
                    ),
                    created_at if terminal else None,
                    kind if terminal else None,
                    int(time.time()),
                    int(new_attempt),
                    int(new_attempt),
                    0,
                ),
            )

    def register_card_attempt(
        self,
        *,
        board: str,
        card_id: str,
        profile: str,
        dispatch_key: str,
        worktree: str,
        branch: str,
    ) -> None:
        now = int(time.time())
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO card_runtime(
                    board, card_id, profile, dispatch_key, worktree, branch,
                    attempt_status, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'created', ?)
                ON CONFLICT(board, card_id) DO UPDATE SET
                    profile=excluded.profile,
                    dispatch_key=excluded.dispatch_key,
                    worktree=excluded.worktree,
                    branch=excluded.branch,
                    updated_at=excluded.updated_at
                """,
                (
                    board,
                    card_id,
                    profile,
                    dispatch_key,
                    worktree,
                    branch,
                    now,
                ),
            )

    def record_attempt_completion(
        self,
        *,
        board: str,
        card_id: str,
        accepted: bool,
        mr_iid: int | None,
        head_sha: str | None,
        reason: str | None = None,
    ) -> None:
        now = int(time.time())
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE card_runtime
                SET mr_iid=COALESCE(?, mr_iid),
                    head_sha=COALESCE(?, head_sha),
                    attempt_status=?,
                    finished_at=COALESCE(finished_at, ?),
                    terminal_reason=COALESCE(?, terminal_reason),
                    updated_at=?
                WHERE board=? AND card_id=?
                """,
                (
                    mr_iid,
                    head_sha,
                    "completed_accepted" if accepted else "completed_rejected",
                    now,
                    reason[:1000] if reason else None,
                    now,
                    board,
                    card_id,
                ),
            )

    def card_runtime(self, board: str, card_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM card_runtime WHERE board=? AND card_id=?",
                (board, card_id),
            ).fetchone()
            return dict(row) if row else None

    def current_attempt(self, board: str, card_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM card_attempts
                WHERE board=? AND card_id=?
                ORDER BY attempt DESC LIMIT 1
                """,
                (board, card_id),
            ).fetchone()
        return dict(row) if row else None

    def record_supervisor_observation(
        self,
        run_id: str,
        *,
        state: str,
        duration_ms: int,
        observed_at: int,
        error_code: str | None = None,
    ) -> None:
        summary = json.dumps(
            {
                "state": state,
                "observed_at": observed_at,
                "error_code": error_code,
            },
            ensure_ascii=True,
            separators=(",", ":"),
        )
        self.record_attempt_metric(
            run_id,
            metric="supervisor_probe",
            duration_ms=duration_ms,
            summary=summary,
        )

    def update_worker_watchdog(
        self,
        *,
        board: str,
        card_id: str,
        attempt_status: str,
        lease_seconds: int | None = None,
        reason: str | None = None,
        increment_redispatch: bool = False,
    ) -> None:
        now = int(time.time())
        deadline = now + lease_seconds if lease_seconds is not None else None
        with self.connect() as conn:
            redispatch_count: int | None = None
            if increment_redispatch:
                current = conn.execute(
                    """
                    SELECT run_id FROM card_attempts
                    WHERE board=? AND card_id=?
                    ORDER BY attempt DESC LIMIT 1
                    """,
                    (board, card_id),
                ).fetchone()
                if current is not None:
                    conn.execute(
                        """
                        UPDATE card_attempts
                        SET recovery_reason=COALESCE(recovery_reason, ?),
                            updated_at=?
                        WHERE run_id=?
                        """,
                        (
                            (reason or "redispatch_requested")[:1000],
                            now,
                            str(current["run_id"]),
                        ),
                    )
                    redispatch_count = int(
                        conn.execute(
                            """
                            SELECT COUNT(*) FROM card_attempts
                            WHERE board=? AND card_id=?
                              AND recovery_reason IS NOT NULL
                            """,
                            (board, card_id),
                        ).fetchone()[0]
                    )
                else:
                    # Compatibility for synthetic/tests events without a
                    # durable Hermes run id. Production attempts always take
                    # the authoritative card_attempts path above.
                    runtime_row = conn.execute(
                        """
                        SELECT redispatch_count FROM card_runtime
                        WHERE board=? AND card_id=?
                        """,
                        (board, card_id),
                    ).fetchone()
                    if runtime_row is not None:
                        redispatch_count = int(runtime_row[0]) + 1
            changed = conn.execute(
                """
                UPDATE card_runtime
                SET attempt_status=?,
                    deadline_at=COALESCE(?, deadline_at),
                    redispatch_count=COALESCE(?, redispatch_count),
                    terminal_reason=COALESCE(?, terminal_reason),
                    updated_at=?
                WHERE board=? AND card_id=?
                """,
                (
                    attempt_status,
                    deadline,
                    redispatch_count,
                    reason[:1000] if reason else None,
                    now,
                    board,
                    card_id,
                ),
            )
        if changed.rowcount != 1:
            raise ValueError(f"unknown_card_runtime:{board}:{card_id}")

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
            supervisor_metrics = {
                str(row["run_id"]): str(row["summary"] or "")
                for row in conn.execute(
                    """
                    SELECT metrics.run_id, metrics.summary
                    FROM attempt_metrics AS metrics
                    JOIN card_attempts AS attempts
                      ON attempts.run_id=metrics.run_id
                    WHERE attempts.run_key=?
                      AND metrics.metric='supervisor_probe'
                    """,
                    (run_key,),
                )
            }
        now = int(time.time())
        results = []
        for row in rows:
            item = dict(row)
            heartbeat = item.get("last_heartbeat_at") or item.get(
                "worker_started_at"
            )
            progress = item.get("last_progress_event_at") or item.get(
                "worker_started_at"
            )
            item["heartbeat_age_seconds"] = (
                max(0, now - int(heartbeat))
                if heartbeat is not None
                else None
            )
            item["progress_age_seconds"] = (
                max(0, now - int(progress))
                if progress is not None
                else None
            )
            session = str(item.get("worker_session_id") or "")
            attempt_run_id = (
                f"{item['board']}:{session.split(':', 1)[1]}"
                if session.startswith("kanban-run:")
                else None
            )
            item["supervisor_state"] = self._supervisor_state(
                supervisor_metrics.get(str(attempt_run_id or ""))
            )
            results.append(item)
        return results

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

    def transition_run(
        self,
        run_key: str,
        *,
        expected_states: set[str],
        new_state: str,
        reason: str,
        expected_version: int | None = None,
        next_retry_at: int | None = None,
        compliance: str | None = None,
        completion_source: str | None = None,
        checked_head: str | None = None,
        merge_commit_sha: str | None = None,
    ) -> dict:
        if new_state not in RUN_STATES:
            raise ValueError(f"invalid_run_state:{new_state}")
        now = int(time.time())
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM run_control WHERE run_key=?",
                (run_key,),
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown_run:{run_key}")
            if str(row["state"]) not in expected_states:
                raise ValueError(
                    f"invalid_run_transition:{row['state']}->{new_state}"
                )
            version = int(row["state_version"])
            if expected_version is not None and version != expected_version:
                raise ValueError(
                    f"stale_run_version:{expected_version}!={version}"
                )
            terminal_at = now if new_state in TERMINAL_RUN_STATES else None
            updated = conn.execute(
                """
                UPDATE run_control
                SET state=?, state_version=state_version+1,
                    terminal_at=?, last_transition_reason=?,
                    next_retry_at=?, compliance=COALESCE(?, compliance),
                    completion_source=COALESCE(?, completion_source),
                    checked_head=COALESCE(?, checked_head),
                    merge_commit_sha=COALESCE(?, merge_commit_sha),
                    updated_at=?
                WHERE run_key=? AND state_version=?
                """,
                (
                    new_state,
                    terminal_at,
                    reason[:1000],
                    next_retry_at,
                    compliance,
                    completion_source,
                    checked_head,
                    merge_commit_sha,
                    now,
                    run_key,
                    version,
                ),
            )
            if updated.rowcount != 1:
                raise ValueError(f"stale_run_version:{run_key}")
            result = conn.execute(
                "SELECT * FROM run_control WHERE run_key=?",
                (run_key,),
            ).fetchone()
        return dict(result)

    def mark_completed(
        self,
        run_key: str,
        *,
        external: bool,
        compliance: str,
        checked_head: str,
        merge_commit_sha: str | None,
        reason: str,
    ) -> dict:
        if (
            len(checked_head) != 40
            or any(character not in "0123456789abcdef" for character in checked_head)
        ):
            raise ValueError("completed run requires a valid checked head SHA")
        if merge_commit_sha is not None and (
            len(merge_commit_sha) != 40
            or any(
                character not in "0123456789abcdef"
                for character in merge_commit_sha
            )
        ):
            raise ValueError("completed run has an invalid merge commit SHA")
        current = self.run_control(run_key)
        if current is None:
            raise ValueError(f"unknown_run:{run_key}")
        if current["state"] in TERMINAL_RUN_STATES:
            return current
        return self.transition_run(
            run_key,
            expected_states={
                "active",
                "transition_pending",
                "human_blocked",
                "retry_wait",
                "dependency_degraded",
                "merge_wait",
                "exception",
            },
            new_state="completed",
            reason=reason,
            compliance=compliance,
            completion_source="external" if external else "controller",
            checked_head=checked_head,
            merge_commit_sha=merge_commit_sha,
        )

    def mark_flow_completed(
        self,
        run_key: str,
        *,
        state: str,
        checked_head: str,
        reason: str,
    ) -> dict:
        allowed = {
            "completed_ready": "verified",
            "completed_with_findings": "unverified",
            "completed_test_failed": "unverified",
        }
        if state not in allowed:
            raise ValueError(f"invalid flow terminal state {state}")
        if len(checked_head) != 40 or any(
            character not in "0123456789abcdef" for character in checked_head
        ):
            raise ValueError("flow completion requires a valid checked head SHA")
        current = self.run_control(run_key)
        if current is None:
            raise ValueError(f"unknown_run:{run_key}")
        if current["state"] in TERMINAL_RUN_STATES:
            return current
        return self.transition_run(
            run_key,
            expected_states={
                "active",
                "transition_pending",
                "human_blocked",
                "retry_wait",
                "dependency_degraded",
                "merge_wait",
            },
            new_state=state,
            reason=reason,
            compliance=allowed[state],
            completion_source="controller",
            checked_head=checked_head,
        )

    def set_run_exception(self, run_key: str, reason: str) -> dict:
        current = self.run_control(run_key)
        if current is None:
            raise ValueError(f"unknown_run:{run_key}")
        if current["state"] == "exception":
            return current
        return self.transition_run(
            run_key,
            expected_states={
                "active",
                "transition_pending",
                "human_blocked",
                "retry_wait",
                "dependency_degraded",
                "merge_wait",
            },
            new_state="exception",
            reason=reason,
        )

    def active_reconcile_run_keys(self, now: int | None = None) -> list[str]:
        current_time = int(time.time()) if now is None else now
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT run_key FROM run_control
                WHERE state IN (
                    'active', 'transition_pending', 'retry_wait',
                    'dependency_degraded', 'merge_wait'
                )
                  AND (next_retry_at IS NULL OR next_retry_at <= ?)
                ORDER BY updated_at, run_key
                """,
                (current_time,),
            ).fetchall()
        return [str(row["run_key"]) for row in rows]

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
            if control["state"] not in {
                "active",
                "dependency_degraded",
                "merge_wait",
                "exception",
            }:
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
                    abort_reason=?, abort_requested_at=?,
                    state_version=state_version+1,
                    last_transition_reason='human_abort_confirmed',
                    terminal_at=NULL, next_retry_at=NULL,
                    updated_at=?
                WHERE run_key=? AND state IN (
                    'active', 'dependency_degraded', 'merge_wait', 'exception'
                )
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
                UPDATE run_control SET state='aborting',
                    state_version=state_version+1,
                    last_transition_reason='abort_execution_started',
                    terminal_at=NULL, next_retry_at=NULL,
                    updated_at=?
                WHERE run_key=? AND state='abort_requested'
                """,
                (now, run_key),
            )

    def finish_abort(
        self,
        run_key: str,
        state: str = "aborted",
        *,
        checked_head: str | None = None,
        merge_commit_sha: str | None = None,
    ) -> None:
        if state not in {"aborted", "completed_before_abort"}:
            raise ValueError(f"invalid abort terminal state {state}")
        if state == "completed_before_abort" and (
            checked_head is None
            or len(checked_head) != 40
            or any(
                character not in "0123456789abcdef"
                for character in checked_head
            )
        ):
            raise ValueError(
                "completed_before_abort requires a valid checked head SHA"
            )
        if merge_commit_sha is not None and (
            len(merge_commit_sha) != 40
            or any(
                character not in "0123456789abcdef"
                for character in merge_commit_sha
            )
        ):
            raise ValueError("abort terminal has an invalid merge commit SHA")
        now = int(time.time())
        with self.connect() as conn:
            changed = conn.execute(
                """
                UPDATE run_control SET state=?, aborted_at=?, terminal_at=?,
                    state_version=state_version+1,
                    last_transition_reason=?,
                    checked_head=COALESCE(?, checked_head),
                    merge_commit_sha=COALESCE(?, merge_commit_sha),
                    next_retry_at=NULL,
                    updated_at=?
                WHERE run_key=? AND state IN ('abort_requested', 'aborting')
                """,
                (
                    state,
                    now,
                    now,
                    "merged_before_abort" if state == "completed_before_abort"
                    else "human_abort_completed",
                    checked_head,
                    merge_commit_sha,
                    now,
                    run_key,
                ),
            )
            if changed.rowcount == 1:
                conn.execute(
                    "DELETE FROM merge_wait WHERE run_key=?",
                    (run_key,),
                )

    def active_abort_run_keys(self, now: int | None = None) -> list[str]:
        current_time = int(time.time()) if now is None else now
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT run_key FROM run_control
                WHERE state IN ('abort_requested', 'aborting')
                  AND (next_retry_at IS NULL OR next_retry_at <= ?)
                ORDER BY updated_at
                """,
                (current_time,),
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
        endpoint: str | None = None,
        retry_after_seconds: int | None = None,
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
            if retry_after_seconds is not None:
                delay = max(delay, retry_after_seconds)
            next_retry_at = now + max(1, int(delay))
            started_at = (
                int(previous["started_at"]) if continuing else now
            )
            peak_failures = max(
                int(previous["peak_failures"]) if continuing else 0,
                failures,
            )
            conn.execute(
                """
                INSERT INTO dependency_outages(
                    dependency, outage_id, status, error_class, error_summary,
                    failures, peak_failures, started_at, last_failure_at,
                    last_success_at, endpoint, circuit_state, next_retry_at,
                    recovered_at, updated_at
                ) VALUES (?, ?, 'open', ?, ?, ?, ?, ?, ?, NULL, ?, 'open',
                          ?, NULL, ?)
                ON CONFLICT(dependency) DO UPDATE SET
                    outage_id=excluded.outage_id,
                    status='open',
                    error_class=excluded.error_class,
                    error_summary=excluded.error_summary,
                    failures=excluded.failures,
                    peak_failures=MAX(
                        dependency_outages.peak_failures,
                        excluded.peak_failures
                    ),
                    started_at=excluded.started_at,
                    last_failure_at=excluded.last_failure_at,
                    endpoint=excluded.endpoint,
                    circuit_state='open',
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
                    peak_failures,
                    started_at,
                    now,
                    endpoint,
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
                SET status='recovered', recovered_at=?, last_success_at=?,
                    failures=0, circuit_state='closed', updated_at=?
                WHERE dependency=?
                """,
                (now, now, now, dependency),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO dependency_outage_history(
                    dependency, outage_id, error_class, error_summary,
                    peak_failures, started_at, recovered_at, duration_seconds,
                    endpoint
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    dependency,
                    previous["outage_id"],
                    previous["error_class"],
                    previous["error_summary"],
                    previous["peak_failures"],
                    previous["started_at"],
                    now,
                    max(0, now - int(previous["started_at"])),
                    previous["endpoint"],
                ),
            )
            result = dict(previous)
            result["recovered_at"] = now
            result["last_success_at"] = now
        return result

    def mark_dependency_half_open(self, dependency: str) -> bool:
        now = int(time.time())
        with self.connect() as conn:
            changed = conn.execute(
                """
                UPDATE dependency_outages
                SET circuit_state='half_open', updated_at=?
                WHERE dependency=? AND status='open'
                  AND circuit_state='open' AND next_retry_at<=?
                """,
                (now, dependency, now),
            )
        return changed.rowcount == 1

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

    def dependency_outage_history(self, limit: int = 20) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM dependency_outage_history
                ORDER BY recovered_at DESC LIMIT ?
                """,
                (limit,),
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
        blocker_url: str | None = None,
        blocker_owner: str | None = None,
        blocker_updated_at: str | None = None,
        retry_seconds: int,
    ) -> dict:
        now = int(time.time())
        with self.connect() as conn:
            previous = conn.execute(
                "SELECT * FROM merge_wait WHERE run_key=?",
                (run_key,),
            ).fetchone()
            same_identity = (
                previous is not None
                and previous["head_sha"] == head_sha
                and previous["blocker_kind"] == blocker_kind
            )
            same_evidence = (
                same_identity
                and previous["blocker"] == blocker[:1000]
                and previous["blocker_url"] == blocker_url
                and previous["blocker_owner"] == blocker_owner
                and previous["blocker_updated_at"] == blocker_updated_at
            )
            first_seen_at = (
                int(previous["first_seen_at"]) if same_identity else now
            )
            conn.execute(
                """
                INSERT INTO merge_wait(
                    run_key, mr_iid, head_sha, blocker_kind, blocker,
                    blocker_url, blocker_owner, blocker_updated_at,
                    first_seen_at, last_checked_at, next_retry_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_key) DO UPDATE SET
                    mr_iid=excluded.mr_iid,
                    head_sha=excluded.head_sha,
                    blocker_kind=excluded.blocker_kind,
                    blocker=excluded.blocker,
                    blocker_url=excluded.blocker_url,
                    blocker_owner=excluded.blocker_owner,
                    blocker_updated_at=excluded.blocker_updated_at,
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
                    blocker_url,
                    blocker_owner,
                    blocker_updated_at,
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
        result["changed"] = not same_evidence
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

    def request(self, key: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM requests WHERE request_key=?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        if result["response"]:
            result["response"] = json.loads(result["response"])
        return result

    def finish_request(self, key: str, response: dict) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE requests SET status='done', response=?, error=NULL, updated_at=?
                WHERE request_key=?
                """,
                (json.dumps(response, ensure_ascii=False), int(time.time()), key),
            )

    def bind_request_run(self, key: str, run_key: str) -> None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT run_key, status FROM requests WHERE request_key=?",
                (key,),
            ).fetchone()
            if row is None or row["status"] != "running":
                raise ValueError(f"request_not_running:{key}")
            if row["run_key"] not in {None, run_key}:
                raise ValueError(f"request_run_key_changed:{key}")
            conn.execute(
                """
                UPDATE requests SET run_key=?, updated_at=?
                WHERE request_key=?
                """,
                (run_key, int(time.time()), key),
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
                SELECT request_key, kind, run_key, payload FROM requests
                WHERE status='running' ORDER BY created_at
                """
            ).fetchall()
        return [
            {
                "request_key": row["request_key"],
                "kind": row["kind"],
                "run_key": row["run_key"],
                "payload": json.loads(row["payload"]),
            }
            for row in rows
        ]

    def operation_result(
        self,
        key: str,
        kind: str,
        payload: dict,
        *,
        expected_state_version: int | None = None,
        expected_head_sha: str | None = None,
    ) -> dict | None:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        now = int(time.time())
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM operations WHERE operation_key=?",
                (key,),
            ).fetchone()
            if row:
                if row["kind"] != kind or row["payload"] != encoded:
                    raise ValueError(f"operation key {key} was reused")
                if row["expected_head_sha"] != expected_head_sha:
                    raise ValueError(f"operation expectation changed for {key}")
                stored_version = row["expected_state_version"]
                version_advanced = (
                    stored_version is not None
                    and expected_state_version is not None
                    and expected_state_version > int(stored_version)
                )
                if (
                    stored_version != expected_state_version
                    and not version_advanced
                ):
                    raise ValueError(f"operation expectation changed for {key}")
                if row["status"] == "done" and row["result"]:
                    return json.loads(row["result"])
                uncertain_at = (
                    now
                    if row["status"] in {"executing", "running", "uncertain"}
                    else row["uncertain_at"]
                )
                conn.execute(
                    """
                    UPDATE operations SET status='executing',
                        uncertain_at=?, attempts=attempts+1,
                        expected_state_version=?,
                        updated_at=? WHERE operation_key=?
                    """,
                    (
                        uncertain_at,
                        expected_state_version,
                        now,
                        key,
                    ),
                )
                return None
            conn.execute(
                """
                INSERT INTO operations(
                    operation_key, kind, payload, status,
                    expected_state_version, expected_head_sha,
                    attempts, created_at, updated_at
                ) VALUES (?, ?, ?, 'executing', ?, ?, 1, ?, ?)
                """,
                (
                    key,
                    kind,
                    encoded,
                    expected_state_version,
                    expected_head_sha,
                    now,
                    now,
                ),
            )
        return None

    def finish_operation(self, key: str, result: dict) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE operations SET status='done', result=?, error=NULL,
                    uncertain_at=NULL, updated_at=?
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

    def block_operation(self, key: str, error: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE operations SET status='blocked', error=?, updated_at=?
                WHERE operation_key=?
                """,
                (error[:2000], int(time.time()), key),
            )

    def supersede_operation(self, key: str, error: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE operations SET status='superseded', error=?, updated_at=?
                WHERE operation_key=?
                """,
                (error[:2000], int(time.time()), key),
            )

    def mark_operation_uncertain(self, key: str, error: str) -> None:
        now = int(time.time())
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE operations SET status='uncertain', error=?,
                    uncertain_at=COALESCE(uncertain_at, ?), updated_at=?
                WHERE operation_key=?
                """,
                (error[:2000], now, now, key),
            )

    def operation_record(self, key: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM operations WHERE operation_key=?",
                (key,),
            ).fetchone()
        return dict(row) if row else None

    def enqueue(self, key: str, run_key: str, event: str, payload: dict) -> None:
        now = int(time.time())
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO outbox(
                    outbox_key, run_key, event, payload, next_attempt_at,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    key,
                    run_key,
                    event,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    now,
                    now,
                    now,
                ),
            )

    def pending_outbox(
        self,
        limit: int = 20,
        *,
        now: int | None = None,
    ) -> list[dict]:
        due = int(time.time()) if now is None else now
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM outbox
                WHERE status IN ('pending', 'retrying')
                  AND next_attempt_at <= ?
                ORDER BY next_attempt_at, created_at LIMIT ?
                """,
                (due, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def finish_outbox(self, key: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE outbox SET status='done', attempts=attempts+1,
                    last_error=NULL, error_class=NULL, dead_at=NULL,
                    updated_at=? WHERE outbox_key=?
                """,
                (int(time.time()), key),
            )

    def fail_outbox(
        self,
        key: str,
        error: str,
        *,
        initial_backoff_seconds: int = 5,
        maximum_backoff_seconds: int = 300,
        error_class: str = "dependency_transient",
        permanent: bool = False,
    ) -> None:
        now = int(time.time())
        with self.connect() as conn:
            row = conn.execute(
                "SELECT attempts FROM outbox WHERE outbox_key=?",
                (key,),
            ).fetchone()
            attempts = int(row["attempts"]) + 1 if row else 1
            delay = min(
                maximum_backoff_seconds,
                initial_backoff_seconds * (2 ** max(0, attempts - 1)),
            )
            conn.execute(
                """
                UPDATE outbox SET status=?, attempts=attempts+1,
                    last_error=?, error_class=?, next_attempt_at=?,
                    dead_at=?, updated_at=? WHERE outbox_key=?
                """,
                (
                    "dead" if permanent else "retrying",
                    error[:2000],
                    error_class,
                    now + max(1, delay),
                    now if permanent else None,
                    now,
                    key,
                ),
            )

    def retry_dead_outbox(self, key: str) -> None:
        now = int(time.time())
        with self.connect() as conn:
            changed = conn.execute(
                """
                UPDATE outbox SET status='pending', next_attempt_at=?,
                    dead_at=NULL, updated_at=? WHERE outbox_key=? AND status='dead'
                """,
                (now, now, key),
            )
        if changed.rowcount != 1:
            raise ValueError(f"outbox_not_dead:{key}")

    def begin_boot(self, boot_id: str) -> None:
        now = int(time.time())
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE controller_boots
                SET stopped_at=?, exit_reason='unclean_restart_detected', fatal=1
                WHERE stopped_at IS NULL
                """,
                (now,),
            )
            conn.execute(
                """
                INSERT INTO controller_boots(boot_id, started_at)
                VALUES (?, ?)
                """,
                (boot_id, now),
            )

    def record_profile_preflight(self, result: dict, *, deep: bool) -> None:
        now = int(time.time())
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO profile_preflight(
                    profile, role, api_ok, repository_read_ok,
                    repository_write_ok, https_username_ok, remote_protocol,
                    error_code, deep, wrapper_checked_at, checked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(profile) DO UPDATE SET
                    role=excluded.role,
                    api_ok=excluded.api_ok,
                    repository_read_ok=excluded.repository_read_ok,
                    repository_write_ok=excluded.repository_write_ok,
                    https_username_ok=excluded.https_username_ok,
                    remote_protocol=excluded.remote_protocol,
                    error_code=excluded.error_code,
                    deep=excluded.deep,
                    wrapper_checked_at=COALESCE(
                        excluded.wrapper_checked_at,
                        profile_preflight.wrapper_checked_at
                    ),
                    checked_at=excluded.checked_at
                """,
                (
                    str(result["profile"]),
                    str(result.get("role") or "unknown"),
                    (
                        None
                        if result.get("api_ok") is None
                        else int(bool(result["api_ok"]))
                    ),
                    (
                        None
                        if result.get("repository_read_ok") is None
                        else int(bool(result["repository_read_ok"]))
                    ),
                    (
                        None
                        if result.get("repository_write_ok") is None
                        else str(result["repository_write_ok"]).lower()
                    ),
                    (
                        None
                        if result.get("https_username_ok") is None
                        else int(bool(result["https_username_ok"]))
                    ),
                    result.get("remote_protocol"),
                    result.get("error_code"),
                    int(deep),
                    now if deep else None,
                    now,
                ),
            )

    def record_deployment_preflight(
        self,
        *,
        ok: bool,
        deep: bool,
        credential_contract_digest: str,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO deployment_preflight(
                    singleton, ok, deep, credential_contract_digest, checked_at
                ) VALUES (1, ?, ?, ?, ?)
                ON CONFLICT(singleton) DO UPDATE SET
                    ok=excluded.ok,
                    deep=excluded.deep,
                    credential_contract_digest=excluded.credential_contract_digest,
                    checked_at=excluded.checked_at
                """,
                (
                    int(ok),
                    int(deep),
                    credential_contract_digest,
                    int(time.time()),
                ),
            )

    def deployment_preflight(self, *, include_digest: bool = False) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM deployment_preflight WHERE singleton=1"
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["ok"] = bool(result["ok"])
        result["deep"] = bool(result["deep"])
        if not include_digest:
            result.pop("credential_contract_digest", None)
        return result

    def profile_preflight_health(self) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM profile_preflight ORDER BY profile"
            ).fetchall()
        results: list[dict] = []
        for row in rows:
            item = dict(row)
            for key in (
                "api_ok",
                "repository_read_ok",
                "https_username_ok",
                "deep",
            ):
                if item[key] is not None:
                    item[key] = bool(item[key])
            if item["repository_write_ok"] in {"true", "false"}:
                item["repository_write_ok"] = (
                    item["repository_write_ok"] == "true"
                )
            results.append(item)
        return results

    def finish_boot(
        self,
        boot_id: str,
        *,
        exit_reason: str,
        fatal: bool,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE controller_boots
                SET stopped_at=?, exit_reason=?, fatal=?
                WHERE boot_id=?
                """,
                (int(time.time()), exit_reason[:1000], int(fatal), boot_id),
            )

    def boot_health(self) -> dict:
        with self.connect() as conn:
            latest = conn.execute(
                """
                SELECT * FROM controller_boots
                ORDER BY started_at DESC, rowid DESC LIMIT 1
                """
            ).fetchone()
            previous_exit = conn.execute(
                """
                SELECT * FROM controller_boots
                WHERE stopped_at IS NOT NULL
                ORDER BY stopped_at DESC, rowid DESC LIMIT 1
                """
            ).fetchone()
            restart_count = conn.execute(
                "SELECT MAX((SELECT COUNT(*) FROM controller_boots) - 1, 0)"
            ).fetchone()[0]
        return {
            "boot_id": latest["boot_id"] if latest else None,
            "started_at": latest["started_at"] if latest else None,
            "restart_count": int(restart_count or 0),
            "last_exit_reason": (
                previous_exit["exit_reason"] if previous_exit else None
            ),
            "last_exit_fatal": (
                bool(previous_exit["fatal"]) if previous_exit else None
            ),
        }

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
            uncertain_ops = conn.execute(
                "SELECT COUNT(*) FROM operations WHERE status='uncertain'"
            ).fetchone()[0]
            dead_outbox = conn.execute(
                "SELECT COUNT(*) FROM outbox WHERE status='dead'"
            ).fetchone()[0]
            oldest_outbox = conn.execute(
                """
                SELECT MIN(created_at) FROM outbox
                WHERE status IN ('pending', 'retrying', 'dead')
                """
            ).fetchone()[0]
            aborting = conn.execute(
                """
                SELECT COUNT(*) FROM run_control
                WHERE state IN ('abort_requested', 'aborting')
                """
            ).fetchone()[0]
            state_counts = {
                str(row["state"]): int(row["count"])
                for row in conn.execute(
                    """
                    SELECT state, COUNT(*) AS count FROM run_control
                    GROUP BY state ORDER BY state
                    """
                )
            }
            schema_version = int(
                conn.execute("PRAGMA user_version").fetchone()[0]
            )
            merge_waits = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM merge_wait ORDER BY first_seen_at, run_key"
                )
            ]
            stale_workers = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT runtime.*, cards.run_key, cards.stage,
                           cards.iteration
                    FROM card_runtime AS runtime
                    JOIN managed_cards AS cards
                      ON cards.board=runtime.board
                     AND cards.card_id=runtime.card_id
                    WHERE runtime.deadline_at IS NOT NULL
                      AND runtime.deadline_at < ?
                      AND runtime.finished_at IS NULL
                      AND runtime.attempt_status IN (
                          'created', 'running', 'redispatch_requested'
                      )
                    ORDER BY runtime.deadline_at
                    """,
                    (int(time.time()),),
                )
            ]
            reconcile_rows = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT * FROM reconcile_intents
                    WHERE status != 'done'
                    ORDER BY created_at, intent_id
                    """
                )
            ]
            recent_attempts = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT run_id, run_key, card_id, attempt, status,
                           started_at, blocked_at, completion_at, exited_at,
                           last_heartbeat_at, last_progress_at,
                           terminal_reason, updated_at
                    FROM card_attempts
                    ORDER BY updated_at DESC, run_id DESC LIMIT 50
                    """
                )
            ]
            worker_runtime = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT runtime.*, cards.run_key, cards.stage,
                           cards.iteration
                    FROM card_runtime AS runtime
                    JOIN managed_cards AS cards
                      ON cards.board=runtime.board
                     AND cards.card_id=runtime.card_id
                    WHERE runtime.finished_at IS NULL
                    ORDER BY runtime.updated_at DESC
                    """
                )
            ]
            overlapping_attempts = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT board, card_id, COUNT(*) AS attempt_count,
                           GROUP_CONCAT(run_id) AS run_ids
                    FROM card_attempts
                    WHERE exited_at IS NULL
                      AND status IN (
                          'created', 'running', 'redispatch_requested',
                          'completion_recorded'
                      )
                    GROUP BY board, card_id
                    HAVING COUNT(*) > 1
                    ORDER BY board, card_id
                    """
                )
            ]
            supervisor_metrics = {
                str(row["run_id"]): str(row["summary"] or "")
                for row in conn.execute(
                    """
                    SELECT run_id, summary FROM attempt_metrics
                    WHERE metric='supervisor_probe'
                    """
                )
            }
        now = int(time.time())
        for item in recent_attempts:
            heartbeat = item.get("last_heartbeat_at") or item.get("started_at")
            progress = item.get("last_progress_at") or item.get("started_at")
            item["heartbeat_age_seconds"] = (
                max(0, now - int(heartbeat))
                if heartbeat is not None
                else None
            )
            item["progress_age_seconds"] = (
                max(0, now - int(progress))
                if progress is not None
                else None
            )
            item["supervisor_state"] = self._supervisor_state(
                supervisor_metrics.get(str(item["run_id"]))
            )
        for item in merge_waits:
            item["waiting_seconds"] = max(
                0,
                now - int(item["first_seen_at"]),
            )
        for item in worker_runtime:
            heartbeat = item.get("last_heartbeat_at") or item.get(
                "worker_started_at"
            )
            progress = item.get("last_progress_event_at") or item.get(
                "worker_started_at"
            )
            item["heartbeat_age_seconds"] = (
                max(0, now - int(heartbeat)) if heartbeat is not None else None
            )
            item["progress_age_seconds"] = (
                max(0, now - int(progress)) if progress is not None else None
            )
            session = str(item.get("worker_session_id") or "")
            attempt_run_id = None
            if session.startswith("kanban-run:"):
                attempt_run_id = f"{item['board']}:{session.split(':', 1)[1]}"
            supervisor_state: dict = {"state": "unknown"}
            raw_summary = supervisor_metrics.get(str(attempt_run_id or ""))
            if raw_summary:
                try:
                    decoded = json.loads(raw_summary)
                    if isinstance(decoded, dict):
                        supervisor_state = decoded
                except json.JSONDecodeError:
                    pass
            item["supervisor_state"] = supervisor_state
        for item in stale_workers:
            matching = next(
                (
                    runtime
                    for runtime in worker_runtime
                    if runtime["board"] == item["board"]
                    and runtime["card_id"] == item["card_id"]
                ),
                None,
            )
            if matching is not None:
                item["heartbeat_age_seconds"] = matching[
                    "heartbeat_age_seconds"
                ]
                item["progress_age_seconds"] = matching[
                    "progress_age_seconds"
                ]
                item["supervisor_state"] = matching["supervisor_state"]
        for item in overlapping_attempts:
            item["run_ids"] = str(item["run_ids"] or "").split(",")
        return {
            "schema_version": schema_version,
            "quick_check": "ok",
            "event_cursors": cursors,
            "outbox_pending": pending,
            "outbox_dead": dead_outbox,
            "outbox_oldest_created_at": oldest_outbox,
            "outbox_oldest_age_seconds": (
                max(0, now - int(oldest_outbox))
                if oldest_outbox is not None
                else 0
            ),
            "failed_operations": failed_ops,
            "uncertain_operations": uncertain_ops,
            "aborting_runs": aborting,
            "transition_pending": state_counts.get("transition_pending", 0),
            "human_blocked": state_counts.get("human_blocked", 0),
            "retry_wait": state_counts.get("retry_wait", 0),
            "run_states": state_counts,
            "reconcile_pending": len(reconcile_rows),
            "reconcile_oldest_age_seconds": (
                max(0, now - int(reconcile_rows[0]["created_at"]))
                if reconcile_rows
                else 0
            ),
            "reconcile_queue": reconcile_rows,
            "attempt_timeline": recent_attempts,
            "worker_runtime": worker_runtime,
            "overlapping_attempts": overlapping_attempts,
            "merge_waits": merge_waits,
            "stale_workers": stale_workers,
            "dependency_outages": self.open_dependency_outages(),
            "dependency_history": self.dependency_outage_history(),
            "controller": self.boot_health(),
            "profile_preflight": self.profile_preflight_health(),
            "deployment_preflight": self.deployment_preflight(),
        }
