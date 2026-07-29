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
        gitlab_host="green-git.hollysys.net",
        controller_mode="active",
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


def write_profile_env(
    cfg: ControllerConfig,
    *,
    profile: str = "dispatcher",
    token: str = "controller-token",
    mode: int = 0o600,
) -> Path:
    profile_root = cfg.profiles_root / profile
    (profile_root / "home").mkdir(parents=True, exist_ok=True)
    env_file = profile_root / ".env"
    env_file.write_text(
        "\n".join(
            [
                f"HERMES_PROFILE={profile}",
                f"GITLAB_HOST={cfg.gitlab_base_url}",
                f"GITLAB_ALLOWED_GROUPS={','.join(cfg.allowed_groups)}",
                f"GITLAB_TOKEN={token}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    env_file.chmod(mode)
    return env_file


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
            host="green-git.hollysys.net",
            project_id=12,
            project_path="group/project",
            project_display_name="项目",
            default_branch="main",
        ),
        source=SourceFacts(
            prd_path="docs/prds/example.md",
            prd_commit_sha="a" * 40,
            prd_blob_sha="f" * 40,
            prd_blob_url=(
                "https://green-git.hollysys.net/group/project/-/blob/"
                + "a" * 40
                + "/docs/prds/example.md"
            ),
            prd_mr_url=(
                "https://green-git.hollysys.net/group/project/-/merge_requests/1"
            ),
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
            repository_base_sha="9" * 40,
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
        "protocol_version": "hollysys-controller/v3",
        "run_key": run.run_key,
        "stage": stage,
        "iteration": 1,
        "mode": "normal",
        "outcome": "pass",
        "project_id": run.project.project_id,
        "project_path": run.project.project_path,
        "checkout": run.workspace.checkout,
        "worktree": run.workspace.worktree,
        "branch": run.workspace.branch,
        "target_branch": run.workspace.target_branch,
        "prd_path": run.source.prd_path,
        "prd_commit_sha": run.source.prd_commit_sha,
        "prd_blob_sha": run.source.prd_blob_sha,
        "prd_mr_url": str(run.source.prd_mr_url),
        "kanban_card_id": "t_abc",
        "verification": ["unit tests"],
    }
    data.update(overrides)
    if (
        stage
        in {
            Stage.SPEC_WRITE,
            Stage.PLAN_WRITE,
            Stage.TASKS_WRITE,
            Stage.IMPLEMENT,
        }
        and data["outcome"] == "pass"
    ):
        data.setdefault("mr_iid", 2)
        data.setdefault(
            "mr_url",
            "https://gitlab.example.com/group/project/-/merge_requests/2",
        )
        data.setdefault("head_sha", "d" * 40)
        if "repository_evidence" not in overrides:
            data["repository_evidence"] = {
                "repository_base_sha": run.workspace.repository_base_sha,
                "inspected_paths": [
                    "src/existing-module",
                    "docs/architecture.md",
                ],
                "existing_capabilities": ["existing MES application framework"],
                "change_strategy": "extend_existing",
                "reuse_decisions": [
                    "reuse the existing module and conventions"
                ],
            }
    if (
        stage == Stage.TEST
        and data["outcome"] in {"pass", "fail"}
        and "test_disposition" not in overrides
    ):
        data["test_disposition"] = "executed"
    if (
        stage in {Stage.TASKS_REVIEW, Stage.CODE_REVIEW}
        and data["outcome"] == "pass"
        and "gate_phase" not in overrides
    ):
        is_entry = stage == Stage.TASKS_REVIEW
        data.update(
            {
                "gate_phase": (
                    "implementation_entry"
                    if is_entry
                    else "implementation_completion"
                ),
                "gate_decision": "approved",
                "gate_reviewer": (
                    "id:11" if is_entry else "id:10"
                ),
                "gate_reviewed_at": "2026-07-28T00:00:00+08:00",
                "gate_reason": "frozen TASKS contract is satisfied",
                "gate_evidence_refs": [
                    (
                        "https://green-git.hollysys.net/group/project/"
                        f"-/merge_requests/2#note_{21 if is_entry else 22}"
                    )
                ],
                "gate_artifact_paths": (
                    data.get("artifact_paths")
                    if is_entry
                    else ["docs/tasks/feature/tasks.md"]
                ),
                "gate_artifact_commit_sha": (
                    data.get("artifact_commit_sha") if is_entry else "c" * 40
                ),
                "gate_artifact_digest": (
                    data.get("artifact_digest") if is_entry else "b" * 64
                ),
                "contract_refs": ["PLAN-BLK-001"],
                "requirement_ids": ["OP-001"],
            }
        )
    return CompletionMetadata.model_validate(data)
