from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from hollysys_controller.cli import (
    _run,
    build_parser,
    params_for,
)
from hollysys_controller.daemon import ControllerDaemon
from hollysys_controller.errors import ControllerFatalError
from hollysys_controller.models import RpcRequest, RpcResponse
from hollysys_controller.rpc import RpcServer, rpc_call


class RpcTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.socket = Path(self.temp.name) / "controller.sock"

        async def handler(request):
            return {"method": request.method, **request.params}

        self.server = RpcServer(self.socket, handler)
        await self.server.start()

    async def asyncTearDown(self) -> None:
        await self.server.close()
        self.temp.cleanup()

    async def test_unix_socket_round_trip(self) -> None:
        response = await rpc_call(
            self.socket,
            RpcRequest(id="1", method="health", params={"probe": True}),
        )
        self.assertTrue(response.ok)
        self.assertEqual(response.result, {"method": "health", "probe": True})

    async def test_disconnected_client_does_not_raise_broken_pipe(self) -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(
            RpcRequest(
                id="disconnect",
                method="health",
                params={"probe": "readiness"},
            ).model_dump_json().encode("utf-8")
            + b"\n"
        )
        reader.feed_eof()

        class DisconnectedWriter:
            def __init__(self) -> None:
                self.closed = False

            def write(self, data: bytes) -> None:
                del data

            async def drain(self) -> None:
                raise ConnectionResetError("client disconnected")

            def close(self) -> None:
                self.closed = True

            async def wait_closed(self) -> None:
                raise BrokenPipeError("already closed")

        writer = DisconnectedWriter()

        await self.server._handle_connection(  # type: ignore[arg-type]
            reader,
            writer,
        )

        self.assertTrue(writer.closed)

    async def test_long_start_returns_running_snapshot_before_completion(
        self,
    ) -> None:
        daemon = ControllerDaemon.__new__(ControllerDaemon)
        daemon.config = SimpleNamespace(
            start_request_sync_timeout_seconds=0.01
        )
        daemon.background_requests = set()
        daemon.service = SimpleNamespace(
            start=lambda params: (
                time.sleep(0.05)
                or {"request_status": "done", **params}
            ),
            start_request_status=lambda params: {
                "request_status": "running",
                **params,
            },
        )

        result = await daemon._handle_start({"run_key": "pending"})

        self.assertEqual(
            result,
            {"request_status": "running", "run_key": "pending"},
        )
        await asyncio.gather(
            *tuple(daemon.background_requests),
            return_exceptions=True,
        )

    async def test_rpc_controller_fatal_signals_daemon_exit(self) -> None:
        daemon = ControllerDaemon.__new__(ControllerDaemon)
        daemon.service = SimpleNamespace(
            health=lambda probe: (_ for _ in ()).throw(
                ControllerFatalError("controller_store_database_error")
            )
        )
        daemon.fatal_event = asyncio.Event()
        daemon.rpc_fatal_error = None

        with self.assertRaisesRegex(
            ControllerFatalError,
            "controller_store_database_error",
        ):
            await daemon.handle_rpc(
                RpcRequest(
                    id="fatal-1",
                    method="health",
                    params={"probe": "liveness"},
                )
            )

        self.assertTrue(daemon.fatal_event.is_set())
        self.assertIsInstance(
            daemon.rpc_fatal_error,
            ControllerFatalError,
        )

    def test_status_summary_is_an_rpc_command(self) -> None:
        args = build_parser().parse_args(
            [
                "status-summary",
                "--run-key",
                "hollysys-abcdefghijklmnopqrst",
            ]
        )

        self.assertEqual(
            params_for(args),
            (
                "status-summary",
                {"run_key": "hollysys-abcdefghijklmnopqrst"},
            ),
        )

    async def test_daemon_routes_status_summary_to_service(self) -> None:
        daemon = ControllerDaemon.__new__(ControllerDaemon)
        daemon.service = SimpleNamespace(
            status_summary=lambda run_key: {"run_key": run_key},
        )
        daemon.fatal_event = asyncio.Event()
        daemon.rpc_fatal_error = None

        result = await daemon.handle_rpc(
            RpcRequest(
                id="summary-1",
                method="status-summary",
                params={"run_key": "hollysys-abcdefghijklmnopqrst"},
            )
        )

        self.assertEqual(
            result,
            {"run_key": "hollysys-abcdefghijklmnopqrst"},
        )

    async def test_preflight_cli_runs_through_controller_rpc(self) -> None:
        args = build_parser().parse_args(
            [
                "--socket",
                str(self.socket),
                "preflight",
                "--deep",
            ]
        )
        call = AsyncMock(
            return_value=RpcResponse(
                id="preflight-response",
                ok=True,
                result={"ok": True, "mode": "deep"},
            )
        )
        output = StringIO()

        with patch("hollysys_controller.cli.rpc_call", call), redirect_stdout(
            output
        ):
            exit_code = await _run(args)

        self.assertEqual(exit_code, 0)
        request = call.await_args.args[1]
        self.assertEqual(request.method, "preflight")
        self.assertEqual(request.params, {"deep": True})
        self.assertEqual(
            json.loads(output.getvalue()),
            {"ok": True, "mode": "deep"},
        )

    def test_supervisor_preflight_is_an_explicit_deep_gate(self) -> None:
        args = build_parser().parse_args(
            ["preflight", "--deep", "--require-supervisor"]
        )
        method, params = params_for(args)
        self.assertEqual(method, "preflight")
        self.assertEqual(
            params,
            {"deep": True, "require_supervisor": True},
        )

    def test_validate_completion_reads_json_before_rpc(self) -> None:
        metadata = Path(self.temp.name) / "completion.json"
        metadata.write_text('{"outcome":"pass"}', encoding="utf-8")
        args = build_parser().parse_args(
            [
                "validate-completion",
                "--card-id",
                "t_work",
                "--metadata",
                str(metadata),
            ]
        )
        self.assertEqual(
            params_for(args),
            (
                "validate-completion",
                {
                    "card_id": "t_work",
                    "metadata": {"outcome": "pass"},
                },
            ),
        )

    def test_validate_completion_accepts_inline_json_for_compatibility(self) -> None:
        args = build_parser().parse_args(
            [
                "validate-completion",
                "--card-id",
                "t_work",
                "--metadata",
                '{"outcome":"fail","issues":["P1"]}',
            ]
        )

        self.assertEqual(
            params_for(args)[1]["metadata"],
            {"outcome": "fail", "issues": ["P1"]},
        )

    def test_validate_completion_rejects_missing_file_without_traceback(
        self,
    ) -> None:
        args = build_parser().parse_args(
            [
                "validate-completion",
                "--card-id",
                "t_work",
                "--metadata",
                "missing-completion.json",
            ]
        )

        with self.assertRaisesRegex(
            ValueError,
            "existing JSON file or an inline JSON object",
        ):
            params_for(args)

    def test_recover_cli_method_is_allowed_by_rpc_contract(self) -> None:
        args = build_parser().parse_args(
            [
                "recover",
                "--run-key",
                "hollysys-abcdefghijklmnopqrst",
                "--message-id",
                "om_recover",
                "--sender",
                "ou_admin",
                "--chat-id",
                "oc_origin",
                "--reason",
                "credential rotation verified",
            ]
        )
        method, params = params_for(args)

        request = RpcRequest(id="recover-1", method=method, params=params)

        self.assertEqual(request.method, "recover")
        self.assertEqual(request.params["sender"], "ou_admin")


if __name__ == "__main__":
    unittest.main()
