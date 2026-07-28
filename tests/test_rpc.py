from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hollysys_controller.models import RpcRequest
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


if __name__ == "__main__":
    unittest.main()
