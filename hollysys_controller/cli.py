from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

from .config import ControllerConfig
from .models import RpcRequest
from .rpc import rpc_call
from .service import ControllerService


def controller_snapshot_config(config: ControllerConfig) -> ControllerConfig:
    """Use the Controller data root even when an Agent has a profile home."""
    return config.model_copy(
        update={"hermes_home": config.state_dir.parent},
    )


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

    status_summary = sub.add_parser("status-summary")
    status_summary.add_argument("--run-key", required=True)

    resolve = sub.add_parser("resolve")
    resolve.add_argument("--run-key", required=True)
    resolve.add_argument("--card-id", required=True)
    resolve.add_argument("--block-id", required=True)
    resolve.add_argument("--message-id", required=True)
    resolve.add_argument("--sender", required=True)
    resolve.add_argument("--chat-id", required=True)
    resolve.add_argument("--thread-id")
    resolve.add_argument("--answer", required=True)

    abort_request = sub.add_parser("abort-request")
    abort_request.add_argument("--run-key", required=True)
    abort_request.add_argument("--message-id", required=True)
    abort_request.add_argument("--sender", required=True)
    abort_request.add_argument("--chat-id", required=True)
    abort_request.add_argument("--thread-id")
    abort_request.add_argument("--reason", required=True)

    abort_confirm = sub.add_parser("abort-confirm")
    abort_confirm.add_argument("--run-key", required=True)
    abort_confirm.add_argument("--token", required=True)
    abort_confirm.add_argument("--message-id", required=True)
    abort_confirm.add_argument("--sender", required=True)
    abort_confirm.add_argument("--chat-id", required=True)
    abort_confirm.add_argument("--thread-id")

    recover = sub.add_parser("recover")
    recover.add_argument("--run-key", required=True)
    recover.add_argument("--message-id", required=True)
    recover.add_argument("--sender", required=True)
    recover.add_argument("--chat-id", required=True)
    recover.add_argument("--thread-id")
    recover.add_argument("--reason", required=True)

    validate_completion = sub.add_parser("validate-completion")
    validate_completion.add_argument("--card-id", required=True)
    validate_completion.add_argument("--metadata", required=True, type=Path)

    publish_delivery = sub.add_parser("publish-delivery")
    publish_delivery.add_argument("--card-id", required=True)
    publish_delivery.add_argument("--head-sha", required=True)
    publish_delivery.add_argument(
        "--description-file",
        required=True,
        type=Path,
    )

    card_context = sub.add_parser("card-context")
    card_context.add_argument("--card-id", required=True)

    completion_template = sub.add_parser("completion-template")
    completion_template.add_argument("--card-id", required=True)
    completion_template.add_argument(
        "--outcome",
        required=True,
        choices=["pass", "fail", "cancelled"],
    )

    validate_artifact = sub.add_parser("validate-artifact")
    validate_artifact.add_argument("--card-id", required=True)

    preflight = sub.add_parser("preflight")
    preflight.add_argument(
        "--deep",
        action="store_true",
        help="also verify isolated Profile API and repository access",
    )
    health = sub.add_parser("health")
    health.add_argument(
        "--probe",
        choices=["liveness", "readiness"],
        default="readiness",
    )
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
    elif method == "validate-completion":
        metadata_path = values.pop("metadata")
        values["metadata"] = json.loads(
            metadata_path.read_text(encoding="utf-8")
        )
    elif method == "publish-delivery":
        description_path = values.pop("description_file")
        if not description_path.is_file():
            raise ValueError(
                f"description file does not exist: {description_path}"
            )
        values["description"] = description_path.read_text(encoding="utf-8")
    return method, {key: value for key, value in values.items() if value is not None}


async def _run(args: argparse.Namespace) -> int:
    method, params = params_for(args)
    if method == "preflight":
        config = controller_snapshot_config(ControllerConfig.load())
        result = ControllerService(config).preflight(
            deep=bool(params.get("deep", False))
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 1
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
