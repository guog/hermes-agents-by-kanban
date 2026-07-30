from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from pathlib import Path

from .models import RpcRequest, RpcResponse


class RpcServer:
    def __init__(
        self,
        socket_path: Path,
        handler: Callable[[RpcRequest], Awaitable[dict]],
    ):
        self.socket_path = socket_path
        self.handler = handler
        self.server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            self.socket_path.unlink()
        self.server = await asyncio.start_unix_server(
            self._handle_connection, path=str(self.socket_path)
        )
        if os.geteuid() == 0:
            try:
                socket_uid = int(os.environ.get("PUID", "1000"))
                socket_gid = int(os.environ.get("PGID", "1000"))
            except ValueError as exc:
                raise ValueError("PUID and PGID must be numeric") from exc
            os.chown(self.socket_path, socket_uid, socket_gid)
        self.socket_path.chmod(0o660)

    async def close(self) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        if self.socket_path.exists():
            self.socket_path.unlink()

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=30)
            request = RpcRequest.model_validate_json(raw)
            try:
                result = await self.handler(request)
                response = RpcResponse(id=request.id, ok=True, result=result)
            except Exception as exc:  # noqa: BLE001 - RPC error boundary
                response = RpcResponse(id=request.id, ok=False, error=str(exc))
            writer.write(
                response.model_dump_json(exclude_none=True).encode("utf-8") + b"\n"
            )
            await writer.drain()
        except Exception as exc:  # noqa: BLE001 - malformed client boundary
            writer.write(
                (
                    json.dumps(
                        {"id": "invalid", "ok": False, "error": str(exc)},
                        ensure_ascii=False,
                    )
                    + "\n"
                ).encode("utf-8")
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()


async def rpc_call(
    socket_path: Path, request: RpcRequest, timeout: float = 130
) -> RpcResponse:
    reader, writer = await asyncio.wait_for(
        asyncio.open_unix_connection(str(socket_path)), timeout=timeout
    )
    try:
        writer.write(request.model_dump_json().encode("utf-8") + b"\n")
        await writer.drain()
        raw = await asyncio.wait_for(reader.readline(), timeout=timeout)
        return RpcResponse.model_validate_json(raw)
    finally:
        writer.close()
        await writer.wait_closed()
