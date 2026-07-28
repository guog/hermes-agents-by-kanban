from __future__ import annotations

from pathlib import Path

from hollysys_controller.config import ControllerConfig
from hollysys_controller.models import (
    CompletionMetadata,
    FeishuOrigin,
    ProjectFacts,
    RunRecord,
    SourceFacts,
    Stage,
    WorkspaceFacts,
)


def config(tmp_path: Path) -> ControllerConfig:
    return ControllerConfig(
        hermes_home=tmp_path / "data",
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "state" / "controller.sock",
        lock_path=tmp_path / "state" / "controller.lock",
        profiles_root=tmp_path / "profiles",
        projects_root=tmp_path / "projects",
        token_file=tmp_path / "token",
        gitlab_host="gitlab.example.com",
        allowed_groups=["group"],
        stage_assignees={
            Stage.SPEC_WRITE: "spec-writer",
            Stage.SPEC_REVIEW: "spec-reviewer",
            Stage.PLAN_WRITE: "planner",
            Stage.PLAN_REVIEW: "plan-reviewer",
            Stage.TASKS_WRITE: "tasker",
            Stage.TASKS_REVIEW: "task-reviewer",
            Stage.IMPLEMENT: "coder",
            Stage.TEST: "tester",
            Stage.CODE_REVIEW: "code-reviewer",
        },
        stage_skills={stage: [f"skill-{stage.value}", "glab"] for stage in Stage},
        artifact_patterns={
            "spec-review": ["docs/specs/**/*.md"],
            "plan-review": ["docs/plans/**/*.md"],
            "tasks-review": ["docs/tasks/**/*.md"],
        },
        reviewer_identities={
            "spec-review": ["spec-reviewer"],
            "plan-review": ["plan-reviewer"],
            "tasks-review": ["tasks-reviewer"],
            "test": ["tester"],
            "code-review": ["code-reviewer"],
        },
    )


def origin() -> FeishuOrigin:
    return FeishuOrigin(
        message_id="om_abc",
        chat_id="oc_abc",
        thread_id="omt_abc",
        chat_type="group",
        initiator_open_id="ou_abc",
    )


def run_record(tmp_path: Path) -> RunRecord:
    return RunRecord(
        run_key="hollysys-abcdefghijklmnopqrst",
        project=ProjectFacts(
            host="gitlab.example.com",
            project_id=12,
            project_path="group/project",
            project_display_name="项目",
            default_branch="main",
        ),
        source=SourceFacts(
            prd_path="docs/prds/example.md",
            prd_commit_sha="a" * 40,
            prd_blob_url=(
                "https://gitlab.example.com/group/project/-/blob/"
                + "a" * 40
                + "/docs/prds/example.md"
            ),
            prd_mr_url=("https://gitlab.example.com/group/project/-/merge_requests/1"),
        ),
        workspace=WorkspaceFacts(
            board="gitlab-p12",
            checkout=str(tmp_path / "projects" / "p12-project"),
            worktree=str(
                tmp_path
                / "projects"
                / "worktrees"
                / "p12"
                / "hollysys-abcdefghijklmnopqrst"
            ),
            branch="feature/example-aaaaaaaa",
            target_branch="main",
        ),
        origin=origin(),
    )


def completion(
    tmp_path: Path,
    stage: Stage = Stage.SPEC_WRITE,
    **overrides,
) -> CompletionMetadata:
    run = run_record(tmp_path)
    data = {
        "protocol_version": "hollysys-controller/v1",
        "run_key": run.run_key,
        "stage": stage,
        "iteration": 1,
        "outcome": "pass",
        "project_id": run.project.project_id,
        "project_path": run.project.project_path,
        "checkout": run.workspace.checkout,
        "worktree": run.workspace.worktree,
        "branch": run.workspace.branch,
        "target_branch": run.workspace.target_branch,
        "prd_path": run.source.prd_path,
        "prd_commit_sha": run.source.prd_commit_sha,
        "prd_mr_url": str(run.source.prd_mr_url),
        "kanban_card_id": "t_abc",
        "verification": ["unit tests"],
    }
    data.update(overrides)
    return CompletionMetadata.model_validate(data)
