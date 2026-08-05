#!/usr/bin/env python3
"""Exercise the patched Hermes worker spawn contract without starting a model."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from hermes_cli.kanban_db import Task, _default_spawn


def main() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        scratch_root = root / "scratch"
        scratch = scratch_root / "attempt"
        workspace = root / "workspace"
        scratch.mkdir(parents=True)
        workspace.mkdir()
        body = (
            "[hollysys-controller-card:v4]\n\n"
            "```json\n"
            f"{json.dumps({'scratch_dir': str(scratch)})}\n"
            "```\n"
        )
        task = Task(
            id="t_spawn_contract",
            title="spawn contract",
            body=body,
            assignee="spec-writer",
            status="running",
            priority=0,
            created_by="hollysys-controller",
            created_at=1,
            started_at=1,
            completed_at=None,
            workspace_kind="dir",
            workspace_path=str(workspace),
            claim_lock="claim",
            claim_expires=None,
            tenant="run",
            current_run_id=7,
            skills=[],
        )
        captured: dict[str, Any] = {}

        class FakeProcess:
            pid = 4242

        def fake_popen(command: list[str], **kwargs: Any) -> FakeProcess:
            captured["command"] = command
            captured["env"] = kwargs["env"]
            captured["cwd"] = kwargs["cwd"]
            return FakeProcess()

        previous = {
            key: os.environ.get(key)
            for key in ("HERMES_HOME", "HERMES_KANBAN_DB", "HERMES_SCRATCH_DIR")
        }
        original_popen = subprocess.Popen
        try:
            os.environ["HERMES_HOME"] = str(root / "hermes-home")
            os.environ["HERMES_KANBAN_DB"] = str(root / "kanban.db")
            os.environ["HERMES_SCRATCH_DIR"] = str(scratch_root)
            subprocess.Popen = fake_popen  # type: ignore[assignment]
            pid = _default_spawn(task, str(workspace), board="contract")
        finally:
            subprocess.Popen = original_popen
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        env = captured["env"]
        assert pid == 4242
        assert captured["cwd"] == str(workspace)
        assert env["HERMES_RUN_SCRATCH_DIR"] == str(scratch)
        assert env["HERMES_KANBAN_TASK"] == task.id
        assert env["HERMES_KANBAN_RUN_ID"] == "7"
        assert captured["command"][-1] == "-Q"

    print("worker spawn contract passed: pid=4242 scratch=validated")


if __name__ == "__main__":
    main()
