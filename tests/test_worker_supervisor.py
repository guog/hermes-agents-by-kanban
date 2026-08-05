from __future__ import annotations

import errno
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from hollysys_controller.worker_recovery import (
    SupervisorObservation,
    UnixWorkerSupervisorClient,
    WorkerIdentity,
    WorkerRecoveryCoordinator,
)
from hollysys_controller.worker_supervisor import (
    ProcessIdentity,
    SupervisorRequestError,
    WorkerSupervisorServer,
)


class FakeSupervisor:
    def __init__(
        self,
        probe: SupervisorObservation,
        terminate: SupervisorObservation,
    ) -> None:
        self.probe_result = probe
        self.terminate_result = terminate
        self.probes = 0
        self.terminations = 0

    def probe(self, identity: WorkerIdentity) -> SupervisorObservation:
        self.probes += 1
        return self.probe_result

    def terminate(self, identity: WorkerIdentity) -> SupervisorObservation:
        self.terminations += 1
        return self.terminate_result


class WorkerRecoveryCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.identity = WorkerIdentity("gitlab-p12", "t_work", 4, 123)

    def test_running_worker_is_only_terminated_when_authorized(self) -> None:
        running = SupervisorObservation("running", 10, 123)
        terminated = SupervisorObservation("terminated", 11, 123)
        fake = FakeSupervisor(running, terminated)
        coordinator = WorkerRecoveryCoordinator(fake)

        self.assertEqual(
            coordinator.observe(self.identity, terminate_running=False).state,
            "running",
        )
        self.assertEqual(fake.terminations, 0)
        self.assertEqual(
            coordinator.observe(self.identity, terminate_running=True).state,
            "terminated",
        )
        self.assertEqual(fake.terminations, 1)

    def test_unavailable_probe_never_attempts_termination(self) -> None:
        unavailable = SupervisorObservation(
            "unavailable",
            10,
            123,
            error_code="socket_missing",
        )
        fake = FakeSupervisor(unavailable, unavailable)
        result = WorkerRecoveryCoordinator(fake).observe(
            self.identity,
            terminate_running=True,
        )
        self.assertEqual(result.error_code, "socket_missing")
        self.assertEqual(fake.terminations, 0)

    def test_missing_socket_is_fail_closed(self) -> None:
        client = UnixWorkerSupervisorClient(Path("/missing/supervisor.sock"))
        result = client.probe(self.identity)
        self.assertEqual(result.state, "unavailable")
        self.assertEqual(result.error_code, "socket_missing")
        readiness = client.readiness()
        self.assertFalse(readiness.ready)
        self.assertEqual(readiness.error_code, "socket_missing")

    def test_protocol_level_identity_rejection_proves_readiness(self) -> None:
        client = UnixWorkerSupervisorClient(Path("/unused"))
        client.probe = lambda identity: SupervisorObservation(
            "unavailable",
            10,
            identity.worker_pid,
            error_code="task_missing",
        )
        self.assertTrue(client.readiness().ready)


class WorkerSupervisorServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        database = self.root / "kanban" / "boards" / "gitlab-p12" / "kanban.db"
        database.parent.mkdir(parents=True)
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute(
                """
                CREATE TABLE tasks (
                    id TEXT PRIMARY KEY,
                    status TEXT,
                    created_by TEXT,
                    current_run_id INTEGER,
                    worker_pid INTEGER
                )
                """
            )
        self.database = database
        self.server = WorkerSupervisorServer(
            hermes_home=self.root,
            socket_path=self.root / "worker-supervisor.sock",
            term_grace_seconds=0.5,
            kill_grace_seconds=0.5,
        )
        self.processes: list[subprocess.Popen[str]] = []
        self.child_pids: list[int] = []

    def test_pidfd_signal_refuses_reused_process_identity(self) -> None:
        expected = ProcessIdentity(77, 1, 77, "S", 100)
        reused = ProcessIdentity(77, 1, 77, "S", 101)
        with (
            patch.object(os, "pidfd_open", return_value=9, create=True),
            patch.object(signal, "pidfd_send_signal", create=True) as send,
            patch.object(os, "close") as close,
            patch.object(WorkerSupervisorServer, "_read_process", return_value=reused),
        ):
            signaled = self.server._signal_process(expected, signal.SIGTERM)

        self.assertFalse(signaled)
        send.assert_not_called()
        close.assert_called_once_with(9)

    def test_pidfd_enosys_uses_start_time_checked_signal(self) -> None:
        expected = ProcessIdentity(77, 1, 77, "S", 100)
        with (
            patch.object(
                os,
                "pidfd_open",
                side_effect=OSError(errno.ENOSYS, "not implemented"),
                create=True,
            ),
            patch.object(signal, "pidfd_send_signal", create=True),
            patch.object(os, "kill") as kill,
            patch.object(
                WorkerSupervisorServer,
                "_read_process",
                return_value=expected,
            ),
        ):
            signaled = self.server._signal_process(expected, signal.SIGTERM)

        self.assertTrue(signaled)
        kill.assert_called_once_with(77, signal.SIGTERM)

    def tearDown(self) -> None:
        for process in self.processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
        for pid in self.child_pids:
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass
        self.temporary.cleanup()

    def _spawn_worker(self, *, child: bool = False) -> subprocess.Popen[str]:
        environment = os.environ.copy()
        environment.update(
            {
                "HERMES_KANBAN_BOARD": "gitlab-p12",
                "HERMES_KANBAN_TASK": "t_work",
                "HERMES_KANBAN_RUN_ID": "4",
            }
        )
        code = (
            "import subprocess,time; "
            "child=subprocess.Popen(['sleep','60'], start_new_session=True); "
            "print(child.pid, flush=True); "
            "time.sleep(60)"
            if child
            else "import time; time.sleep(60)"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", code],
            env=environment,
            text=True,
            start_new_session=True,
            stdout=subprocess.PIPE if child else None,
        )
        self.processes.append(process)
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute(
                "INSERT INTO tasks VALUES (?, ?, ?, ?, ?)",
                ("t_work", "running", "hollysys-controller", 4, process.pid),
            )
        if child:
            assert process.stdout is not None
            self.child_pids.append(int(process.stdout.readline().strip()))
            process.stdout.close()
        else:
            time.sleep(0.05)
        return process

    @staticmethod
    def _request(method: str, pid: int) -> dict:
        return {
            "v": 1,
            "id": str(uuid.uuid4()),
            "method": method,
            "params": {
                "board": "gitlab-p12",
                "card_id": "t_work",
                "run_id": 4,
                "worker_pid": pid,
            },
        }

    @unittest.skipUnless(
        Path("/proc/self/stat").is_file(),
        "requires Linux procfs",
    )
    def test_probe_requires_database_and_process_identity(self) -> None:
        process = self._spawn_worker()
        result = self.server.handle_request(self._request("probe", process.pid))
        self.assertEqual(result["state"], "running")
        self.assertEqual(result["worker_pid"], process.pid)
        self.assertGreater(result["process_start_ticks"], 0)

        wrong = self._request("probe", process.pid)
        wrong["params"]["run_id"] = 5
        with self.assertRaisesRegex(SupervisorRequestError, "identity_mismatch"):
            self.server.handle_request(wrong)

    @unittest.skipUnless(
        Path("/proc/self/stat").is_file(),
        "requires Linux procfs",
    )
    def test_terminate_stops_worker_and_independent_child_session(self) -> None:
        process = self._spawn_worker(child=True)
        result = self.server.handle_request(
            self._request("terminate", process.pid)
        )
        self.assertEqual(result["state"], "terminated")
        self.assertGreaterEqual(result["process_count"], 2)
        process.wait(timeout=5)

    @unittest.skipUnless(
        Path("/proc/self/stat").is_file(),
        "requires Linux procfs",
    )
    def test_orphaned_attempt_child_is_not_mistaken_for_full_exit(self) -> None:
        process = self._spawn_worker(child=True)
        process.kill()
        process.wait(timeout=5)

        probe = self.server.handle_request(
            self._request("probe", process.pid)
        )
        self.assertEqual(probe["state"], "running")
        self.assertGreaterEqual(probe["process_count"], 1)
        self.assertIsNone(probe["process_start_ticks"])

        terminated = self.server.handle_request(
            self._request("terminate", process.pid)
        )
        self.assertEqual(terminated["state"], "terminated")
        self.assertGreaterEqual(terminated["process_count"], 1)

    def test_request_contract_rejects_extra_fields_and_unknown_method(self) -> None:
        process = self._spawn_worker()
        request = self._request("probe", process.pid)
        request["extra"] = "unsafe"
        with self.assertRaisesRegex(
            SupervisorRequestError,
            "invalid_request_fields",
        ):
            self.server.handle_request(request)

        request = self._request("shell", process.pid)
        with self.assertRaisesRegex(SupervisorRequestError, "unknown_method"):
            self.server.handle_request(request)

        request = self._request("probe", process.pid)
        request["id"] = "not-a-uuid"
        with self.assertRaisesRegex(SupervisorRequestError, "invalid_request_id"):
            self.server.handle_request(request)

    @unittest.skipUnless(
        hasattr(socket, "SO_PEERCRED"),
        "requires Linux peer credentials",
    )
    def test_connection_contract_rejects_oversized_requests(self) -> None:
        server_side, client_side = socket.socketpair()
        self.addCleanup(server_side.close)
        self.addCleanup(client_side.close)
        client_side.sendall(b"x" * 4097 + b"\n")
        self.server._serve_connection(server_side)
        response = json.loads(client_side.recv(4096))
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "request_too_large")

    @unittest.skipUnless(
        Path("/proc/self/stat").is_file(),
        "requires Linux procfs",
    )
    def test_pid_reuse_without_attempt_environment_is_rejected(self) -> None:
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            text=True,
        )
        self.processes.append(process)
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute(
                "INSERT INTO tasks VALUES (?, ?, ?, ?, ?)",
                ("t_work", "running", "hollysys-controller", 4, process.pid),
            )
        with self.assertRaisesRegex(
            SupervisorRequestError,
            "process_identity_mismatch",
        ):
            self.server.handle_request(self._request("probe", process.pid))


if __name__ == "__main__":
    unittest.main()
