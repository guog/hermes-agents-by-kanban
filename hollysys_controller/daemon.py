from __future__ import annotations

import asyncio
import fcntl
import logging
import signal
import sys
import uuid
from pathlib import Path

from .config import ControllerConfig
from .errors import ControllerFatalError, DependencyError
from .models import RpcRequest
from .rpc import RpcServer
from .service import ControllerService

LOG = logging.getLogger("hollysys_controller")


class ControllerDaemon:
    def __init__(self, config: ControllerConfig):
        self.config = config
        self.service = ControllerService(config)
        self.boot_id = uuid.uuid4().hex
        self.service.store.begin_boot(self.boot_id)
        self.rpc = RpcServer(config.socket_path, self.handle_rpc)
        self.stop_event = asyncio.Event()
        self.fatal_event = asyncio.Event()
        self.rpc_fatal_error: ControllerFatalError | None = None
        self.background_requests: set[asyncio.Task[dict]] = set()

    async def handle_rpc(self, request: RpcRequest) -> dict:
        try:
            if request.method == "start":
                return await self._handle_start(request.params)
            if request.method in {"status", "status-summary"}:
                run_key = str(request.params.get("run_key") or "")
                if not run_key:
                    raise ValueError("run_key is required")
                return await asyncio.to_thread(
                    self.service.status_summary,
                    run_key,
                )
            if request.method == "resolve":
                return await asyncio.to_thread(
                    self.service.resolve,
                    request.params,
                )
            if request.method == "recover":
                return await asyncio.to_thread(
                    self.service.recover,
                    request.params,
                )
            if request.method == "abort-request":
                return await asyncio.to_thread(
                    self.service.abort_request, request.params
                )
            if request.method == "abort-confirm":
                return await asyncio.to_thread(
                    self.service.abort_confirm, request.params
                )
            if request.method == "preflight":
                return await asyncio.to_thread(
                    self.service.preflight,
                    deep=bool(request.params.get("deep", False)),
                )
            if request.method == "validate-completion":
                return await asyncio.to_thread(
                    self.service.validate_completion,
                    request.params,
                )
            if request.method == "publish-delivery":
                return await asyncio.to_thread(
                    self.service.publish_delivery,
                    request.params,
                )
            if request.method == "card-context":
                return await asyncio.to_thread(
                    self.service.card_context,
                    request.params,
                )
            if request.method == "completion-template":
                return await asyncio.to_thread(
                    self.service.completion_template,
                    request.params,
                )
            if request.method == "validate-artifact":
                return await asyncio.to_thread(
                    self.service.validate_artifact,
                    request.params,
                )
            if request.method == "health":
                return await asyncio.to_thread(
                    self.service.health,
                    str(request.params.get("probe") or "readiness"),
                )
            raise ValueError(f"unsupported method {request.method}")
        except ControllerFatalError as exc:
            self.rpc_fatal_error = exc
            self.fatal_event.set()
            raise

    async def _handle_start(self, params: dict) -> dict:
        """Accept long cold-start work without holding the RPC client open."""
        task = asyncio.create_task(
            asyncio.to_thread(self.service.start, params),
            name="start-request",
        )
        self.background_requests.add(task)
        task.add_done_callback(self._background_request_done)
        try:
            return await asyncio.wait_for(
                asyncio.shield(task),
                timeout=self.config.start_request_sync_timeout_seconds,
            )
        except TimeoutError:
            return await asyncio.to_thread(
                self.service.start_request_status,
                params,
            )

    def _background_request_done(self, task: asyncio.Task[dict]) -> None:
        self.background_requests.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            LOG.error(
                "accepted background start request failed: %s",
                error,
            )
            if isinstance(error, ControllerFatalError):
                self.rpc_fatal_error = error
                self.fatal_event.set()

    async def run(self) -> None:
        await self.rpc.start()
        stop_task = asyncio.create_task(self.stop_event.wait(), name="shutdown")
        fatal_task = asyncio.create_task(
            self.fatal_event.wait(),
            name="rpc-fatal",
        )
        background: list[asyncio.Task] = []
        exit_reason = "clean_shutdown"
        fatal = False
        try:
            if self.config.controller_mode == "active":
                await asyncio.to_thread(
                    self.service.assert_activation_preflight
                )
                await asyncio.to_thread(self.service.reconcile_all)
                background = [
                    asyncio.create_task(
                        self._poll_loop(),
                        name="kanban-event-poll",
                    ),
                    asyncio.create_task(
                        self._reconcile_loop(),
                        name="full-reconcile",
                    ),
                    asyncio.create_task(
                        self._outbox_loop(),
                        name="durable-outbox",
                    ),
                    *[
                        asyncio.create_task(
                            self._reconcile_intent_loop(index),
                            name=f"reconcile-intent-{index}",
                        )
                        for index in range(
                            max(1, int(self.config.reconcile_workers))
                        )
                    ],
                ]
            done, _ = await asyncio.wait(
                (stop_task, fatal_task, *background),
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in done:
                if task is fatal_task:
                    raise self.rpc_fatal_error or ControllerFatalError(
                        "rpc_fatal_without_error"
                    )
                if task is not stop_task:
                    task.result()
        except ControllerFatalError as exc:
            fatal = True
            exit_reason = f"controller_fatal:{exc}"
            raise
        except Exception as exc:
            fatal = True
            exit_reason = f"unclassified_fatal:{type(exc).__name__}:{exc}"
            raise ControllerFatalError(exit_reason) from exc
        finally:
            request_tasks = tuple(self.background_requests)
            for task in (stop_task, fatal_task, *background, *request_tasks):
                task.cancel()
            await asyncio.gather(
                stop_task,
                fatal_task,
                *background,
                *request_tasks,
                return_exceptions=True,
            )
            await self.rpc.close()
            await asyncio.to_thread(
                self.service.store.finish_boot,
                self.boot_id,
                exit_reason=exit_reason,
                fatal=fatal,
            )

    async def _poll_loop(self) -> None:
        while True:
            delay = self.config.poll_interval_seconds
            try:
                await asyncio.to_thread(self.service.poll_once)
                await asyncio.to_thread(
                    self.service.store.recover_dependency,
                    "kanban-event-poll",
                )
            except ControllerFatalError:
                raise
            except DependencyError as exc:
                LOG.exception(
                    "event polling degraded; dependency retry remains active"
                )
                outage = await asyncio.to_thread(
                    self.service.store.record_dependency_failure,
                    "kanban-event-poll",
                    str(exc),
                    initial_backoff_seconds=(
                        self.config.dependency_backoff_initial_seconds
                    ),
                    maximum_backoff_seconds=(
                        self.config.dependency_backoff_max_seconds
                    ),
                    error_class=exc.error_class,
                    endpoint=exc.context.endpoint,
                    retry_after_seconds=exc.context.retry_after_seconds,
                )
                delay = max(
                    delay,
                    int(outage["next_retry_at"]) - int(outage["updated_at"]),
                )
            await asyncio.sleep(delay)

    async def _reconcile_loop(self) -> None:
        while True:
            await asyncio.sleep(self.config.reconcile_interval_seconds)
            try:
                await asyncio.to_thread(self.service.reconcile_all)
                await asyncio.to_thread(
                    self.service.store.recover_dependency,
                    "full-reconcile",
                )
            except ControllerFatalError:
                raise
            except DependencyError as exc:
                LOG.exception(
                    "full reconciliation degraded; dependency retry remains active"
                )
                outage = await asyncio.to_thread(
                    self.service.store.record_dependency_failure,
                    "full-reconcile",
                    str(exc),
                    initial_backoff_seconds=(
                        self.config.dependency_backoff_initial_seconds
                    ),
                    maximum_backoff_seconds=(
                        self.config.dependency_backoff_max_seconds
                    ),
                    error_class=exc.error_class,
                    endpoint=exc.context.endpoint,
                    retry_after_seconds=exc.context.retry_after_seconds,
                )
                delay = max(
                    1,
                    int(outage["next_retry_at"]) - int(outage["updated_at"]),
                )
                await asyncio.sleep(delay)

    async def _reconcile_intent_loop(self, index: int) -> None:
        lease_owner = f"{self.boot_id}:{index}"
        while True:
            try:
                consumed = await asyncio.to_thread(
                    self.service.consume_reconcile_once,
                    lease_owner,
                )
            except ControllerFatalError:
                raise
            except Exception:
                LOG.exception("persistent reconcile intent failed")
                await asyncio.sleep(1)
                continue
            if not consumed:
                await asyncio.sleep(0.5)

    async def _outbox_loop(self) -> None:
        while True:
            await asyncio.to_thread(self.service.flush_outbox)
            await asyncio.sleep(self.config.outbox_poll_interval_seconds)


def _acquire_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise RuntimeError(f"another Hollysys controller holds {path}") from exc
    handle.seek(0)
    handle.truncate()
    handle.write(f"{os_getpid()}\n")
    handle.flush()
    return handle


def os_getpid() -> int:
    # Kept in a tiny function so the lock behavior is easy to unit test.
    import os

    return os.getpid()


async def _main() -> None:
    config = ControllerConfig.load()
    config.read_token()
    lock = _acquire_lock(config.lock_path)
    daemon = ControllerDaemon(config)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, daemon.stop_event.set)
    try:
        await daemon.run()
    finally:
        lock.close()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        asyncio.run(_main())
    except Exception:
        LOG.exception("controller stopped")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
