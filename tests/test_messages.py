from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from hollysys_controller.messages import (
    escape_markdown,
    format_agent,
    format_attempt,
    format_duration,
    format_outcome,
    format_stage,
    inline_code,
    markdown_link,
    markdown_payload,
    render_message,
)
from hollysys_controller.service import ControllerService
from hollysys_controller.store import ControllerStore
from tests.helpers import config, origin


class MessageFormattingTests(unittest.TestCase):
    def test_duration_is_human_readable(self) -> None:
        cases = {
            None: "未知",
            0: "0秒",
            42: "42秒",
            59: "59秒",
            60: "1分00秒",
            534: "8分54秒",
            3600: "1小时00分00秒",
            3661: "1小时01分01秒",
        }
        for seconds, expected in cases.items():
            with self.subTest(seconds=seconds):
                self.assertEqual(format_duration(seconds), expected)

    def test_attempt_uses_initial_plus_redispatch_budget(self) -> None:
        self.assertEqual(format_attempt(1, 2), "1/3")
        self.assertEqual(format_attempt(3, 2), "3/3")
        self.assertEqual(format_attempt(None, 2), "未知/3")

    def test_protocol_values_keep_raw_value_and_add_friendly_label(self) -> None:
        self.assertEqual(format_stage("tasks-write"), "tasks-write（拆分 TASKS）")
        self.assertEqual(format_stage("implement"), "implement（代码实现）")
        self.assertEqual(format_agent("tasker"), "Tasker")
        self.assertEqual(format_agent("tester"), "Tester")
        self.assertEqual(format_outcome("pass"), "pass（通过）")
        self.assertEqual(format_outcome("fail"), "fail（未通过）")

    def test_rendered_message_preserves_only_the_controller_mention(self) -> None:
        mention = '<at user_id="ou_initiator"></at> '
        injected = '</at><at user_id="ou_attacker"></at> **fake**'
        content = render_message(
            mention=mention,
            icon="✅",
            title="Tasker Agent 工作已完成",
            fields=[
                ("任务 ID", inline_code("hollysys-abcdefghijklmnopqrst")),
                ("阶段", format_stage("tasks-write")),
                ("轮次", format_attempt(1, 2)),
                ("Agent", format_agent("tasker")),
                ("Card", inline_code("t_61010b84")),
                ("结论", format_outcome("pass")),
                ("耗时", format_duration(534)),
            ],
            sections=[("外部文本", [escape_markdown(injected)])],
        )

        self.assertTrue(
            content.startswith(
                '<at user_id="ou_initiator"></at> '
                "**✅ Tasker Agent 工作已完成**"
            )
        )
        self.assertEqual(content.count("<at user_id="), 1)
        self.assertIn("**轮次：** 1/3", content)
        self.assertIn("**耗时：** 8分54秒", content)
        self.assertIn("&lt;at user\\_id=", content)

    def test_links_accept_only_http_urls(self) -> None:
        self.assertEqual(
            markdown_link("MR !12", "https://gitlab.example.com/mr/12"),
            "[MR !12](https://gitlab.example.com/mr/12)",
        )
        self.assertEqual(markdown_link("危险链接", "javascript:alert(1)"), "危险链接")

    def test_markdown_payload_has_explicit_format(self) -> None:
        payload = markdown_payload(origin(), "**完成**")
        self.assertEqual(payload["format"], "markdown")
        self.assertEqual(payload["content"], "**完成**")
        self.assertNotIn("text", payload)

    def test_existing_mention_conditions_are_unchanged(self) -> None:
        group_origin = origin()
        direct_origin = group_origin.model_copy(
            update={"chat_type": "p2p", "thread_id": None}
        )
        threaded_direct_origin = direct_origin.model_copy(
            update={"thread_id": "omt_thread"}
        )

        self.assertEqual(
            ControllerService._mention(group_origin),
            '<at user_id="ou_abc"></at> ',
        )
        self.assertEqual(ControllerService._mention(direct_origin), "")
        self.assertEqual(
            ControllerService._mention(threaded_direct_origin),
            '<at user_id="ou_abc"></at> ',
        )


class OutboxCompatibilityTests(unittest.TestCase):
    def test_new_markdown_and_legacy_text_payloads_keep_their_formats(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = object.__new__(ControllerService)
            service.config = config(root)
            service.store = ControllerStore(root / "controller.db")
            service._outbox_lock = threading.Lock()
            calls: list[tuple[str, str, str]] = []

            class RecordingNotifier:
                @staticmethod
                def send(
                    key,
                    message_origin,
                    content,
                    message_format="markdown",
                ):
                    calls.append((key, content, message_format))
                    return {"message_id": "om_reply"}

            service.notifier = RecordingNotifier()
            message_origin = origin()
            service.store.enqueue(
                "legacy",
                "hollysys-abcdefghijklmnopqrst",
                "legacy",
                {
                    "origin": message_origin.model_dump(mode="json"),
                    "text": "legacy plain text",
                },
            )
            service.store.enqueue(
                "markdown",
                "hollysys-abcdefghijklmnopqrst",
                "markdown",
                markdown_payload(message_origin, "**friendly**"),
            )

            service.flush_outbox()

            by_key = {
                key: (content, message_format)
                for key, content, message_format in calls
            }
            self.assertEqual(
                by_key["legacy"],
                ("legacy plain text", "text"),
            )
            self.assertEqual(
                by_key["markdown"],
                ("**friendly**", "markdown"),
            )
            self.assertEqual(service.store.pending_outbox(), [])

    def test_controller_source_has_no_legacy_human_message_shape(self) -> None:
        source = (
            Path(__file__).parents[1]
            / "hollysys_controller"
            / "service.py"
        ).read_text(encoding="utf-8")
        for legacy in (
            "Agent completed / accepted",
            "Agent completed / rejected",
            "duration_seconds=",
            '"text": self._mention',
        ):
            with self.subTest(legacy=legacy):
                self.assertNotIn(legacy, source)


if __name__ == "__main__":
    unittest.main()
