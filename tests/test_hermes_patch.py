from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


class HermesPatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        path = (
            Path(__file__).parents[1]
            / "container"
            / "patch-hermes-terminal.py"
        )
        spec = importlib.util.spec_from_file_location("hermes_patch", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cls.patch = module

    def test_conversation_loop_stops_after_terminal_tool(self) -> None:
        source = (
            "                agent._execute_tool_calls("
            "assistant_message, messages, effective_task_id, api_call_count)\n\n"
            "                if agent._tool_guardrail_halt_decision is not None:\n"
        )

        patched = self.patch.patch_loop(source)

        self.assertIn("hollysys_terminal_tool", patched)

    def test_successful_terminal_exit_is_not_reported_as_stuck(self) -> None:
        source = (
            '    if _last_msg_role == "tool" and not interrupted:\n'
            '        # Agent was mid-work — this is the "just stops" case.\n'
            "        logger.warning(\n"
            '            "Turn ended with pending tool result (agent may appear stuck). "\n'
            '            + _diag_msg + " last_tool=%s",\n'
            "            *_diag_args, _last_tool_name,\n"
            "        )\n"
            "    else:\n"
            "        logger.info(_diag_msg, *_diag_args)\n"
            "\n"
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
            "                )\n"
        )

        patched = self.patch.patch_turn_finalizer(source)

        self.assertIn(
            '_turn_exit_reason != "hollysys_terminal_tool"',
            patched,
        )
        self.assertIn("skipped delegated child budget failure", patched)
        self.assertIn("expected_run_id=_kanban_run_id", patched)
        self.assertIn("skipped stale budget failure", patched)
        self.assertIn("missing valid run id", patched)

    def test_transport_exhaustion_uses_kanban_tempfail_exit(self) -> None:
        source = '                            ) in ("rate_limit", "billing"):\n'

        patched = self.patch.patch_cli(source)

        self.assertIn('"timeout"', patched)
        self.assertIn('"server_error"', patched)
        self.assertIn('"upstream_rate_limit"', patched)

    def test_all_kanban_workers_use_machine_exit_contract(self) -> None:
        source = (
            "    if task.goal_mode:\n"
            "        # Goal-mode workers must take the fully-quiet single-query path:\n"
            "        # the kanban goal-loop hook (_run_kanban_goal_loop_q) only runs in\n"
            "        # cli.py's quiet branch. Without -Q the worker gets exactly one\n"
            "        # turn, prints text, exits rc=0, and the dispatcher records a\n"
            "        # protocol violation (incident 2026-06-09 t_d9cbe312).\n"
            '        cmd.append("-Q")\n'
            "\n"
            "    event_payload_extra: Optional[dict] = None,\n"
            ") -> bool:\n"
            "\n"
            "    Returns True when the task was auto-blocked (counter reached\n"
            "    ``failure_limit``), False when it was just updated in place.\n"
            "\n"
            '            "SELECT consecutive_failures, status, max_retries "\n'
            '            "FROM tasks WHERE id = ?", (task_id,),\n'
            "        ).fetchone()\n"
            "        if row is None:\n"
            "            return False\n"
            '        failures = int(row["consecutive_failures"]) + 1\n'
        )

        patched = self.patch.patch_kanban(source)

        self.assertNotIn("if task.goal_mode", patched)
        self.assertIn('    cmd.append("-Q")', patched)
        self.assertIn("human-facing -q branch", patched)
        self.assertIn("expected_run_id: Optional[int]", patched)
        self.assertIn('row["current_run_id"]', patched)
        self.assertIn("return None", patched)

    def test_delegated_children_cannot_mutate_parent_kanban_task(self) -> None:
        source = (
            '        "cronjob",  # no scheduling more work in the parent\'s name\n'
            "\n"
            '        "\\nComplete this task using the tools available to you. "\n'
        )

        patched = self.patch.patch_delegate(source)

        for tool in (
            "kanban_complete",
            "kanban_block",
            "kanban_heartbeat",
            "kanban_comment",
            "kanban_create",
            "kanban_link",
            "kanban_unblock",
            "kanban_attach",
            "kanban_attach_url",
        ):
            self.assertIn(f'"{tool}"', patched)
        self.assertIn("delegated child, not", patched)
        self.assertIn("Return findings to", patched)

    def test_completion_uses_stable_dispatch_attempt_provenance(self) -> None:
        source = (
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
            "    return stamped\n"
            "\n"
            "    metadata = _stamp_worker_session_metadata(tid, metadata)\n"
        )

        patched = self.patch.patch_kanban_tools(source)

        self.assertIn('session_id=kw.get("session_id")', patched)
        self.assertIn("trusted_session_id", patched)
        self.assertIn('f"kanban-run:{run_id}"', patched)
        self.assertIn("HERMES_KANBAN_RUN_ID", patched)
        self.assertIn("process-global HERMES_SESSION_ID", patched)

    def test_gateway_binds_triggering_message_id_to_session_context(self) -> None:
        source = (
            "        # Build session context\n"
            "        context = build_session_context(source, self.config, session_entry)\n"
        )

        patched = self.patch.patch_gateway(source)

        self.assertIn(
            "source.message_id = str(event.message_id)",
            patched,
        )
        self.assertLess(
            patched.index("source.message_id"),
            patched.index("build_session_context"),
        )

    def test_executor_rejects_terminal_tools_from_delegated_session(self) -> None:
        source = (
            "logger = logging.getLogger(__name__)\n\n\n"
            "def _ensure_file_checkpoint(\n"
            "\n"
            '        if function_name == "todo":\n'
            "\n"
            "        # ── Per-tool /steer drain ───────────────────────────────────\n"
            "        # Drain pending steer BETWEEN individual tool calls so the\n"
            "\n"
            "    for kind, calls in segments:\n"
            "\n"
            '        if getattr(agent, "_incremental_persistence_failed", False):\n'
            "            return\n\n"
            "    # ── Whole-turn finalize (budget + /steer) ─────────────────────────\n"
        )

        patched = self.patch.patch_executor(source)

        self.assertIn('getattr(agent, "_parent_session_id", None)', patched)
        self.assertIn("delegated subagents cannot complete or block", patched)
        self.assertIn("for segment_index, (kind, calls)", patched)
        self.assertIn("segments[segment_index + 1:]", patched)
        self.assertIn("terminal skipped tool result", patched)

    def test_named_profile_prompt_uses_active_and_default_roots(self) -> None:
        source = (
            "    else:\n"
            "        post_workspace_parts.append(\n"
            "            f\"Active Hermes profile: {active_profile}. This session reads \"\n"
            "            f\"and writes {get_hermes_home()}/profiles/{active_profile}/. The default \"\n"
            "            f\"profile's data lives at {get_hermes_home()}/skills/, {get_hermes_home()}/plugins/, \"\n"
            "            f\"{get_hermes_home()}/cron/, {get_hermes_home()}/memories/ — those belong to a \"\n"
        )

        patched = self.patch.patch_system_prompt(source)

        self.assertIn("get_default_hermes_root", patched)
        self.assertIn('f"and writes {get_hermes_home()}/.', patched)
        self.assertIn(
            "f\"profile's data lives at {profile_root}/skills/",
            patched,
        )


if __name__ == "__main__":
    unittest.main()
