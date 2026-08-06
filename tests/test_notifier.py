from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hollysys_controller.errors import DependencyContractError
from hollysys_controller.notifier import LarkNotifier
from tests.helpers import config, origin


class LarkNotifierTests(unittest.TestCase):
    def test_stable_key_and_original_thread_are_reused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            notifier = LarkNotifier(config(Path(tmp)))
            calls: list[list[str]] = []

            def record(command, **kwargs):
                calls.append(command)
                return subprocess.CompletedProcess(
                    command, 0, stdout='{"message_id":"om_reply"}', stderr=""
                )

            with patch("hollysys_controller.notifier.subprocess.run", record):
                notifier.send("run:spec:frozen:digest", origin(), "first")
                notifier.send(
                    "run:spec:frozen:digest",
                    origin(),
                    "retry",
                    message_format="text",
                )

        first_key = calls[0][calls[0].index("--idempotency-key") + 1]
        second_key = calls[1][calls[1].index("--idempotency-key") + 1]
        self.assertEqual(first_key, second_key)
        self.assertEqual(
            calls[0][calls[0].index("--message-id") + 1],
            origin().message_id,
        )
        self.assertEqual(
            calls[0][calls[0].index("--markdown") + 1],
            "first",
        )
        self.assertEqual(
            calls[1][calls[1].index("--text") + 1],
            "retry",
        )
        self.assertNotIn("--text", calls[0])
        self.assertNotIn("--markdown", calls[1])
        self.assertIn("--as", calls[0])
        self.assertEqual(calls[0][calls[0].index("--as") + 1], "bot")
        self.assertIn("--reply-in-thread", calls[0])

    def test_unknown_message_format_is_rejected_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            notifier = LarkNotifier(config(Path(tmp)))
            with self.assertRaisesRegex(
                ValueError,
                "unsupported Feishu message format",
            ):
                notifier.send(
                    "run:event",
                    origin(),
                    "content",
                    message_format="interactive",  # type: ignore[arg-type]
                )

    def test_bot_not_in_chat_is_a_sanitized_contract_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            notifier = LarkNotifier(config(Path(tmp)))

            def rejected(command, **kwargs):
                del kwargs
                return subprocess.CompletedProcess(
                    command,
                    1,
                    stdout="",
                    stderr=(
                        '{"ok":false,"error":{"code":230002,'
                        '"message":"Bot/User can NOT be out of the chat."}}'
                    ),
                )

            with patch(
                "hollysys_controller.notifier.subprocess.run",
                rejected,
            ), self.assertRaisesRegex(
                DependencyContractError,
                "feishu_bot_not_in_origin_chat",
            ) as raised:
                notifier.send(
                    "run:exception",
                    origin(),
                    "sensitive notification content",
                )

        self.assertEqual(
            raised.exception.context.error_code,
            "bot_not_in_chat",
        )
        self.assertNotIn(
            "sensitive notification content",
            str(raised.exception),
        )

    def test_missing_origin_message_is_a_sanitized_contract_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            notifier = LarkNotifier(config(Path(tmp)))

            def rejected(command, **kwargs):
                del kwargs
                return subprocess.CompletedProcess(
                    command,
                    1,
                    stdout="",
                    stderr=(
                        '{"ok":false,"error":{"code":99992354,'
                        '"message":"these ids not existed"}}'
                    ),
                )

            with patch(
                "hollysys_controller.notifier.subprocess.run",
                rejected,
            ), self.assertRaisesRegex(
                DependencyContractError,
                "feishu_origin_message_not_found",
            ) as raised:
                notifier.send(
                    "run:exception",
                    origin(),
                    "sensitive notification content",
                )

        self.assertEqual(
            raised.exception.context.error_code,
            "origin_message_not_found",
        )
        self.assertNotIn(
            "sensitive notification content",
            str(raised.exception),
        )


if __name__ == "__main__":
    unittest.main()
