from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
                notifier.send("run:spec:frozen:digest", origin(), "retry")

        first_key = calls[0][calls[0].index("--idempotency-key") + 1]
        second_key = calls[1][calls[1].index("--idempotency-key") + 1]
        self.assertEqual(first_key, second_key)
        self.assertEqual(
            calls[0][calls[0].index("--message-id") + 1],
            origin().message_id,
        )
        self.assertIn("--reply-in-thread", calls[0])


if __name__ == "__main__":
    unittest.main()
