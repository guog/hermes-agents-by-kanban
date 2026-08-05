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

    def test_fresh_docker_config_is_migrated_and_version_stamped(self) -> None:
        source = (
            "    get_env_path,\n"
            "    migrate_config,\n"
            "\n"
            "    if current_ver < SUPPORT_FLOOR_VERSION:\n"
        )

        patched = self.patch.patch_docker_config_migrate(source)

        self.assertIn("_raw_config_has_explicit_version,", patched)
        self.assertIn(
            "_raw_config_has_explicit_version()\n"
            "        and current_ver < SUPPORT_FLOOR_VERSION",
            patched,
        )

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
            "def complete_task(\n"
            "    now = int(time.time())\n"
            "\n"
            "    # Gate: verify created_cards BEFORE the main write txn. A rejected\n"
            "        if phantom_cards:\n"
            "            with write_txn(conn):\n"
            "                _append_event(\n"
            "    if kind is not None and kind not in VALID_BLOCK_KINDS:\n"
            "    now = int(time.time())\n"
            "    with write_txn(conn):\n"
            "        if expected_run_id is None:\n"
            "            cur = conn.execute(\n"
            '                "UPDATE tasks SET last_heartbeat_at = ? "\n'
            "    failure is still counted into ``consecutive_failures``.\n"
            '    """\n'
            "    if failure_limit is None:\n"
            "        failure_limit = DEFAULT_FAILURE_LIMIT\n"
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
        self.assertIn("def _worker_expected_run_id", patched)
        self.assertGreaterEqual(
            patched.count("_worker_expected_run_id(task_id, expected_run_id)"),
            4,
        )
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

    def test_parent_worker_mutations_forward_atomic_attempt_identity(self) -> None:
        source = (
            '    return stamped\n'
            "\n"
            "            cid = kb.add_comment(conn, tid, author=author, body=str(body))\n"
            "            att_id = kb.store_attachment_bytes(\n"
            "                conn,\n"
            "                tid,\n"
            "                str(filename),\n"
            "                data,\n"
            "                content_type=content_type,\n"
            '                uploaded_by="agent",\n'
            "                board=board,\n"
            "            )\n"
            "            att_id = kb.store_attachment_bytes(\n"
            "                conn,\n"
            "                tid,\n"
            "                str(filename),\n"
            "                data,\n"
            "                content_type=content_type or fetched_ct,\n"
            '                uploaded_by="agent",\n'
            "                board=board,\n"
            "            )\n"
            "                project_source_task_id=project_source_task_id,\n"
            "                triage=triage,\n"
            "            kb.link_tasks(conn, parent_id=parent_id, child_id=child_id)\n"
        )

        patched = self.patch.patch_kanban_tool_attempt_fencing(source)

        self.assertIn("def _worker_attempt_kwargs", patched)
        self.assertGreaterEqual(patched.count("**_worker_attempt_kwargs()"), 5)

    def test_mutation_db_checks_owner_run_inside_write_transaction(self) -> None:
        source = (
            "def add_comment(\n"
            "    conn: sqlite3.Connection, task_id: str, author: str, body: str\n"
            ") -> int:\n"
            "    if not author or not author.strip():\n"
            '        raise ValueError("comment author is required")\n'
            "    now = int(time.time())\n"
            "    with write_txn(conn):\n"
            "        if not conn.execute(\n"
            '            "SELECT 1 FROM tasks WHERE id = ?", (task_id,)\n'
            "        ).fetchone():\n"
            "\n"
            "def link_tasks(conn: sqlite3.Connection, parent_id: str, child_id: str) -> None:\n"
            "    if parent_id == child_id:\n"
            '        raise ValueError("a task cannot depend on itself")\n'
            "    with write_txn(conn):\n"
            "        missing = _find_missing_parents(conn, [parent_id, child_id])\n"
        )

        patched = self.patch.patch_mutation_db_attempt_fencing(source)

        self.assertIn("def _assert_expected_worker_attempt", patched)
        self.assertIn("expected_owner_task_id: Optional[str]", patched)
        self.assertIn("expected_run_id: Optional[int]", patched)
        self.assertIn('os.environ.get("HERMES_KANBAN_RUN_ID")', patched)
        self.assertEqual(
            patched.count("_assert_expected_worker_attempt("),
            3,
        )

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
            "        if function_name == \"memory\":\n"
            "            agent._turns_since_memory = 0\n"
            "        elif function_name == \"skill_manage\":\n"
            "            agent._iters_since_skill = 0\n"
            "\n"
            "        _advance_start_order(_begin)\n"
            "        return execute(final_args)\n"
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

    def test_executor_fences_stale_attempt_before_every_tool(self) -> None:
        source = (
            "logger = logging.getLogger(__name__)\n\n\n"
            "def _ensure_file_checkpoint(\n"
            "\n"
            "        if function_name == \"memory\":\n"
            "            agent._turns_since_memory = 0\n"
            "        elif function_name == \"skill_manage\":\n"
            "            agent._iters_since_skill = 0\n"
            "\n"
            "        _advance_start_order(_begin)\n"
            "        return execute(final_args)\n"
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

        self.assertIn("def _hollysys_attempt_is_current", patched)
        self.assertIn('"error": "stale_attempt"', patched)
        self.assertIn("agent._hollysys_stale_attempt = True", patched)
        self.assertLess(
            patched.index("_hollysys_attempt_is_current"),
            patched.index("return execute(final_args)"),
        )
        self.assertNotIn('"worker_exited", function_name', patched)

    def test_parent_tool_progress_is_throttled_and_payload_is_structured(self) -> None:
        source = (
            "logger = logging.getLogger(__name__)\n"
            "\n"
            "        # ── Per-tool /steer drain ───────────────────────────────────\n"
            "        # Same as the sequential path: drain between each collected\n"
            "\n"
            "        # ── Per-tool /steer drain ───────────────────────────────────\n"
            "        # Drain pending steer BETWEEN individual tool calls so the\n"
        )

        patched = self.patch.patch_executor_progress(source)

        self.assertIn("_HOLLYSYS_PROGRESS_INTERVAL_SECONDS = 300", patched)
        self.assertIn("def _hollysys_record_progress", patched)
        self.assertIn('"tool_categories"', patched)
        self.assertIn('"metrics"', patched)
        self.assertNotIn("function_args", patched)
        self.assertEqual(patched.count("_hollysys_record_progress("), 3)

    def test_model_and_retry_wait_metrics_are_mutually_classified(self) -> None:
        source = (
            "logger = logging.getLogger(__name__)\n"
            "\n"
            "                _redirect_crossed_response = False\n"
            "                try:\n"
            "                    response = run_llm_execution_middleware(\n"
            "                finally:\n"
            "                    if _redirect_lock is not None:\n"
            "\n"
            "                    sleep_end = time.time() + wait_time\n"
            "                    _backoff_touch_counter = 0\n"
            "                    while time.time() < sleep_end:\n"
            "\n"
            "                    if _retry.restart_with_redirected_messages:\n"
            "\n"
            "                sleep_end = time.time() + wait_time\n"
            "                _backoff_touch_counter = 0\n"
            "                while time.time() < sleep_end:\n"
            "\n"
            "                if _retry.restart_with_redirected_messages:\n"
        )

        patched = self.patch.patch_loop_metrics(source)

        self.assertIn("def _hollysys_add_runtime_metric", patched)
        self.assertIn('"model_wait"', patched)
        self.assertGreaterEqual(patched.count('"retry_wait"'), 3)

    def test_progress_event_is_attempt_bound(self) -> None:
        source = "\n\ndef heartbeat_worker(\n"

        patched = self.patch.patch_progress_worker(source)

        self.assertIn("def progress_worker", patched)
        self.assertIn("expected_run_id", patched)
        self.assertIn('"progress"', patched)
        self.assertIn('row["current_run_id"]', patched)
        self.assertIn("with write_txn(conn)", patched)

    def test_conversation_loop_ends_stale_attempt_without_exit_claim(self) -> None:
        source = (
            "                agent._execute_tool_calls("
            "assistant_message, messages, effective_task_id, api_call_count)\n\n"
            "                if agent._tool_guardrail_halt_decision is not None:\n"
        )

        patched = self.patch.patch_loop(source)

        self.assertIn("_hollysys_stale_attempt", patched)
        self.assertIn('"stale_attempt"', patched)
        self.assertNotIn('"worker_exited terminal_tool="', patched)

    def test_native_recovery_skips_controller_managed_tasks(self) -> None:
        source = (
            '        "WHERE status = \'running\' AND claim_expires IS NOT NULL "\n'
            '        "  AND claim_expires < ?",\n'
            '        "WHERE t.status = \'running\' AND t.max_runtime_seconds IS NOT NULL "\n'
            '        "  AND COALESCE(r.started_at, t.started_at) IS NOT NULL "\n'
            '        "  AND t.worker_pid IS NOT NULL"\n'
            '        "WHERE t.status = \'running\'"\n'
            '            "WHERE status = \'running\' AND worker_pid IS NOT NULL"\n'
        )

        patched = self.patch.patch_native_recovery_fencing(source)

        self.assertEqual(patched.count("hollysys-controller"), 4)
        self.assertIn("COALESCE(t.created_by, '')", patched)
        self.assertIn("COALESCE(created_by, '')", patched)

    def test_reclaim_cli_requires_attempt_cas_identity(self) -> None:
        source = (
            '    p_reclaim.add_argument("task_id")\n'
            "    p_reclaim.add_argument(\n"
            '        "--reason", default=None,\n'
            '        help="Human-readable reason (recorded on the reclaimed event)",\n'
            "    )\n"
            '    p_archive.add_argument("task_ids", nargs="*",\n'
            '                           help="Task ids to archive (default mode)")\n'
            "\n"
            "def _cmd_reclaim(args: argparse.Namespace) -> int:\n"
            "    with kb.connect_closing() as conn:\n"
            "        ok = kb.reclaim_task(\n"
            "            conn, args.task_id,\n"
            '            reason=getattr(args, "reason", None),\n'
            "        )\n"
            "            if not kb.archive_task(conn, tid):\n"
        )

        patched = self.patch.patch_kanban_cli(source)

        self.assertIn('"--expected-run-id"', patched)
        self.assertIn('"--expected-worker-pid"', patched)
        self.assertIn('"--archive"', patched)
        self.assertIn("expected_run_id=getattr", patched)
        self.assertIn("expected_worker_pid=getattr", patched)
        self.assertIn("archive_after_reclaim=bool", patched)
        self.assertIn('"--expected-unclaimed"', patched)
        self.assertIn("expected_unclaimed=bool", patched)

    def test_controller_reclaim_is_double_cas_and_never_resignals(self) -> None:
        source = (
            "def reclaim_task(\n"
            "    conn: sqlite3.Connection,\n"
            "    task_id: str,\n"
            "    *,\n"
            "    reason: Optional[str] = None,\n"
            "    signal_fn=None,\n"
            ") -> bool:\n"
            "    row = conn.execute(\n"
            '        "SELECT status, claim_lock, worker_pid FROM tasks WHERE id = ?",\n'
            "        (task_id,),\n"
            "    ).fetchone()\n"
            "    if not row:\n"
            "        return False\n"
            '    if row["status"] != "running" and row["claim_lock"] is None:\n'
            "        # Nothing to reclaim — already ready / blocked / done.\n"
            "        return False\n"
            '    prev_lock = row["claim_lock"]\n'
            "    termination = _terminate_reclaimed_worker(\n"
            '        row["worker_pid"], prev_lock, signal_fn=signal_fn,\n'
            "    )\n"
            "    with write_txn(conn):\n"
            "        cur = conn.execute(\n"
            '            "UPDATE tasks SET status = \'ready\', claim_lock = NULL, "\n'
            '            "claim_expires = NULL, worker_pid = NULL "\n'
            '            "WHERE id = ? AND status IN (\'running\', \'ready\', \'blocked\') "\n'
            '            "AND claim_lock IS ?",\n'
            "            (task_id, prev_lock),\n"
            "        )\n"
            "        _append_event(\n"
            '            conn, task_id, "reclaimed",\n'
            "            payload,\n"
            "            run_id=run_id,\n"
            "        )\n"
            "    _clear_failure_counter(conn, task_id)\n"
            "    return True\n"
        )

        patched = self.patch.patch_reclaim_cas(source)

        self.assertIn("expected_run_id: Optional[int]", patched)
        self.assertIn("expected_worker_pid: Optional[int]", patched)
        self.assertIn('row["created_by"] != "hollysys-controller"', patched)
        self.assertIn('"supervisor_confirmed": True', patched)
        self.assertIn("AND current_run_id = ? AND worker_pid = ?", patched)
        self.assertIn('target_status = "archived"', patched)
        self.assertIn('conn, task_id, "archived"', patched)

    def test_controller_archive_is_transactionally_unclaimed(self) -> None:
        source = (
            "def archive_task(conn: sqlite3.Connection, task_id: str) -> bool:\n"
            "    with write_txn(conn):\n"
            "        cur = conn.execute(\n"
            '            "UPDATE tasks SET status = \'archived\', "\n'
            '            "    claim_lock = NULL, claim_expires = NULL, worker_pid = NULL "\n'
            '            "WHERE id = ? AND status != \'archived\'",\n'
            "            (task_id,),\n"
            "        )\n"
        )

        patched = self.patch.patch_archive_cas(source)

        self.assertIn("expected_unclaimed: bool", patched)
        self.assertIn("worker_pid IS NULL", patched)
        self.assertIn("claim_lock IS NULL", patched)
        self.assertIn("current_run_id IS NULL", patched)

    def test_waitpid_is_the_worker_exit_event_source(self) -> None:
        source = (
            '_recent_worker_exits: "dict[int, tuple[int, float]]" = {}\n'
            "\n"
            "\n"
            "def _record_worker_exit(pid: int, raw_status: int) -> None:\n"
            '    """Record a reaped child\'s exit status for later classification.\n'
            "\n"
            "    Called from the reap loop in ``dispatch_once``. Safe to call many\n"
            "    times; duplicate pids overwrite (pids can cycle, latest wins).\n"
            '    """\n'
            "    if not pid or pid <= 0:\n"
            "        return\n"
            "    now = time.time()\n"
            "    _recent_worker_exits[int(pid)] = (int(raw_status), now)\n"
            "\n"
            "            if pid:\n"
            "                _set_worker_pid(conn, claimed.id, int(pid))\n"
            "\n"
            "            if pid:\n"
            "                _set_worker_pid(conn, claimed.id, int(pid))\n"
        )

        patched = self.patch.patch_worker_exit_events(source)

        self.assertIn("_worker_attempts", patched)
        self.assertIn('"worker_exited"', patched)
        self.assertIn("_register_worker_attempt", patched)
        self.assertEqual(patched.count("_register_worker_attempt("), 3)

    def test_controller_worker_receives_validated_attempt_scratch(self) -> None:
        source = (
            "    if task.tenant:\n"
            '        env["HERMES_TENANT"] = task.tenant\n'
            '    env["HERMES_KANBAN_TASK"] = task.id\n'
        )

        patched = self.patch.patch_worker_run_scratch(source)

        self.assertIn('task.created_by == "hollysys-controller"', patched)
        self.assertIn("os.path.commonpath", patched)
        self.assertIn('env["HERMES_RUN_SCRATCH_DIR"]', patched)

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
