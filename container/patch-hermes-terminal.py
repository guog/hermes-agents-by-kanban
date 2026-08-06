#!/usr/bin/env python3
"""Apply Hollysys Kanban runtime patches to one pinned Hermes source tree."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

EXPECTED = {
    "agent/tool_executor.py": (
        "6201f54c6b7ebdf455bd468687bd447a7f352ccf64f0fcd503c5608d9170a4e1"
    ),
    "agent/conversation_loop.py": (
        "316e07a67ddb321a317bc9f2727bc24b144c3fa242da7f560ca746c1281d0529"
    ),
    "agent/codex_runtime.py": (
        "246b73dc6b1b035d7071cd53efe3116affcf013f818661bc89009208be57e3a9"
    ),
    "agent/system_prompt.py": (
        "b19e30b1bbc0cb46b5344be81e2000a7fade00ee7fe2331ca2328458d3df5e94"
    ),
    "agent/turn_finalizer.py": (
        "b9b48011dc1c226ca4c8e2890919b748922c2379334b4ab45c18e11d45946e72"
    ),
    "cli.py": (
        "f91980953205e15bec824085736708cdf4082674169811a567132385ccf4c544"
    ),
    "gateway/run.py": (
        "223cabed56396b163b57d13258f83070959231d4632bddce14c468ea3b212c18"
    ),
    "hermes_cli/kanban_db.py": (
        "f96e9f76fb505c6e4c6c1a9534e3aa79584e9eb9009aa5ab2c741fbd0a43fe69"
    ),
    "hermes_cli/kanban.py": (
        "9ad3d2dfa659dc1c7786e7da758c81e8b763dc51f5b4143512cf483bf1d212ad"
    ),
    "scripts/docker_config_migrate.py": (
        "62fdbd91cad654c0ce6277527ef2c25184d62f5dfca09025d670135baacb1fc6"
    ),
    "tools/delegate_tool.py": (
        "9c537dd695d990c27e7a7cec51267ed9167fbe6bb752cdb803d43880d50a1cff"
    ),
    "tools/delegation_live_log.py": (
        "5437fa2adaf4890b25250add92d87b7fe869b21458a7f6df2ebaaba5205c27b2"
    ),
    "tools/kanban_tools.py": (
        "a2244b040d8d8399d42f8509f86aea0ccd426751234ae6fc06b42867e53fbb34"
    ),
    "tools/tool_result_storage.py": (
        "b1cd165327019277199e52dbbb4fb6b308bf5dbee557070dcfdef06a27507784"
    ),
}
TERMINAL_TOOLS = frozenset({"kanban_complete", "kanban_block"})


def replace_once(text: str, old: str, new: str, *, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one patch anchor, found {count}")
    return text.replace(old, new)


def replace_line_once(text: str, old: str, new: str, *, label: str) -> str:
    lines = text.splitlines(keepends=True)
    matches = [index for index, line in enumerate(lines) if line == old]
    if len(matches) != 1:
        raise RuntimeError(
            f"{label}: expected one exact line anchor, found {len(matches)}"
        )
    lines[matches[0]] = new
    return "".join(lines)


def replace_block(
    text: str,
    start: str,
    end: str,
    replacement: str,
    *,
    label: str,
) -> str:
    if text.count(start) != 1 or text.count(end) != 1:
        raise RuntimeError(f"{label}: block anchors are not unique")
    start_index = text.index(start)
    end_index = text.index(end, start_index)
    return text[:start_index] + replacement + text[end_index:]


def patch_executor(text: str) -> str:
    text = replace_once(
        text,
        "logger = logging.getLogger(__name__)\n\n\n"
        "def _ensure_file_checkpoint(\n",
        "logger = logging.getLogger(__name__)\n\n"
        "_HOLLYSYS_TERMINAL_TOOLS = frozenset("
        '{"kanban_complete", "kanban_block"})\n\n\n'
        "def _hollysys_attempt_is_current() -> bool:\n"
        "    task_id = str(os.environ.get(\"HERMES_KANBAN_TASK\") or \"\").strip()\n"
        "    if not task_id:\n"
        "        return True\n"
        "    try:\n"
        "        run_id = int(os.environ.get(\"HERMES_KANBAN_RUN_ID\", \"\"))\n"
        "    except (TypeError, ValueError):\n"
        "        return False\n"
        "    try:\n"
        "        from hermes_cli import kanban_db as _hollysys_kb\n"
        "        board = str(\n"
        "            os.environ.get(\"HERMES_KANBAN_BOARD\") or \"\"\n"
        "        ).strip() or None\n"
        "        conn = _hollysys_kb.connect(board=board)\n"
        "        try:\n"
        "            row = conn.execute(\n"
        "                \"SELECT status, current_run_id, worker_pid \"\n"
        "                \"FROM tasks WHERE id = ?\", (task_id,),\n"
        "            ).fetchone()\n"
        "        finally:\n"
        "            conn.close()\n"
        "    except Exception:\n"
        "        logger.warning(\n"
        "            \"attempt fence could not verify task %s run %s\",\n"
        "            task_id, run_id, exc_info=True,\n"
        "        )\n"
        "        return False\n"
        "    return bool(\n"
        "        row\n"
        "        and row[\"status\"] == \"running\"\n"
        "        and row[\"current_run_id\"] is not None\n"
        "        and int(row[\"current_run_id\"]) == run_id\n"
        "        and row[\"worker_pid\"] is not None\n"
        "        and int(row[\"worker_pid\"]) == os.getpid()\n"
        "    )\n\n\n"
        "def _hollysys_terminal_success(function_name: str, result: Any) -> bool:\n"
        "    if function_name not in _HOLLYSYS_TERMINAL_TOOLS:\n"
        "        return False\n"
        "    failed, _ = _detect_tool_failure(function_name, result)\n"
        "    return not failed\n\n\n"
        "def _ensure_file_checkpoint(\n",
        label="executor helper",
    )
    text = replace_once(
        text,
        "        if function_name == \"memory\":\n"
        "            agent._turns_since_memory = 0\n"
        "        elif function_name == \"skill_manage\":\n"
        "            agent._iters_since_skill = 0\n"
        "\n"
        "        _advance_start_order(_begin)\n"
        "        return execute(final_args)\n",
        "        if not _hollysys_attempt_is_current():\n"
        "            _advance_start_order()\n"
        "            state[\"blocked\"] = True\n"
        "            agent._hollysys_stale_attempt = True\n"
        "            result = json.dumps({\n"
        "                \"error\": \"stale_attempt\",\n"
        "                \"message\": (\n"
        "                    \"this worker no longer owns the current Kanban \"\n"
        "                    \"attempt; no tool was executed\"\n"
        "                ),\n"
        "            }, ensure_ascii=False)\n"
        "            _emit_terminal_post_tool_call(\n"
        "                agent,\n"
        "                function_name=function_name,\n"
        "                function_args=final_args,\n"
        "                result=result,\n"
        "                effective_task_id=effective_task_id,\n"
        "                tool_call_id=tool_call_id,\n"
        "                status=\"blocked\",\n"
        "                error_type=\"stale_attempt\",\n"
        "                error_message=\"stale_attempt\",\n"
        "                middleware_trace=list(state[\"middleware_trace\"]),\n"
        "            )\n"
        "            return result\n"
        "\n"
        "        if function_name == \"memory\":\n"
        "            agent._turns_since_memory = 0\n"
        "        elif function_name == \"skill_manage\":\n"
        "            agent._iters_since_skill = 0\n"
        "\n"
        "        _advance_start_order(_begin)\n"
        "        return execute(final_args)\n",
        label="executor attempt fence",
    )
    text = replace_once(
        text,
        "        # ── Per-tool /steer drain ───────────────────────────────────\n"
        "        # Drain pending steer BETWEEN individual tool calls so the\n",
        "        if (\n"
        "            not _execution_blocked\n"
        "            and _hollysys_terminal_success(function_name, function_result)\n"
        "        ):\n"
        "            agent._hollysys_terminal_tool = function_name\n"
        "            for skipped_tc in assistant_message.tool_calls[i:]:\n"
        "                skipped_name = skipped_tc.function.name\n"
        "                messages.append(make_tool_result_message(\n"
        "                    skipped_name,\n"
        "                    \"[Tool execution skipped — a successful terminal \"\n"
        "                    \"Kanban tool ended this worker turn]\",\n"
        "                    skipped_tc.id,\n"
        "                    effect_disposition=\"none\",\n"
        "                ))\n"
        "                if not _flush_session_db_after_tool_progress(\n"
        "                    agent, messages,\n"
        "                    stage=f\"terminal skipped tool result {skipped_name}\",\n"
        "                ):\n"
        "                    return\n"
        "            break\n\n"
        "        # ── Per-tool /steer drain ───────────────────────────────────\n"
        "        # Drain pending steer BETWEEN individual tool calls so the\n",
        label="sequential terminal stop",
    )
    text = replace_once(
        text,
        '        if function_name == "todo":\n',
        "        if (\n"
        "            function_name in _HOLLYSYS_TERMINAL_TOOLS\n"
        "            and getattr(agent, \"_parent_session_id\", None)\n"
        "        ):\n"
        "            function_result = json.dumps({\n"
        "                \"error\": (\n"
        "                    \"delegated subagents cannot complete or block the \"\n"
        "                    \"parent Kanban task; return findings to the parent\"\n"
        "                )\n"
        "            }, ensure_ascii=False)\n"
        "            tool_duration = time.time() - tool_start_time\n"
        '        elif function_name == "todo":\n',
        label="delegated terminal runtime guard",
    )
    text = replace_once(
        text,
        "    for kind, calls in segments:\n",
        "    for segment_index, (kind, calls) in enumerate(segments):\n",
        label="segmented terminal index",
    )
    text = replace_once(
        text,
        "        if getattr(agent, \"_incremental_persistence_failed\", False):\n"
        "            return\n\n"
        "    # ── Whole-turn finalize (budget + /steer) ─────────────────────────\n",
        "        if getattr(agent, \"_incremental_persistence_failed\", False):\n"
        "            return\n\n"
        "        if getattr(agent, \"_hollysys_terminal_tool\", None):\n"
        "            for _, remaining_calls in segments[segment_index + 1:]:\n"
        "                for skipped_tc in remaining_calls:\n"
        "                    skipped_name = skipped_tc.function.name\n"
        "                    messages.append(make_tool_result_message(\n"
        "                        skipped_name,\n"
        "                        \"[Tool execution skipped — a successful terminal \"\n"
        "                        \"Kanban tool ended this worker turn]\",\n"
        "                        skipped_tc.id,\n"
        "                        effect_disposition=\"none\",\n"
        "                    ))\n"
        "                    if not _flush_session_db_after_tool_progress(\n"
        "                        agent, messages,\n"
        "                        stage=(\n"
        "                            \"terminal skipped tool result \" + skipped_name\n"
        "                        ),\n"
        "                    ):\n"
        "                        return\n"
        "            break\n\n"
        "    # ── Whole-turn finalize (budget + /steer) ─────────────────────────\n",
        label="segmented terminal stop",
    )
    return text


def patch_executor_progress(text: str) -> str:
    text = replace_once(
        text,
        "logger = logging.getLogger(__name__)\n"
        "\n",
        "logger = logging.getLogger(__name__)\n"
        "\n"
        "_HOLLYSYS_PROGRESS_INTERVAL_SECONDS = 300\n"
        "\n"
        "\n"
        "def _hollysys_tool_category(function_name: str) -> str:\n"
        "    name = function_name.lower()\n"
        "    if name == \"delegate_task\":\n"
        "        return \"delegation\"\n"
        "    if name in {\"kanban_complete\", \"kanban_block\"}:\n"
        "        return \"terminal\"\n"
        "    if \"search\" in name:\n"
        "        return \"search\"\n"
        "    if name.startswith(\"read\") or name in {\"view_image\", \"browser\"}:\n"
        "        return \"read\"\n"
        "    if name in {\"patch\", \"write_file\", \"edit_file\"}:\n"
        "        return \"patch\"\n"
        "    if name in {\"git\", \"github\", \"gitlab\"}:\n"
        "        return \"git\"\n"
        "    return \"tool\"\n"
        "\n"
        "\n"
        "def _hollysys_record_progress(\n"
        "    agent, function_name: str, duration: float, blocked: bool\n"
        ") -> None:\n"
        "    if blocked or getattr(agent, \"_parent_session_id\", None):\n"
        "        return\n"
        "    task_id = str(os.environ.get(\"HERMES_KANBAN_TASK\") or \"\").strip()\n"
        "    board = str(os.environ.get(\"HERMES_KANBAN_BOARD\") or \"\").strip()\n"
        "    try:\n"
        "        run_id = int(os.environ.get(\"HERMES_KANBAN_RUN_ID\", \"\"))\n"
        "    except (TypeError, ValueError):\n"
        "        return\n"
        "    if not task_id:\n"
        "        return\n"
        "    category = _hollysys_tool_category(function_name)\n"
        "    counts = getattr(agent, \"_hollysys_tool_category_counts\", None)\n"
        "    if not isinstance(counts, dict):\n"
        "        counts = {}\n"
        "        agent._hollysys_tool_category_counts = counts\n"
        "    counts[category] = int(counts.get(category, 0)) + 1\n"
        "    metrics = getattr(agent, \"_hollysys_runtime_metrics\", None)\n"
        "    if not isinstance(metrics, dict):\n"
        "        metrics = {}\n"
        "        agent._hollysys_runtime_metrics = metrics\n"
        "    metric = \"delegation_wait\" if category == \"delegation\" else \"tool_execution\"\n"
        "    metrics[metric] = float(metrics.get(metric, 0.0)) + max(0.0, duration)\n"
        "    now = time.monotonic()\n"
        "    last = float(getattr(agent, \"_hollysys_progress_last_at\", 0.0) or 0.0)\n"
        "    if last and now - last < _HOLLYSYS_PROGRESS_INTERVAL_SECONDS:\n"
        "        return\n"
        "    payload = {\n"
        "        \"tool_categories\": dict(sorted(counts.items())),\n"
        "        \"tool_count\": int(sum(counts.values())),\n"
        "        \"elapsed_seconds\": int(sum(float(v) for v in metrics.values())),\n"
        "        \"run_id\": run_id,\n"
        "        \"session\": f\"kanban-run:{run_id}\",\n"
        "        \"metrics\": {\n"
        "            key: round(float(metrics.get(key, 0.0)), 3)\n"
        "            for key in (\n"
        "                \"model_wait\", \"tool_execution\",\n"
        "                \"delegation_wait\", \"retry_wait\",\n"
        "            )\n"
        "        },\n"
        "    }\n"
        "    try:\n"
        "        from hermes_cli import kanban_db as _hollysys_kb\n"
        "        conn = _hollysys_kb.connect(board=board or None)\n"
        "        try:\n"
        "            recorded = _hollysys_kb.progress_worker(\n"
        "                conn, task_id, payload=payload, expected_run_id=run_id\n"
        "            )\n"
        "        finally:\n"
        "            conn.close()\n"
        "        if recorded:\n"
        "            agent._hollysys_progress_last_at = now\n"
        "    except Exception:\n"
        "        logger.warning(\n"
        "            \"failed to record structured progress for task %s run %s\",\n"
        "            task_id, run_id, exc_info=True,\n"
        "        )\n"
        "\n",
        label="structured tool progress helper",
    )
    text = replace_once(
        text,
        "        # ── Per-tool /steer drain ───────────────────────────────────\n"
        "        # Same as the sequential path: drain between each collected\n",
        "        _hollysys_record_progress(agent, name, tool_duration, blocked)\n"
        "\n"
        "        # ── Per-tool /steer drain ───────────────────────────────────\n"
        "        # Same as the sequential path: drain between each collected\n",
        label="concurrent tool progress event",
    )
    return replace_once(
        text,
        "        # ── Per-tool /steer drain ───────────────────────────────────\n"
        "        # Drain pending steer BETWEEN individual tool calls so the\n",
        "        _hollysys_record_progress(\n"
        "            agent, function_name, tool_duration, _execution_blocked\n"
        "        )\n"
        "\n"
        "        # ── Per-tool /steer drain ───────────────────────────────────\n"
        "        # Drain pending steer BETWEEN individual tool calls so the\n",
        label="sequential tool progress event",
    )


def patch_executor_all(text: str) -> str:
    return patch_executor_progress(patch_executor(text))


def patch_loop(text: str) -> str:
    text = replace_once(
        text,
        "                if agent._tool_guardrail_halt_decision is not None:\n",
        "                if getattr(agent, \"_hollysys_stale_attempt\", False):\n"
        "                    _turn_exit_reason = \"stale_attempt\"\n"
        "                    final_response = \"\"\n"
        "                    agent._emit_status(\"stale_attempt\")\n"
        "                    break\n\n"
        "                if getattr(agent, \"_hollysys_terminal_tool\", None):\n"
        "                    _turn_exit_reason = \"hollysys_terminal_tool\"\n"
        "                    final_response = \"\"\n"
        "                    agent._emit_status(\n"
        "                        \"worker_terminal_tool=\"\n"
        "                        + str(agent._hollysys_terminal_tool)\n"
        "                    )\n"
        "                    break\n\n"
        "                if agent._tool_guardrail_halt_decision is not None:\n",
        label="conversation terminal exit",
    )
    return text


def patch_loop_metrics(text: str) -> str:
    text = replace_once(
        text,
        "logger = logging.getLogger(__name__)\n"
        "\n",
        "logger = logging.getLogger(__name__)\n"
        "\n"
        "\n"
        "def _hollysys_add_runtime_metric(agent: Any, metric: str, elapsed: float) -> None:\n"
        "    if metric not in {\n"
        "        \"model_wait\", \"tool_execution\", \"delegation_wait\", \"retry_wait\"\n"
        "    }:\n"
        "        return\n"
        "    metrics = getattr(agent, \"_hollysys_runtime_metrics\", None)\n"
        "    if not isinstance(metrics, dict):\n"
        "        metrics = {}\n"
        "        agent._hollysys_runtime_metrics = metrics\n"
        "    metrics[metric] = float(metrics.get(metric, 0.0)) + max(0.0, elapsed)\n"
        "\n",
        label="runtime metric helper",
    )
    text = replace_once(
        text,
        "                _redirect_crossed_response = False\n"
        "                try:\n"
        "                    response = run_llm_execution_middleware(\n",
        "                _redirect_crossed_response = False\n"
        "                _model_wait_started = time.monotonic()\n"
        "                try:\n"
        "                    response = run_llm_execution_middleware(\n",
        label="model wait metric start",
    )
    text = replace_once(
        text,
        "                finally:\n"
        "                    if _redirect_lock is not None:\n",
        "                finally:\n"
        "                    _hollysys_add_runtime_metric(\n"
        "                        agent, \"model_wait\",\n"
        "                        time.monotonic() - _model_wait_started,\n"
        "                    )\n"
        "                    if _redirect_lock is not None:\n",
        label="model wait metric finish",
    )
    for indent, label in (
        ("                    ", "invalid response retry wait metric"),
        ("                ", "API error retry wait metric"),
    ):
        text = replace_once(
            text,
            f"{indent}sleep_end = time.time() + wait_time\n"
            f"{indent}_backoff_touch_counter = 0\n"
            f"{indent}while time.time() < sleep_end:\n",
            f"{indent}_retry_wait_started = time.monotonic()\n"
            f"{indent}sleep_end = time.time() + wait_time\n"
            f"{indent}_backoff_touch_counter = 0\n"
            f"{indent}while time.time() < sleep_end:\n",
            label=label + " start",
        )
        text = replace_line_once(
            text,
            f"{indent}if _retry.restart_with_redirected_messages:\n",
            f"{indent}_hollysys_add_runtime_metric(\n"
            f"{indent}    agent, \"retry_wait\",\n"
            f"{indent}    time.monotonic() - _retry_wait_started,\n"
            f"{indent})\n"
            f"{indent}if _retry.restart_with_redirected_messages:\n",
            label=label + " finish",
        )
    return text


def patch_loop_all(text: str) -> str:
    return patch_loop_metrics(patch_loop(text))


def patch_native_recovery_fencing(text: str) -> str:
    text = replace_once(
        text,
        '        "WHERE status = \'running\' AND claim_expires IS NOT NULL "\n'
        '        "  AND claim_expires < ?",\n',
        '        "WHERE status = \'running\' AND claim_expires IS NOT NULL "\n'
        '        "  AND COALESCE(created_by, \'\') != \'hollysys-controller\' "\n'
        '        "  AND claim_expires < ?",\n',
        label="native stale claim controller fence",
    )
    text = replace_once(
        text,
        '        "WHERE t.status = \'running\' AND t.max_runtime_seconds IS NOT NULL "\n'
        '        "  AND COALESCE(r.started_at, t.started_at) IS NOT NULL "\n'
        '        "  AND t.worker_pid IS NOT NULL"\n',
        '        "WHERE t.status = \'running\' AND t.max_runtime_seconds IS NOT NULL "\n'
        '        "  AND COALESCE(t.created_by, \'\') != \'hollysys-controller\' "\n'
        '        "  AND COALESCE(r.started_at, t.started_at) IS NOT NULL "\n'
        '        "  AND t.worker_pid IS NOT NULL"\n',
        label="native max runtime controller fence",
    )
    text = replace_once(
        text,
        '        "WHERE t.status = \'running\'"\n',
        '        "WHERE t.status = \'running\' "\n'
        '        "  AND COALESCE(t.created_by, \'\') != \'hollysys-controller\'"\n',
        label="native stale running controller fence",
    )
    return replace_once(
        text,
        '            "WHERE status = \'running\' AND worker_pid IS NOT NULL"\n',
        '            "WHERE status = \'running\' AND worker_pid IS NOT NULL "\n'
        '            "AND COALESCE(created_by, \'\') != \'hollysys-controller\'"\n',
        label="native crashed worker controller fence",
    )


def patch_worker_exit_events(text: str) -> str:
    text = replace_once(
        text,
        '_recent_worker_exits: "dict[int, tuple[int, float]]" = {}\n'
        "\n"
        "\n"
        "def _record_worker_exit(pid: int, raw_status: int) -> None:\n",
        '_recent_worker_exits: "dict[int, tuple[int, float]]" = {}\n'
        '_worker_attempts: "dict[int, tuple[Optional[str], str, int]]" = {}\n'
        "\n"
        "\n"
        "def _register_worker_attempt(\n"
        "    pid: int, board: Optional[str], task_id: str, run_id: Optional[int]\n"
        ") -> None:\n"
        "    if pid > 0 and run_id is not None:\n"
        "        _worker_attempts[int(pid)] = (board, task_id, int(run_id))\n"
        "\n"
        "\n"
        "def _record_worker_exit(pid: int, raw_status: int) -> None:\n",
        label="waitpid worker attempt registry",
    )
    text = replace_once(
        text,
        "    now = time.time()\n"
        "    _recent_worker_exits[int(pid)] = (int(raw_status), now)\n",
        "    now = time.time()\n"
        "    _recent_worker_exits[int(pid)] = (int(raw_status), now)\n"
        "    attempt = _worker_attempts.pop(int(pid), None)\n"
        "    if attempt is not None:\n"
        "        board, task_id, run_id = attempt\n"
        "        try:\n"
        "            if os.WIFEXITED(raw_status):\n"
        "                exit_kind = \"exited\"\n"
        "                exit_code = int(os.WEXITSTATUS(raw_status))\n"
        "            elif os.WIFSIGNALED(raw_status):\n"
        "                exit_kind = \"signaled\"\n"
        "                exit_code = int(os.WTERMSIG(raw_status))\n"
        "            else:\n"
        "                exit_kind = \"unknown\"\n"
        "                exit_code = None\n"
        "            exit_conn = connect(board=board)\n"
        "            try:\n"
        "                with write_txn(exit_conn):\n"
        "                    exists = exit_conn.execute(\n"
        "                        \"SELECT 1 FROM tasks WHERE id = ?\", (task_id,),\n"
        "                    ).fetchone()\n"
        "                    if exists:\n"
        "                        _append_event(\n"
        "                            exit_conn, task_id, \"worker_exited\",\n"
        "                            {\n"
        "                                \"pid\": int(pid),\n"
        "                                \"exit_kind\": exit_kind,\n"
        "                                \"exit_code\": exit_code,\n"
        "                            },\n"
        "                            run_id=run_id,\n"
        "                        )\n"
        "            finally:\n"
        "                exit_conn.close()\n"
        "        except Exception:\n"
        "            _log.warning(\n"
        "                \"failed to persist reaped worker exit pid=%s task=%s run=%s\",\n"
        "                pid, task_id, run_id, exc_info=True,\n"
        "            )\n",
        label="waitpid worker exit event",
    )
    spawn_anchor = (
        "            if pid:\n"
        "                _set_worker_pid(conn, claimed.id, int(pid))\n"
    )
    if text.count(spawn_anchor) != 2:
        raise RuntimeError(
            "worker attempt registration: expected two spawn anchors, found "
            f"{text.count(spawn_anchor)}"
        )
    return text.replace(
        spawn_anchor,
        spawn_anchor
        + "                _register_worker_attempt(\n"
        + "                    int(pid), board, claimed.id,\n"
        + "                    _current_run_id(conn, claimed.id),\n"
        + "                )\n",
    )


def patch_worker_run_scratch(text: str) -> str:
    return replace_once(
        text,
        "    if task.tenant:\n"
        "        env[\"HERMES_TENANT\"] = task.tenant\n"
        "    env[\"HERMES_KANBAN_TASK\"] = task.id\n",
        "    if task.tenant:\n"
        "        env[\"HERMES_TENANT\"] = task.tenant\n"
        "    if task.created_by == \"hollysys-controller\":\n"
        "        try:\n"
        "            body = str(task.body or \"\")\n"
        "            markers = (\n"
        "                \"[hollysys-controller-card:v4]\",\n"
        "                \"[hollysys-controller-exception:v4]\",\n"
        "            )\n"
        "            marker_at = max(body.find(marker) for marker in markers)\n"
        "            start = body.find(\"```json\", marker_at)\n"
        "            end = body.rfind(\"```\")\n"
        "            if (\n"
        "                marker_at < 0\n"
        "                or start < 0\n"
        "                or end < 0\n"
        "                or body[end + len(\"```\"):].strip()\n"
        "            ):\n"
        "                raise ValueError(\"invalid controller card body\")\n"
        "            task_payload = json.loads(\n"
        "                body[start + len(\"```json\"):end].strip()\n"
        "            )\n"
        "            candidate = str(task_payload.get(\"scratch_dir\") or \"\")\n"
        "            scratch_root = os.path.realpath(\n"
        "                os.environ.get(\"HERMES_SCRATCH_DIR\", \"/opt/data/scratch\")\n"
        "            )\n"
        "            resolved = os.path.realpath(candidate)\n"
        "            if (\n"
        "                candidate\n"
        "                and resolved != scratch_root\n"
        "                and os.path.commonpath([scratch_root, resolved]) == scratch_root\n"
        "                and os.path.isdir(resolved)\n"
        "            ):\n"
        "                env[\"HERMES_RUN_SCRATCH_DIR\"] = resolved\n"
        "            else:\n"
        "                raise ValueError(\"unsafe controller run scratch\")\n"
        "        except (AttributeError, TypeError, ValueError, OSError) as exc:\n"
        "            raise RuntimeError(\n"
        "                f\"controller task {task.id} has no safe run scratch\"\n"
        "            ) from exc\n"
        "    env[\"HERMES_KANBAN_TASK\"] = task.id\n",
        label="attempt run scratch environment",
    )


def patch_kanban_cli(text: str) -> str:
    text = replace_once(
        text,
        '    p_reclaim.add_argument("task_id")\n'
        "    p_reclaim.add_argument(\n"
        '        "--reason", default=None,\n'
        '        help="Human-readable reason (recorded on the reclaimed event)",\n'
        "    )\n",
        '    p_reclaim.add_argument("task_id")\n'
        "    p_reclaim.add_argument(\n"
        '        "--reason", default=None,\n'
        '        help="Human-readable reason (recorded on the reclaimed event)",\n'
        "    )\n"
        "    p_reclaim.add_argument(\n"
        '        "--expected-run-id", type=int, default=None,\n'
        '        help="CAS guard for a Controller-managed attempt",\n'
        "    )\n"
        "    p_reclaim.add_argument(\n"
        '        "--expected-worker-pid", type=int, default=None,\n'
        '        help="CAS guard for the Supervisor-confirmed worker PID",\n'
        "    )\n"
        "    p_reclaim.add_argument(\n"
        '        "--archive", action="store_true",\n'
        '        help="atomically archive a Controller-reclaimed task",\n'
        "    )\n",
        label="reclaim cli CAS arguments",
    )
    text = replace_once(
        text,
        "        ok = kb.reclaim_task(\n"
        "            conn, args.task_id,\n"
        '            reason=getattr(args, "reason", None),\n'
        "        )\n",
        "        ok = kb.reclaim_task(\n"
        "            conn, args.task_id,\n"
        '            reason=getattr(args, "reason", None),\n'
        '            expected_run_id=getattr(args, "expected_run_id", None),\n'
        '            expected_worker_pid=getattr(\n'
        '                args, "expected_worker_pid", None\n'
        "            ),\n"
        '            archive_after_reclaim=bool(getattr(args, "archive", False)),\n'
        "        )\n",
        label="reclaim cli CAS call",
    )
    text = replace_once(
        text,
        '    p_archive.add_argument("task_ids", nargs="*",\n'
        '                           help="Task ids to archive (default mode)")\n',
        '    p_archive.add_argument("task_ids", nargs="*",\n'
        '                           help="Task ids to archive (default mode)")\n'
        "    p_archive.add_argument(\n"
        '        "--expected-unclaimed", action="store_true",\n'
        '        help="archive only if no Worker attempt owns the task",\n'
        "    )\n",
        label="archive CLI unclaimed CAS argument",
    )
    return replace_once(
        text,
        "            if not kb.archive_task(conn, tid):\n",
        "            if not kb.archive_task(\n"
        "                conn, tid,\n"
        "                expected_unclaimed=bool(\n"
        '                    getattr(args, "expected_unclaimed", False)\n'
        "                ),\n"
        "            ):\n",
        label="archive CLI unclaimed CAS call",
    )


def patch_reclaim_cas(text: str) -> str:
    text = replace_once(
        text,
        "def reclaim_task(\n"
        "    conn: sqlite3.Connection,\n"
        "    task_id: str,\n"
        "    *,\n"
        "    reason: Optional[str] = None,\n"
        "    signal_fn=None,\n"
        ") -> bool:\n",
        "def reclaim_task(\n"
        "    conn: sqlite3.Connection,\n"
        "    task_id: str,\n"
        "    *,\n"
        "    reason: Optional[str] = None,\n"
        "    signal_fn=None,\n"
        "    expected_run_id: Optional[int] = None,\n"
        "    expected_worker_pid: Optional[int] = None,\n"
        "    archive_after_reclaim: bool = False,\n"
        ") -> bool:\n",
        label="reclaim CAS signature",
    )
    text = replace_once(
        text,
        "    row = conn.execute(\n"
        '        "SELECT status, claim_lock, worker_pid FROM tasks WHERE id = ?",\n'
        "        (task_id,),\n"
        "    ).fetchone()\n",
        "    row = conn.execute(\n"
        '        "SELECT status, claim_lock, worker_pid, created_by, current_run_id "\n'
        '        "FROM tasks WHERE id = ?",\n'
        "        (task_id,),\n"
        "    ).fetchone()\n",
        label="reclaim CAS identity read",
    )
    text = replace_once(
        text,
        '    prev_lock = row["claim_lock"]\n'
        "    termination = _terminate_reclaimed_worker(\n"
        '        row["worker_pid"], prev_lock, signal_fn=signal_fn,\n'
        "    )\n",
        '    prev_lock = row["claim_lock"]\n'
        "    controller_cas = (\n"
        "        expected_run_id is not None or expected_worker_pid is not None\n"
        "    )\n"
        "    if archive_after_reclaim and not controller_cas:\n"
        "        return False\n"
        "    if controller_cas:\n"
        "        if expected_run_id is None or expected_worker_pid is None:\n"
        "            return False\n"
        "        if (\n"
        '            row["created_by"] != "hollysys-controller"\n'
        '            or row["status"] != "running"\n'
        '            or row["current_run_id"] is None\n'
        '            or int(row["current_run_id"]) != int(expected_run_id)\n'
        '            or row["worker_pid"] is None\n'
        '            or int(row["worker_pid"]) != int(expected_worker_pid)\n'
        "        ):\n"
        "            return False\n"
        "        # The Controller may reach this branch only after the in-container\n"
        "        # Supervisor proved the exact process identity has exited. Never\n"
        "        # signal again from the CLI process (which may have another PID ns).\n"
        "        termination = {\n"
        '            "prev_pid": int(expected_worker_pid),\n'
        '            "supervisor_confirmed": True,\n'
        '            "termination_attempted": False,\n'
        '            "terminated": True,\n'
        '            "sigkill": False,\n'
        "        }\n"
        "    else:\n"
        "        termination = _terminate_reclaimed_worker(\n"
        '            row["worker_pid"], prev_lock, signal_fn=signal_fn,\n'
        "        )\n",
        label="reclaim Supervisor proof",
    )
    text = replace_once(
        text,
        "        cur = conn.execute(\n"
        '            "UPDATE tasks SET status = \'ready\', claim_lock = NULL, "\n'
        '            "claim_expires = NULL, worker_pid = NULL "\n'
        '            "WHERE id = ? AND status IN (\'running\', \'ready\', \'blocked\') "\n'
        '            "AND claim_lock IS ?",\n'
        "            (task_id, prev_lock),\n"
        "        )\n",
        "        if controller_cas:\n"
        '            target_status = "archived" if archive_after_reclaim else "ready"\n'
        "            cur = conn.execute(\n"
        '                "UPDATE tasks SET status = ?, claim_lock = NULL, "\n'
        '                "claim_expires = NULL, worker_pid = NULL "\n'
        '                "WHERE id = ? AND status = \'running\' "\n'
        '                "AND current_run_id = ? AND worker_pid = ? "\n'
        '                "AND claim_lock IS ?",\n'
        "                (\n"
        "                    target_status, task_id, int(expected_run_id),\n"
        "                    int(expected_worker_pid), prev_lock,\n"
        "                ),\n"
        "            )\n"
        "        else:\n"
        "            cur = conn.execute(\n"
        '                "UPDATE tasks SET status = \'ready\', claim_lock = NULL, "\n'
        '                "claim_expires = NULL, worker_pid = NULL "\n'
        '                "WHERE id = ? AND status IN "\n'
        '                "(\'running\', \'ready\', \'blocked\') "\n'
        '                "AND claim_lock IS ?",\n'
        "                (task_id, prev_lock),\n"
        "            )\n",
        label="reclaim transactional CAS",
    )
    text = replace_once(
        text,
        "        _append_event(\n"
        '            conn, task_id, "reclaimed",\n'
        "            payload,\n"
        "            run_id=run_id,\n"
        "        )\n",
        "        _append_event(\n"
        '            conn, task_id, "reclaimed",\n'
        "            payload,\n"
        "            run_id=run_id,\n"
        "        )\n"
        "        if archive_after_reclaim:\n"
        "            _append_event(\n"
        '                conn, task_id, "archived",\n'
        '                {"reason": "controller_abort"}, run_id=run_id,\n'
        "            )\n",
        label="atomic Controller reclaim archive event",
    )
    return replace_once(
        text,
        "    _clear_failure_counter(conn, task_id)\n"
        "    return True\n",
        "    _clear_failure_counter(conn, task_id)\n"
        "    if archive_after_reclaim:\n"
        "        recompute_ready(conn)\n"
        "    return True\n",
        label="atomic Controller reclaim archive promotion",
    )


def patch_archive_cas(text: str) -> str:
    text = replace_once(
        text,
        "def archive_task(conn: sqlite3.Connection, task_id: str) -> bool:\n",
        "def archive_task(\n"
        "    conn: sqlite3.Connection, task_id: str, *,\n"
        "    expected_unclaimed: bool = False,\n"
        ") -> bool:\n",
        label="archive unclaimed CAS signature",
    )
    return replace_once(
        text,
        '            "WHERE id = ? AND status != \'archived\'",\n'
        "            (task_id,),\n",
        '            "WHERE id = ? AND status != \'archived\' "\n'
        '            "AND (? = 0 OR (worker_pid IS NULL "\n'
        '            "AND claim_lock IS NULL AND current_run_id IS NULL))",\n'
        "            (task_id, int(expected_unclaimed)),\n",
        label="archive unclaimed transactional CAS",
    )


def patch_mutation_db_attempt_fencing(text: str) -> str:
    text = replace_once(
        text,
        "def add_comment(\n"
        "    conn: sqlite3.Connection, task_id: str, author: str, body: str\n"
        ") -> int:\n",
        "class StaleAttemptError(RuntimeError):\n"
        "    pass\n"
        "\n"
        "\n"
        "def _assert_expected_worker_attempt(\n"
        "    conn: sqlite3.Connection,\n"
        "    expected_owner_task_id: Optional[str],\n"
        "    expected_run_id: Optional[int],\n"
        ") -> None:\n"
        "    if expected_owner_task_id is None and expected_run_id is None:\n"
        "        env_owner = str(\n"
        "            os.environ.get(\"HERMES_KANBAN_TASK\") or \"\"\n"
        "        ).strip()\n"
        "        env_run = str(\n"
        "            os.environ.get(\"HERMES_KANBAN_RUN_ID\") or \"\"\n"
        "        ).strip()\n"
        "        if not env_owner and not env_run:\n"
        "            return\n"
        "        expected_owner_task_id = env_owner or None\n"
        "        try:\n"
        "            expected_run_id = int(env_run)\n"
        "        except (TypeError, ValueError):\n"
        "            expected_run_id = None\n"
        "    if expected_owner_task_id is None or expected_run_id is None:\n"
        "        raise StaleAttemptError(\"stale_attempt: incomplete identity\")\n"
        "    row = conn.execute(\n"
        "        \"SELECT status, current_run_id FROM tasks WHERE id = ?\",\n"
        "        (expected_owner_task_id,),\n"
        "    ).fetchone()\n"
        "    if (\n"
        "        row is None\n"
        "        or row[\"status\"] != \"running\"\n"
        "        or row[\"current_run_id\"] is None\n"
        "        or int(row[\"current_run_id\"]) != int(expected_run_id)\n"
        "    ):\n"
        "        raise StaleAttemptError(\"stale_attempt: run ownership changed\")\n"
        "\n"
        "\n"
        "def add_comment(\n"
        "    conn: sqlite3.Connection,\n"
        "    task_id: str,\n"
        "    author: str,\n"
        "    body: str,\n"
        "    *,\n"
        "    expected_owner_task_id: Optional[str] = None,\n"
        "    expected_run_id: Optional[int] = None,\n"
        ") -> int:\n",
        label="comment mutation attempt signature",
    )
    text = replace_once(
        text,
        "    if not author or not author.strip():\n"
        '        raise ValueError("comment author is required")\n'
        "    now = int(time.time())\n"
        "    with write_txn(conn):\n"
        "        if not conn.execute(\n"
        '            "SELECT 1 FROM tasks WHERE id = ?", (task_id,)\n'
        "        ).fetchone():\n",
        "    if not author or not author.strip():\n"
        '        raise ValueError("comment author is required")\n'
        "    now = int(time.time())\n"
        "    with write_txn(conn):\n"
        "        _assert_expected_worker_attempt(\n"
        "            conn, expected_owner_task_id, expected_run_id\n"
        "        )\n"
        "        if not conn.execute(\n"
        '            "SELECT 1 FROM tasks WHERE id = ?", (task_id,)\n'
        "        ).fetchone():\n",
        label="comment transactional attempt fence",
    )
    text = replace_once(
        text,
        "def link_tasks(conn: sqlite3.Connection, parent_id: str, child_id: str) -> None:\n"
        "    if parent_id == child_id:\n"
        '        raise ValueError("a task cannot depend on itself")\n'
        "    with write_txn(conn):\n"
        "        missing = _find_missing_parents(conn, [parent_id, child_id])\n",
        "def link_tasks(\n"
        "    conn: sqlite3.Connection,\n"
        "    parent_id: str,\n"
        "    child_id: str,\n"
        "    *,\n"
        "    expected_owner_task_id: Optional[str] = None,\n"
        "    expected_run_id: Optional[int] = None,\n"
        ") -> None:\n"
        "    if parent_id == child_id:\n"
        '        raise ValueError("a task cannot depend on itself")\n'
        "    with write_txn(conn):\n"
        "        _assert_expected_worker_attempt(\n"
        "            conn, expected_owner_task_id, expected_run_id\n"
        "        )\n"
        "        missing = _find_missing_parents(conn, [parent_id, child_id])\n",
        label="link transactional attempt fence",
    )
    return text


def _patch_attachment_create_signature(text: str) -> str:
    text = replace_once(
        text,
        "    project_id: Optional[str] = None,\n"
        "    project_source_task_id: Optional[str] = None,\n"
        ") -> str:\n",
        "    project_id: Optional[str] = None,\n"
        "    project_source_task_id: Optional[str] = None,\n"
        "    expected_owner_task_id: Optional[str] = None,\n"
        "    expected_run_id: Optional[int] = None,\n"
        ") -> str:\n",
        label="create mutation attempt signature",
    )
    return text


def patch_progress_worker(text: str) -> str:
    return replace_once(
        text,
        "\n\n"
        "def heartbeat_worker(\n",
        "\n\n"
        "def progress_worker(\n"
        "    conn: sqlite3.Connection,\n"
        "    task_id: str,\n"
        "    *,\n"
        "    payload: dict[str, Any],\n"
        "    expected_run_id: Optional[int],\n"
        ") -> bool:\n"
        "    \"\"\"Append one sanitized, attempt-bound worker progress event.\"\"\"\n"
        "    with write_txn(conn):\n"
        "        row = conn.execute(\n"
        "            \"SELECT status, current_run_id FROM tasks WHERE id = ?\",\n"
        "            (task_id,),\n"
        "        ).fetchone()\n"
        "        if (\n"
        "            row is None\n"
        "            or row[\"status\"] != \"running\"\n"
        "            or expected_run_id is None\n"
        "            or row[\"current_run_id\"] is None\n"
        "            or int(row[\"current_run_id\"]) != int(expected_run_id)\n"
        "        ):\n"
        "            return False\n"
        "        categories = payload.get(\"tool_categories\")\n"
        "        safe_categories = {}\n"
        "        if isinstance(categories, dict):\n"
        "            for key, value in categories.items():\n"
        "                if (\n"
        "                    isinstance(key, str) and len(key) <= 32\n"
        "                    and isinstance(value, int) and value >= 0\n"
        "                ):\n"
        "                    safe_categories[key] = value\n"
        "        metrics = payload.get(\"metrics\")\n"
        "        safe_metrics = {}\n"
        "        if isinstance(metrics, dict):\n"
        "            for key in (\n"
        "                \"model_wait\", \"tool_execution\",\n"
        "                \"delegation_wait\", \"retry_wait\",\n"
        "            ):\n"
        "                value = metrics.get(key, 0)\n"
        "                if isinstance(value, (int, float)) and value >= 0:\n"
        "                    safe_metrics[key] = round(float(value), 3)\n"
        "        safe_payload = {\n"
        "            \"tool_categories\": safe_categories,\n"
        "            \"tool_count\": max(0, int(payload.get(\"tool_count\", 0))),\n"
        "            \"elapsed_seconds\": max(\n"
        "                0, int(payload.get(\"elapsed_seconds\", 0))\n"
        "            ),\n"
        "            \"run_id\": int(expected_run_id),\n"
        "            \"session\": f\"kanban-run:{int(expected_run_id)}\",\n"
        "            \"metrics\": safe_metrics,\n"
        "        }\n"
        "        _append_event(\n"
        "            conn, task_id, \"progress\", safe_payload,\n"
        "            run_id=int(expected_run_id),\n"
        "        )\n"
        "    return True\n"
        "\n\n"
        "def heartbeat_worker(\n",
        label="attempt-bound progress event",
    )


def patch_attachment_create_db_fencing(text: str) -> str:
    text = _patch_attachment_create_signature(text)
    text = replace_once(
        text,
        "            with write_txn(conn):\n"
        "                # Determine task status from parent status, unless the caller\n",
        "            with write_txn(conn):\n"
        "                _assert_expected_worker_attempt(\n"
        "                    conn, expected_owner_task_id, expected_run_id\n"
        "                )\n"
        "                # Determine task status from parent status, unless the caller\n",
        label="create transactional attempt fence",
    )
    text = replace_once(
        text,
        "    board: Optional[str] = None,\n"
        "    max_bytes: Optional[int] = None,\n"
        ") -> int:\n",
        "    board: Optional[str] = None,\n"
        "    max_bytes: Optional[int] = None,\n"
        "    expected_owner_task_id: Optional[str] = None,\n"
        "    expected_run_id: Optional[int] = None,\n"
        ") -> int:\n",
        label="store attachment attempt signature",
    )
    text = replace_once(
        text,
        "            size=len(data),\n"
        "            uploaded_by=uploaded_by,\n"
        "        )\n",
        "            size=len(data),\n"
        "            uploaded_by=uploaded_by,\n"
        "            expected_owner_task_id=expected_owner_task_id,\n"
        "            expected_run_id=expected_run_id,\n"
        "        )\n",
        label="store attachment attempt forwarding",
    )
    text = replace_once(
        text,
        "    uploaded_by: Optional[str] = None,\n"
        ") -> int:\n"
        '    """Record a file attachment for a task. Returns the new attachment id.\n',
        "    uploaded_by: Optional[str] = None,\n"
        "    expected_owner_task_id: Optional[str] = None,\n"
        "    expected_run_id: Optional[int] = None,\n"
        ") -> int:\n"
        '    """Record a file attachment for a task. Returns the new attachment id.\n',
        label="attachment metadata attempt signature",
    )
    return replace_once(
        text,
        "    now = int(time.time())\n"
        "    with write_txn(conn):\n"
        "        if not conn.execute(\n"
        '            "SELECT 1 FROM tasks WHERE id = ?", (task_id,)\n'
        "        ).fetchone():\n"
        "            raise ValueError(f\"unknown task {task_id}\")\n"
        "        cur = conn.execute(\n"
        '            "INSERT INTO task_attachments "\n',
        "    now = int(time.time())\n"
        "    with write_txn(conn):\n"
        "        _assert_expected_worker_attempt(\n"
        "            conn, expected_owner_task_id, expected_run_id\n"
        "        )\n"
        "        if not conn.execute(\n"
        '            "SELECT 1 FROM tasks WHERE id = ?", (task_id,)\n'
        "        ).fetchone():\n"
        "            raise ValueError(f\"unknown task {task_id}\")\n"
        "        cur = conn.execute(\n"
        '            "INSERT INTO task_attachments "\n',
        label="attachment transactional attempt fence",
    )


def patch_kanban_tool_attempt_fencing(text: str) -> str:
    text = replace_once(
        text,
        "    return stamped\n"
        "\n",
        "    return stamped\n"
        "\n"
        "\n"
        "def _worker_attempt_kwargs() -> dict[str, Any]:\n"
        "    owner_task_id = str(\n"
        "        os.environ.get(\"HERMES_KANBAN_TASK\") or \"\"\n"
        "    ).strip()\n"
        "    if not owner_task_id:\n"
        "        return {}\n"
        "    return {\n"
        "        \"expected_owner_task_id\": owner_task_id,\n"
        "        \"expected_run_id\": _worker_run_id(owner_task_id),\n"
        "    }\n"
        "\n",
        label="Kanban tool attempt kwargs",
    )
    text = replace_once(
        text,
        "            cid = kb.add_comment(conn, tid, author=author, body=str(body))\n",
        "            cid = kb.add_comment(\n"
        "                conn, tid, author=author, body=str(body),\n"
        "                **_worker_attempt_kwargs(),\n"
        "            )\n",
        label="comment tool attempt identity",
    )
    for content_expr, label in (
        ("content_type", "inline attachment tool attempt identity"),
        ("content_type or fetched_ct", "URL attachment tool attempt identity"),
    ):
        text = replace_once(
            text,
            "            att_id = kb.store_attachment_bytes(\n"
            "                conn,\n"
            "                tid,\n"
            "                str(filename),\n"
            "                data,\n"
            f"                content_type={content_expr},\n"
            '                uploaded_by="agent",\n'
            "                board=board,\n"
            "            )\n",
            "            att_id = kb.store_attachment_bytes(\n"
            "                conn,\n"
            "                tid,\n"
            "                str(filename),\n"
            "                data,\n"
            f"                content_type={content_expr},\n"
            '                uploaded_by="agent",\n'
            "                board=board,\n"
            "                **_worker_attempt_kwargs(),\n"
            "            )\n",
            label=label,
        )
    text = replace_once(
        text,
        "                project_source_task_id=project_source_task_id,\n"
        "                triage=triage,\n",
        "                project_source_task_id=project_source_task_id,\n"
        "                **_worker_attempt_kwargs(),\n"
        "                triage=triage,\n",
        label="create tool attempt identity",
    )
    return replace_once(
        text,
        "            kb.link_tasks(conn, parent_id=parent_id, child_id=child_id)\n",
        "            kb.link_tasks(\n"
        "                conn, parent_id=parent_id, child_id=child_id,\n"
        "                **_worker_attempt_kwargs(),\n"
        "            )\n",
        label="link tool attempt identity",
    )


def patch_turn_finalizer(text: str) -> str:
    text = replace_once(
        text,
        '    if _last_msg_role == "tool" and not interrupted:\n'
        '        # Agent was mid-work — this is the "just stops" case.\n'
        "        logger.warning(\n"
        '            "Turn ended with pending tool result (agent may appear stuck). "\n'
        '            + _diag_msg + " last_tool=%s",\n'
        "            *_diag_args, _last_tool_name,\n"
        "        )\n"
        "    else:\n"
        "        logger.info(_diag_msg, *_diag_args)\n",
        '    if (\n'
        '        _last_msg_role == "tool"\n'
        "        and not interrupted\n"
        '        and _turn_exit_reason != "hollysys_terminal_tool"\n'
        "    ):\n"
        '        # Agent was mid-work — this is the "just stops" case.\n'
        "        logger.warning(\n"
        '            "Turn ended with pending tool result (agent may appear stuck). "\n'
        '            + _diag_msg + " last_tool=%s",\n'
        "            *_diag_args, _last_tool_name,\n"
        "        )\n"
        "    else:\n"
        "        logger.info(_diag_msg, *_diag_args)\n",
        label="successful terminal tool exit diagnostic",
    )
    return replace_once(
        text,
        '        _kanban_task = os.environ.get("HERMES_KANBAN_TASK")\n'
        "        if _kanban_task:\n"
        "            try:\n"
        "                from hermes_cli import kanban_db as _kb\n"
        "                _conn = _kb.connect()\n"
        "                try:\n"
        "                    _kb._record_task_failure(\n"
        "                        _conn,\n"
        "                        _kanban_task,\n"
        "                        error=(\n"
        '                            f"Iteration budget exhausted "\n'
        '                            f"({api_call_count}/{agent.max_iterations}) — "\n'
        '                            "task could not complete within the allowed "\n'
        '                            "iterations"\n'
        "                        ),\n"
        '                        outcome="timed_out",\n'
        "                        release_claim=True,\n"
        "                        end_run=True,\n"
        "                        event_payload_extra={\n"
        '                            "budget_used": api_call_count,\n'
        '                            "budget_max": agent.max_iterations,\n'
        "                        },\n"
        "                    )\n"
        "                    logger.info(\n"
        '                        "recorded budget-exhausted failure for task %s (%d/%d)",\n'
        "                        _kanban_task, api_call_count, agent.max_iterations,\n"
        "                    )\n"
        "                finally:\n"
        "                    try:\n"
        "                        _conn.close()\n"
        "                    except Exception:\n"
        "                        pass\n"
        "            except Exception:\n"
        "                logger.warning(\n"
        '                    "Failed to record budget-exhausted failure for task %s",\n'
        "                    _kanban_task,\n"
        "                    exc_info=True,\n"
        "                )\n",
        '        _kanban_task = os.environ.get("HERMES_KANBAN_TASK")\n'
        '        _kanban_run_raw = os.environ.get("HERMES_KANBAN_RUN_ID", "").strip()\n'
        "        try:\n"
        "            _kanban_run_id = int(_kanban_run_raw)\n"
        "        except (TypeError, ValueError):\n"
        "            _kanban_run_id = None\n"
        '        if _kanban_task and getattr(agent, "_parent_session_id", None):\n'
        "            logger.info(\n"
        '                "skipped delegated child budget failure for parent task %s",\n'
        "                _kanban_task,\n"
        "            )\n"
        "        elif _kanban_task and _kanban_run_id is None:\n"
        "            # Fail closed: without stable attempt provenance this process\n"
        "            # must not be allowed to close whichever retry is current now.\n"
        "            logger.warning(\n"
        '                "skipped budget failure for task %s: missing valid run id",\n'
        "                _kanban_task,\n"
        "            )\n"
        "        elif _kanban_task:\n"
        "            try:\n"
        "                from hermes_cli import kanban_db as _kb\n"
        "                _conn = _kb.connect()\n"
        "                try:\n"
        "                    _recorded = _kb._record_task_failure(\n"
        "                        _conn,\n"
        "                        _kanban_task,\n"
        "                        error=(\n"
        '                            f"Iteration budget exhausted "\n'
        '                            f"({api_call_count}/{agent.max_iterations}) — "\n'
        '                            "task could not complete within the allowed "\n'
        '                            "iterations"\n'
        "                        ),\n"
        '                        outcome="timed_out",\n'
        "                        release_claim=True,\n"
        "                        end_run=True,\n"
        "                        expected_run_id=_kanban_run_id,\n"
        "                        event_payload_extra={\n"
        '                            "budget_used": api_call_count,\n'
        '                            "budget_max": agent.max_iterations,\n'
        "                        },\n"
        "                    )\n"
        "                    if _recorded is None:\n"
        "                        logger.warning(\n"
        '                            "skipped stale budget failure for task %s run %s",\n'
        "                            _kanban_task, _kanban_run_id,\n"
        "                        )\n"
        "                    else:\n"
        "                        logger.info(\n"
        '                            "recorded budget-exhausted failure for task %s "\n'
        '                            "run %s (%d/%d)",\n'
        "                            _kanban_task, _kanban_run_id,\n"
        "                            api_call_count, agent.max_iterations,\n"
        "                        )\n"
        "                finally:\n"
        "                    try:\n"
        "                        _conn.close()\n"
        "                    except Exception:\n"
        "                        pass\n"
        "            except Exception:\n"
        "                logger.warning(\n"
        '                    "Failed to record budget-exhausted failure for task %s",\n'
        "                    _kanban_task,\n"
        "                    exc_info=True,\n"
        "                )\n",
        label="attempt-bound budget failure",
    )


def patch_delegated_child_review(text: str) -> str:
    text = replace_once(
        text,
        "    if (agent._skill_nudge_interval > 0\n"
        "            and agent._iters_since_skill >= agent._skill_nudge_interval\n",
        "    if (not getattr(agent, \"_parent_session_id\", None)\n"
        "            and agent._skill_nudge_interval > 0\n"
        "            and agent._iters_since_skill >= agent._skill_nudge_interval\n",
        label="delegated child skill review fence",
    )
    return replace_once(
        text,
        "    if final_response and not interrupted and (_should_review_memory or _should_review_skills):\n",
        "    if (\n"
        "        final_response\n"
        "        and not interrupted\n"
        "        and not getattr(agent, \"_parent_session_id\", None)\n"
        "        and (_should_review_memory or _should_review_skills)\n"
        "    ):\n",
        label="delegated child background review fence",
    )


def patch_turn_finalizer_all(text: str) -> str:
    return patch_delegated_child_review(patch_turn_finalizer(text))


def patch_codex_runtime(text: str) -> str:
    text = replace_once(
        text,
        "    if (\n"
        "        agent._skill_nudge_interval > 0\n"
        "        and agent._iters_since_skill >= agent._skill_nudge_interval\n",
        "    if (\n"
        "        not getattr(agent, \"_parent_session_id\", None)\n"
        "        and agent._skill_nudge_interval > 0\n"
        "        and agent._iters_since_skill >= agent._skill_nudge_interval\n",
        label="delegated Codex child skill review fence",
    )
    return replace_once(
        text,
        "    if (\n"
        "        turn.final_text\n"
        "        and not turn.interrupted\n"
        "        and (should_review_memory or should_review_skills)\n"
        "    ):\n",
        "    if (\n"
        "        turn.final_text\n"
        "        and not turn.interrupted\n"
        "        and not getattr(agent, \"_parent_session_id\", None)\n"
        "        and (should_review_memory or should_review_skills)\n"
        "    ):\n",
        label="delegated Codex child background review fence",
    )


def patch_cli(text: str) -> str:
    return replace_once(
        text,
        '                            ) in ("rate_limit", "billing"):\n',
        "                            ) in (\n"
        '                                "billing",\n'
        '                                "overloaded",\n'
        '                                "rate_limit",\n'
        '                                "server_error",\n'
        '                                "timeout",\n'
        '                                "unknown",\n'
        '                                "upstream_rate_limit",\n'
        "                            ):\n",
        label="kanban dependency tempfail exit",
    )


def patch_docker_config_migrate(text: str) -> str:
    text = replace_once(
        text,
        "    get_env_path,\n"
        "    migrate_config,\n",
        "    get_env_path,\n"
        "    migrate_config,\n"
        "    _raw_config_has_explicit_version,\n",
        label="docker config explicit-version import",
    )
    return replace_once(
        text,
        "    if current_ver < SUPPORT_FLOOR_VERSION:\n",
        "    if (\n"
        "        _raw_config_has_explicit_version()\n"
        "        and current_ver < SUPPORT_FLOOR_VERSION\n"
        "    ):\n",
        label="docker config support-floor guard",
    )


def patch_kanban(text: str) -> str:
    text = replace_once(
        text,
        "    if task.goal_mode:\n"
        "        # Goal-mode workers must take the fully-quiet single-query path:\n"
        "        # the kanban goal-loop hook (_run_kanban_goal_loop_q) only runs in\n"
        "        # cli.py's quiet branch. Without -Q the worker gets exactly one\n"
        "        # turn, prints text, exits rc=0, and the dispatcher records a\n"
        "        # protocol violation (incident 2026-06-09 t_d9cbe312).\n"
        '        cmd.append("-Q")\n',
        "    # Every Kanban worker must use the fully-quiet single-query path.\n"
        "    # The human-facing -q branch prints a failed API result and then\n"
        "    # returns rc=0, which is indistinguishable from a clean protocol\n"
        "    # violation to the dispatcher.  -Q preserves the structured result\n"
        "    # through cli.py so real failures get a non-zero or EX_TEMPFAIL exit.\n"
        '    cmd.append("-Q")\n',
        label="all kanban workers use quiet exit contract",
    )
    text = replace_once(
        text,
        "def complete_task(\n",
        "def _worker_expected_run_id(\n"
        "    task_id: str, expected_run_id: Optional[int]\n"
        ") -> Optional[int]:\n"
        "    if expected_run_id is not None:\n"
        "        return int(expected_run_id)\n"
        "    owner = str(os.environ.get(\"HERMES_KANBAN_TASK\") or \"\").strip()\n"
        "    if not owner:\n"
        "        return None\n"
        "    if owner != task_id:\n"
        "        raise StaleAttemptError(\"stale_attempt: task ownership changed\")\n"
        "    try:\n"
        "        return int(os.environ.get(\"HERMES_KANBAN_RUN_ID\", \"\"))\n"
        "    except (TypeError, ValueError) as exc:\n"
        "        raise StaleAttemptError(\"stale_attempt: invalid run identity\") from exc\n"
        "\n"
        "\n"
        "def complete_task(\n",
        label="worker run identity helper",
    )
    text = replace_once(
        text,
        "    now = int(time.time())\n"
        "\n"
        "    # Gate: verify created_cards BEFORE the main write txn. A rejected\n",
        "    expected_run_id = _worker_expected_run_id(task_id, expected_run_id)\n"
        "    now = int(time.time())\n"
        "\n"
        "    # Gate: verify created_cards BEFORE the main write txn. A rejected\n",
        label="complete worker run identity",
    )
    text = replace_once(
        text,
        "        if phantom_cards:\n"
        "            with write_txn(conn):\n"
        "                _append_event(\n",
        "        if phantom_cards:\n"
        "            with write_txn(conn):\n"
        "                _assert_expected_worker_attempt(\n"
        "                    conn, task_id, expected_run_id\n"
        "                )\n"
        "                _append_event(\n",
        label="complete rejection event attempt fence",
    )
    text = replace_once(
        text,
        "    if kind is not None and kind not in VALID_BLOCK_KINDS:\n",
        "    expected_run_id = _worker_expected_run_id(task_id, expected_run_id)\n"
        "    if kind is not None and kind not in VALID_BLOCK_KINDS:\n",
        label="block worker run identity",
    )
    text = replace_once(
        text,
        "    now = int(time.time())\n"
        "    with write_txn(conn):\n"
        "        if expected_run_id is None:\n"
        "            cur = conn.execute(\n"
        "                \"UPDATE tasks SET last_heartbeat_at = ? \"\n",
        "    expected_run_id = _worker_expected_run_id(task_id, expected_run_id)\n"
        "    now = int(time.time())\n"
        "    with write_txn(conn):\n"
        "        if expected_run_id is None:\n"
        "            cur = conn.execute(\n"
        "                \"UPDATE tasks SET last_heartbeat_at = ? \"\n",
        label="heartbeat worker run identity",
    )
    text = replace_once(
        text,
        "    failure is still counted into ``consecutive_failures``.\n"
        "    \"\"\"\n"
        "    if failure_limit is None:\n"
        "        failure_limit = DEFAULT_FAILURE_LIMIT\n",
        "    failure is still counted into ``consecutive_failures``.\n"
        "    \"\"\"\n"
        "    expected_run_id = _worker_expected_run_id(task_id, expected_run_id)\n"
        "    if failure_limit is None:\n"
        "        failure_limit = DEFAULT_FAILURE_LIMIT\n",
        label="failure worker run identity",
    )
    text = replace_once(
        text,
        "    event_payload_extra: Optional[dict] = None,\n"
        ") -> bool:\n",
        "    event_payload_extra: Optional[dict] = None,\n"
        "    expected_run_id: Optional[int] = None,\n"
        ") -> Optional[bool]:\n",
        label="failure attempt guard signature",
    )
    text = replace_once(
        text,
        "    Returns True when the task was auto-blocked (counter reached\n"
        "    ``failure_limit``), False when it was just updated in place.\n",
        "    Returns True when the task was auto-blocked (counter reached\n"
        "    ``failure_limit``), False when it was just updated in place, and\n"
        "    None when ``expected_run_id`` no longer owns the task.  The stale\n"
        "    case is a no-op so an old worker cannot close a newer retry.\n",
        label="failure attempt guard contract",
    )
    text = replace_once(
        text,
        '            "SELECT consecutive_failures, status, max_retries "\n'
        '            "FROM tasks WHERE id = ?", (task_id,),\n'
        "        ).fetchone()\n"
        "        if row is None:\n"
        "            return False\n"
        '        failures = int(row["consecutive_failures"]) + 1\n',
        '            "SELECT consecutive_failures, status, max_retries, "\n'
        '            "current_run_id FROM tasks WHERE id = ?", (task_id,),\n'
        "        ).fetchone()\n"
        "        if row is None:\n"
        "            return False\n"
        "        if expected_run_id is not None and (\n"
        '            row["current_run_id"] is None\n'
        '            or int(row["current_run_id"]) != int(expected_run_id)\n'
        "        ):\n"
        "            return None\n"
        '        failures = int(row["consecutive_failures"]) + 1\n',
        label="failure attempt guard transaction",
    )
    return text


def patch_kanban_all(text: str) -> str:
    text = patch_kanban(text)
    text = patch_reclaim_cas(text)
    text = patch_archive_cas(text)
    text = patch_mutation_db_attempt_fencing(text)
    text = patch_attachment_create_db_fencing(text)
    text = patch_progress_worker(text)
    text = patch_native_recovery_fencing(text)
    text = patch_worker_run_scratch(text)
    return patch_worker_exit_events(text)


def patch_delegate(text: str) -> str:
    text = replace_once(
        text,
        '        "cronjob",  # no scheduling more work in the parent\'s name\n',
        '        "cronjob",  # no scheduling more work in the parent\'s name\n'
        '        "kanban_complete",  # only the parent worker may close its task\n'
        '        "kanban_block",  # only the parent worker may block its task\n'
        '        "kanban_heartbeat",  # child liveness is owned by the parent\n'
        '        "kanban_comment",  # no child-authored parent task mutations\n'
        '        "kanban_create",  # no formal graph mutation from children\n'
        '        "kanban_link",  # no formal graph mutation from children\n'
        '        "kanban_unblock",  # no formal lifecycle mutation from children\n'
        '        "kanban_attach",  # no parent task attachment mutation\n'
        '        "kanban_attach_url",  # no parent task attachment mutation\n',
        label="delegated kanban mutation blocklist",
    )
    text = replace_once(
        text,
        '        "\\nComplete this task using the tools available to you. "\n',
        '        "\\nKanban lifecycle boundary: you are a delegated child, not "\n'
        '        "the parent worker. Never call mutating kanban_* tools or the "\n'
        '        "`hermes kanban` CLI to complete, block, comment on, attach to, "\n'
        '        "create, link, heartbeat, or unblock tasks. Return findings to "\n'
        '        "the parent only.\\n"\n'
        '        "\\nComplete this task using the tools available to you. "\n',
        label="delegated kanban mutation prompt",
    )
    return text


def patch_delegate_artifacts(text: str) -> str:
    text = replace_once(
        text,
        "import contextvars\n"
        "import json\n"
        "import logging\n",
        "import contextvars\n"
        "import hashlib\n"
        "import json\n"
        "import logging\n"
        "from pathlib import Path\n",
        label="delegation artifact imports",
    )
    text = replace_block(
        text,
        "def _spill_summary_to_file(",
        "def _trim_summary_with_footer(",
        "def _spill_summary_to_file(\n"
        "    task_index: int, summary: str\n"
        ") -> Optional[tuple[str, int, str]]:\n"
        '    """Persist a full child summary as a mode-0600 run artifact."""\n'
        "    try:\n"
        "        import datetime as _dt\n"
        "\n"
        "        scratch = str(os.environ.get(\"HERMES_RUN_SCRATCH_DIR\") or \"\").strip()\n"
        "        if scratch and os.environ.get(\"HERMES_KANBAN_TASK\"):\n"
        "            artifact_dir = Path(scratch) / \"delegation\"\n"
        "        else:\n"
        "            from hermes_constants import get_hermes_dir\n"
        "\n"
        "            artifact_dir = Path(\n"
        "                get_hermes_dir(\"cache/delegation\", \"delegation_cache\")\n"
        "            )\n"
        "        artifact_dir.mkdir(parents=True, exist_ok=True, mode=0o700)\n"
        "        artifact_dir.chmod(0o700)\n"
        "        ts = _dt.datetime.now().strftime(\"%Y%m%d_%H%M%S_%f\")\n"
        "        path = artifact_dir / f\"subagent-summary-{task_index}-{ts}.txt\"\n"
        "        encoded = summary.encode(\"utf-8\")\n"
        "        path.write_bytes(encoded)\n"
        "        path.chmod(0o600)\n"
        "        return str(path), len(encoded), hashlib.sha256(encoded).hexdigest()\n"
        "    except Exception as exc:\n"
        "        logger.debug(\"Failed to persist subagent summary: %s\", exc)\n"
        "        return None\n"
        "\n"
        "\n",
        label="delegation summary artifact writer",
    )
    text = replace_block(
        text,
        "def _trim_summary_with_footer(",
        "def _parent_summary_char_budget(",
        "def _trim_summary_with_footer(\n"
        "    summary: str, cap: int, task_index: int\n"
        ") -> tuple[str, Optional[str], Optional[int], Optional[str]]:\n"
        '    """Trim one summary while keeping artifact metadata inside cap."""\n'
        "    original_len = len(summary)\n"
        "    artifact = _spill_summary_to_file(task_index, summary)\n"
        "    spill_path = artifact[0] if artifact else None\n"
        "    spill_bytes = artifact[1] if artifact else None\n"
        "    spill_sha256 = artifact[2] if artifact else None\n"
        "    footer_lines = [\n"
        "        \"\",\n"
        "        \"-------- [SUMMARY TRUNCATED] --------\",\n"
        "        f\"Original chars: {original_len:,}\",\n"
        "    ]\n"
        "    if spill_path:\n"
        "        footer_lines.extend([\n"
        "            f\"Full subagent output: {spill_path}\",\n"
        "            f\"Bytes: {spill_bytes}\",\n"
        "            f\"SHA-256: {spill_sha256}\",\n"
        "        ])\n"
        "    else:\n"
        "        footer_lines.append(\"Full output could not be persisted.\")\n"
        "    footer = \"\\n\".join(footer_lines)\n"
        "    separator = \"\\n\\n[... middle omitted ...]\\n\\n\"\n"
        "    content_budget = max(0, cap - len(separator) - len(footer))\n"
        "    head_budget = int(content_budget * 0.75)\n"
        "    tail_budget = content_budget - head_budget\n"
        "    head = summary[:head_budget]\n"
        "    tail = summary[-tail_budget:] if tail_budget else \"\"\n"
        "    model_text = head + separator + tail + footer\n"
        "    if len(model_text) > cap:\n"
        "        model_text = model_text[:cap]\n"
        "    return model_text, spill_path, spill_bytes, spill_sha256\n"
        "\n"
        "\n",
        label="delegation capped summary footer",
    )
    return replace_once(
        text,
        "    for entry in summaries:\n"
        "        summary = entry[\"summary\"]\n"
        "        if len(summary) <= cap:\n"
        "            continue\n"
        "        original_len = len(summary)\n"
        "        model_text, spill_path = _trim_summary_with_footer(\n"
        "            summary, cap, entry.get(\"task_index\", -1)\n"
        "        )\n"
        "        entry[\"summary\"] = model_text\n"
        "        entry[\"summary_truncated\"] = True\n"
        "        if spill_path:\n"
        "            entry[\"summary_full_path\"] = spill_path\n",
        "    for entry in summaries:\n"
        "        summary = entry[\"summary\"]\n"
        "        original_len = len(summary)\n"
        "        if len(summary) <= cap:\n"
        "            artifact = _spill_summary_to_file(\n"
        "                entry.get(\"task_index\", -1), summary\n"
        "            )\n"
        "            if artifact:\n"
        "                entry[\"summary_full_path\"] = artifact[0]\n"
        "                entry[\"summary_full_bytes\"] = artifact[1]\n"
        "                entry[\"summary_full_sha256\"] = artifact[2]\n"
        "            continue\n"
        "        model_text, spill_path, spill_bytes, spill_sha256 = (\n"
        "            _trim_summary_with_footer(\n"
        "                summary, cap, entry.get(\"task_index\", -1)\n"
        "            )\n"
        "        )\n"
        "        entry[\"summary\"] = model_text\n"
        "        entry[\"summary_truncated\"] = True\n"
        "        if spill_path:\n"
        "            entry[\"summary_full_path\"] = spill_path\n"
        "            entry[\"summary_full_bytes\"] = spill_bytes\n"
        "            entry[\"summary_full_sha256\"] = spill_sha256\n",
        label="delegation artifact metadata",
    )


def patch_delegate_all(text: str) -> str:
    return patch_delegate_artifacts(patch_delegate(text))


def patch_delegation_live_log(text: str) -> str:
    text = replace_once(
        text,
        "import json\n"
        "import logging\n",
        "import json\n"
        "import logging\n"
        "import os\n",
        label="delegation live log os import",
    )
    text = replace_once(
        text,
        "def live_transcript_root() -> Path:\n"
        '    """Root directory for live transcripts (profile-safe, never ~/.hermes)."""\n'
        "    from hermes_constants import get_hermes_dir\n"
        "\n"
        "    return get_hermes_dir(\"cache/delegation\", \"delegation_cache\") / \"live\"\n",
        "def live_transcript_root() -> Path:\n"
        '    """Root directory for mode-0600 live transcript evidence."""\n'
        "    scratch = str(os.environ.get(\"HERMES_RUN_SCRATCH_DIR\") or \"\").strip()\n"
        "    if scratch and os.environ.get(\"HERMES_KANBAN_TASK\"):\n"
        "        return Path(scratch) / \"delegation\" / \"live\"\n"
        "    from hermes_constants import get_hermes_dir\n"
        "\n"
        "    return get_hermes_dir(\"cache/delegation\", \"delegation_cache\") / \"live\"\n",
        label="delegation live log run scratch",
    )
    text = replace_once(
        text,
        "            d.mkdir(parents=True, exist_ok=True)\n"
        "            self.path: Optional[Path] = d / f\"task-{task_index}.log\"\n",
        "            d.mkdir(parents=True, exist_ok=True, mode=0o700)\n"
        "            d.chmod(0o700)\n"
        "            self.path: Optional[Path] = d / f\"task-{task_index}.log\"\n",
        label="delegation live directory permissions",
    )
    text = replace_once(
        text,
        "            self.path.write_text(\"\\n\".join(header) + \"\\n\", encoding=\"utf-8\")\n"
        "            self.event(\"user\", \"kickoff: \" + _one_line(goal, _KICKOFF_MAX)\n",
        "            self.path.write_text(\"\\n\".join(header) + \"\\n\", encoding=\"utf-8\")\n"
        "            self.path.chmod(0o600)\n"
        "            self.event(\"user\", \"kickoff: \" + _one_line(goal, _KICKOFF_MAX)\n",
        label="delegation live file permissions",
    )
    text = replace_once(
        text,
        "        _manifest_path(delegation_id).write_text(\n"
        "            json.dumps(manifest, indent=2, ensure_ascii=False), encoding=\"utf-8\"\n"
        "        )\n",
        "        manifest_path = _manifest_path(delegation_id)\n"
        "        manifest_path.write_text(\n"
        "            json.dumps(manifest, indent=2, ensure_ascii=False), encoding=\"utf-8\"\n"
        "        )\n"
        "        manifest_path.chmod(0o600)\n",
        label="delegation manifest permissions",
    )
    return replace_once(
        text,
        "        mp.write_text(json.dumps(manifest, indent=2, ensure_ascii=False),\n"
        "                      encoding=\"utf-8\")\n",
        "        mp.write_text(json.dumps(manifest, indent=2, ensure_ascii=False),\n"
        "                      encoding=\"utf-8\")\n"
        "        mp.chmod(0o600)\n",
        label="delegation completed manifest permissions",
    )


def patch_tool_result_storage(text: str) -> str:
    text = replace_once(
        text,
        "def _resolve_storage_dir(env) -> str:\n"
        '    """Return the best temp-backed storage dir for this environment."""\n'
        "    if env is not None:\n",
        "def _resolve_storage_dir(env) -> str:\n"
        '    """Return the run-scoped evidence dir when a managed worker has one."""\n'
        "    scratch = str(os.environ.get(\"HERMES_RUN_SCRATCH_DIR\") or \"\").strip()\n"
        "    if scratch and os.environ.get(\"HERMES_KANBAN_TASK\"):\n"
        "        return f\"{scratch.rstrip('/')}/tool-results\"\n"
        "    if env is not None:\n",
        label="tool output run scratch",
    )
    text = replace_once(
        text,
        "    cmd = f\"mkdir -p {shlex.quote(storage_dir)} && cat > {shlex.quote(remote_path)}\"\n",
        "    cmd = (\n"
        "        f\"umask 077 && mkdir -p {shlex.quote(storage_dir)} && \"\n"
        "        f\"cat > {shlex.quote(remote_path)} && chmod 600 {shlex.quote(remote_path)}\"\n"
        "    )\n",
        label="tool output file permissions",
    )
    text = replace_once(
        text,
        "def _build_persisted_message(\n"
        "    preview: str,\n"
        "    has_more: bool,\n"
        "    original_size: int,\n"
        "    file_path: str,\n"
        ") -> str:\n",
        "def _build_persisted_message(\n"
        "    preview: str,\n"
        "    has_more: bool,\n"
        "    original_size: int,\n"
        "    file_path: str,\n"
        "    *,\n"
        "    byte_count: int,\n"
        "    sha256: str,\n"
        ") -> str:\n",
        label="tool output evidence signature",
    )
    text = replace_once(
        text,
        "    msg += f\"Full output saved to: {file_path}\\n\"\n"
        "    msg += \"Use the read_file tool with offset and limit to access specific sections of this output.\\n\\n\"\n",
        "    msg += f\"Full output saved to: {file_path}\\n\"\n"
        "    msg += f\"Bytes: {byte_count}\\nSHA-256: {sha256}\\n\"\n"
        "    msg += \"Use the read_file tool with offset and limit to access specific sections of this output.\\n\\n\"\n",
        label="tool output evidence footer",
    )
    return replace_once(
        text,
        "                return _build_persisted_message(preview, has_more, len(content), remote_path)\n",
        "                encoded = content.encode(\"utf-8\")\n"
        "                return _build_persisted_message(\n"
        "                    preview, has_more, len(content), remote_path,\n"
        "                    byte_count=len(encoded),\n"
        "                    sha256=hashlib.sha256(encoded).hexdigest(),\n"
        "                )\n",
        label="tool output evidence metadata",
    )


def patch_kanban_tools(text: str) -> str:
    text = replace_once(
        text,
        "def _stamp_worker_session_metadata(\n"
        "    task_id: str, metadata: Optional[dict]\n"
        ") -> Optional[dict]:\n"
        '    """Add trusted worker session id metadata for this worker\'s own task."""\n'
        '    if os.environ.get("HERMES_KANBAN_TASK") != task_id:\n'
        "        return metadata\n"
        '    session_id = os.environ.get("HERMES_SESSION_ID")\n'
        "    if not session_id:\n"
        "        return metadata\n"
        "    stamped = dict(metadata or {})\n"
        '    stamped["worker_session_id"] = session_id\n'
        "    return stamped\n",
        "def _stamp_worker_session_metadata(\n"
        "    task_id: str,\n"
        "    metadata: Optional[dict],\n"
        "    *,\n"
        "    session_id: Optional[str] = None,\n"
        ") -> Optional[dict]:\n"
        '    """Add stable attempt provenance for this worker\'s own task."""\n'
        '    if os.environ.get("HERMES_KANBAN_TASK") != task_id:\n'
        "        return metadata\n"
        "    # Controller observes an attempt before Hermes creates its AIAgent,\n"
        "    # so the durable identity is the dispatcher run id.  Do not use the\n"
        "    # process-global HERMES_SESSION_ID first: delegate_task children\n"
        "    # share this process and overwrite that environment mirror.\n"
        "    run_id = str(\n"
        '        os.environ.get("HERMES_KANBAN_RUN_ID") or ""\n'
        "    ).strip()\n"
        '    trusted_session_id = f"kanban-run:{run_id}" if run_id else ""\n'
        "    if not trusted_session_id:\n"
        "        # Compatibility for non-dispatcher callers and old boards.\n"
        "        trusted_session_id = str(session_id or \"\").strip()\n"
        "    if not trusted_session_id:\n"
        "        trusted_session_id = str(\n"
        '            os.environ.get("HERMES_SESSION_ID") or ""\n'
        "        ).strip()\n"
        "    if not trusted_session_id:\n"
        "        return metadata\n"
        "    stamped = dict(metadata or {})\n"
        '    stamped["worker_session_id"] = trusted_session_id\n'
        "    return stamped\n",
        label="invoking agent completion provenance",
    )
    text = replace_once(
        text,
        "    metadata = _stamp_worker_session_metadata(tid, metadata)\n",
        "    metadata = _stamp_worker_session_metadata(\n"
        '        tid, metadata, session_id=kw.get("session_id")\n'
        "    )\n",
        label="completion provenance callsite",
    )
    return text


def patch_kanban_tools_all(text: str) -> str:
    return patch_kanban_tool_attempt_fencing(patch_kanban_tools(text))


def patch_gateway(text: str) -> str:
    return replace_once(
        text,
        "        # Build session context\n"
        "        context = build_session_context(source, self.config, session_entry)\n",
        "        # MessageEvent carries the triggering id separately from its\n"
        "        # SessionSource.  Bind it before creating SessionContext so\n"
        "        # HERMES_SESSION_MESSAGE_ID reaches terminal subprocesses.\n"
        "        source.message_id = str(event.message_id) if event.message_id else None\n"
        "\n"
        "        # Build session context\n"
        "        context = build_session_context(source, self.config, session_entry)\n",
        label="gateway triggering message context",
    )


def patch_system_prompt(text: str) -> str:
    return replace_once(
        text,
        "    else:\n"
        "        post_workspace_parts.append(\n"
        "            f\"Active Hermes profile: {active_profile}. This session reads \"\n"
        "            f\"and writes {get_hermes_home()}/profiles/{active_profile}/. The default \"\n"
        "            f\"profile's data lives at {get_hermes_home()}/skills/, {get_hermes_home()}/plugins/, \"\n"
        "            f\"{get_hermes_home()}/cron/, {get_hermes_home()}/memories/ — those belong to a \"\n",
        "    else:\n"
        "        from hermes_constants import get_default_hermes_root\n"
        "        profile_root = get_default_hermes_root()\n"
        "        post_workspace_parts.append(\n"
        "            f\"Active Hermes profile: {active_profile}. This session reads \"\n"
        "            f\"and writes {get_hermes_home()}/. The default \"\n"
        "            f\"profile's data lives at {profile_root}/skills/, {profile_root}/plugins/, \"\n"
        "            f\"{profile_root}/cron/, {profile_root}/memories/ — those belong to a \"\n",
        label="active profile path",
    )


def main() -> None:
    system_prompt_only = (
        len(sys.argv) == 3 and sys.argv[1] == "--system-prompt-only"
    )
    if len(sys.argv) != 2 and not system_prompt_only:
        raise SystemExit(
            "usage: patch-hermes-terminal.py "
            "[--system-prompt-only] HERMES_SOURCE_ROOT"
        )
    root = Path(sys.argv[-1]).resolve()
    patches = {
        "agent/tool_executor.py": patch_executor_all,
        "agent/conversation_loop.py": patch_loop_all,
        "agent/codex_runtime.py": patch_codex_runtime,
        "agent/system_prompt.py": patch_system_prompt,
        "agent/turn_finalizer.py": patch_turn_finalizer_all,
        "cli.py": patch_cli,
        "gateway/run.py": patch_gateway,
        "hermes_cli/kanban_db.py": patch_kanban_all,
        "hermes_cli/kanban.py": patch_kanban_cli,
        "scripts/docker_config_migrate.py": patch_docker_config_migrate,
        "tools/delegate_tool.py": patch_delegate_all,
        "tools/delegation_live_log.py": patch_delegation_live_log,
        "tools/kanban_tools.py": patch_kanban_tools_all,
        "tools/tool_result_storage.py": patch_tool_result_storage,
    }
    if system_prompt_only:
        patches = {"agent/system_prompt.py": patch_system_prompt}
    for relative, patcher in patches.items():
        path = root / relative
        original = path.read_bytes()
        digest = hashlib.sha256(original).hexdigest()
        if digest != EXPECTED[relative]:
            raise RuntimeError(
                f"{relative}: source fingerprint {digest} does not match "
                f"pinned v2026.7.30 fingerprint {EXPECTED[relative]}"
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
