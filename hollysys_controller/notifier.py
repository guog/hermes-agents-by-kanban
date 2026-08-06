from __future__ import annotations

import hashlib
import json
import os
import subprocess

from .config import ControllerConfig
from .errors import DependencyContractError, ErrorContext
from .kanban import CommandError
from .messages import MessageFormat
from .models import FeishuOrigin


class LarkNotifier:
    def __init__(self, config: ControllerConfig):
        self.config = config

    def send(
        self,
        key: str,
        origin: FeishuOrigin,
        content: str,
        message_format: MessageFormat = "markdown",
    ) -> dict:
        if message_format not in {"text", "markdown"}:
            raise ValueError(f"unsupported Feishu message format: {message_format}")
        env = os.environ.copy()
        env.update(
            {
                "LARKSUITE_CLI_CONFIG_DIR": str(
                    self.config.profiles_root / "dispatcher" / ".lark-cli" / "config"
                ),
                "LARKSUITE_CLI_DATA_DIR": str(
                    self.config.profiles_root / "dispatcher" / ".lark-cli" / "data"
                ),
                "LARKSUITE_CLI_NO_UPDATE_NOTIFIER": "1",
                "LARKSUITE_CLI_NO_SKILLS_NOTIFIER": "1",
            }
        )
        idempotency = "hollysys-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:40]
        command = [
            self.config.lark_command,
            "im",
            "+messages-reply",
            "--message-id",
            origin.message_id,
            f"--{message_format}",
            content,
            "--as",
            "bot",
            "--idempotency-key",
            idempotency,
        ]
        if origin.thread_id:
            command.append("--reply-in-thread")
        result = subprocess.run(
            command,
            env=env,
            text=True,
            capture_output=True,
            timeout=self.config.command_timeout_seconds,
            check=False,
        )
        if result.returncode != 0:
            try:
                failure = json.loads(result.stderr)
            except (json.JSONDecodeError, TypeError):
                failure = None
            error = (
                failure.get("error")
                if isinstance(failure, dict)
                else None
            )
            if isinstance(error, dict) and error.get("code") == 230002:
                raise DependencyContractError(
                    "feishu_bot_not_in_origin_chat",
                    context=ErrorContext(
                        dependency="feishu",
                        endpoint="messages-reply",
                        status_code=400,
                        error_code="bot_not_in_chat",
                    ),
                )
            if isinstance(error, dict) and error.get("code") == 99992354:
                raise DependencyContractError(
                    "feishu_origin_message_not_found",
                    context=ErrorContext(
                        dependency="feishu",
                        endpoint="messages-reply",
                        status_code=400,
                        error_code="origin_message_not_found",
                    ),
                )
            raise CommandError(command, result.returncode, result.stderr)
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            payload = {"stdout": result.stdout.strip()}
        return payload if isinstance(payload, dict) else {"result": payload}
