from __future__ import annotations

import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from hollysys_controller.errors import (
    ControllerFatalError,
    DependencyContractError,
    DependencyTransientError,
    ErrorContext,
    MergeBlocked,
    ReconcileSuperseded,
)
from hollysys_controller.gitlab import CheckedHeadConflict
from hollysys_controller.kanban import EventRecord
from hollysys_controller.service import ControllerService
from hollysys_controller.store import ControllerStore, ManagedCard
from tests.helpers import config, run_record


class LongRunningStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = ControllerStore(self.root / "controller.db")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_fresh_schema_rejects_unknown_versions(self) -> None:
        legacy = self.root / "legacy.db"
        with closing(sqlite3.connect(legacy)) as connection:
            connection.execute("PRAGMA user_version=2")
            connection.execute("CREATE TABLE legacy(value TEXT)")
            connection.commit()
        with self.assertRaisesRegex(
            ControllerFatalError,
            "fresh v4 state is required",
        ):
            ControllerStore(legacy)

    def test_existing_schema_rejects_state_machine_invariant_corruption(
        self,
    ) -> None:
        run_key = "hollysys-corrupt-state-0001"
        self.store.ensure_run_control(run_key)
        with self.store.connect() as connection:
            connection.execute(
                "UPDATE run_control SET state='mystery' WHERE run_key=?",
                (run_key,),
            )
        with self.assertRaisesRegex(
            ControllerFatalError,
            "controller_store_invariant_failed:unknown_run_state",
        ):
            ControllerStore(self.store.path)

    def test_runtime_controller_database_error_is_fatal(self) -> None:
        with self.store.connect() as connection:
            connection.execute("DROP TABLE requests")

        with self.assertRaisesRegex(
            ControllerFatalError,
            "controller_store_database_error",
        ):
            self.store.begin_request("request-1", "start", {})

    def test_new_boot_marks_unfinished_previous_boot_as_unclean(self) -> None:
        self.store.begin_boot("boot-1")
        self.store.begin_boot("boot-2")
        health = self.store.boot_health()
        self.assertEqual(health["boot_id"], "boot-2")
        self.assertEqual(health["last_exit_reason"], "unclean_restart_detected")
        self.assertTrue(health["last_exit_fatal"])

    def test_deployment_preflight_digest_is_not_exposed_by_health(self) -> None:
        self.store.record_deployment_preflight(
            ok=True,
            deep=True,
            credential_contract_digest="a" * 64,
        )
        public = self.store.deployment_preflight()
        internal = self.store.deployment_preflight(include_digest=True)
        assert public is not None
        assert internal is not None
        self.assertTrue(public["ok"])
        self.assertTrue(public["deep"])
        self.assertNotIn("credential_contract_digest", public)
        self.assertNotIn(
            "credential_contract_digest",
            self.store.health()["deployment_preflight"],
        )
        self.assertEqual(
            internal["credential_contract_digest"],
            "a" * 64,
        )

    def test_run_retry_and_terminal_state_are_authoritative(self) -> None:
        run_key = "hollysys-abcdefghijklmnopqrst"
        self.store.ensure_run_control(run_key)
        initial = self.store.run_control(run_key)
        assert initial is not None
        waiting = self.store.transition_run(
            run_key,
            expected_states={"active"},
            new_state="merge_wait",
            reason="merge_blocked:draft",
            expected_version=int(initial["state_version"]),
            next_retry_at=int(time.time()) + 60,
            checked_head="a" * 40,
        )
        self.assertEqual(waiting["state"], "merge_wait")
        self.assertNotIn(run_key, self.store.active_reconcile_run_keys())

        completed = self.store.mark_completed(
            run_key,
            external=True,
            compliance="unverified",
            checked_head="a" * 40,
            merge_commit_sha="b" * 40,
            reason="merged_without_current_controller_gate_evidence",
        )
        self.assertEqual(completed["state"], "completed")
        self.assertEqual(completed["completion_source"], "external")
        self.assertIsNotNone(completed["terminal_at"])
        self.assertNotIn(run_key, self.store.active_reconcile_run_keys())
        self.assertEqual(
            self.store.mark_completed(
                run_key,
                external=False,
                compliance="verified",
                checked_head="c" * 40,
                merge_commit_sha="d" * 40,
                reason="duplicate",
            )["state_version"],
            completed["state_version"],
        )

    def test_operation_expectations_and_uncertainty_are_persisted(self) -> None:
        payload = {"mr": 2}
        self.assertIsNone(
            self.store.operation_result(
                "merge",
                "merge",
                payload,
                expected_state_version=4,
                expected_head_sha="a" * 40,
            )
        )
        self.store.mark_operation_uncertain("merge", "network timeout")
        record = self.store.operation_record("merge")
        assert record is not None
        self.assertEqual(record["status"], "uncertain")
        self.assertIsNotNone(record["uncertain_at"])
        self.assertIsNone(
            self.store.operation_result(
                "merge",
                "merge",
                payload,
                expected_state_version=5,
                expected_head_sha="a" * 40,
            )
        )
        self.assertEqual(
            self.store.operation_record("merge")["expected_state_version"],
            5,
        )
        with self.assertRaisesRegex(ValueError, "expectation changed"):
            self.store.operation_result(
                "merge",
                "merge",
                payload,
                expected_state_version=6,
                expected_head_sha="b" * 40,
            )

    def test_late_worker_event_cannot_replace_current_attempt(self) -> None:
        self.store.register_card_attempt(
            board="gitlab-p12",
            card_id="t_work",
            profile="coder",
            dispatch_key="dispatch-1",
            worktree="/workspace/run",
            branch="feature/run",
        )
        self.store.record_card_runtime_event(
            board="gitlab-p12",
            card_id="t_work",
            kind="worker_started",
            created_at=100,
            worker_session_id="session-1",
            worker_pid=101,
            lease_seconds=30,
        )
        self.store.update_worker_watchdog(
            board="gitlab-p12",
            card_id="t_work",
            attempt_status="redispatch_requested",
            lease_seconds=30,
            reason="confirmed_worker_exit",
            increment_redispatch=True,
        )
        self.store.record_card_runtime_event(
            board="gitlab-p12",
            card_id="t_work",
            kind="worker_started",
            created_at=200,
            worker_session_id="session-2",
            worker_pid=202,
            lease_seconds=30,
        )
        self.store.record_card_runtime_event(
            board="gitlab-p12",
            card_id="t_work",
            kind="completed",
            created_at=210,
            worker_session_id="session-1",
            worker_pid=101,
            lease_seconds=30,
        )
        self.store.record_card_runtime_event(
            board="gitlab-p12",
            card_id="t_work",
            kind="worker_started",
            created_at=150,
            worker_session_id="session-1",
            worker_pid=101,
            lease_seconds=30,
        )
        runtime = self.store.card_runtime("gitlab-p12", "t_work")
        assert runtime is not None
        self.assertEqual(runtime["worker_session_id"], "session-2")
        self.assertEqual(runtime["worker_pid"], 202)
        self.assertEqual(runtime["attempt"], 2)
        self.assertEqual(runtime["redispatch_count"], 1)
        self.assertEqual(runtime["attempt_status"], "running")

    def test_late_heartbeat_cannot_resurrect_reclaimed_attempt(self) -> None:
        self.store.register_card_attempt(
            board="gitlab-p12",
            card_id="t_reclaimed_heartbeat",
            profile="coder",
            dispatch_key="dispatch-reclaimed-heartbeat",
            worktree="/workspace/run",
            branch="feature/run",
        )
        for kind, created_at in (
            ("claimed", 100),
            ("reclaimed", 200),
            ("heartbeat", 150),
        ):
            self.store.record_card_runtime_event(
                board="gitlab-p12",
                card_id="t_reclaimed_heartbeat",
                kind=kind,
                created_at=created_at,
                worker_session_id="kanban-run:17",
                worker_pid=170,
                lease_seconds=300,
            )

        runtime = self.store.card_runtime(
            "gitlab-p12",
            "t_reclaimed_heartbeat",
        )
        assert runtime is not None
        self.assertEqual(runtime["attempt_status"], "finished")
        self.assertEqual(runtime["terminal_reason"], "reclaimed")
        self.assertEqual(runtime["finished_at"], 200)
        self.assertIsNone(runtime["last_heartbeat_at"])

    def test_waitpid_closes_attempt_without_replacing_card_terminal_state(
        self,
    ) -> None:
        run_key = "hollysys-abcdefghijklmnopqrst"
        self.store.save_run(run_record(self.root))
        self.store.add_managed_card(
            board="gitlab-p12",
            card_id="t_waitpid",
            run_key=run_key,
            stage="code",
            iteration=1,
            idempotency_key="dispatch-waitpid",
            parent_card_id=None,
        )
        for kind, created_at in (
            ("claimed", 100),
            ("completed", 200),
            ("worker_exited", 210),
        ):
            self.store.record_card_runtime_event(
                board="gitlab-p12",
                card_id="t_waitpid",
                kind=kind,
                created_at=created_at,
                worker_session_id="kanban-run:18",
                worker_pid=180,
                lease_seconds=300,
                run_id="gitlab-p12:18",
            )

        runtime = self.store.card_runtime("gitlab-p12", "t_waitpid")
        assert runtime is not None
        self.assertEqual(runtime["attempt_status"], "finished")
        self.assertEqual(runtime["terminal_reason"], "completed")
        self.assertEqual(runtime["finished_at"], 200)
        attempt = self.store.attempts_for_run(run_key)[0]
        self.assertEqual(attempt["status"], "exited")
        self.assertEqual(attempt["exited_at"], 210)
        self.assertEqual(attempt["terminal_reason"], "completed")

    def test_dependency_tempfail_attempt_does_not_consume_redispatch_budget(
        self,
    ) -> None:
        self.store.register_card_attempt(
            board="gitlab-p12",
            card_id="t_tempfail",
            profile="spec-reviewer",
            dispatch_key="dispatch-tempfail",
            worktree="/workspace/run",
            branch="feature/run",
        )
        self.store.record_card_runtime_event(
            board="gitlab-p12",
            card_id="t_tempfail",
            kind="claimed",
            created_at=100,
            worker_session_id="kanban-run:1",
            worker_pid=101,
            lease_seconds=300,
        )
        self.store.record_card_runtime_event(
            board="gitlab-p12",
            card_id="t_tempfail",
            kind="rate_limited",
            created_at=160,
            worker_session_id="kanban-run:1",
            worker_pid=101,
            lease_seconds=300,
        )
        self.store.record_card_runtime_event(
            board="gitlab-p12",
            card_id="t_tempfail",
            kind="claimed",
            created_at=460,
            worker_session_id="kanban-run:2",
            worker_pid=202,
            lease_seconds=300,
        )

        runtime = self.store.card_runtime("gitlab-p12", "t_tempfail")
        assert runtime is not None
        self.assertEqual(runtime["attempt"], 2)
        self.assertEqual(runtime["redispatch_count"], 0)
        self.assertEqual(runtime["worker_session_id"], "kanban-run:2")

    def test_formal_failure_is_terminal_in_attempt_and_health_projection(
        self,
    ) -> None:
        run_key = "hollysys-abcdefghijklmnopqrst"
        self.store.save_run(run_record(self.root))
        for index, kind in enumerate(("timed_out", "gave_up"), start=1):
            card_id = f"t_terminal_{index}"
            run_id = str(9 + index)
            self.store.add_managed_card(
                board="gitlab-p12",
                card_id=card_id,
                run_key=run_key,
                stage="plan-review",
                iteration=1,
                idempotency_key=f"dispatch-{index}",
                parent_card_id=None,
            )
            self.store.record_card_runtime_event(
                board="gitlab-p12",
                card_id=card_id,
                kind="claimed",
                created_at=100,
                worker_session_id=f"kanban-run:{run_id}",
                worker_pid=100 + index,
                lease_seconds=30,
                run_id=run_id,
            )
            self.store.record_card_runtime_event(
                board="gitlab-p12",
                card_id=card_id,
                kind=kind,
                created_at=200,
                worker_session_id=f"kanban-run:{run_id}",
                worker_pid=100 + index,
                lease_seconds=30,
                run_id=run_id,
            )

        attempts = {
            item["card_id"]: item
            for item in self.store.attempts_for_run(run_key)
        }
        self.assertEqual(attempts["t_terminal_1"]["status"], "timed_out")
        self.assertEqual(
            attempts["t_terminal_1"]["terminal_reason"],
            "timed_out",
        )
        self.assertEqual(attempts["t_terminal_2"]["status"], "gave_up")
        self.assertEqual(
            attempts["t_terminal_2"]["terminal_reason"],
            "gave_up",
        )
        health = self.store.health()
        self.assertEqual(health["stale_workers"], [])
        timeline = {
            item["card_id"]: item for item in health["attempt_timeline"]
        }
        self.assertEqual(
            timeline["t_terminal_2"]["terminal_reason"],
            "gave_up",
        )

    def test_progress_metrics_and_health_fields_use_existing_attempt_tables(
        self,
    ) -> None:
        run_key = "hollysys-abcdefghijklmnopqrst"
        self.store.save_run(run_record(self.root))
        self.store.add_managed_card(
            board="gitlab-p12",
            card_id="t_metrics",
            run_key=run_key,
            stage="code",
            iteration=1,
            idempotency_key="dispatch-metrics",
            parent_card_id=None,
        )
        self.store.record_card_runtime_event(
            board="gitlab-p12",
            card_id="t_metrics",
            kind="claimed",
            created_at=int(time.time()) - 10,
            worker_session_id="kanban-run:12",
            worker_pid=212,
            lease_seconds=1800,
            run_id="gitlab-p12:12",
        )
        self.store.record_card_runtime_event(
            board="gitlab-p12",
            card_id="t_metrics",
            kind="progress",
            created_at=int(time.time()) - 2,
            worker_session_id="kanban-run:12",
            worker_pid=212,
            lease_seconds=1800,
            run_id="gitlab-p12:12",
            runtime_metrics={
                "model_wait": 3.5,
                "tool_execution": 1.25,
                "delegation_wait": 2.0,
                "retry_wait": 0.5,
            },
            progress_summary={
                "tool_categories": {"read": 2},
                "tool_count": 2,
                "elapsed_seconds": 7,
            },
        )
        self.store.record_supervisor_observation(
            "gitlab-p12:12",
            state="running",
            duration_ms=4,
            observed_at=int(time.time()) - 1,
        )

        attempts = self.store.attempts_for_run(run_key)
        self.assertEqual(len(attempts), 1)
        metrics = {item["metric"]: item for item in attempts[0]["metrics"]}
        self.assertEqual(metrics["model_wait"]["duration_ms"], 3500)
        self.assertEqual(metrics["tool_execution"]["duration_ms"], 1250)
        self.assertNotIn("password", str(metrics).lower())
        self.assertIn("heartbeat_age_seconds", attempts[0])
        self.assertIn("progress_age_seconds", attempts[0])
        self.assertEqual(attempts[0]["supervisor_state"]["state"], "running")

        health = self.store.health()
        timeline = next(
            item
            for item in health["attempt_timeline"]
            if item["card_id"] == "t_metrics"
        )
        self.assertIn("heartbeat_age_seconds", timeline)
        self.assertIn("progress_age_seconds", timeline)
        self.assertEqual(timeline["supervisor_state"]["state"], "running")

    def test_reclaimed_attempt_links_the_next_attempt_without_schema_change(
        self,
    ) -> None:
        run_key = "hollysys-abcdefghijklmnopqrst"
        self.store.save_run(run_record(self.root))
        self.store.add_managed_card(
            board="gitlab-p12",
            card_id="t_reclaimed",
            run_key=run_key,
            stage="code",
            iteration=1,
            idempotency_key="dispatch-reclaimed",
            parent_card_id=None,
        )
        for kind, event_at in (
            ("claimed", 100),
            ("reclaimed", 200),
            ("worker_exited", 210),
        ):
            self.store.record_card_runtime_event(
                board="gitlab-p12",
                card_id="t_reclaimed",
                kind=kind,
                created_at=event_at,
                worker_session_id="kanban-run:20",
                worker_pid=220,
                lease_seconds=1800,
                run_id="gitlab-p12:20",
            )
        self.store.record_card_runtime_event(
            board="gitlab-p12",
            card_id="t_reclaimed",
            kind="claimed",
            created_at=300,
            worker_session_id="kanban-run:21",
            worker_pid=221,
            lease_seconds=1800,
            run_id="gitlab-p12:21",
        )

        attempts = self.store.attempts_for_run(run_key)
        self.assertEqual(attempts[0]["status"], "reclaimed")
        self.assertEqual(attempts[0]["terminal_reason"], "reclaimed")
        self.assertEqual(attempts[0]["exited_at"], 200)
        link = next(
            metric
            for metric in attempts[1]["metrics"]
            if metric["metric"] == "redispatched_from_run_id"
        )
        self.assertEqual(link["summary"], "gitlab-p12:20")

    def test_abort_can_take_over_exception_and_is_restart_idempotent(self) -> None:
        run_key = "hollysys-abcdefghijklmnopqrst"
        self.store.ensure_run_control(run_key)
        self.store.set_run_exception(run_key, "human decision required")
        self.store.create_abort_request(
            request_id="abort-1",
            run_key=run_key,
            token_hash="token-hash",
            sender="ou_requester",
            chat_id="oc_chat",
            thread_id="omt_thread",
            reason="stop",
            expires_at=int(time.time()) + 600,
        )
        confirmed = self.store.confirm_abort_request(
            run_key=run_key,
            token_hash="token-hash",
            sender="ou_requester",
            chat_id="oc_chat",
            thread_id="omt_thread",
            message_id="om_confirm",
        )
        self.assertEqual(confirmed["state"], "abort_requested")
        self.store.mark_aborting(run_key)
        version = self.store.run_control(run_key)["state_version"]
        self.store.mark_aborting(run_key)
        self.assertEqual(
            self.store.run_control(run_key)["state_version"],
            version,
        )
        self.store.set_merge_wait(
            run_key,
            mr_iid=2,
            head_sha="a" * 40,
            blocker_kind="approval_missing",
            blocker="approval required",
            retry_seconds=30,
        )
        self.store.finish_abort(
            run_key,
            "completed_before_abort",
            checked_head="a" * 40,
            merge_commit_sha="b" * 40,
        )
        terminal = self.store.run_control(run_key)
        self.assertEqual(terminal["state"], "completed_before_abort")
        self.assertEqual(terminal["checked_head"], "a" * 40)
        self.assertEqual(terminal["merge_commit_sha"], "b" * 40)
        self.assertIsNone(self.store.merge_wait(run_key))


class LongRunningServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_lifecycle_notification_dependency_failure_is_replayable(
        self,
    ) -> None:
        cfg = config(self.root)
        store = ControllerStore(cfg.state_dir / "controller.db")
        service = ControllerService(cfg, store=store)
        persisted_run = run_record(self.root)
        store.save_run(persisted_run)
        managed = ManagedCard(
            board="gitlab-p12",
            card_id="t_agent",
            run_key=persisted_run.run_key,
            stage="implement",
            iteration=1,
            idempotency_key="dispatch-1",
            parent_card_id="t_parent",
            purpose="work",
            created_at=90,
        )
        event = EventRecord(
            id=1,
            task_id=managed.card_id,
            run_id=1,
            kind="worker_started",
            payload={"worker_session_id": "session-1", "worker_pid": 101},
            created_at=100,
        )
        store.add_managed_card(
            board=managed.board,
            card_id=managed.card_id,
            run_key=managed.run_key,
            stage=managed.stage,
            iteration=managed.iteration,
            idempotency_key=managed.idempotency_key,
            parent_card_id=managed.parent_card_id,
            purpose=managed.purpose,
            created_at=managed.created_at,
        )
        unavailable = DependencyTransientError(
            "Kanban DB is locked",
            context=ErrorContext(
                dependency="kanban",
                endpoint="task_history",
                error_code="database_locked",
            ),
        )

        with patch.object(
            service,
            "_history",
            side_effect=unavailable,
        ), self.assertRaises(DependencyTransientError):
            service._record_agent_lifecycle_event(managed, event)

        runtime = store.card_runtime(managed.board, managed.card_id)
        self.assertEqual(runtime["worker_session_id"], "session-1")
        self.assertEqual(runtime["attempt"], 1)

    def test_successful_kanban_retry_closes_outage_and_restores_runs(
        self,
    ) -> None:
        cfg = config(self.root)
        store = ControllerStore(cfg.state_dir / "controller.db")
        service = ControllerService(cfg, store=store)
        run_key = "hollysys-kanban-recovery-01"
        dependency = f"kanban:{run_key}"
        store.ensure_run_control(run_key)
        outage = store.record_dependency_failure(
            dependency,
            "database is locked",
            initial_backoff_seconds=1,
            maximum_backoff_seconds=10,
            endpoint="kanban:promote",
        )
        store.associate_outage_run(
            dependency,
            str(outage["outage_id"]),
            run_key,
        )
        control = store.run_control(run_key)
        assert control is not None
        store.transition_run(
            run_key,
            expected_states={"active"},
            new_state="dependency_degraded",
            reason="kanban:dependency_transient:command_failed",
            expected_version=int(control["state_version"]),
            next_retry_at=int(outage["next_retry_at"]),
        )

        service._recover_run_dependency(run_key, "kanban")
        self.assertEqual(
            store.run_control(run_key)["state"],
            "dependency_degraded",
        )
        with patch(
            "hollysys_controller.service.time.time",
            return_value=int(outage["next_retry_at"]),
        ):
            service._recover_run_dependency(run_key, "kanban")

        restored = store.run_control(run_key)
        assert restored is not None
        self.assertEqual(restored["state"], "active")
        self.assertEqual(
            restored["last_transition_reason"],
            "kanban_dependency_recovered",
        )
        self.assertEqual(store.open_dependency_outages(), [])
        self.assertEqual(
            store.dependency_outage_history()[0]["dependency"],
            dependency,
        )

    def test_transient_gitlab_failure_is_scoped_to_one_run(self) -> None:
        cfg = config(self.root)
        store = ControllerStore(cfg.state_dir / "controller.db")
        service = ControllerService(cfg, store=store)
        failed_run = "hollysys-scoped-timeout-01"
        other_run = "hollysys-independent-run-02"
        store.ensure_run_control(failed_run)
        store.ensure_run_control(other_run)

        outage = service._handle_dependency_error(
            failed_run,
            DependencyTransientError(
                "GitLab timed out",
                context=ErrorContext(
                    dependency="gitlab",
                    endpoint="projects/12",
                    error_code="timeout",
                ),
            ),
        )

        self.assertEqual(
            outage["dependency"],
            f"gitlab:{failed_run}",
        )
        self.assertFalse(service._dependency_retry_blocked("gitlab"))
        self.assertEqual(
            store.run_control(failed_run)["state"],
            "dependency_degraded",
        )
        self.assertEqual(store.run_control(other_run)["state"], "active")

    def test_repeated_gitlab_transient_failures_remain_run_scoped(
        self,
    ) -> None:
        cfg = config(self.root).model_copy(
            update={"dependency_circuit_failure_threshold": 2}
        )
        store = ControllerStore(cfg.state_dir / "controller.db")
        service = ControllerService(cfg, store=store)
        run_key = "hollysys-repeated-timeout-01"
        store.ensure_run_control(run_key)
        error = DependencyTransientError(
            "GitLab timed out",
            context=ErrorContext(
                dependency="gitlab",
                endpoint="projects/12",
                error_code="timeout",
            ),
        )

        service._handle_dependency_error(run_key, error)
        self.assertFalse(service._dependency_retry_blocked("gitlab"))
        service._handle_dependency_error(run_key, error)

        self.assertFalse(service._dependency_retry_blocked("gitlab"))
        dependencies = {
            item["dependency"]
            for item in store.open_dependency_outages()
        }
        self.assertEqual(
            dependencies,
            {f"gitlab:{run_key}"},
        )

    def test_distinct_gitlab_transient_failures_open_global_circuit(
        self,
    ) -> None:
        cfg = config(self.root).model_copy(
            update={"dependency_circuit_failure_threshold": 2}
        )
        store = ControllerStore(cfg.state_dir / "controller.db")
        service = ControllerService(cfg, store=store)
        first_run = "hollysys-correlated-timeout-01"
        second_run = "hollysys-correlated-timeout-02"
        for run_key in (first_run, second_run):
            store.ensure_run_control(run_key)
        error = DependencyTransientError(
            "GitLab timed out",
            context=ErrorContext(
                dependency="gitlab",
                endpoint="projects/12",
                error_code="timeout",
            ),
        )

        service._handle_dependency_error(first_run, error)
        self.assertFalse(service._dependency_retry_blocked("gitlab"))
        service._handle_dependency_error(second_run, error)

        self.assertTrue(service._dependency_retry_blocked("gitlab"))
        global_outage = next(
            item
            for item in store.open_dependency_outages()
            if item["dependency"] == "gitlab"
        )
        self.assertEqual(
            set(
                store.outage_run_keys(
                    "gitlab",
                    str(global_outage["outage_id"]),
                )
            ),
            {first_run, second_run},
        )

    def test_active_mode_requires_matching_deep_preflight(self) -> None:
        cfg = config(self.root)
        store = ControllerStore(cfg.state_dir / "controller.db")
        service = ControllerService(cfg, store=store)
        service._protected_executable = lambda path: True

        with self.assertRaisesRegex(
            ControllerFatalError,
            "requires_successful_deep_preflight",
        ):
            service.assert_activation_preflight()

        store.record_deployment_preflight(
            ok=True,
            deep=True,
            credential_contract_digest="accepted",
        )
        with self.assertRaisesRegex(
            ControllerFatalError,
            "requires_worker_supervisor_preflight",
        ):
            service.assert_activation_preflight()
        store.record_deployment_preflight(
            ok=True,
            deep=True,
            credential_contract_digest="worker-supervisor-v1:accepted",
        )
        with patch(
            "hollysys_controller.service.summarize_profile_preflight",
            return_value={
                "ok": True,
                "_credential_contract_digest": "accepted",
            },
        ):
            service.assert_activation_preflight()
        with patch(
            "hollysys_controller.service.summarize_profile_preflight",
            return_value={
                "ok": True,
                "_credential_contract_digest": "changed",
            },
        ), self.assertRaisesRegex(
            ControllerFatalError,
            "profile_contract_changed_after_deep_preflight",
        ):
            service.assert_activation_preflight()

    def test_abort_dependency_failure_uses_persisted_retry_schedule(
        self,
    ) -> None:
        cfg = config(self.root)
        store = ControllerStore(cfg.state_dir / "controller.db")
        service = ControllerService(cfg, store=store)
        run_key = "hollysys-abort-backoff-01"
        store.ensure_run_control(run_key)
        store.create_abort_request(
            request_id="abort-backoff",
            run_key=run_key,
            token_hash="hash",
            sender="ou_owner",
            chat_id="oc_chat",
            thread_id="omt_thread",
            reason="stop",
            expires_at=int(time.time()) + 600,
        )
        store.confirm_abort_request(
            run_key=run_key,
            token_hash="hash",
            sender="ou_owner",
            chat_id="oc_chat",
            thread_id="omt_thread",
            message_id="om_confirm",
        )

        service._handle_dependency_error(
            run_key,
            DependencyTransientError(
                "database is locked",
                context=ErrorContext(
                    dependency="kanban",
                    endpoint="kanban:archive",
                    error_code="command_failed",
                ),
            ),
        )

        control = store.run_control(run_key)
        assert control is not None
        retry_at = int(control["next_retry_at"])
        self.assertEqual(control["state"], "abort_requested")
        self.assertEqual(store.active_abort_run_keys(now=retry_at - 1), [])
        self.assertEqual(
            store.active_abort_run_keys(now=retry_at),
            [run_key],
        )
        ControllerStore(store.path)

    def test_durable_start_request_respects_gitlab_circuit_backoff(
        self,
    ) -> None:
        cfg = config(self.root)
        store = ControllerStore(cfg.state_dir / "controller.db")
        service = ControllerService(cfg, store=store)
        store.begin_request(
            "start:om_retry",
            "start",
            {"message_id": "om_retry"},
        )
        store.record_dependency_failure(
            "gitlab",
            "401 Unauthorized",
            initial_backoff_seconds=60,
            maximum_backoff_seconds=60,
            endpoint="user",
        )
        calls: list[dict] = []
        service.start = lambda payload: calls.append(payload) or {}

        service.reconcile_all()

        self.assertEqual(calls, [])
        self.assertEqual(
            store.running_requests()[0]["request_key"],
            "start:om_retry",
        )

    def test_durable_start_request_respects_bound_run_backoff(self) -> None:
        cfg = config(self.root)
        store = ControllerStore(cfg.state_dir / "controller.db")
        service = ControllerService(cfg, store=store)
        run_key = "hollysys-start-bound-run-01"
        request_key = "start:om_bound_retry"
        store.begin_request(
            request_key,
            "start",
            {"message_id": "om_bound_retry"},
        )
        store.bind_request_run(request_key, run_key)
        store.record_dependency_failure(
            f"kanban:{run_key}",
            "database is locked",
            initial_backoff_seconds=60,
            maximum_backoff_seconds=60,
            endpoint="kanban:create",
        )
        calls: list[dict] = []
        service.start = lambda payload: calls.append(payload) or {}

        service.reconcile_all()

        self.assertEqual(calls, [])
        running = store.running_requests()[0]
        self.assertEqual(running["request_key"], request_key)
        self.assertEqual(running["run_key"], run_key)

    def test_due_gitlab_circuit_uses_one_probe_before_requests(self) -> None:
        cfg = config(self.root)
        store = ControllerStore(cfg.state_dir / "controller.db")
        service = ControllerService(cfg, store=store)
        store.begin_request(
            "start:om_half_open",
            "start",
            {"message_id": "om_half_open"},
        )
        outage = store.record_dependency_failure(
            "gitlab",
            "service unavailable",
            initial_backoff_seconds=1,
            maximum_backoff_seconds=1,
            endpoint="user",
        )
        request_calls: list[dict] = []
        probe_calls: list[str] = []
        service.start = lambda payload: request_calls.append(payload) or {}

        def unavailable_probe():
            probe_calls.append("health")
            raise DependencyTransientError(
                "service unavailable",
                context=ErrorContext(
                    dependency="gitlab",
                    endpoint="user",
                    error_code="timeout",
                ),
            )

        service.gitlab = type(
            "UnavailableGitLab",
            (),
            {"health": staticmethod(unavailable_probe)},
        )()
        with patch(
            "hollysys_controller.service.time.time",
            return_value=int(outage["next_retry_at"]),
        ):
            service.reconcile_all()

        self.assertEqual(request_calls, [])
        self.assertEqual(probe_calls, ["health"])

    def test_different_runs_do_not_share_a_reconcile_lock(self) -> None:
        service = ControllerService.__new__(ControllerService)
        active = 0
        maximum = 0
        guard = threading.Lock()
        entered = threading.Barrier(2)

        def reconcile(run_key: str) -> None:
            nonlocal active, maximum
            with guard:
                active += 1
                maximum = max(maximum, active)
            entered.wait(timeout=2)
            with guard:
                active -= 1

        service._reconcile_run = reconcile
        threads = [
            threading.Thread(target=service.reconcile_run, args=(run_key,))
            for run_key in ("run-a", "run-b")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(maximum, 2)

    def test_same_run_has_only_one_inflight_reconcile(self) -> None:
        service = ControllerService.__new__(ControllerService)
        entered = threading.Event()
        release = threading.Event()
        calls = 0

        def reconcile(run_key: str) -> None:
            nonlocal calls
            calls += 1
            entered.set()
            release.wait(timeout=2)

        service._reconcile_run = reconcile
        first = threading.Thread(
            target=service.reconcile_run,
            args=("run-a",),
        )
        first.start()
        self.assertTrue(entered.wait(timeout=1))
        self.assertFalse(service.reconcile_run("run-a"))
        release.set()
        first.join(timeout=2)
        self.assertFalse(first.is_alive())
        self.assertEqual(calls, 1)

    def test_external_operation_result_is_discarded_after_state_change(
        self,
    ) -> None:
        cfg = config(self.root)
        store = ControllerStore(cfg.state_dir / "controller.db")
        run_key = "hollysys-operation-race-01"
        store.ensure_run_control(run_key)
        service = ControllerService(cfg, store=store)
        version = int(store.run_control(run_key)["state_version"])

        def action() -> dict:
            store.set_run_exception(run_key, "concurrent state change")
            return {"remote": "committed"}

        with self.assertRaises(ReconcileSuperseded):
            service._operation(
                f"{run_key}:mutation",
                "mutation",
                {"run_key": run_key},
                action,
                run_key=run_key,
                expected_state_version=version,
            )
        operation = store.operation_record(f"{run_key}:mutation")
        self.assertEqual(operation["status"], "done")

    def test_merge_blocker_pauses_operation_without_failed_mutation(self) -> None:
        cfg = config(self.root)
        store = ControllerStore(cfg.state_dir / "controller.db")
        run_key = "hollysys-operation-blocked-01"
        store.ensure_run_control(run_key)
        service = ControllerService(cfg, store=store)
        version = int(store.run_control(run_key)["state_version"])

        with self.assertRaises(MergeBlocked):
            service._operation(
                f"{run_key}:merge",
                "checked-head-merge",
                {"run_key": run_key},
                lambda: (_ for _ in ()).throw(
                    MergeBlocked(
                        "discussion_unresolved",
                        "new discussion appeared before merge",
                    )
                ),
                run_key=run_key,
                expected_state_version=version,
                expected_head_sha="a" * 40,
            )

        operation = store.operation_record(f"{run_key}:merge")
        self.assertEqual(operation["status"], "blocked")
        self.assertEqual(store.health()["failed_operations"], 0)

    def test_checked_head_drift_supersedes_merge_operation(self) -> None:
        cfg = config(self.root)
        store = ControllerStore(cfg.state_dir / "controller.db")
        run_key = "hollysys-operation-superseded-01"
        store.ensure_run_control(run_key)
        service = ControllerService(cfg, store=store)
        version = int(store.run_control(run_key)["state_version"])

        with self.assertRaises(CheckedHeadConflict):
            service._operation(
                f"{run_key}:merge",
                "checked-head-merge",
                {"run_key": run_key},
                lambda: (_ for _ in ()).throw(
                    CheckedHeadConflict(
                        "merge evidence changed before checked-head merge"
                    )
                ),
                run_key=run_key,
                expected_state_version=version,
                expected_head_sha="a" * 40,
            )

        operation = store.operation_record(f"{run_key}:merge")
        self.assertEqual(operation["status"], "superseded")
        self.assertEqual(store.health()["failed_operations"], 0)

    def test_external_merge_becomes_completed_without_rescheduling(self) -> None:
        cfg = config(self.root)
        store = ControllerStore(cfg.state_dir / "controller.db")
        run = run_record(self.root)
        store.ensure_run_control(run.run_key)
        service = ControllerService(cfg, store=store)
        service._finalize_merged(
            run,
            [],
            {
                "iid": 2,
                "state": "merged",
                "sha": "a" * 40,
                "merge_commit_sha": "b" * 40,
                "web_url": (
                    "https://gitlab.example.com/group/project/-/merge_requests/2"
                ),
            },
        )
        control = store.run_control(run.run_key)
        assert control is not None
        self.assertEqual(control["state"], "completed")
        self.assertEqual(control["completion_source"], "external")
        self.assertEqual(control["compliance"], "unverified")
        self.assertNotIn(run.run_key, store.active_reconcile_run_keys())
        outbox = store.pending_outbox()
        self.assertEqual(outbox[0]["event"], "merged-policy-violation")

    def test_merged_terminal_rejects_missing_checked_head(self) -> None:
        cfg = config(self.root)
        store = ControllerStore(cfg.state_dir / "controller.db")
        run = run_record(self.root)
        store.ensure_run_control(run.run_key)
        service = ControllerService(cfg, store=store)

        with self.assertRaises(DependencyContractError):
            service._finalize_merged(
                run,
                [],
                {
                    "iid": 2,
                    "state": "merged",
                    "sha": "",
                    "web_url": (
                        "https://gitlab.example.com/group/project/"
                        "-/merge_requests/2"
                    ),
                },
            )

        self.assertEqual(store.run_control(run.run_key)["state"], "active")

    def test_superseded_merge_operation_does_not_claim_external_merge(self) -> None:
        cfg = config(self.root)
        store = ControllerStore(cfg.state_dir / "controller.db")
        run = run_record(self.root)
        store.ensure_run_control(run.run_key)
        service = ControllerService(cfg, store=store)
        head = "a" * 40
        operation_key = f"{run.run_key}:merge:{head}"
        store.operation_result(
            operation_key,
            "checked-head-merge",
            {
                "project_id": run.project.project_id,
                "mr_iid": 2,
                "checked_head": head,
            },
            expected_state_version=1,
            expected_head_sha=head,
        )
        store.supersede_operation(operation_key, "gate evidence changed")

        service._finalize_merged(
            run,
            [],
            {
                "iid": 2,
                "state": "merged",
                "sha": head,
                "merge_commit_sha": "b" * 40,
                "web_url": (
                    "https://gitlab.example.com/group/project/-/merge_requests/2"
                ),
            },
        )

        control = store.run_control(run.run_key)
        self.assertEqual(control["completion_source"], "external")
        self.assertEqual(
            store.operation_record(operation_key)["status"],
            "superseded",
        )

    def test_unfinished_merge_operation_does_not_claim_external_merge(self) -> None:
        cfg = config(self.root)
        store = ControllerStore(cfg.state_dir / "controller.db")
        run = run_record(self.root)
        store.ensure_run_control(run.run_key)
        service = ControllerService(cfg, store=store)
        head = "a" * 40
        operation_key = f"{run.run_key}:merge:{head}"
        store.operation_result(
            operation_key,
            "checked-head-merge",
            {
                "project_id": run.project.project_id,
                "mr_iid": 2,
                "checked_head": head,
            },
            expected_state_version=1,
            expected_head_sha=head,
        )

        service._finalize_merged(
            run,
            [],
            {
                "iid": 2,
                "state": "merged",
                "sha": head,
                "merge_commit_sha": "b" * 40,
                "web_url": (
                    "https://gitlab.example.com/group/project/-/merge_requests/2"
                ),
            },
        )

        control = store.run_control(run.run_key)
        self.assertEqual(control["completion_source"], "external")
        self.assertEqual(control["compliance"], "unverified")
        self.assertEqual(
            store.operation_record(operation_key)["status"],
            "executing",
        )


if __name__ == "__main__":
    unittest.main()
