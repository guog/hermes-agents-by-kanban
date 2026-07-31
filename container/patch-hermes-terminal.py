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
    "scripts/docker_config_migrate.py": (
        "62fdbd91cad654c0ce6277527ef2c25184d62f5dfca09025d670135baacb1fc6"
    ),
    "tools/delegate_tool.py": (
        "9c537dd695d990c27e7a7cec51267ed9167fbe6bb752cdb803d43880d50a1cff"
    ),
    "tools/kanban_tools.py": (
        "a2244b040d8d8399d42f8509f86aea0ccd426751234ae6fc06b42867e53fbb34"
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
        "def _ensure_file_checkpoint(\n",
        "logger = logging.getLogger(__name__)\n\n"
        "_HOLLYSYS_TERMINAL_TOOLS = frozenset("
        '{"kanban_complete", "kanban_block"})\n\n\n'
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
        "        # ── Per-tool /steer drain ───────────────────────────────────\n"
        "        # Drain pending steer BETWEEN individual tool calls so the\n",
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


def patch_loop(text: str) -> str:
    return replace_once(
        text,
        "                if agent._tool_guardrail_halt_decision is not None:\n",
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
    return replace_once(
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
        "agent/tool_executor.py": patch_executor,
        "agent/conversation_loop.py": patch_loop,
        "agent/system_prompt.py": patch_system_prompt,
        "agent/turn_finalizer.py": patch_turn_finalizer,
        "cli.py": patch_cli,
        "gateway/run.py": patch_gateway,
        "hermes_cli/kanban_db.py": patch_kanban,
        "scripts/docker_config_migrate.py": patch_docker_config_migrate,
        "tools/delegate_tool.py": patch_delegate,
        "tools/kanban_tools.py": patch_kanban_tools,
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
