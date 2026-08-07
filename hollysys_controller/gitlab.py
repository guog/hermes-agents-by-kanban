from __future__ import annotations

import base64
import fnmatch
import hashlib
import json
import os
import re
import secrets
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote, urlparse

from .config import ControllerConfig
from .errors import (
    ControllerFatalError,
    DependencyAuthError,
    DependencyContractError,
    DependencyError,
    DependencyRateLimitedError,
    DependencyTransientError,
    ErrorContext,
    MergeBlocked,
)
from .models import (
    ArtifactBaseline,
    ArtifactScope,
    CompletionMetadata,
    DeliveryBinding,
    FeishuOrigin,
    GatePhase,
    Outcome,
    ProjectFacts,
    RunRecord,
    SourceFacts,
    Stage,
    WorkMode,
    WorkspaceFacts,
)
from .validators import validate_task_documents

PRD_URL_RE = re.compile(
    r"^/(?P<project>.+?)/-/(?:blob|raw)/(?P<sha>[0-9a-f]{40})/(?P<path>.+)$"
)
MR_URL_RE = re.compile(r"^/(?P<project>.+?)/-/merge_requests/(?P<iid>[1-9][0-9]*)/?$")
GATE_RE = re.compile(
    r"HOLLYSYS-GATE:\s+v=5\s+run=(?P<run>\S+)\s+stage=(?P<stage>\S+)"
    r"\s+result=(?P<result>\S+)\s+digest=(?P<digest>\S+)"
    r"\s+artifact=(?P<artifact>\S+)\s+head=(?P<head>\S+)"
    r"\s+test=(?P<test>\S+)\s+task=(?P<task>\S+)"
)
SEMANTIC_GATE_RE = re.compile(
    r"HOLLYSYS-SEMANTIC-GATE:\s+v=1\s+run=(?P<run>\S+)"
    r"\s+phase=(?P<phase>\S+)\s+decision=(?P<decision>\S+)"
    r"\s+artifact=(?P<artifact>[0-9a-f]{40})"
    r"\s+digest=(?P<digest>[0-9a-f]{64})"
)
FORCED_ADVANCE_RE = re.compile(
    r"HOLLYSYS-FORCED-ADVANCE:\s+v=1\s+run=(?P<run>\S+)"
    r"\s+phase=(?P<phase>spec|plan|tasks)\s+review_limit=(?P<limit>[1-9][0-9]*)"
    r"\s+task=(?P<task>\S+)"
)
TASK_HEADER_RE = re.compile(
    r"(?m)^- \[ \] (?P<task_id>T[0-9]{3,})\b[^\n]*$"
)
TASK_DEPENDENCY_RE = re.compile(
    r"(?m)^\s+- depends_on[：:]\s*\[(?P<dependencies>[^\]]*)\]\s*$"
)
TASK_ACTION_RE = re.compile(
    r"(?m)^\s+- 动作[：:]\s*(?P<action>reuse|modify|extend|create)\s*$"
)
PROJECT_PATH_RE = re.compile(
    r"^[A-Za-z0-9_][A-Za-z0-9_.-]*"
    r"(?:/[A-Za-z0-9_][A-Za-z0-9_.-]*)+$"
)
CONTROLLER_MERGE_SUBMITTED_FIELD = "_hollysys_controller_merge_submitted_v4"


@dataclass(frozen=True)
class StartFacts:
    run: RunRecord
    base_sha: str


class CheckedHeadConflict(ValueError):
    """The MR head changed between validation and the SHA-bound merge."""


class GitLabClient:
    def __init__(self, config: ControllerConfig):
        self.config = config

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        for key in (
            "GLAB_TOKEN",
            "CI_JOB_TOKEN",
            "PRIVATE_TOKEN",
            "GIT_TRACE",
            "GIT_TRACE_PACKET",
            "GIT_TRACE_CURL",
            "GIT_CURL_VERBOSE",
            "GIT_ASKPASS",
            "SSH_ASKPASS",
            "GIT_CONFIG",
            "GIT_CONFIG_COUNT",
            "GIT_CONFIG_PARAMETERS",
            "GIT_CONFIG_GLOBAL",
            "GIT_CONFIG_SYSTEM",
            "GIT_SSH",
            "GIT_SSH_COMMAND",
        ):
            env.pop(key, None)
        for key in tuple(env):
            if key.startswith("GIT_"):
                env.pop(key, None)
        env["GITLAB_HOST"] = self.config.gitlab_base_url
        env["GITLAB_TOKEN"] = self.config.read_token()
        env["GLAB_CHECK_UPDATE"] = "false"
        return env

    def _run(
        self,
        command: list[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        tolerate: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        try:
            env = self._env()
        except (OSError, ValueError) as exc:
            raise ControllerFatalError(
                "controller_gitlab_token_invalid"
            ) from exc
        try:
            result = subprocess.run(
                command,
                cwd=cwd,
                env=env,
                input=input_text,
                text=True,
                capture_output=True,
                timeout=self.config.command_timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            endpoint = (
                str(command[2])
                if len(command) >= 3
                and command[0] == self.config.glab_command
                and command[1] == "api"
                else command[0]
            )
            raise DependencyTransientError(
                f"GitLab command timed out: {endpoint}",
                context=ErrorContext(
                    dependency="gitlab",
                    endpoint=endpoint,
                    error_code="timeout",
                ),
            ) from exc
        except OSError as exc:
            raise ControllerFatalError(
                f"gitlab_command_unavailable:{command[0]}"
            ) from exc
        if result.returncode != 0 and not tolerate:
            if (
                len(command) >= 3
                and command[0] == self.config.glab_command
                and command[1] == "api"
            ):
                raise self._api_error(str(command[2]), result)
            raise ControllerFatalError(
                f"unclassified_gitlab_command_failure:{command[0]}"
            )
        return result

    @staticmethod
    def _http_status(text: str) -> int | None:
        matches = re.findall(r"\b([1-5][0-9]{2})\b", text)
        for candidate in reversed(matches):
            value = int(candidate)
            if 400 <= value <= 599:
                return value
        return None

    @staticmethod
    def _retry_after(text: str) -> int | None:
        match = re.search(
            r"retry-after(?:\s*[:=]\s*|\s+)([0-9]+)",
            text,
            flags=re.IGNORECASE,
        )
        return int(match.group(1)) if match else None

    def _api_error(
        self,
        endpoint: str,
        result: subprocess.CompletedProcess[str],
    ) -> DependencyError:
        summary = (result.stderr or result.stdout or "GitLab command failed").strip()
        status = self._http_status(summary)
        context = ErrorContext(
            dependency="gitlab",
            endpoint=endpoint,
            status_code=status,
            retry_after_seconds=self._retry_after(summary),
        )
        safe = self._redact(summary)[:1000]
        if status in {401, 403}:
            return DependencyAuthError(safe, context=context)
        if status == 429:
            return DependencyRateLimitedError(safe, context=context)
        if status in {400, 404, 409, 422}:
            return DependencyContractError(safe, context=context)
        return DependencyTransientError(safe, context=context)

    def _redact(self, text: str) -> str:
        safe = text
        try:
            token = self.config.read_token()
        except (OSError, ValueError):
            token = ""
        if token:
            safe = safe.replace(token, "[REDACTED]")
        safe = re.sub(
            r"(?i)(authorization\s*:\s*(?:bearer|basic)\s+)\S+",
            r"\1[REDACTED]",
            safe,
        )
        return safe

    def api(
        self,
        endpoint: str,
        *,
        method: str = "GET",
        fields: dict[str, str | int | bool] | None = None,
    ) -> dict | list:
        command = [
            self.config.glab_command,
            "api",
            endpoint,
            "--hostname",
            self.config.gitlab_hostname,
            "--method",
            method,
        ]
        for key, value in (fields or {}).items():
            rendered = str(value).lower() if isinstance(value, bool) else value
            command.extend(["--field", f"{key}={rendered}"])
        result = self._run(command, tolerate=True)
        if result.returncode != 0:
            raise self._api_error(endpoint, result)
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise DependencyContractError(
                f"GitLab returned invalid JSON for {endpoint}",
                context=ErrorContext(
                    dependency="gitlab",
                    endpoint=endpoint,
                    error_code="invalid_json",
                ),
            ) from exc

    @staticmethod
    def _project_endpoint(project_id: int | str) -> str:
        return f"projects/{quote(str(project_id), safe='')}"

    @staticmethod
    def _require_object(value: object, endpoint: str) -> dict:
        if isinstance(value, dict):
            return value
        raise DependencyContractError(
            f"GitLab returned a non-object response for {endpoint}",
            context=ErrorContext(
                dependency="gitlab",
                endpoint=endpoint,
                error_code="invalid_object_response",
            ),
        )

    def paginated_list(self, endpoint: str) -> list[dict]:
        items: list[dict] = []
        page = 1
        separator = "&" if "?" in endpoint else "?"
        while True:
            response = self.api(f"{endpoint}{separator}per_page=100&page={page}")
            if not isinstance(response, list):
                raise DependencyContractError(
                    f"GitLab list response is invalid for {endpoint}",
                    context=ErrorContext(
                        dependency="gitlab",
                        endpoint=endpoint,
                        error_code="invalid_list_response",
                    ),
                )
            page_items = [item for item in response if isinstance(item, dict)]
            items.extend(page_items)
            if len(response) < 100:
                return items
            page += 1

    def validate_start(
        self,
        *,
        prd_blob_url: str,
        prd_mr_url: str,
        origin: FeishuOrigin,
    ) -> StartFacts:
        blob = urlparse(prd_blob_url)
        mr_url = urlparse(prd_mr_url)
        for label, parsed in (("PRD", blob), ("MR", mr_url)):
            try:
                port = parsed.port
            except ValueError as exc:
                raise ValueError(f"{label} GitLab URL has an invalid port") from exc
            if (
                parsed.scheme != "https"
                or parsed.username
                or parsed.password
                or port is not None
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError(
                    f"{label} GitLab URL must be a credential-free HTTPS "
                    "origin URL without port, query, or fragment"
                )
        if blob.hostname != mr_url.hostname:
            raise ValueError("PRD and MR URLs use different hosts")
        if self.config.gitlab_hostname and blob.hostname != self.config.gitlab_hostname:
            raise ValueError("URL host is not the configured GitLab host")
        blob_match = PRD_URL_RE.match(blob.path)
        mr_match = MR_URL_RE.match(mr_url.path)
        if not blob_match or not mr_match:
            raise ValueError(
                "PRD URL must be a blob/raw URL pinned to a 40-character commit "
                "and MR URL must end in /-/merge_requests/<iid>"
            )
        blob_project = blob_match.group("project")
        mr_project = mr_match.group("project")
        project_path = unquote(blob_project)
        if (
            project_path != blob_project
            or unquote(mr_project) != mr_project
            or not PROJECT_PATH_RE.fullmatch(project_path)
            or any(part in {".", ".."} for part in project_path.split("/"))
        ):
            raise ValueError("GitLab project path is not canonical")
        if project_path != mr_project:
            raise ValueError("PRD and MR URLs refer to different projects")
        if self.config.allowed_groups and not any(
            project_path.startswith(f"{group}/")
            for group in self.config.allowed_groups
        ):
            raise ValueError(f"project {project_path} is outside allowed groups")
        prd_path = unquote(blob_match.group("path"))
        if (
            prd_path.startswith("/")
            or "\0" in prd_path
            or any(part in {"", ".", ".."} for part in prd_path.split("/"))
        ):
            raise ValueError("PRD path is not a canonical repository path")

        project_endpoint = self._project_endpoint(project_path)
        project = self._require_object(
            self.api(project_endpoint),
            project_endpoint,
        )
        if project.get("archived"):
            raise ValueError(f"project {project_path} is archived")
        try:
            project_id = int(project["id"])
            default_branch = str(project["default_branch"]).strip()
        except (KeyError, TypeError, ValueError) as exc:
            raise DependencyContractError(
                "GitLab project response lacks id/default_branch",
                context=ErrorContext(
                    dependency="gitlab",
                    endpoint=project_endpoint,
                    error_code="invalid_project_response",
                ),
            ) from exc
        if project_id <= 0 or not default_branch:
            raise DependencyContractError(
                "GitLab project response has invalid id/default_branch",
                context=ErrorContext(
                    dependency="gitlab",
                    endpoint=project_endpoint,
                    error_code="invalid_project_response",
                ),
            )
        mr_iid = int(mr_match.group("iid"))
        mr_endpoint = (
            f"{self._project_endpoint(project_id)}/merge_requests/{mr_iid}"
        )
        mr = self._require_object(self.api(mr_endpoint), mr_endpoint)
        if mr.get("state") != "merged":
            raise ValueError("the PRD merge request is not merged")
        if mr.get("target_branch") != default_branch:
            raise ValueError("the PRD MR was not merged to the current default branch")

        prd_sha = blob_match.group("sha")
        changes_endpoint = (
            f"{self._project_endpoint(project_id)}/merge_requests/"
            f"{mr_iid}/changes"
        )
        changes = self._require_object(
            self.api(changes_endpoint),
            changes_endpoint,
        )
        raw_changes = changes.get("changes")
        if not isinstance(raw_changes, list):
            raise DependencyContractError(
                "GitLab MR changes response lacks a changes list",
                context=ErrorContext(
                    dependency="gitlab",
                    endpoint=changes_endpoint,
                    error_code="invalid_changes_response",
                ),
            )
        changed_paths = {
            str(item.get("new_path"))
            for item in raw_changes
            if isinstance(item, dict)
        }
        if prd_path not in changed_paths:
            raise ValueError(
                "the merged PRD MR does not contain the requested PRD path"
            )

        branch_endpoint = (
            f"{self._project_endpoint(project_id)}/repository/branches/"
            f"{quote(default_branch, safe='')}"
        )
        branch = self._require_object(
            self.api(branch_endpoint),
            branch_endpoint,
        )
        try:
            base_sha = str(branch["commit"]["id"])
        except (KeyError, TypeError) as exc:
            raise DependencyContractError(
                "GitLab branch response lacks commit id",
                context=ErrorContext(
                    dependency="gitlab",
                    endpoint=branch_endpoint,
                    error_code="invalid_branch_response",
                ),
            ) from exc
        if not re.fullmatch(r"[0-9a-f]{40}", base_sha):
            raise DependencyContractError(
                "GitLab branch response has an invalid commit id",
                context=ErrorContext(
                    dependency="gitlab",
                    endpoint=branch_endpoint,
                    error_code="invalid_branch_response",
                ),
            )

        requested_file = self.file(project_id, prd_path, prd_sha)
        current_file = self.file(project_id, prd_path, base_sha)
        requested_blob = str(requested_file.get("blob_id") or "")
        current_blob = str(current_file.get("blob_id") or "")
        if not re.fullmatch(r"[0-9a-f]{40}", requested_blob) or not re.fullmatch(
            r"[0-9a-f]{40}",
            current_blob,
        ):
            raise DependencyContractError(
                "GitLab file response has an invalid blob id",
                context=ErrorContext(
                    dependency="gitlab",
                    endpoint="repository/files",
                    error_code="invalid_file_response",
                ),
            )
        if requested_blob != current_blob:
            raise ValueError(
                "the PRD on the default branch no longer equals the requested version"
            )
        identity = (
            f"{blob.hostname}|{project_id}|{prd_path}|{requested_blob}".encode()
        )
        digest = hashlib.sha256(identity).digest()
        encoded = base64.b32encode(digest).decode("ascii").lower().rstrip("=")
        source_key = f"source-{encoded[:20]}"
        run_generation = secrets.token_hex(10)
        run_key = f"hollysys-{secrets.token_hex(10)}"
        repo_slug = project_path.rsplit("/", 1)[1].lower()
        prd_name = Path(prd_path).stem.lower()
        safe_name = re.sub(r"[^a-z0-9]+", "-", prd_name).strip("-") or "prd"
        checkout = self.config.projects_root / f"p{project_id}-{repo_slug}"
        worktree = self.config.projects_root / "worktrees" / f"p{project_id}" / run_key
        delivery_branch = (
            f"feature/{safe_name[:48]}-{run_key.removeprefix('hollysys-')}"
        )
        display_name = str(project.get("description") or "").strip() or str(
            project.get("name_with_namespace") or project_path
        )
        run = RunRecord(
            run_key=run_key,
            source_key=source_key,
            run_generation=run_generation,
            started_at=datetime.now(timezone.utc),
            project=ProjectFacts(
                host=str(blob.hostname),
                project_id=project_id,
                project_path=project_path,
                project_display_name=display_name,
                default_branch=default_branch,
            ),
            source=SourceFacts(
                prd_path=prd_path,
                prd_commit_sha=prd_sha,
                prd_blob_sha=requested_blob,
                prd_blob_url=prd_blob_url,
                prd_mr_url=prd_mr_url,
            ),
            artifact_scope=ArtifactScope.from_prd_path(
                prd_path,
                self.config.artifact_relative_patterns,
            ),
            workspace=WorkspaceFacts(
                board=f"gitlab-p{project_id}",
                checkout=str(checkout),
                worktree=str(worktree),
                branch=delivery_branch,
                target_branch=default_branch,
                repository_base_sha=base_sha,
            ),
            origin=origin,
        )
        return StartFacts(run=run, base_sha=base_sha)

    def file(self, project_id: int, path: str, ref: str) -> dict:
        result = self.api(
            f"{self._project_endpoint(project_id)}/repository/files/"
            f"{quote(path, safe='')}?ref={quote(ref, safe='')}"
        )
        if not isinstance(result, dict):
            raise DependencyContractError(
                f"unexpected file response for {path}",
                context=ErrorContext(
                    dependency="gitlab",
                    endpoint="repository/files",
                    error_code="invalid_file_response",
                ),
            )
        return result

    def ensure_workspace(self, run: RunRecord, base_sha: str) -> None:
        checkout = Path(run.workspace.checkout)
        worktree = Path(run.workspace.worktree)
        checkout.parent.mkdir(parents=True, exist_ok=True)
        worktree.parent.mkdir(parents=True, exist_ok=True)
        if not (checkout / ".git").exists():
            if checkout.exists() and any(checkout.iterdir()):
                raise ValueError(
                    f"checkout path is non-empty and not a Git repo: {checkout}"
                )
            clone_url = (
                f"{self.config.gitlab_base_url}/{run.project.project_path}.git"
            )
            self._git(
                checkout.parent,
                ["clone", clone_url, str(checkout)],
            )
        origin = self._git(checkout, ["remote", "get-url", "origin"]).stdout.strip()
        expected_suffix = f"/{run.project.project_path}.git"
        parsed_origin = urlparse(origin)
        if (
            parsed_origin.scheme != "https"
            or parsed_origin.hostname != run.project.host
            or parsed_origin.port is not None
            or parsed_origin.path.rstrip("/") != expected_suffix.rstrip("/")
            or parsed_origin.username
            or parsed_origin.password
            or parsed_origin.query
            or parsed_origin.fragment
        ):
            raise ValueError(
                "checkout origin is not the validated token-free project URL"
            )

        if (
            self._git(
                checkout, ["cat-file", "-e", f"{base_sha}^{{commit}}"], tolerate=True
            ).returncode
            != 0
        ):
            self._git(checkout, ["fetch", "origin", base_sha])
        if (
            self._git(
                checkout,
                ["rev-parse", "--verify", "HEAD^{commit}"],
                tolerate=True,
            ).returncode
            != 0
        ):
            self._repair_incomplete_checkout(checkout, run, base_sha)
        if worktree.exists():
            actual = self._git(worktree, ["branch", "--show-current"]).stdout.strip()
            if actual != run.workspace.branch:
                raise ValueError(
                    f"existing worktree uses {actual!r}, expected {run.workspace.branch!r}"
                )
            common_raw = self._git(
                worktree,
                ["rev-parse", "--git-common-dir"],
            ).stdout.strip()
            common_path = Path(common_raw)
            if not common_path.is_absolute():
                common_path = worktree / common_path
            if common_path.resolve() != (checkout / ".git").resolve():
                raise ValueError(
                    "existing worktree is not attached to the validated checkout"
                )
            return

        local_branch = (
            self._git(
                checkout,
                ["show-ref", "--verify", f"refs/heads/{run.workspace.branch}"],
                tolerate=True,
            ).returncode
            == 0
        )
        if local_branch:
            self._git(
                checkout,
                ["worktree", "add", str(worktree), run.workspace.branch],
            )
        else:
            self._git(
                checkout,
                [
                    "worktree",
                    "add",
                    "-b",
                    run.workspace.branch,
                    str(worktree),
                    base_sha,
                ],
            )

    def _repair_incomplete_checkout(
        self,
        checkout: Path,
        run: RunRecord,
        base_sha: str,
    ) -> None:
        """Finish a clone interrupted after Git created its repository metadata."""
        git_dir = checkout / ".git"
        if git_dir.is_symlink() or not git_dir.is_dir():
            raise ValueError("incomplete checkout has invalid Git metadata")
        if any(path.name != ".git" for path in checkout.iterdir()):
            raise ValueError("incomplete checkout contains non-Git content")
        local_refs = self._git(checkout, ["show-ref", "--heads"], tolerate=True)
        linked_worktrees = git_dir / "worktrees"
        if (
            local_refs.returncode not in {0, 1}
            or local_refs.stdout.strip()
            or (
                linked_worktrees.is_dir()
                and any(linked_worktrees.iterdir())
            )
        ):
            raise ValueError("incomplete checkout has existing refs or worktrees")

        target_ref = f"refs/heads/{run.workspace.target_branch}"
        self._git(checkout, ["symbolic-ref", "HEAD", target_ref])
        self._git(checkout, ["update-ref", target_ref, base_sha])
        self._git(checkout, ["reset", "--hard", base_sha])
        if (
            self._git(
                checkout,
                ["rev-parse", "--verify", "HEAD^{commit}"],
                tolerate=True,
            ).returncode
            != 0
        ):
            raise ControllerFatalError("incomplete_checkout_repair_failed")

    def create_delivery_branch(self, run: RunRecord) -> dict:
        project = self._project_endpoint(run.project.project_id)
        branch_name = run.workspace.branch
        branch_list_endpoint = (
            f"{project}/repository/branches?search="
            f"{quote(f'^{branch_name}$', safe='')}&per_page=100"
        )
        branches = self.api(branch_list_endpoint)
        if not isinstance(branches, list):
            raise DependencyContractError(
                "GitLab branch collision response is not an array",
                context=ErrorContext(
                    dependency="gitlab",
                    endpoint=branch_list_endpoint,
                    error_code="invalid_list_response",
                ),
            )
        if any(
            isinstance(item, dict) and item.get("name") == branch_name
            for item in branches
        ):
            raise ValueError(
                f"delivery_branch_already_exists:{branch_name}"
            )
        mr_endpoint = (
            f"{project}/merge_requests?source_branch="
            f"{quote(branch_name, safe='')}&scope=all&per_page=100"
        )
        merge_requests = self.api(mr_endpoint)
        if not isinstance(merge_requests, list):
            raise DependencyContractError(
                "GitLab MR collision response is not an array",
                context=ErrorContext(
                    dependency="gitlab",
                    endpoint=mr_endpoint,
                    error_code="invalid_list_response",
                ),
            )
        if any(
            isinstance(item, dict)
            and item.get("source_branch") == branch_name
            for item in merge_requests
        ):
            raise ValueError(
                f"historical_delivery_mr_exists:{branch_name}"
            )
        endpoint = f"{project}/repository/branches"
        created = self._require_object(
            self.api(
                endpoint,
                method="POST",
                fields={
                    "branch": branch_name,
                    "ref": run.workspace.repository_base_sha,
                },
            ),
            endpoint,
        )
        try:
            created_head = str(created["commit"]["id"])
        except (KeyError, TypeError) as exc:
            raise DependencyContractError(
                "GitLab created branch response lacks commit id",
                context=ErrorContext(
                    dependency="gitlab",
                    endpoint=endpoint,
                    error_code="invalid_branch_response",
                ),
            ) from exc
        if (
            created.get("name") != branch_name
            or created_head != run.workspace.repository_base_sha
        ):
            raise DependencyContractError(
                "GitLab created branch does not match run identity",
                context=ErrorContext(
                    dependency="gitlab",
                    endpoint=endpoint,
                    error_code="created_branch_mismatch",
                ),
            )
        return {"branch": branch_name, "head_sha": created_head}

    def publish_delivery(
        self,
        run: RunRecord,
        *,
        head_sha: str,
        description: str,
    ) -> DeliveryBinding:
        if not re.fullmatch(r"[0-9a-f]{40}", head_sha):
            raise ValueError("publish_delivery requires a full head SHA")
        state = self.local_workspace_state(run)
        if (
            not state.get("ok")
            or state.get("head_sha") != head_sha
            or state.get("branch") != run.workspace.branch
        ):
            raise ValueError("publish_delivery local head/branch mismatch")
        project = self._project_endpoint(run.project.project_id)
        branch_endpoint = (
            f"{project}/repository/branches/"
            f"{quote(run.workspace.branch, safe='')}"
        )
        branch = self._require_object(
            self.api(branch_endpoint),
            branch_endpoint,
        )
        remote_head = str(
            branch.get("commit", {}).get("id")
            if isinstance(branch.get("commit"), dict)
            else ""
        )
        if remote_head != head_sha:
            raise ValueError(
                f"publish_delivery remote head mismatch:{remote_head}"
            )
        collision_endpoint = (
            f"{project}/merge_requests?source_branch="
            f"{quote(run.workspace.branch, safe='')}&scope=all&per_page=100"
        )
        collisions = self.api(collision_endpoint)
        if not isinstance(collisions, list):
            raise DependencyContractError(
                "GitLab MR collision response is not an array",
                context=ErrorContext(
                    dependency="gitlab",
                    endpoint=collision_endpoint,
                    error_code="invalid_list_response",
                ),
            )
        if any(
            isinstance(item, dict)
            and item.get("source_branch") == run.workspace.branch
            for item in collisions
        ):
            raise ValueError(
                f"unbound_delivery_mr_exists:{run.workspace.branch}"
            )
        current_user_endpoint = "user"
        current_user = self._require_object(
            self.api(current_user_endpoint),
            current_user_endpoint,
        )
        creator = str(
            current_user.get("username")
            or current_user.get("id")
            or ""
        ).strip()
        if not creator:
            raise DependencyContractError(
                "GitLab current user response lacks identity",
                context=ErrorContext(
                    dependency="gitlab",
                    endpoint=current_user_endpoint,
                    error_code="invalid_user_response",
                ),
            )
        mr_endpoint = f"{project}/merge_requests"
        title = f"Draft: Hollysys {Path(run.source.prd_path).stem} [{run.run_key}]"
        mr = self._require_object(
            self.api(
                mr_endpoint,
                method="POST",
                fields={
                    "source_branch": run.workspace.branch,
                    "target_branch": run.workspace.target_branch,
                    "title": title,
                    "description": description,
                    "remove_source_branch": False,
                },
            ),
            mr_endpoint,
        )
        author = mr.get("author") if isinstance(mr.get("author"), dict) else {}
        actual_creator = str(
            author.get("username") or author.get("id") or ""
        ).strip()
        if actual_creator != creator:
            raise DependencyContractError(
                "created MR author is not the Controller identity",
                context=ErrorContext(
                    dependency="gitlab",
                    endpoint=mr_endpoint,
                    error_code="mr_creator_mismatch",
                ),
            )
        if (
            mr.get("source_branch") != run.workspace.branch
            or mr.get("target_branch") != run.workspace.target_branch
            or str(mr.get("sha") or "") != head_sha
        ):
            raise DependencyContractError(
                "created MR does not match run branch/head",
                context=ErrorContext(
                    dependency="gitlab",
                    endpoint=mr_endpoint,
                    error_code="created_mr_mismatch",
                ),
            )
        iid = int(mr.get("iid") or 0)
        claim = (
            "[hollysys-run-claim:v4] "
            f"run={run.run_key} source={run.source_key} "
            f"generation={run.run_generation} initial_head={head_sha} "
            f"started_at={run.started_at.isoformat()}"
        )
        note_endpoint = f"{project}/merge_requests/{iid}/notes"
        note = self._require_object(
            self.api(
                note_endpoint,
                method="POST",
                fields={"body": claim},
            ),
            note_endpoint,
        )
        created_at = datetime.fromisoformat(
            str(mr.get("created_at") or "").replace("Z", "+00:00")
        )
        return DeliveryBinding(
            mr_iid=iid,
            mr_url=str(mr.get("web_url") or ""),
            creator=creator,
            created_at=created_at,
            initial_head_sha=head_sha,
            claim_note_id=int(note.get("id") or 0),
        )

    def mark_delivery_ready(
        self,
        run: RunRecord,
        binding: DeliveryBinding,
    ) -> dict:
        mr = self.delivery_mr(run, binding.mr_iid)
        title = str(mr.get("title") or "")
        ready_title = re.sub(r"^(?:Draft:|WIP:)\s*", "", title).strip()
        endpoint = (
            f"{self._project_endpoint(run.project.project_id)}"
            f"/merge_requests/{binding.mr_iid}"
        )
        return self._require_object(
            self.api(
                endpoint,
                method="PUT",
                fields={"title": ready_title or title},
            ),
            endpoint,
        )

    def validate_delivery_binding(
        self,
        run: RunRecord,
        binding: DeliveryBinding,
    ) -> dict:
        mr = self.delivery_mr(run, binding.mr_iid)
        author = mr.get("author") if isinstance(mr.get("author"), dict) else {}
        creator = str(
            author.get("username") or author.get("id") or ""
        ).strip()
        created_at = datetime.fromisoformat(
            str(mr.get("created_at") or "").replace("Z", "+00:00")
        )
        if (
            creator != binding.creator
            or created_at != binding.created_at
            or created_at < run.started_at
        ):
            raise ValueError("delivery binding creator/time mismatch")
        note_endpoint = (
            f"{self._project_endpoint(run.project.project_id)}"
            f"/merge_requests/{binding.mr_iid}/notes/{binding.claim_note_id}"
        )
        note = self._require_object(self.api(note_endpoint), note_endpoint)
        marker = (
            f"[hollysys-run-claim:v4] run={run.run_key} "
            f"source={run.source_key} generation={run.run_generation} "
            f"initial_head={binding.initial_head_sha}"
        )
        note_author = (
            note.get("author")
            if isinstance(note.get("author"), dict)
            else {}
        )
        note_creator = str(
            note_author.get("username") or note_author.get("id") or ""
        ).strip()
        if marker not in str(note.get("body") or "") or note_creator != creator:
            raise ValueError("delivery binding claim note mismatch")
        return mr

    def local_workspace_state(self, run: RunRecord) -> dict:
        worktree = Path(run.workspace.worktree)
        if not worktree.is_dir():
            return {
                "ok": False,
                "worktree": str(worktree),
                "error_code": "worktree_missing",
            }
        branch = self._git(
            worktree,
            ["branch", "--show-current"],
            tolerate=True,
        )
        head = self._git(
            worktree,
            ["rev-parse", "HEAD"],
            tolerate=True,
        )
        status = self._git(
            worktree,
            ["status", "--porcelain"],
            tolerate=True,
        )
        branch_name = branch.stdout.strip()
        head_sha = head.stdout.strip()
        clean = status.returncode == 0 and not status.stdout.strip()
        ok = (
            branch.returncode == 0
            and head.returncode == 0
            and status.returncode == 0
            and branch_name == run.workspace.branch
            and bool(re.fullmatch(r"[0-9a-f]{40}", head_sha))
        )
        return {
            "ok": ok,
            "worktree": str(worktree),
            "branch": branch_name or None,
            "head_sha": head_sha or None,
            "clean": clean,
            "error_code": None if ok else "workspace_identity_mismatch",
        }

    def validate_repository_evidence(
        self,
        run: RunRecord,
        metadata: CompletionMetadata,
    ) -> None:
        evidence = metadata.repository_evidence
        if evidence is None:
            return
        if evidence.repository_base_sha != run.workspace.repository_base_sha:
            raise ValueError("repository evidence uses another base commit")
        worktree = Path(run.workspace.worktree)
        for path in evidence.inspected_paths:
            result = self._git(
                worktree,
                [
                    "cat-file",
                    "-e",
                    f"{run.workspace.repository_base_sha}:{path}",
                ],
                tolerate=True,
            )
            if result.returncode != 0:
                raise ValueError(
                    f"repository evidence path does not exist at base: {path}"
                )

    def validate_author_completion(
        self,
        run: RunRecord,
        metadata: CompletionMetadata,
    ) -> dict:
        if (
            metadata.stage
            not in {
                Stage.SPEC_WRITE,
                Stage.PLAN_WRITE,
                Stage.TASKS_WRITE,
                Stage.IMPLEMENT,
            }
            or metadata.outcome != Outcome.PASS
        ):
            raise ValueError("completion is not an authoring pass")
        if (
            metadata.mr_iid is None
            or metadata.mr_url is None
            or metadata.head_sha is None
        ):
            raise ValueError("authoring pass lacks delivery MR/head identity")
        mr = self.delivery_mr(run, metadata.mr_iid)
        if mr is None:
            raise ValueError("delivery MR does not exist")
        if (
            mr.get("state") != "opened"
            or mr.get("source_branch") != run.workspace.branch
            or mr.get("target_branch") != run.workspace.target_branch
            or str(mr.get("web_url") or "") != str(metadata.mr_url)
            or str(mr.get("sha") or "") != metadata.head_sha
        ):
            raise ValueError(
                "authoring pass is not bound to the current shared delivery MR/head"
            )
        return mr

    def validate_semantic_gate(
        self,
        run: RunRecord,
        metadata: CompletionMetadata,
    ) -> None:
        if metadata.gate_phase is None:
            return
        ref = metadata.gate_artifact_commit_sha
        digest = metadata.gate_artifact_digest
        if ref is None or digest is None:
            raise ValueError("semantic gate is missing frozen artifact identity")
        paths = sorted(metadata.gate_artifact_paths)
        if self.artifact_digest(run.project.project_id, ref, paths) != digest:
            raise ValueError("semantic gate artifact digest drifted")

        documents: list[str] = []
        for path in paths:
            file_result = self.file(run.project.project_id, path, ref)
            if str(file_result.get("encoding") or "base64") != "base64":
                raise ValueError(f"unsupported GitLab file encoding for {path}")
            try:
                documents.append(
                    base64.b64decode(
                        "".join(
                            str(file_result.get("content") or "").split()
                        ),
                        validate=True,
                    ).decode("utf-8")
                )
            except (ValueError, UnicodeDecodeError) as exc:
                raise ValueError(
                    f"semantic gate artifact is not valid UTF-8: {path}"
                ) from exc
        frozen_text = "\n".join(documents)
        if metadata.gate_phase == GatePhase.IMPLEMENTATION_ENTRY:
            self._validate_task_graph(documents)
        for requirement_id in metadata.requirement_ids:
            if not self._contains_reference(frozen_text, requirement_id):
                raise ValueError(
                    f"semantic gate requirement does not exist: {requirement_id}"
                )
        for contract_ref in metadata.contract_refs:
            if not self._contains_reference(frozen_text, contract_ref):
                raise ValueError(
                    f"semantic gate contract does not exist: {contract_ref}"
                )

        reviewer_match = re.fullmatch(
            r"id:([1-9][0-9]*)",
            str(metadata.gate_reviewer or ""),
        )
        if reviewer_match is None:
            raise ValueError("semantic gate reviewer must use stable id:<numeric-id>")
        reviewer_id = int(reviewer_match.group(1))
        reviewer_note_found = False
        for evidence_ref in metadata.gate_evidence_refs:
            parsed = urlparse(evidence_ref)
            if parsed.scheme:
                expected_prefix = f"/{run.project.project_path}/"
                decoded_path = unquote(parsed.path)
                if (
                    parsed.scheme != "https"
                    or parsed.hostname != run.project.host
                    or parsed.username
                    or parsed.password
                    or parsed.port is not None
                    or decoded_path != parsed.path
                    or any(
                        part in {"", ".", ".."}
                        for part in PurePosixPath(parsed.path).parts[1:]
                    )
                    or not parsed.path.startswith(expected_prefix)
                ):
                    raise ValueError(
                        "semantic gate external evidence must be a validated "
                        "project HTTPS reference"
                    )
                note_match = re.fullmatch(
                    re.escape(expected_prefix)
                    + r"-/merge_requests/([1-9][0-9]*)/?",
                    parsed.path,
                )
                fragment_match = re.fullmatch(
                    r"note_([1-9][0-9]*)",
                    parsed.fragment,
                )
                if note_match is None or fragment_match is None:
                    raise ValueError(
                        "semantic gate external evidence must reference an "
                        "exact delivery MR note"
                    )
                mr_iid = int(note_match.group(1))
                self.delivery_mr(run, mr_iid)
                note_endpoint = (
                    f"{self._project_endpoint(run.project.project_id)}/"
                    f"merge_requests/{mr_iid}/notes/"
                    f"{int(fragment_match.group(1))}"
                )
                note = self._require_object(
                    self.api(note_endpoint),
                    note_endpoint,
                )
                author = note.get("author")
                marker = SEMANTIC_GATE_RE.search(
                    str(note.get("body") or "")
                )
                if (
                    not isinstance(author, dict)
                    or int(author.get("id") or 0) != reviewer_id
                    or marker is None
                    or marker.group("run") != run.run_key
                    or marker.group("phase") != metadata.gate_phase.value
                    or marker.group("decision") != metadata.gate_decision.value
                    or marker.group("artifact")
                    != metadata.gate_artifact_commit_sha
                    or marker.group("digest")
                    != metadata.gate_artifact_digest
                ):
                    raise ValueError(
                        "semantic gate note author or frozen identity does not match"
                    )
                reviewer_note_found = True
                continue
            evidence_path = PurePosixPath(evidence_ref)
            if (
                evidence_path.is_absolute()
                or ".." in evidence_path.parts
                or evidence_ref in {"", "."}
            ):
                raise ValueError("semantic gate evidence path is unsafe")
            self.file(run.project.project_id, evidence_ref, ref)
        if not reviewer_note_found:
            raise ValueError(
                "semantic gate requires an authored delivery MR note reference"
            )

    @staticmethod
    def _contains_reference(text: str, reference: str) -> bool:
        boundary = r"A-Za-z0-9_.:/-"
        return (
            re.search(
                rf"(?<![{boundary}]){re.escape(reference)}(?![{boundary}])",
                text,
            )
            is not None
        )

    @staticmethod
    def _validate_task_graph(documents: list[str]) -> None:
        result = validate_task_documents(documents)
        if not result.passed:
            raise ValueError(
                "TASKS_VALIDATION_FAILED:"
                + ",".join(result.error_codes)
            )

    @staticmethod
    def _looks_like_frozen_upstream(path: str) -> bool:
        normalized = path.strip().lstrip("./").lower()
        return (
            normalized.startswith(("specs/", "plans/", "prds/"))
            or "/specs/" in normalized
            or "/plans/" in normalized
            or "/prds/" in normalized
            or normalized.endswith(("/spec.md", "/plan.md", "/prd.md"))
        )

    def _git(
        self, cwd: Path, args: list[str], tolerate: bool = False
    ) -> subprocess.CompletedProcess[str]:
        # GIT_ASKPASS keeps the token out of argv, Git config, and origin URLs.
        with tempfile.TemporaryDirectory(prefix="hollysys-askpass-") as tmp:
            askpass = Path(tmp) / "askpass"
            askpass.write_text(
                "#!/bin/sh\n"
                'case "$1" in\n'
                '  *Username*) printf "%s\\n" oauth2 ;;\n'
                '  *) printf "%s\\n" "$HOLLYSYS_GIT_ASKPASS_TOKEN" ;;\n'
                "esac\n",
                encoding="utf-8",
            )
            askpass.chmod(0o700)
            env = self._env()
            env.update(
                {
                    "GIT_ASKPASS": str(askpass),
                    "GIT_CONFIG_GLOBAL": os.devnull,
                    "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_TERMINAL_PROMPT": "0",
                    "HOLLYSYS_GIT_ASKPASS_TOKEN": env["GITLAB_TOKEN"],
                }
            )
            try:
                result = subprocess.run(
                    [self.config.system_git_command, *args],
                    cwd=cwd,
                    env=env,
                    text=True,
                    capture_output=True,
                    timeout=self.config.command_timeout_seconds,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                if args and args[0] in {
                    "clone",
                    "fetch",
                    "pull",
                    "push",
                    "ls-remote",
                }:
                    raise DependencyTransientError(
                        f"Git HTTPS operation timed out: {args[0]}",
                        context=ErrorContext(
                            dependency="gitlab",
                            endpoint=f"git:{args[0]}",
                            error_code="timeout",
                        ),
                    ) from exc
                raise ControllerFatalError(
                    f"local_git_operation_timed_out:{args[0] if args else 'unknown'}"
                ) from exc
            except OSError as exc:
                raise ControllerFatalError(
                    f"system_git_unavailable:{self.config.system_git_command}"
                ) from exc
        if result.returncode != 0 and not tolerate:
            operation = args[0] if args else "unknown"
            summary = self._redact(
                result.stderr or result.stdout or "Git operation failed"
            )[:1000]
            context = ErrorContext(
                dependency="gitlab",
                endpoint=f"git:{operation}",
                error_code="git_https_failed",
            )
            lowered = summary.lower()
            if operation in {"clone", "fetch", "pull", "push", "ls-remote"}:
                if (
                    "authentication failed" in lowered
                    or "access denied" in lowered
                    or "401" in lowered
                    or "403" in lowered
                ):
                    raise DependencyAuthError(summary, context=context)
                if "429" in lowered or "too many requests" in lowered:
                    raise DependencyRateLimitedError(
                        summary,
                        context=ErrorContext(
                            dependency="gitlab",
                            endpoint=f"git:{operation}",
                            retry_after_seconds=self._retry_after(summary),
                            error_code="rate_limited",
                        ),
                    )
                if any(
                    marker in lowered
                    for marker in (
                        "could not resolve host",
                        "failed to connect",
                        "connection timed out",
                        "connection reset",
                        "tls",
                        "ssl",
                        "unexpected eof",
                        "gnutls",
                        "500",
                        "502",
                        "503",
                        "504",
                    )
                ):
                    raise DependencyTransientError(summary, context=context)
                raise DependencyContractError(summary, context=context)
            raise ControllerFatalError(f"local_git_operation_failed:{operation}")
        return result

    def delivery_mr(self, run: RunRecord, mr_iid: int) -> dict:
        project = self._project_endpoint(run.project.project_id)
        endpoint = f"{project}/merge_requests/{mr_iid}"
        mr = self._require_object(self.api(endpoint), endpoint)
        if (
            mr.get("source_branch") != run.workspace.branch
            or mr.get("target_branch") != run.workspace.target_branch
        ):
            raise ValueError(
                "delivery MR does not belong to the run branch/target"
            )
        return mr

    def abort_delivery(
        self,
        run: RunRecord,
        *,
        mr_iid: int | None,
        requested_by: str,
        reason: str,
    ) -> dict:
        """Write one abort audit note and close an unmerged delivery MR."""
        if mr_iid is None:
            return {"state": "absent", "iid": None, "web_url": None}
        mr = self.delivery_mr(run, mr_iid)
        if mr.get("state") == "merged":
            return {
                "state": "merged",
                "iid": mr.get("iid"),
                "web_url": mr.get("web_url"),
                "sha": mr.get("sha"),
                "merge_commit_sha": mr.get("merge_commit_sha"),
            }
        iid = int(mr["iid"])
        project = self._project_endpoint(run.project.project_id)
        marker = f"[hollysys-aborted:v4] run={run.run_key}"
        notes = self.paginated_list(
            f"{project}/merge_requests/{iid}/notes"
        )
        if not any(marker in str(note.get("body") or "") for note in notes):
            self.api(
                f"{project}/merge_requests/{iid}/notes",
                method="POST",
                fields={
                    "body": (
                        f"{marker}\n"
                        f"requested_by={requested_by}\n"
                        f"reason={reason[:1000]}\n"
                        "Controller stopped all managed work and preserved "
                        "the branch/worktree for human inspection."
                    )
                },
            )
        current = self.delivery_mr(run, iid)
        if current and current.get("state") == "opened":
            endpoint = f"{project}/merge_requests/{iid}"
            current = self._require_object(
                self.api(
                    endpoint,
                    method="PUT",
                    fields={"state_event": "close"},
                ),
                endpoint,
            )
        if current is None or current.get("state") not in {"closed", "merged"}:
            raise DependencyContractError(
                "GitLab did not confirm delivery MR closure",
                context=ErrorContext(
                    dependency="gitlab",
                    endpoint=f"{project}/merge_requests/{iid}",
                    error_code="abort_close_not_confirmed",
                ),
            )
        return current

    def validate_gate(self, run: RunRecord, metadata: CompletionMetadata) -> str | None:
        if metadata.stage not in {
            Stage.SPEC_REVIEW,
            Stage.PLAN_REVIEW,
            Stage.TASKS_REVIEW,
            Stage.TEST,
            Stage.CODE_REVIEW,
        }:
            return None
        mr = self.delivery_mr(run, metadata.mr_iid)
        if mr is None:
            raise ValueError("delivery MR does not exist")
        if metadata.mr_url and str(metadata.mr_url) != str(mr.get("web_url")):
            raise ValueError("metadata MR URL does not match GitLab")

        if metadata.stage in {
            Stage.SPEC_REVIEW,
            Stage.PLAN_REVIEW,
            Stage.TASKS_REVIEW,
        }:
            self.validate_artifact_completion(run, metadata)
        else:
            if metadata.head_sha != mr.get("sha"):
                raise ValueError(f"{metadata.stage} is not bound to current MR head")

        return self._validate_gate_note(
            run,
            int(mr["iid"]),
            metadata,
        )

    def validate_artifact_completion(
        self, run: RunRecord, metadata: CompletionMetadata
    ) -> None:
        stage_for_patterns = {
            Stage.SPEC_WRITE: Stage.SPEC_REVIEW,
            Stage.SPEC_REVIEW: Stage.SPEC_REVIEW,
            Stage.PLAN_WRITE: Stage.PLAN_REVIEW,
            Stage.PLAN_REVIEW: Stage.PLAN_REVIEW,
            Stage.TASKS_WRITE: Stage.TASKS_REVIEW,
            Stage.TASKS_REVIEW: Stage.TASKS_REVIEW,
        }.get(metadata.stage)
        if stage_for_patterns is None:
            raise ValueError(f"{metadata.stage} has no document artifact contract")
        if metadata.artifact_commit_sha is None or metadata.artifact_digest is None:
            raise ValueError("document artifact metadata is incomplete")
        refs = self.paginated_list(
            f"{self._project_endpoint(run.project.project_id)}/repository/"
            f"commits/{metadata.artifact_commit_sha}/refs?type=branch"
        )
        if not any(
            ref.get("type") == "branch" and ref.get("name") == run.workspace.branch
            for ref in refs
        ):
            raise ValueError(
                f"{metadata.stage} artifact commit is not on the delivery branch"
            )
        patterns = run.artifact_scope.patterns_for(stage_for_patterns)
        actual_paths = self.artifact_paths(
            run.project.project_id,
            metadata.artifact_commit_sha,
            patterns,
        )
        if sorted(metadata.artifact_paths) != actual_paths:
            raise ValueError(
                f"{metadata.stage} artifact path set differs from repository tree"
            )
        digest = self.artifact_digest(
            run.project.project_id,
            metadata.artifact_commit_sha,
            actual_paths,
        )
        if digest != metadata.artifact_digest:
            raise ValueError(f"{metadata.stage} artifact digest mismatch")
        changed_paths = self.changed_paths(
            run.project.project_id,
            metadata.head_before_sha,
            metadata.artifact_commit_sha,
        )
        if metadata.stage in {
            Stage.SPEC_WRITE,
            Stage.PLAN_WRITE,
            Stage.TASKS_WRITE,
        }:
            outside_phase = [
                path
                for path in changed_paths
                if not run.artifact_scope.phase_contains(metadata.stage, path)
            ]
            if outside_phase:
                raise ValueError(
                    f"{metadata.stage} changed paths outside current PRD scope: "
                    + ", ".join(sorted(outside_phase))
                )
        elif changed_paths:
            raise ValueError(f"{metadata.stage} reviewer changed repository head")
        run_changed_paths = self.changed_paths(
            run.project.project_id,
            run.workspace.repository_base_sha,
            metadata.artifact_commit_sha,
        )
        cross_scope = [
            path
            for path in run_changed_paths
            if path.startswith("docs/prds/")
            and not run.artifact_scope.run_allows_document_path(path)
        ]
        if cross_scope:
            raise ValueError(
                "delivery changed documents outside current PRD scope: "
                + ", ".join(sorted(cross_scope))
            )
        if metadata.mode == WorkMode.FINALIZATION:
            self.validate_forced_advance(run, metadata)

    def validate_forced_advance(
        self, run: RunRecord, metadata: CompletionMetadata
    ) -> None:
        forced = metadata.forced_advance
        if forced is None or metadata.mr_iid is None:
            raise ValueError("finalization is missing forced-advance evidence")
        mr = self.delivery_mr(run, metadata.mr_iid)
        if mr is None:
            raise ValueError("delivery MR does not exist")
        phase = {
            Stage.SPEC_WRITE: "spec",
            Stage.PLAN_WRITE: "plan",
            Stage.TASKS_WRITE: "tasks",
        }.get(metadata.stage)
        notes = self.paginated_list(
            f"{self._project_endpoint(run.project.project_id)}/merge_requests/"
            f"{metadata.mr_iid}/notes"
        )
        decision_matches: list[str] = []
        final_review_found = False
        expected_review_stage = f"{phase}-review"
        for note in notes:
            if not isinstance(note, dict):
                continue
            note_id = note.get("id")
            note_url = (
                f"{mr.get('web_url')}#note_{note_id}" if note_id is not None else ""
            )
            gate = GATE_RE.search(str(note.get("body") or ""))
            author = note.get("author") or {}
            author_values = {
                str(author.get("id") or ""),
                str(author.get("username") or ""),
                str(author.get("name") or ""),
            }
            reviewer_identities = set(
                self.config.reviewer_identities.get(expected_review_stage, [])
            )
            if (
                note_url == str(forced.final_review_url)
                and gate is not None
                and gate.group("run") == run.run_key
                and gate.group("stage") == expected_review_stage
                and gate.group("result") == "fail"
                and gate.group("task") == forced.final_review_card_id
                and bool(reviewer_identities.intersection(author_values))
            ):
                final_review_found = True
            match = FORCED_ADVANCE_RE.search(str(note.get("body") or ""))
            if not match:
                continue
            if (
                match.group("run") == run.run_key
                and match.group("phase") == phase
                and int(match.group("limit")) == forced.review_limit
                and match.group("task") == metadata.kanban_card_id
            ):
                decision_matches.append(note_url)
        if not final_review_found:
            raise ValueError(
                "final_review_url is not the third failed review gate note"
            )
        if (
            len(decision_matches) != 1
            or decision_matches[0] != str(forced.decision_url)
        ):
            raise ValueError(
                "expected exactly one idempotent HOLLYSYS-FORCED-ADVANCE "
                "decision note"
            )

    def artifact_paths(
        self, project_id: int, ref: str, patterns: list[str]
    ) -> list[str]:
        if not patterns:
            raise ValueError("artifact patterns are not configured")
        project = self._project_endpoint(project_id)
        result = self._run(
            [
                self.config.glab_command,
                "api",
                (
                    f"{project}/repository/tree?ref={quote(ref, safe='')}"
                    "&recursive=true&per_page=100"
                ),
                "--hostname",
                self.config.gitlab_hostname,
                "--paginate",
                "--output",
                "ndjson",
            ]
        )
        paths: list[str] = []
        for line in result.stdout.splitlines():
            decoded = json.loads(line)
            page_items = decoded if isinstance(decoded, list) else [decoded]
            for item in page_items:
                if not isinstance(item, dict) or item.get("type") != "blob":
                    continue
                path = str(item["path"])
                if any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns):
                    paths.append(path)
        paths.sort()
        if not paths:
            raise ValueError("configured artifact patterns matched no files")
        return paths

    def changed_paths(
        self,
        project_id: int,
        from_ref: str,
        to_ref: str,
    ) -> list[str]:
        if from_ref == to_ref:
            return []
        endpoint = (
            f"{self._project_endpoint(project_id)}/repository/compare"
            f"?from={quote(from_ref, safe='')}&to={quote(to_ref, safe='')}"
            "&straight=true"
        )
        result = self._require_object(self.api(endpoint), endpoint)
        if result.get("compare_timeout") or result.get("overflow"):
            raise DependencyContractError(
                "GitLab compare response is incomplete",
                context=ErrorContext(
                    dependency="gitlab",
                    endpoint="repository/compare",
                    error_code="incomplete_compare_response",
                ),
            )
        diffs = result.get("diffs")
        if not isinstance(diffs, list):
            raise DependencyContractError(
                "GitLab compare response lacks diffs",
                context=ErrorContext(
                    dependency="gitlab",
                    endpoint="repository/compare",
                    error_code="invalid_compare_response",
                ),
            )
        paths: set[str] = set()
        for diff in diffs:
            if not isinstance(diff, dict):
                continue
            for key in ("old_path", "new_path"):
                path = str(diff.get(key) or "")
                if path:
                    paths.add(path)
        return sorted(paths)

    def artifact_digest(self, project_id: int, ref: str, paths: list[str]) -> str:
        lines = []
        for path in sorted(paths):
            blob = self.file(project_id, path, ref).get("blob_id")
            if not blob:
                raise ValueError(f"GitLab returned no blob id for {path}@{ref}")
            lines.append(f"{path}\0{blob}\n")
        return hashlib.sha256("".join(lines).encode("utf-8")).hexdigest()

    def validate_artifact_gate_at_ref(
        self,
        run: RunRecord,
        metadata: CompletionMetadata,
        ref: str,
    ) -> None:
        pattern_stage = {
            Stage.SPEC_WRITE: Stage.SPEC_REVIEW,
            Stage.SPEC_REVIEW: Stage.SPEC_REVIEW,
            Stage.PLAN_WRITE: Stage.PLAN_REVIEW,
            Stage.PLAN_REVIEW: Stage.PLAN_REVIEW,
            Stage.TASKS_WRITE: Stage.TASKS_REVIEW,
            Stage.TASKS_REVIEW: Stage.TASKS_REVIEW,
        }.get(metadata.stage)
        if pattern_stage is None:
            return
        patterns = run.artifact_scope.patterns_for(pattern_stage)
        current_paths = self.artifact_paths(
            run.project.project_id,
            ref,
            patterns,
        )
        if current_paths != sorted(metadata.artifact_paths):
            raise ValueError(
                f"{metadata.stage} artifact set changed after it was frozen"
            )
        current_digest = self.artifact_digest(
            run.project.project_id,
            ref,
            current_paths,
        )
        if current_digest != metadata.artifact_digest:
            raise ValueError(
                f"{metadata.stage} artifacts changed after they were frozen"
            )

    def validate_baseline_at_ref(
        self, run: RunRecord, baseline: ArtifactBaseline, ref: str
    ) -> None:
        if baseline.phase == "prd":
            blob_id = self.file(
                run.project.project_id,
                run.source.prd_path,
                ref,
            ).get("blob_id")
            if blob_id != run.source.prd_blob_sha:
                raise ValueError("PRD changed after the run source was frozen")
            return
        pattern_stage = {
            "spec": Stage.SPEC_REVIEW,
            "plan": Stage.PLAN_REVIEW,
            "tasks": Stage.TASKS_REVIEW,
        }[baseline.phase]
        current_paths = self.artifact_paths(
            run.project.project_id,
            ref,
            run.artifact_scope.patterns_for(pattern_stage),
        )
        if current_paths != sorted(baseline.artifact_paths):
            raise ValueError(
                f"{baseline.phase} artifact set changed after it was frozen"
            )
        current_digest = self.artifact_digest(
            run.project.project_id,
            ref,
            current_paths,
        )
        if current_digest != baseline.artifact_digest:
            raise ValueError(
                f"{baseline.phase} artifacts changed after they were frozen"
            )

    def _validate_gate_note(
        self, run: RunRecord, mr_iid: int, metadata: CompletionMetadata
    ) -> str:
        identities = set(self.config.reviewer_identities.get(metadata.stage.value, []))
        if not identities:
            raise ValueError(f"no allowed identities configured for {metadata.stage}")
        notes = self.paginated_list(
            f"{self._project_endpoint(run.project.project_id)}/merge_requests/"
            f"{mr_iid}/notes"
        )
        expected_digest = metadata.artifact_digest or "na"
        expected_artifact = metadata.artifact_commit_sha or "na"
        expected_head = metadata.head_sha or "na"
        expected_test = (
            metadata.test_disposition.value
            if metadata.test_disposition is not None
            else "na"
        )
        for note in notes:
            if not isinstance(note, dict):
                continue
            match = GATE_RE.search(str(note.get("body") or ""))
            if not match:
                continue
            author = note.get("author") or {}
            author_values = {
                str(author.get("id") or ""),
                str(author.get("username") or ""),
                str(author.get("name") or ""),
            }
            if not identities.intersection(author_values):
                continue
            if (
                match.group("run") == run.run_key
                and match.group("stage") == metadata.stage.value
                and match.group("result") == metadata.outcome.value
                and match.group("digest") == expected_digest
                and match.group("artifact") == expected_artifact
                and match.group("head") == expected_head
                and match.group("test") == expected_test
                and match.group("task") == metadata.kanban_card_id
            ):
                author_id = author.get("id")
                if author_id is not None:
                    return f"id:{author_id}"
                return f"username:{author.get('username')}"
        raise ValueError(
            f"no valid {metadata.stage} gate note by an allowed GitLab identity"
        )

    def validate_merge(
        self,
        run: RunRecord,
        *,
        mr_iid: int,
        test: CompletionMetadata,
        code_review: CompletionMetadata,
    ) -> tuple[dict, str]:
        mr = self.delivery_mr(run, mr_iid)
        if mr is None:
            raise MergeBlocked("not_mergeable", "delivery MR does not exist")
        if mr.get("state") == "merged":
            return mr, str(mr.get("sha") or "")
        if mr.get("state") != "opened":
            raise MergeBlocked(
                "not_mergeable",
                f"delivery MR state is {mr.get('state') or 'unknown'}",
                url=str(mr.get("web_url") or "") or None,
            )
        if mr.get("draft") or mr.get("work_in_progress"):
            raise MergeBlocked(
                "draft",
                "delivery MR is still draft",
                url=str(mr.get("web_url") or "") or None,
            )
        head = str(mr.get("sha") or "")
        if not head or test.head_sha != head or code_review.head_sha != head:
            raise ValueError("test and code-review are not valid for current MR head")
        if test.mr_iid != mr_iid or code_review.mr_iid != mr_iid:
            raise ValueError("test/code-review refer to another MR")
        test_author = self.validate_gate(run, test)
        review_author = self.validate_gate(run, code_review)
        if test_author is None or review_author is None:
            raise ValueError("test/code-review gate author is missing")
        if test_author == review_author:
            raise ValueError(
                "test and code-review must be published by independent GitLab users"
            )
        pipelines = self.api(
            f"{self._project_endpoint(run.project.project_id)}/pipelines"
            f"?sha={quote(head, safe='')}"
            f"&ref={quote(run.workspace.branch, safe='')}"
            "&order_by=id&sort=desc&per_page=1"
        )
        if not isinstance(pipelines, list):
            raise DependencyContractError(
                "GitLab pipeline list is not an array",
                context=ErrorContext(
                    dependency="gitlab",
                    endpoint="pipelines",
                    error_code="invalid_pipeline_response",
                ),
            )
        pipeline = pipelines[0] if pipelines else None
        if isinstance(pipeline, dict) and (
            str(pipeline.get("sha") or "") != head
            or str(pipeline.get("ref") or "") != run.workspace.branch
        ):
            raise DependencyContractError(
                "GitLab pipeline response is not bound to the delivery head/ref",
                context=ErrorContext(
                    dependency="gitlab",
                    endpoint="pipelines",
                    error_code="pipeline_identity_mismatch",
                ),
            )
        pipeline_status = (
            str(pipeline.get("status") or "") if isinstance(pipeline, dict) else ""
        )
        pipeline_url = (
            str(pipeline.get("web_url") or "") or None
            if isinstance(pipeline, dict)
            else None
        )
        if pipeline_status in {"failed", "canceled"}:
            raise MergeBlocked(
                "pipeline_failed",
                f"checked-head pipeline is {pipeline_status}",
                url=pipeline_url,
                immediate_exception=True,
            )
        if pipeline_status == "skipped":
            raise MergeBlocked(
                "pipeline_skipped",
                "checked-head pipeline is skipped",
                url=pipeline_url,
                immediate_exception=True,
            )
        if pipeline_status != "success":
            raise MergeBlocked(
                "pipeline_pending",
                (
                    f"checked-head pipeline is {pipeline_status}"
                    if pipeline_status
                    else "checked-head pipeline does not exist"
                ),
                url=pipeline_url,
            )
        discussions = self.paginated_list(
            f"{self._project_endpoint(run.project.project_id)}/merge_requests/"
            f"{mr_iid}/discussions"
        )
        for discussion in discussions:
            if not isinstance(discussion, dict):
                continue
            for note in discussion.get("notes", []):
                if (
                    isinstance(note, dict)
                    and note.get("resolvable")
                    and not note.get("resolved")
                ):
                    note_id = note.get("id")
                    base_url = str(mr.get("web_url") or "")
                    note_url = (
                        f"{base_url}#note_{note_id}"
                        if base_url and note_id is not None
                        else base_url or None
                    )
                    author = note.get("author") or {}
                    raise MergeBlocked(
                        "discussion_unresolved",
                        "MR has unresolved blocking discussions",
                        url=note_url,
                        owner=str(author.get("username") or "") or None,
                        updated_at=str(note.get("updated_at") or "") or None,
                    )
        merge_status = str(mr.get("detailed_merge_status") or mr.get("merge_status"))
        if merge_status not in {"mergeable", "can_be_merged"}:
            if merge_status in {"not_approved", "approvals_missing"}:
                kind = "approval_missing"
            else:
                kind = "not_mergeable"
            raise MergeBlocked(
                kind,
                f"MR is not mergeable: {merge_status}",
                url=str(mr.get("web_url") or "") or None,
            )
        return mr, head

    def merge(self, run: RunRecord, mr_iid: int, checked_head: str) -> dict:
        mr = self.delivery_mr(run, mr_iid)
        if mr and mr.get("state") == "merged":
            if str(mr.get("sha") or "") != checked_head:
                raise CheckedHeadConflict(
                    "merged MR head differs from checked head"
                )
            return {
                **mr,
                CONTROLLER_MERGE_SUBMITTED_FIELD: False,
            }
        try:
            result = self.api(
                f"{self._project_endpoint(run.project.project_id)}/merge_requests/"
                f"{mr_iid}/merge",
                method="PUT",
                fields={"sha": checked_head},
            )
        except DependencyError as exc:
            # The merge may have committed even if the client lost the
            # response. Re-read GitLab before deciding whether to retry.
            try:
                current = self.delivery_mr(run, mr_iid)
            except DependencyError:
                raise exc
            if current and current.get("state") == "merged":
                if str(current.get("sha") or "") != checked_head:
                    raise CheckedHeadConflict(
                        "merged MR head differs from checked head"
                    ) from exc
                return {
                    **current,
                    CONTROLLER_MERGE_SUBMITTED_FIELD: True,
                }
            if current and str(current.get("sha") or "") != checked_head:
                raise CheckedHeadConflict(
                    "MR head changed during checked-head merge"
                ) from exc
            raise
        if not isinstance(result, dict) or result.get("state") != "merged":
            raise DependencyContractError(
                "GitLab did not confirm the checked-head merge",
                context=ErrorContext(
                    dependency="gitlab",
                    endpoint="merge_requests/merge",
                    error_code="merge_response_not_confirmed",
                ),
            )
        if (
            str(result.get("sha") or "") != checked_head
            or int(result.get("iid") or 0) != mr_iid
        ):
            raise CheckedHeadConflict(
                "GitLab merged a different MR head than the checked head"
            )
        return {
            **result,
            CONTROLLER_MERGE_SUBMITTED_FIELD: True,
        }

    def health(self) -> dict:
        user = self.api("user")
        return {
            "ok": isinstance(user, dict) and bool(user.get("id")),
            "user_id": user.get("id") if isinstance(user, dict) else None,
            "username": user.get("username") if isinstance(user, dict) else None,
        }
