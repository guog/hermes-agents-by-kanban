from __future__ import annotations

import json
import socket
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

MAX_SUPERVISOR_MESSAGE_BYTES = 4096


@dataclass(frozen=True)
class WorkerIdentity:
    board: str
    card_id: str
    run_id: int
    worker_pid: int


@dataclass(frozen=True)
class SupervisorObservation:
    state: str
    observed_at: int
    worker_pid: int
    process_start_ticks: int | None = None
    signal: str | None = None
    sigkill: bool = False
    process_count: int = 0
    error_code: str | None = None

    @property
    def exit_confirmed(self) -> bool:
        return self.state in {"exited", "terminated"}


@dataclass(frozen=True)
class SupervisorReadiness:
    ready: bool
    observed_at: int
    error_code: str | None = None


class WorkerSupervisor(Protocol):
    def probe(self, identity: WorkerIdentity) -> SupervisorObservation: ...

    def terminate(self, identity: WorkerIdentity) -> SupervisorObservation: ...


class UnixWorkerSupervisorClient:
    def __init__(
        self,
        socket_path: Path,
        *,
        probe_timeout_seconds: float = 2.0,
        terminate_timeout_seconds: float = 17.0,
    ) -> None:
        self.socket_path = socket_path
        self.probe_timeout_seconds = probe_timeout_seconds
        self.terminate_timeout_seconds = terminate_timeout_seconds

    def probe(self, identity: WorkerIdentity) -> SupervisorObservation:
        return self._call("probe", identity, self.probe_timeout_seconds)

    def terminate(self, identity: WorkerIdentity) -> SupervisorObservation:
        return self._call("terminate", identity, self.terminate_timeout_seconds)

    def readiness(self) -> SupervisorReadiness:
        """Verify the strict protocol without requiring an active Worker."""
        sentinel = WorkerIdentity("default", "_supervisor_ready", 1, 2)
        observation = self.probe(sentinel)
        protocol_rejections = {
            "board_missing",
            "task_missing",
            "identity_mismatch",
            "process_identity_mismatch",
        }
        return SupervisorReadiness(
            ready=(
                observation.state in {"running", "exited"}
                or observation.error_code in protocol_rejections
            ),
            observed_at=observation.observed_at,
            error_code=observation.error_code,
        )

    def _call(
        self,
        method: str,
        identity: WorkerIdentity,
        timeout_seconds: float,
    ) -> SupervisorObservation:
        request_id = uuid.uuid4().hex
        request = {
            "v": 1,
            "id": request_id,
            "method": method,
            "params": {
                "board": identity.board,
                "card_id": identity.card_id,
                "run_id": identity.run_id,
                "worker_pid": identity.worker_pid,
            },
        }
        encoded = (
            json.dumps(request, ensure_ascii=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        if len(encoded) > MAX_SUPERVISOR_MESSAGE_BYTES:
            return self._unavailable(identity, "request_too_large")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(timeout_seconds)
                client.connect(str(self.socket_path))
                client.sendall(encoded)
                raw = self._read_line(client)
        except FileNotFoundError:
            return self._unavailable(identity, "socket_missing")
        except TimeoutError:
            return self._unavailable(identity, "supervisor_timeout")
        except OSError:
            return self._unavailable(identity, "supervisor_unavailable")
        try:
            response = json.loads(raw)
            if (
                not isinstance(response, dict)
                or response.get("v") != 1
                or response.get("id") != request_id
                or not isinstance(response.get("ok"), bool)
            ):
                raise ValueError("invalid envelope")
            if not response["ok"]:
                error = response.get("error")
                code = error.get("code") if isinstance(error, dict) else None
                return self._unavailable(
                    identity,
                    str(code or "supervisor_rejected"),
                )
            result = response.get("result")
            if not isinstance(result, dict):
                raise TypeError("missing result")
            state = result.get("state")
            if state not in {"running", "exited", "terminated"}:
                raise ValueError("invalid state")
            observed_at = int(result["observed_at"])
            worker_pid = int(result["worker_pid"])
            process_count = int(result.get("process_count") or 0)
            if (
                observed_at <= 0
                or worker_pid != identity.worker_pid
                or process_count < 0
                or (state in {"running", "terminated"} and process_count < 1)
            ):
                raise ValueError("invalid identity result")
            return SupervisorObservation(
                state=state,
                observed_at=observed_at,
                worker_pid=worker_pid,
                process_start_ticks=(
                    int(result["process_start_ticks"])
                    if result.get("process_start_ticks") is not None
                    else None
                ),
                signal=(
                    str(result["signal"])
                    if result.get("signal") is not None
                    else None
                ),
                sigkill=bool(result.get("sigkill", False)),
                process_count=process_count,
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return self._unavailable(identity, "invalid_supervisor_response")

    @staticmethod
    def _read_line(client: socket.socket) -> str:
        chunks = bytearray()
        while True:
            chunk = client.recv(1024)
            if not chunk:
                break
            chunks.extend(chunk)
            if len(chunks) > MAX_SUPERVISOR_MESSAGE_BYTES:
                raise OSError("supervisor response too large")
            if b"\n" in chunk:
                break
        if not chunks.endswith(b"\n") or chunks.count(b"\n") != 1:
            raise OSError("invalid supervisor response framing")
        return chunks[:-1].decode("utf-8")

    @staticmethod
    def _unavailable(
        identity: WorkerIdentity,
        error_code: str,
    ) -> SupervisorObservation:
        return SupervisorObservation(
            state="unavailable",
            observed_at=int(time.time()),
            worker_pid=identity.worker_pid,
            error_code=error_code,
        )


class WorkerRecoveryCoordinator:
    def __init__(self, supervisor: WorkerSupervisor) -> None:
        self.supervisor = supervisor

    def observe(
        self,
        identity: WorkerIdentity,
        *,
        terminate_running: bool,
    ) -> SupervisorObservation:
        observation = self.supervisor.probe(identity)
        if observation.state != "running" or not terminate_running:
            return observation
        return self.supervisor.terminate(identity)

    def readiness(self) -> SupervisorReadiness:
        check = getattr(self.supervisor, "readiness", None)
        if check is None:
            return SupervisorReadiness(
                ready=False,
                observed_at=int(time.time()),
                error_code="readiness_unsupported",
            )
        return check()
