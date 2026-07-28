from __future__ import annotations

import os
import stat
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import Stage


class ControllerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hermes_home: Path = Path("/opt/data")
    state_dir: Path = Path("/opt/data/controller")
    socket_path: Path = Path("/opt/data/controller/controller.sock")
    lock_path: Path = Path("/opt/data/controller/controller.lock")
    config_path: Path = Path("/opt/hollysys-controller/config.yaml")
    profiles_root: Path = Path("/opt/data/profiles")
    projects_root: Path = Path("/workspace/projects")
    token_file: Path = Path("/opt/data/controller/gitlab-token")
    hermes_command: str = "hermes"
    glab_command: str = "glab"
    lark_command: str = "lark-cli"
    controller_profile: str = "dispatcher"
    poll_interval_seconds: float = Field(default=2.0, gt=0)
    reconcile_interval_seconds: float = Field(default=30.0, gt=0)
    command_timeout_seconds: int = Field(default=120, gt=0)
    fatal_loop_error_limit: int = Field(default=5, gt=0)
    gitlab_host: str = ""
    allowed_groups: list[str] = Field(default_factory=list)
    required_pipeline: bool = True
    stage_assignees: dict[Stage, str]
    stage_skills: dict[Stage, list[str]]
    artifact_patterns: dict[str, list[str]]
    reviewer_identities: dict[str, list[str]]
    design_rework_limit: int = 3
    code_rework_limit: int = 5
    protocol_retry_limit: int = 2

    @model_validator(mode="after")
    def validate_controller_contract(self) -> ControllerConfig:
        if not self.gitlab_host or "://" in self.gitlab_host or "/" in self.gitlab_host:
            raise ValueError("gitlab_host must be a non-empty hostname")
        if not self.allowed_groups:
            raise ValueError("allowed_groups must contain at least one GitLab group")
        expected_stages = set(Stage)
        if set(self.stage_assignees) != expected_stages:
            raise ValueError("stage_assignees must define every workflow stage")
        if set(self.stage_skills) != expected_stages or any(
            not skills for skills in self.stage_skills.values()
        ):
            raise ValueError(
                "stage_skills must define a non-empty list for every stage"
            )
        missing_patterns = {
            stage
            for stage in {
                Stage.SPEC_REVIEW.value,
                Stage.PLAN_REVIEW.value,
                Stage.TASKS_REVIEW.value,
            }
            if not self.artifact_patterns.get(stage)
        }
        if missing_patterns:
            raise ValueError(
                "artifact_patterns missing: " + ", ".join(sorted(missing_patterns))
            )
        missing_identities = {
            stage
            for stage in {
                Stage.SPEC_REVIEW.value,
                Stage.PLAN_REVIEW.value,
                Stage.TASKS_REVIEW.value,
                Stage.TEST.value,
                Stage.CODE_REVIEW.value,
            }
            if not self.reviewer_identities.get(stage)
        }
        if missing_identities:
            raise ValueError(
                "reviewer_identities missing: " + ", ".join(sorted(missing_identities))
            )
        return self

    @classmethod
    def load(cls) -> ControllerConfig:
        config_path = Path(
            os.environ.get(
                "HOLLYSYS_CONTROLLER_CONFIG", "/opt/hollysys-controller/config.yaml"
            )
        )
        data: dict = {}
        if config_path.is_file():
            data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        data.update(
            {
                "config_path": config_path,
                "hermes_home": Path(os.environ.get("HERMES_HOME", "/opt/data")),
                "state_dir": Path(
                    os.environ.get(
                        "HOLLYSYS_CONTROLLER_STATE_DIR", "/opt/data/controller"
                    )
                ),
                "socket_path": Path(
                    os.environ.get(
                        "HOLLYSYS_CONTROLLER_SOCKET",
                        "/opt/data/controller/controller.sock",
                    )
                ),
                "lock_path": Path(
                    os.environ.get(
                        "HOLLYSYS_CONTROLLER_LOCK",
                        "/opt/data/controller/controller.lock",
                    )
                ),
                "profiles_root": Path(
                    os.environ.get("HOLLYSYS_PROFILES_ROOT", "/opt/data/profiles")
                ),
                "projects_root": Path(
                    os.environ.get("HOLLYSYS_PROJECTS_ROOT", "/workspace/projects")
                ),
                "token_file": Path(
                    os.environ.get(
                        "HOLLYSYS_GITLAB_TOKEN_FILE",
                        "/opt/data/controller/gitlab-token",
                    )
                ),
            }
        )
        env_map = {
            "HOLLYSYS_GITLAB_HOST": "gitlab_host",
            "HOLLYSYS_GITLAB_ALLOWED_GROUPS": "allowed_groups",
            "HOLLYSYS_SPEC_REVIEWER_IDENTITIES": ("reviewer_identities", "spec-review"),
            "HOLLYSYS_PLAN_REVIEWER_IDENTITIES": ("reviewer_identities", "plan-review"),
            "HOLLYSYS_TASKS_REVIEWER_IDENTITIES": (
                "reviewer_identities",
                "tasks-review",
            ),
            "HOLLYSYS_TESTER_IDENTITIES": ("reviewer_identities", "test"),
            "HOLLYSYS_CODE_REVIEWER_IDENTITIES": (
                "reviewer_identities",
                "code-review",
            ),
        }
        for env_name, target in env_map.items():
            raw = os.environ.get(env_name)
            if raw is None:
                continue
            values = [part.strip() for part in raw.split(",") if part.strip()]
            if isinstance(target, tuple):
                data.setdefault(target[0], {})[target[1]] = values
            else:
                data[target] = values if target == "allowed_groups" else raw.strip()
        return cls.model_validate(data)

    def read_token(self) -> str:
        if self.token_file.is_symlink():
            raise PermissionError(f"{self.token_file} must not be a symlink")
        info = self.token_file.stat()
        if not stat.S_ISREG(info.st_mode):
            raise PermissionError(f"{self.token_file} must be a regular file")
        if info.st_mode & 0o177:
            raise PermissionError(f"{self.token_file} must be mode 0600 (or stricter)")
        token = self.token_file.read_text(encoding="utf-8").strip()
        if not token:
            raise ValueError(f"{self.token_file} is empty")
        return token
