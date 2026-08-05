from __future__ import annotations

import errno
import json
import os
import re
import signal
import socket
import sqlite3
import stat
import struct
import time
import uuid
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .worker_recovery import MAX_SUPERVISOR_MESSAGE_BYTES, WorkerIdentity

BOARD_PATTERN = re.compile(r"^(?:default|[a-z0-9][a-z0-9-]{0,62})$")
CARD_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
ALLOWED_METHODS = frozenset({"probe", "terminate"})


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    parent_pid: int
    process_group: int
    state: str
    start_ticks: int

    @property
    def exited(self) -> bool:
        return self.state == "Z"


class SupervisorRequestError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class WorkerSupervisorServer:
    def __init__(
        self,
        *,
        hermes_home: Path,
        socket_path: Path,
        term_grace_seconds: float = 10.0,
        kill_grace_seconds: float = 5.0,
        request_timeout_seconds: float = 2.0,
        expected_uid: int | None = None,
        expected_gid: int | None = None,
    ) -> None:
        self.hermes_home = hermes_home
        self.socket_path = socket_path
        self.term_grace_seconds = term_grace_seconds
        self.kill_grace_seconds = kill_grace_seconds
        self.request_timeout_seconds = request_timeout_seconds
        self.expected_uid = os.geteuid() if expected_uid is None else expected_uid
        self.expected_gid = os.getegid() if expected_gid is None else expected_gid

    def serve_forever(self) -> None:
        self._prepare_socket_path()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(self.socket_path))
            os.chmod(self.socket_path, 0o660)
            server.listen(16)
            while True:
                connection, _ = server.accept()
                with connection:
                    self._serve_connection(connection)

    def _prepare_socket_path(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        os.chmod(self.socket_path.parent, 0o750)
        try:
            existing = self.socket_path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(existing.st_mode) or existing.st_uid != os.geteuid():
            raise RuntimeError("unsafe_existing_supervisor_socket")
        self.socket_path.unlink()

    def _serve_connection(self, connection: socket.socket) -> None:
        request_id = "unknown"
        try:
            connection.settimeout(self.request_timeout_seconds)
            peer_pid, peer_uid, peer_gid = struct.unpack(
                "3i",
                connection.getsockopt(
                    socket.SOL_SOCKET,
                    socket.SO_PEERCRED,
                    struct.calcsize("3i"),
                ),
            )
            del peer_pid
            if peer_uid != self.expected_uid or peer_gid != self.expected_gid:
                raise SupervisorRequestError("peer_not_authorized")
            request = self._read_request(connection)
            request_id = str(request.get("id") or "unknown")[:128]
            result = self.handle_request(request)
            response = {"v": 1, "id": request_id, "ok": True, "result": result}
        except TimeoutError:
            response = {
                "v": 1,
                "id": request_id,
                "ok": False,
                "error": {"code": "request_timeout"},
            }
        except SupervisorRequestError as exc:
            response = {
                "v": 1,
                "id": request_id,
                "ok": False,
                "error": {"code": exc.code},
            }
        except Exception:  # noqa: BLE001 - never crash the root-owned RPC loop
            response = {
                "v": 1,
                "id": request_id,
                "ok": False,
                "error": {"code": "internal_error"},
            }
        encoded = (
            json.dumps(response, ensure_ascii=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        connection.sendall(encoded[:MAX_SUPERVISOR_MESSAGE_BYTES])

    @staticmethod
    def _read_request(connection: socket.socket) -> dict[str, Any]:
        raw = bytearray()
        while True:
            chunk = connection.recv(1024)
            if not chunk:
                break
            raw.extend(chunk)
            if len(raw) > MAX_SUPERVISOR_MESSAGE_BYTES:
                raise SupervisorRequestError("request_too_large")
            if b"\n" in chunk:
                break
        if not raw.endswith(b"\n") or raw.count(b"\n") != 1:
            raise SupervisorRequestError("invalid_framing")
        try:
            request = json.loads(raw[:-1])
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SupervisorRequestError("invalid_json") from exc
        if not isinstance(request, dict):
            raise SupervisorRequestError("invalid_request")
        return request

    def handle_request(self, request: dict[str, Any]) -> dict[str, Any]:
        if set(request) != {"v", "id", "method", "params"}:
            raise SupervisorRequestError("invalid_request_fields")
        if request["v"] != 1 or not isinstance(request["id"], str):
            raise SupervisorRequestError("invalid_request_version")
        try:
            uuid.UUID(request["id"])
        except (ValueError, AttributeError) as exc:
            raise SupervisorRequestError("invalid_request_id") from exc
        method = request["method"]
        if method not in ALLOWED_METHODS:
            raise SupervisorRequestError("unknown_method")
        params = request["params"]
        if not isinstance(params, dict) or set(params) != {
            "board",
            "card_id",
            "run_id",
            "worker_pid",
        }:
            raise SupervisorRequestError("invalid_params")
        identity = self._parse_identity(params)
        process = self._validate_identity(identity)
        attempt_processes = self._matching_attempt_processes(identity)
        observed_at = int(time.time())
        if not attempt_processes:
            return {
                "state": "exited",
                "observed_at": observed_at,
                "worker_pid": identity.worker_pid,
                "process_start_ticks": (
                    process.start_ticks if process is not None else None
                ),
                "process_count": 0,
            }
        if method == "probe":
            return {
                "state": "running",
                "observed_at": observed_at,
                "worker_pid": identity.worker_pid,
                "process_start_ticks": (
                    process.start_ticks if process is not None else None
                ),
                "process_count": len(attempt_processes),
            }
        return self._terminate(identity, process, attempt_processes)

    @staticmethod
    def _parse_identity(params: dict[str, Any]) -> WorkerIdentity:
        board = params["board"]
        card_id = params["card_id"]
        run_id = params["run_id"]
        worker_pid = params["worker_pid"]
        if not isinstance(board, str) or not BOARD_PATTERN.fullmatch(board):
            raise SupervisorRequestError("invalid_board")
        if not isinstance(card_id, str) or not CARD_PATTERN.fullmatch(card_id):
            raise SupervisorRequestError("invalid_card_id")
        if not isinstance(run_id, int) or isinstance(run_id, bool) or run_id <= 0:
            raise SupervisorRequestError("invalid_run_id")
        if (
            not isinstance(worker_pid, int)
            or isinstance(worker_pid, bool)
            or worker_pid <= 1
        ):
            raise SupervisorRequestError("invalid_worker_pid")
        return WorkerIdentity(board, card_id, run_id, worker_pid)

    def _board_db(self, board: str) -> Path:
        if board == "default":
            return self.hermes_home / "kanban.db"
        return self.hermes_home / "kanban" / "boards" / board / "kanban.db"

    def _validate_identity(
        self,
        identity: WorkerIdentity,
    ) -> ProcessIdentity | None:
        database = self._board_db(identity.board)
        if not database.is_file():
            raise SupervisorRequestError("board_missing")
        try:
            with closing(
                sqlite3.connect(f"file:{database}?mode=ro", uri=True)
            ) as connection:
                connection.row_factory = sqlite3.Row
                row = connection.execute(
                    """
                    SELECT status, created_by, current_run_id, worker_pid
                    FROM tasks WHERE id=?
                    """,
                    (identity.card_id,),
                ).fetchone()
        except sqlite3.DatabaseError as exc:
            raise SupervisorRequestError("board_unavailable") from exc
        if row is None:
            raise SupervisorRequestError("task_missing")
        if (
            row["created_by"] != "hollysys-controller"
            or row["status"] != "running"
            or row["current_run_id"] != identity.run_id
            or row["worker_pid"] != identity.worker_pid
        ):
            raise SupervisorRequestError("identity_mismatch")
        process = self._read_process(identity.worker_pid)
        if process is None or process.exited:
            return process
        try:
            owner_uid = Path(f"/proc/{identity.worker_pid}").stat().st_uid
            environment = self._read_environment(identity.worker_pid)
        except OSError as exc:
            raise SupervisorRequestError("process_evidence_unavailable") from exc
        expected_environment = {
            "HERMES_KANBAN_BOARD": identity.board,
            "HERMES_KANBAN_TASK": identity.card_id,
            "HERMES_KANBAN_RUN_ID": str(identity.run_id),
        }
        if owner_uid != self.expected_uid or any(
            environment.get(key) != value
            for key, value in expected_environment.items()
        ):
            raise SupervisorRequestError("process_identity_mismatch")
        return process

    @staticmethod
    def _read_environment(pid: int) -> dict[str, str]:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
        result: dict[str, str] = {}
        for item in raw.split(b"\0"):
            if b"=" not in item:
                continue
            key, value = item.split(b"=", 1)
            result[key.decode("utf-8", "replace")] = value.decode(
                "utf-8", "replace"
            )
        return result

    def _matching_attempt_processes(
        self,
        identity: WorkerIdentity,
    ) -> dict[int, ProcessIdentity]:
        expected_environment = {
            "HERMES_KANBAN_BOARD": identity.board,
            "HERMES_KANBAN_TASK": identity.card_id,
            "HERMES_KANBAN_RUN_ID": str(identity.run_id),
        }
        matches: dict[int, ProcessIdentity] = {}
        try:
            proc_paths = tuple(Path("/proc").iterdir())
        except OSError as exc:
            raise SupervisorRequestError("process_evidence_unavailable") from exc
        for path in proc_paths:
            if not path.name.isdigit():
                continue
            pid = int(path.name)
            try:
                if path.stat().st_uid != self.expected_uid:
                    continue
                process = self._read_process(pid)
                if process is None or process.exited:
                    continue
                environment = self._read_environment(pid)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise SupervisorRequestError(
                    "process_evidence_unavailable"
                ) from exc
            if all(
                environment.get(key) == value
                for key, value in expected_environment.items()
            ):
                matches[pid] = process
        return matches

    @staticmethod
    def _read_process(pid: int) -> ProcessIdentity | None:
        try:
            raw = Path(f"/proc/{pid}/stat").read_text(
                encoding="utf-8",
                errors="strict",
            )
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise SupervisorRequestError("process_evidence_unavailable") from exc
        try:
            fields = raw.rsplit(")", 1)[1].strip().split()
            return ProcessIdentity(
                pid=pid,
                state=fields[0],
                parent_pid=int(fields[1]),
                process_group=int(fields[2]),
                start_ticks=int(fields[19]),
            )
        except (IndexError, ValueError) as exc:
            raise SupervisorRequestError("invalid_process_stat") from exc

    def _descendants(
        self,
        root_pids: set[int],
    ) -> dict[int, ProcessIdentity]:
        processes: dict[int, ProcessIdentity] = {}
        for path in Path("/proc").iterdir():
            if not path.name.isdigit():
                continue
            process = self._read_process(int(path.name))
            if process is not None and not process.exited:
                processes[process.pid] = process
        selected = set(root_pids)
        changed = True
        while changed:
            changed = False
            for process in processes.values():
                if process.parent_pid in selected and process.pid not in selected:
                    selected.add(process.pid)
                    changed = True
        return {pid: processes[pid] for pid in selected if pid in processes}

    def _freeze_tree(
        self,
        identity: WorkerIdentity,
        initial: dict[int, ProcessIdentity],
    ) -> dict[int, ProcessIdentity]:
        frozen: dict[int, ProcessIdentity] = {}
        for _ in range(8):
            seeds: dict[int, ProcessIdentity] = {}
            for pid, expected in initial.items():
                current = self._read_process(pid)
                if (
                    current is not None
                    and not current.exited
                    and current.start_ticks == expected.start_ticks
                ):
                    seeds[pid] = current
            seeds.update(self._matching_attempt_processes(identity))
            current = self._descendants(set(seeds))
            new_processes = {
                pid: process for pid, process in current.items() if pid not in frozen
            }
            for process in new_processes.values():
                try:
                    if not self._signal_process(process, signal.SIGSTOP):
                        continue
                except (ProcessLookupError, FileNotFoundError):
                    continue
                except OSError as exc:
                    self._resume_tree(frozen)
                    raise SupervisorRequestError("process_tree_signal_failed") from exc
            frozen.update(new_processes)
            if not new_processes:
                return frozen
        self._resume_tree(frozen)
        raise SupervisorRequestError("process_tree_unstable")

    @classmethod
    def _signal_process(
        cls,
        expected: ProcessIdentity,
        signal_number: signal.Signals,
    ) -> bool:
        if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
            return cls._signal_process_by_start_ticks(expected, signal_number)
        try:
            descriptor = os.pidfd_open(expected.pid, 0)
        except OSError as exc:
            if isinstance(exc, ProcessLookupError):
                return False
            if exc.errno in {errno.ENOSYS, errno.EINVAL}:
                return cls._signal_process_by_start_ticks(
                    expected,
                    signal_number,
                )
            raise
        try:
            current = cls._read_process(expected.pid)
            if (
                current is None
                or current.exited
                or current.start_ticks != expected.start_ticks
            ):
                return False
            try:
                signal.pidfd_send_signal(descriptor, signal_number)
            except OSError as exc:
                if exc.errno not in {errno.ENOSYS, errno.EINVAL}:
                    raise
                return cls._signal_process_by_start_ticks(
                    expected,
                    signal_number,
                )
            return True
        finally:
            os.close(descriptor)

    @classmethod
    def _signal_process_by_start_ticks(
        cls,
        expected: ProcessIdentity,
        signal_number: signal.Signals,
    ) -> bool:
        """Compatibility path for kernels without pidfd signal support."""
        current = cls._read_process(expected.pid)
        if (
            current is None
            or current.exited
            or current.start_ticks != expected.start_ticks
        ):
            return False
        try:
            os.kill(expected.pid, signal_number)
        except ProcessLookupError:
            return False
        after = cls._read_process(expected.pid)
        if after is not None and after.start_ticks != expected.start_ticks:
            if signal_number == signal.SIGSTOP:
                try:
                    os.kill(after.pid, signal.SIGCONT)
                except OSError:
                    pass
            raise SupervisorRequestError("pid_reused_during_signal")
        return True

    @classmethod
    def _resume_tree(cls, processes: dict[int, ProcessIdentity]) -> None:
        for process in processes.values():
            try:
                cls._signal_process(process, signal.SIGCONT)
            except (OSError, SupervisorRequestError):
                pass

    @classmethod
    def _signal_tree(
        cls,
        processes: dict[int, ProcessIdentity],
        signal_number: signal.Signals,
    ) -> None:
        for pid in sorted(processes, reverse=True):
            try:
                cls._signal_process(processes[pid], signal_number)
            except ProcessLookupError:
                continue
            except OSError as exc:
                raise SupervisorRequestError("process_tree_signal_failed") from exc

    def _remaining(
        self,
        processes: dict[int, ProcessIdentity],
    ) -> dict[int, ProcessIdentity]:
        remaining: dict[int, ProcessIdentity] = {}
        for pid, expected in processes.items():
            current = self._read_process(pid)
            if (
                current is not None
                and not current.exited
                and current.start_ticks == expected.start_ticks
            ):
                remaining[pid] = current
        return remaining

    def _wait_for_exit(
        self,
        processes: dict[int, ProcessIdentity],
        timeout_seconds: float,
    ) -> dict[int, ProcessIdentity]:
        deadline = time.monotonic() + timeout_seconds
        remaining = self._remaining(processes)
        while remaining and time.monotonic() < deadline:
            time.sleep(0.05)
            remaining = self._remaining(remaining)
        return remaining

    def _terminate(
        self,
        identity: WorkerIdentity,
        root: ProcessIdentity | None,
        attempt_processes: dict[int, ProcessIdentity],
    ) -> dict[str, Any]:
        processes = self._freeze_tree(identity, attempt_processes)
        if not processes:
            return {
                "state": "exited",
                "observed_at": int(time.time()),
                "worker_pid": identity.worker_pid,
                "process_start_ticks": (
                    root.start_ticks if root is not None else None
                ),
                "process_count": 0,
            }
        self._signal_tree(processes, signal.SIGTERM)
        self._resume_tree(processes)
        remaining = self._wait_for_exit(processes, self.term_grace_seconds)
        used_sigkill = bool(remaining)
        if remaining:
            self._signal_tree(remaining, signal.SIGKILL)
            remaining = self._wait_for_exit(remaining, self.kill_grace_seconds)
        if remaining:
            raise SupervisorRequestError("termination_failed")
        return {
            "state": "terminated",
            "observed_at": int(time.time()),
            "worker_pid": identity.worker_pid,
            "process_start_ticks": (
                root.start_ticks if root is not None else None
            ),
            "signal": "SIGKILL" if used_sigkill else "SIGTERM",
            "sigkill": used_sigkill,
            "process_count": len(processes),
        }


def main() -> None:
    server = WorkerSupervisorServer(
        hermes_home=Path(os.environ.get("HERMES_HOME", "/opt/data")),
        socket_path=Path(
            os.environ.get(
                "HOLLYSYS_WORKER_SUPERVISOR_SOCKET",
                "/run/hollysys-controller/worker-supervisor.sock",
            )
        ),
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
