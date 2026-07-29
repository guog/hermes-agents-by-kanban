from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
