from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from hollysys_controller.cli import (
    build_parser,
    controller_snapshot_config,
    params_for,
)
from hollysys_controller.models import RpcRequest
from hollysys_controller.rpc import RpcServer, rpc_call
from tests.helpers import config


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

    def test_status_summary_is_a_local_cli_command(self) -> None:
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

    def test_status_summary_ignores_the_agent_profile_home(self) -> None:
        controller_config = config(Path(self.temp.name)).model_copy(
            update={
                "hermes_home": Path(self.temp.name) / "profiles" / "dispatcher",
                "state_dir": Path(self.temp.name) / "data" / "controller",
            }
        )

        snapshot_config = controller_snapshot_config(controller_config)

        self.assertEqual(
            snapshot_config.hermes_home,
            Path(self.temp.name) / "data",
        )

    def test_validate_completion_reads_exact_json_object(self) -> None:
        metadata_path = Path(self.temp.name) / "completion.json"
        metadata_path.write_text(
            json.dumps({"stage": "plan-write", "outcome": "pass"}),
            encoding="utf-8",
        )
        args = build_parser().parse_args(
            [
                "validate-completion",
                "--card-id",
                "t_plan",
                "--metadata",
                str(metadata_path),
            ]
        )

        self.assertEqual(
            params_for(args),
            (
                "validate-completion",
                {
                    "card_id": "t_plan",
                    "metadata": {
                        "stage": "plan-write",
                        "outcome": "pass",
                    },
                },
            ),
        )

    def test_recover_exception_parameters_are_explicit(self) -> None:
        args = build_parser().parse_args(
            [
                "recover-exception",
                "--run-key",
                "hollysys-abcdefghijklmnopqrst",
                "--exception-card-id",
                "t_exception",
                "--expected-parent-card-id",
                "t_parent",
                "--reason",
                "schema and runtime contract were corrected",
            ]
        )

        self.assertEqual(
            params_for(args),
            (
                "recover-exception",
                {
                    "run_key": "hollysys-abcdefghijklmnopqrst",
                    "exception_card_id": "t_exception",
                    "expected_parent_card_id": "t_parent",
                    "reason": "schema and runtime contract were corrected",
                },
            ),
        )


if __name__ == "__main__":
    unittest.main()
