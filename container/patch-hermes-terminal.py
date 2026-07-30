#!/usr/bin/env python3
"""Apply the Hollysys terminal Kanban patch to one pinned Hermes source tree."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

EXPECTED = {
    "agent/tool_executor.py": (
        "1036cd27e23e17f8d8ede3a2bf812965a31675d444e0a34a53dea8aeae235e26"
    ),
    "agent/conversation_loop.py": (
        "c9111ed90d038299f31848e97de1501ad06915c9459b4437934c79956119231b"
    ),
}
TERMINAL_TOOLS = frozenset({"kanban_complete", "kanban_block"})


def replace_once(text: str, old: str, new: str, *, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one patch anchor, found {count}")
    return text.replace(old, new)


def patch_executor(text: str) -> str:
    text = replace_once(
        text,
        "logger = logging.getLogger(__name__)\n\n\n"
        "def _budget_for_agent(agent) -> BudgetConfig:\n",
        "logger = logging.getLogger(__name__)\n\n"
        "_HOLLYSYS_TERMINAL_TOOLS = frozenset("
        '{"kanban_complete", "kanban_block"})\n\n\n'
        "def _hollysys_terminal_success(function_name: str, result: Any) -> bool:\n"
        "    if function_name not in _HOLLYSYS_TERMINAL_TOOLS:\n"
        "        return False\n"
        "    failed, _ = _detect_tool_failure(function_name, result)\n"
        "    return not failed\n\n\n"
        "def _budget_for_agent(agent) -> BudgetConfig:\n",
        label="executor helper",
    )
    text = replace_once(
        text,
        "    tool_calls = assistant_message.tool_calls\n"
        "    num_tools = len(tool_calls)\n",
        "    tool_calls = assistant_message.tool_calls\n"
        "    if any(\n"
        "        tc.function.name in _HOLLYSYS_TERMINAL_TOOLS\n"
        "        for tc in tool_calls\n"
        "    ):\n"
        "        return execute_tool_calls_sequential(\n"
        "            agent, assistant_message, messages, effective_task_id,\n"
        "            api_call_count, finalize=finalize,\n"
        "        )\n"
        "    num_tools = len(tool_calls)\n",
        label="concurrent terminal barrier",
    )
    text = replace_once(
        text,
        "        _flush_session_db_after_tool_progress(\n"
        "            agent,\n"
        "            messages,\n"
        "            stage=f\"tool result {function_name}\",\n"
        "        )\n\n"
        "        # ── Per-tool /steer drain ───────────────────────────────────\n",
        "        _flush_session_db_after_tool_progress(\n"
        "            agent,\n"
        "            messages,\n"
        "            stage=f\"tool result {function_name}\",\n"
        "        )\n\n"
        "        if (\n"
        "            not _execution_blocked\n"
        "            and _hollysys_terminal_success(function_name, function_result)\n"
        "        ):\n"
        "            agent._hollysys_terminal_tool = function_name\n"
        "            if agent.tool_progress_callback:\n"
        "                try:\n"
        "                    agent.tool_progress_callback(\n"
        "                        \"worker_exited\", function_name, None, None,\n"
        "                        duration=tool_duration, is_error=False,\n"
        "                    )\n"
        "                except Exception as cb_err:\n"
        "                    logging.debug(\n"
        "                        \"worker exit callback error: %s\", cb_err\n"
        "                    )\n"
        "            for skipped_tc in assistant_message.tool_calls[i:]:\n"
        "                skipped_name = skipped_tc.function.name\n"
        "                messages.append(make_tool_result_message(\n"
        "                    skipped_name,\n"
        "                    \"[Tool execution skipped — a successful terminal \"\n"
        "                    \"Kanban tool ended this worker turn]\",\n"
        "                    skipped_tc.id,\n"
        "                    effect_disposition=\"none\",\n"
        "                ))\n"
        "                _flush_session_db_after_tool_progress(\n"
        "                    agent, messages,\n"
        "                    stage=f\"terminal skipped tool result {skipped_name}\",\n"
        "                )\n"
        "            break\n\n"
        "        # ── Per-tool /steer drain ───────────────────────────────────\n",
        label="sequential terminal stop",
    )
    return text


def patch_loop(text: str) -> str:
    return replace_once(
        text,
        "                agent._execute_tool_calls("
        "assistant_message, messages, effective_task_id, api_call_count)\n\n"
        "                if agent._tool_guardrail_halt_decision is not None:\n",
        "                agent._execute_tool_calls("
        "assistant_message, messages, effective_task_id, api_call_count)\n\n"
        "                if getattr(agent, \"_hollysys_terminal_tool\", None):\n"
        "                    _turn_exit_reason = \"hollysys_terminal_tool\"\n"
        "                    final_response = \"\"\n"
        "                    agent._emit_status(\n"
        "                        \"worker_exited terminal_tool=\"\n"
        "                        + str(agent._hollysys_terminal_tool)\n"
        "                    )\n"
        "                    break\n\n"
        "                if agent._tool_guardrail_halt_decision is not None:\n",
        label="conversation terminal exit",
    )


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: patch-hermes-terminal.py HERMES_SOURCE_ROOT")
    root = Path(sys.argv[1]).resolve()
    patches = {
        "agent/tool_executor.py": patch_executor,
        "agent/conversation_loop.py": patch_loop,
    }
    for relative, patcher in patches.items():
        path = root / relative
        original = path.read_bytes()
        digest = hashlib.sha256(original).hexdigest()
        if digest != EXPECTED[relative]:
            raise RuntimeError(
                f"{relative}: source fingerprint {digest} does not match "
                f"pinned v2026.7.20 fingerprint {EXPECTED[relative]}"
            )
        patched = patcher(original.decode("utf-8"))
        path.write_text(patched, encoding="utf-8")
        compile(patched, str(path), "exec")
        print(
            f"patched {relative} "
            f"sha256={hashlib.sha256(patched.encode()).hexdigest()}"
        )


if __name__ == "__main__":
    main()
