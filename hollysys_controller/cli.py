from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

from .models import RpcRequest
from .rpc import rpc_call


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hollysysctl")
    parser.add_argument(
        "--socket",
        default=os.environ.get(
            "HOLLYSYS_CONTROLLER_SOCKET", "/opt/data/controller/controller.sock"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start")
    start.add_argument("--prd-blob-url", required=True)
    start.add_argument("--prd-mr-url", required=True)
    start.add_argument("--message-id", required=True)
    start.add_argument("--chat-id", required=True)
    start.add_argument("--thread-id")
    start.add_argument("--chat-type", required=True, choices=["group", "p2p"])
    start.add_argument("--initiator", required=True)

    status = sub.add_parser("status")
    status.add_argument("--run-key", required=True)

    resolve = sub.add_parser("resolve")
    resolve.add_argument("--run-key", required=True)
    resolve.add_argument("--card-id", required=True)
    resolve.add_argument("--block-id", required=True)
    resolve.add_argument("--message-id", required=True)
    resolve.add_argument("--sender", required=True)
    resolve.add_argument("--chat-id", required=True)
    resolve.add_argument("--thread-id")
    resolve.add_argument("--answer", required=True)

    sub.add_parser("health")
    return parser


def params_for(args: argparse.Namespace) -> tuple[str, dict]:
    values = vars(args).copy()
    method = values.pop("command")
    values.pop("socket")
    if method == "start":
        values = {
            "prd_blob_url": values["prd_blob_url"],
            "prd_mr_url": values["prd_mr_url"],
            "message_id": values["message_id"],
            "chat_id": values["chat_id"],
            "thread_id": values["thread_id"],
            "chat_type": values["chat_type"],
            "initiator": values["initiator"],
        }
    return method, {key: value for key, value in values.items() if value is not None}


async def _run(args: argparse.Namespace) -> int:
    method, params = params_for(args)
    request = RpcRequest(id=str(uuid.uuid4()), method=method, params=params)
    response = await rpc_call(Path(args.socket), request)
    if not response.ok:
        print(response.error or "controller request failed", file=sys.stderr)
        return 1
    print(json.dumps(response.result or {}, ensure_ascii=False, indent=2))
    if method == "health" and not (response.result or {}).get("ok", False):
        return 1
    return 0


def main() -> int:
    return asyncio.run(_run(build_parser().parse_args()))


if __name__ == "__main__":
    sys.exit(main())
