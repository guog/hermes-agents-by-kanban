from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import NotificationLevel, Stage

HOLLYSYS_GITLAB_HOSTNAME = "green-git.hollysys.net"


@dataclass(frozen=True)
class GitLabEndpoint:
    hostname: str
    base_url: str


def trusted_runtime_uids() -> set[int]:
    owners = {0, os.geteuid()}
    configured = os.environ.get("PUID", "").strip()
    if configured.isdigit():
        owners.add(int(configured))
    return owners


def normalize_gitlab_endpoint(raw: str) -> GitLabEndpoint:
    value = raw.strip()
    if not value:
        raise ValueError("gitlab_host must not be empty")
    if "://" not in value:
        value = f"https://{value}"
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.port is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "gitlab_host must be a bare hostname or an HTTPS origin without "
            "credentials, port, path, query, or fragment"
        )
    hostname = parsed.hostname.lower()
    if hostname != HOLLYSYS_GITLAB_HOSTNAME:
        raise ValueError(
            f"gitlab_host must be {HOLLYSYS_GITLAB_HOSTNAME}"
        )
    return GitLabEndpoint(hostname=hostname, base_url=f"https://{hostname}")


class ControllerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hermes_home: Path = Path("/opt/data")
    state_dir: Path = Path("/opt/data/controller")
    socket_path: Path = Path("/opt/data/controller/controller.sock")
    lock_path: Path = Path("/opt/data/controller/controller.lock")
    config_path: Path = Path("/opt/hollysys-controller/config.yaml")
    profiles_root: Path = Path("/opt/data/profiles")
    skills_root: Path = Path("/opt/skills")
    projects_root: Path = Path("/workspace/projects")
    hermes_command: str = "hermes"
    glab_command: str = "/usr/local/bin/glab"
    lark_command: str = "/usr/local/bin/lark-cli"
    system_git_command: str = "/usr/bin/git"
    agent_git_command: str = "/usr/local/bin/git"
    offline_cache_command: Path = Path(
        "/opt/fleet/container/prepare-offline-caches.sh"
    )
    controller_profile: str = "dispatcher"
    controller_token_file: Path = Path(
        "/run/secrets/hollysys_controller_gitlab_token"
    )
    controller_mode: str = "preflight"
    poll_interval_seconds: float = Field(default=2.0, gt=0)
    reconcile_interval_seconds: float = Field(default=30.0, gt=0)
    reconcile_workers: int = Field(default=4, ge=1, le=32)
    outbox_poll_interval_seconds: float = Field(default=2.0, gt=0)
    command_timeout_seconds: int = Field(default=120, gt=0)
    start_request_sync_timeout_seconds: float = Field(
        default=10.0,
        ge=1.0,
        le=60.0,
    )
    offline_cache_timeout_seconds: int = Field(default=1800, ge=60, le=7200)
    gitlab_host: str = ""
    allowed_groups: list[str] = Field(default_factory=list)
    preflight_project_path: str = ""
    preflight_command_timeout_seconds: int = Field(default=120, ge=2, le=120)
    stage_assignees: dict[Stage, str]
    stage_skills: dict[Stage, list[str]]
    artifact_patterns: dict[str, list[str]]
    reviewer_identities: dict[str, list[str]]
    document_review_limit: int = Field(default=3, ge=1)
    code_modification_limit: int = Field(default=5, ge=1)
    protocol_retry_limit: int = 2
    notification_level: NotificationLevel = NotificationLevel.VERBOSE
    abort_admin_open_ids: list[str] = Field(default_factory=list)
    abort_confirmation_ttl_seconds: int = Field(default=600, ge=60, le=3600)
    dependency_backoff_initial_seconds: float = Field(default=5.0, gt=0)
    dependency_backoff_max_seconds: float = Field(default=300.0, gt=0)
    dependency_circuit_failure_threshold: int = Field(default=5, ge=2)
    health_stale_seconds: int = Field(default=120, ge=10)
    event_lag_warning_threshold: int = Field(default=100, ge=1)
    outbox_warning_threshold: int = Field(default=20, ge=1)
    worker_slow_warning_seconds: int = Field(default=900, ge=300)
    worker_progress_lease_seconds: int = Field(default=1800, ge=300)
    worker_heartbeat_stale_seconds: int = Field(default=300, ge=60)
    worker_redispatch_limit: int = Field(default=2, ge=0, le=10)
    worker_supervisor_socket: Path = Path(
        "/run/hollysys-controller/worker-supervisor.sock"
    )
    merge_wait_retry_seconds: int = Field(default=30, ge=5)
    merge_blocker_timeout_seconds: int = Field(default=3600, ge=60)
    merge_draft_grace_seconds: int = Field(default=600, ge=60)
    outbox_backoff_initial_seconds: int = Field(default=5, ge=1)
    outbox_backoff_max_seconds: int = Field(default=300, ge=1)

    @model_validator(mode="after")
    def validate_controller_contract(self) -> ControllerConfig:
        endpoint = normalize_gitlab_endpoint(self.gitlab_host)
        self.gitlab_host = endpoint.hostname
        if self.controller_mode not in {"preflight", "active"}:
            raise ValueError("controller_mode must be preflight or active")
        if self.controller_profile != "dispatcher":
            raise ValueError("controller_profile must be dispatcher")
        if not self.allowed_groups:
            raise ValueError("allowed_groups must contain at least one GitLab group")
        group_pattern = re.compile(
            r"^[A-Za-z0-9_][A-Za-z0-9_.-]*(?:/[A-Za-z0-9_][A-Za-z0-9_.-]*)*$"
        )
        if (
            len(self.allowed_groups) != len(set(self.allowed_groups))
            or any(
                not group_pattern.fullmatch(group)
                or any(part in {".", ".."} for part in group.split("/"))
                for group in self.allowed_groups
            )
        ):
            raise ValueError(
                "allowed_groups must contain unique GitLab group paths"
            )
        if self.preflight_project_path:
            project_parts = self.preflight_project_path.split("/")
            if (
                len(project_parts) < 2
                or not group_pattern.fullmatch(self.preflight_project_path)
                or any(part in {".", ".."} for part in project_parts)
                or self.preflight_project_path.endswith(".git")
            ):
                raise ValueError(
                    "preflight_project_path must be a canonical GitLab "
                    "namespace/project path without .git"
                )
            if not any(
                self.preflight_project_path.startswith(f"{group}/")
                for group in self.allowed_groups
            ):
                raise ValueError(
                    "preflight_project_path must be inside an allowed group"
                )
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
        if self.dependency_backoff_initial_seconds > self.dependency_backoff_max_seconds:
            raise ValueError(
                "dependency_backoff_initial_seconds cannot exceed maximum"
            )
        if self.outbox_backoff_initial_seconds > self.outbox_backoff_max_seconds:
            raise ValueError("outbox initial backoff cannot exceed maximum")
        if self.worker_slow_warning_seconds > self.worker_progress_lease_seconds:
            raise ValueError("worker slow warning cannot exceed progress lease")
        if self.worker_heartbeat_stale_seconds > self.worker_progress_lease_seconds:
            raise ValueError("worker heartbeat threshold cannot exceed progress lease")
        return self

    @staticmethod
    def normalize_gitlab_endpoint(raw: str) -> GitLabEndpoint:
        return normalize_gitlab_endpoint(raw)

    @property
    def gitlab_hostname(self) -> str:
        return normalize_gitlab_endpoint(self.gitlab_host).hostname

    @property
    def gitlab_base_url(self) -> str:
        return normalize_gitlab_endpoint(self.gitlab_host).base_url

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
                "controller_token_file": Path(
                    os.environ.get(
                        "HOLLYSYS_CONTROLLER_GITLAB_TOKEN_FILE",
                        "/run/secrets/hollysys_controller_gitlab_token",
                    )
                ),
                "worker_supervisor_socket": Path(
                    os.environ.get(
                        "HOLLYSYS_WORKER_SUPERVISOR_SOCKET",
                        "/run/hollysys-controller/worker-supervisor.sock",
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
            "HOLLYSYS_ABORT_ADMIN_OPEN_IDS": "abort_admin_open_ids",
        }
        for env_name, target in env_map.items():
            raw = os.environ.get(env_name)
            if raw is None:
                continue
            values = [part.strip() for part in raw.split(",") if part.strip()]
            if isinstance(target, tuple):
                data.setdefault(target[0], {})[target[1]] = values
            else:
                data[target] = (
                    values
                    if target in {"allowed_groups", "abort_admin_open_ids"}
                    else raw.strip()
                )
        scalar_env = {
            "HOLLYSYS_CONTROLLER_MODE": "controller_mode",
            "HOLLYSYS_PREFLIGHT_PROJECT_PATH": "preflight_project_path",
            "HOLLYSYS_PREFLIGHT_COMMAND_TIMEOUT_SECONDS": (
                "preflight_command_timeout_seconds",
                int,
            ),
            "HOLLYSYS_OFFLINE_CACHE_TIMEOUT_SECONDS": (
                "offline_cache_timeout_seconds",
                int,
            ),
            "HOLLYSYS_START_REQUEST_SYNC_TIMEOUT_SECONDS": (
                "start_request_sync_timeout_seconds",
                float,
            ),
            "HOLLYSYS_NOTIFICATION_LEVEL": "notification_level",
            "HOLLYSYS_RECONCILE_WORKERS": ("reconcile_workers", int),
            "HOLLYSYS_OUTBOX_POLL_INTERVAL_SECONDS": (
                "outbox_poll_interval_seconds",
                float,
            ),
            "HOLLYSYS_ABORT_CONFIRMATION_TTL_SECONDS": (
                "abort_confirmation_ttl_seconds",
                int,
            ),
            "HOLLYSYS_DEPENDENCY_BACKOFF_INITIAL_SECONDS": (
                "dependency_backoff_initial_seconds",
                float,
            ),
            "HOLLYSYS_DEPENDENCY_BACKOFF_MAX_SECONDS": (
                "dependency_backoff_max_seconds",
                float,
            ),
            "HOLLYSYS_DEPENDENCY_CIRCUIT_FAILURE_THRESHOLD": (
                "dependency_circuit_failure_threshold",
                int,
            ),
            "HOLLYSYS_HEALTH_STALE_SECONDS": ("health_stale_seconds", int),
            "HOLLYSYS_EVENT_LAG_WARNING_THRESHOLD": (
                "event_lag_warning_threshold",
                int,
            ),
            "HOLLYSYS_OUTBOX_WARNING_THRESHOLD": (
                "outbox_warning_threshold",
                int,
            ),
            "HOLLYSYS_WORKER_PROGRESS_LEASE_SECONDS": (
                "worker_progress_lease_seconds",
                int,
            ),
            "HOLLYSYS_WORKER_SLOW_WARNING_SECONDS": (
                "worker_slow_warning_seconds",
                int,
            ),
            "HOLLYSYS_WORKER_HEARTBEAT_STALE_SECONDS": (
                "worker_heartbeat_stale_seconds",
                int,
            ),
            "HOLLYSYS_WORKER_REDISPATCH_LIMIT": (
                "worker_redispatch_limit",
                int,
            ),
            "HOLLYSYS_MERGE_WAIT_RETRY_SECONDS": (
                "merge_wait_retry_seconds",
                int,
            ),
            "HOLLYSYS_MERGE_BLOCKER_TIMEOUT_SECONDS": (
                "merge_blocker_timeout_seconds",
                int,
            ),
            "HOLLYSYS_MERGE_DRAFT_GRACE_SECONDS": (
                "merge_draft_grace_seconds",
                int,
            ),
            "HOLLYSYS_OUTBOX_BACKOFF_INITIAL_SECONDS": (
                "outbox_backoff_initial_seconds",
                int,
            ),
            "HOLLYSYS_OUTBOX_BACKOFF_MAX_SECONDS": (
                "outbox_backoff_max_seconds",
                int,
            ),
        }
        for env_name, target in scalar_env.items():
            raw = os.environ.get(env_name)
            if raw is None:
                continue
            if isinstance(target, tuple):
                field_name, converter = target
                data[field_name] = converter(raw)
            else:
                data[target] = raw.strip()
        return cls.model_validate(data)

    def read_token(self) -> str:
        path = self.controller_token_file
        if path.is_symlink() or not path.is_file():
            raise ValueError("controller_gitlab_token_file_invalid")
        info = path.stat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_mode & 0o077
            or info.st_uid not in trusted_runtime_uids()
        ):
            raise PermissionError(
                "controller_gitlab_token_file_permissions"
            )
        token = path.read_text(encoding="utf-8").strip()
        if not token or "\n" in token or "\r" in token:
            raise ValueError("controller_gitlab_token_invalid")
        return token
