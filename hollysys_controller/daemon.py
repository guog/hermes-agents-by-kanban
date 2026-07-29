from __future__ import annotations

import asyncio
import fcntl
import logging
import signal
import sys
from pathlib import Path

from .config import ControllerConfig
from .models import RpcRequest
from .rpc import RpcServer
from .service import ControllerService

LOG = logging.getLogger("hollysys_controller")


class ControllerDaemon:
    def __init__(self, config: ControllerConfig):
        self.config = config
        self.service = ControllerService(config)
        self.rpc = RpcServer(config.socket_path, self.handle_rpc)
        self.stop_event = asyncio.Event()

    async def handle_rpc(self, request: RpcRequest) -> dict:
        if request.method == "start":
            return await asyncio.to_thread(self.service.start, request.params)
        if request.method == "status":
            run_key = str(request.params.get("run_key") or "")
            if not run_key:
                raise ValueError("run_key is required")
            return await asyncio.to_thread(self.service.status, run_key)
        if request.method == "resolve":
            return await asyncio.to_thread(self.service.resolve, request.params)
        if request.method == "abort-request":
            return await asyncio.to_thread(
                self.service.abort_request, request.params
            )
        if request.method == "abort-confirm":
            return await asyncio.to_thread(
                self.service.abort_confirm, request.params
            )
        if request.method == "preflight":
            return await asyncio.to_thread(self.service.preflight)
        if request.method == "validate-completion":
            return await asyncio.to_thread(
                self.service.validate_completion,
                request.params,
            )
        if request.method == "health":
            return await asyncio.to_thread(
                self.service.health,
                str(request.params.get("probe") or "readiness"),
            )
        raise ValueError(f"unsupported method {request.method}")

    async def run(self) -> None:
        await self.rpc.start()
        try:
            await asyncio.to_thread(self.service.reconcile_all)
        except Exception:
            LOG.exception(
                "initial reconciliation degraded; background retry remains active"
            )
            await asyncio.to_thread(self.service.flush_outbox)
        poll_task = asyncio.create_task(self._poll_loop(), name="kanban-event-poll")
        reconcile_task = asyncio.create_task(
            self._reconcile_loop(), name="full-reconcile"
        )
        stop_task = asyncio.create_task(self.stop_event.wait(), name="shutdown")
        try:
            done, _ = await asyncio.wait(
                (stop_task, poll_task, reconcile_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in done:
                if task is not stop_task:
                    task.result()
        finally:
            for task in (stop_task, poll_task, reconcile_task):
                task.cancel()
            await asyncio.gather(
                stop_task,
                poll_task,
                reconcile_task,
                return_exceptions=True,
            )
            await self.rpc.close()

    async def _poll_loop(self) -> None:
        while True:
            delay = self.config.poll_interval_seconds
            try:
                await asyncio.to_thread(self.service.poll_once)
                await asyncio.to_thread(
                    self.service.store.recover_dependency,
                    "kanban-event-poll",
                )
            except Exception as exc:
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
            except Exception as exc:
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
                )
                delay = max(
                    1,
                    int(outage["next_retry_at"]) - int(outage["updated_at"]),
                )
                await asyncio.sleep(delay)


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
