from __future__ import annotations

import base64
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

from .config import ControllerConfig
from .kanban import CommandError
from .models import (
    ArtifactBaseline,
    CompletionMetadata,
    FeishuOrigin,
    ProjectFacts,
    RunRecord,
    SourceFacts,
    Stage,
    WorkMode,
    WorkspaceFacts,
)

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
FORCED_ADVANCE_RE = re.compile(
    r"HOLLYSYS-FORCED-ADVANCE:\s+v=1\s+run=(?P<run>\S+)"
    r"\s+phase=(?P<phase>spec|plan|tasks)\s+review_limit=(?P<limit>[1-9][0-9]*)"
    r"\s+task=(?P<task>\S+)"
)


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
        env["GITLAB_HOST"] = self.config.gitlab_host
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
        result = subprocess.run(
            command,
            cwd=cwd,
            env=self._env(),
            input=input_text,
            text=True,
            capture_output=True,
            timeout=self.config.command_timeout_seconds,
            check=False,
        )
        if result.returncode != 0 and not tolerate:
            raise CommandError(command, result.returncode, result.stderr)
        return result

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
            self.config.gitlab_host,
            "--method",
            method,
        ]
        for key, value in (fields or {}).items():
            rendered = str(value).lower() if isinstance(value, bool) else value
            command.extend(["--field", f"{key}={rendered}"])
        result = self._run(command)
        return json.loads(result.stdout)

    @staticmethod
    def _project_endpoint(project_id: int | str) -> str:
        return f"projects/{quote(str(project_id), safe='')}"

    def paginated_list(self, endpoint: str) -> list[dict]:
        items: list[dict] = []
        page = 1
        separator = "&" if "?" in endpoint else "?"
        while True:
            response = self.api(f"{endpoint}{separator}per_page=100&page={page}")
            if not isinstance(response, list):
                raise TypeError(f"GitLab list response is invalid for {endpoint}")
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
        if blob.scheme != "https" or mr_url.scheme != "https":
            raise ValueError("GitLab URLs must use HTTPS")
        if blob.hostname != mr_url.hostname:
            raise ValueError("PRD and MR URLs use different hosts")
        if self.config.gitlab_host and blob.hostname != self.config.gitlab_host:
            raise ValueError("URL host is not the configured GitLab host")
        blob_match = PRD_URL_RE.match(blob.path)
        mr_match = MR_URL_RE.match(mr_url.path)
        if not blob_match or not mr_match:
            raise ValueError(
                "PRD URL must be a blob/raw URL pinned to a 40-character commit "
                "and MR URL must end in /-/merge_requests/<iid>"
            )
        project_path = unquote(blob_match.group("project"))
        if project_path != unquote(mr_match.group("project")):
            raise ValueError("PRD and MR URLs refer to different projects")
        if self.config.allowed_groups and not any(
            project_path == group or project_path.startswith(f"{group}/")
            for group in self.config.allowed_groups
        ):
            raise ValueError(f"project {project_path} is outside allowed groups")

        project = self.api(self._project_endpoint(project_path))
        assert isinstance(project, dict)
        if project.get("archived"):
            raise ValueError(f"project {project_path} is archived")
        project_id = int(project["id"])
        default_branch = str(project["default_branch"])
        mr_iid = int(mr_match.group("iid"))
        mr = self.api(f"{self._project_endpoint(project_id)}/merge_requests/{mr_iid}")
        assert isinstance(mr, dict)
        if mr.get("state") != "merged":
            raise ValueError("the PRD merge request is not merged")
        if mr.get("target_branch") != default_branch:
            raise ValueError("the PRD MR was not merged to the current default branch")

        prd_path = unquote(blob_match.group("path"))
        prd_sha = blob_match.group("sha")
        changes = self.api(
            f"{self._project_endpoint(project_id)}/merge_requests/{mr_iid}/changes"
        )
        assert isinstance(changes, dict)
        changed_paths = {
            str(item.get("new_path"))
            for item in changes.get("changes", [])
            if isinstance(item, dict)
        }
        if prd_path not in changed_paths:
            raise ValueError(
                "the merged PRD MR does not contain the requested PRD path"
            )

        requested_file = self.file(project_id, prd_path, prd_sha)
        current_file = self.file(project_id, prd_path, default_branch)
        if requested_file.get("blob_id") != current_file.get("blob_id"):
            raise ValueError(
                "the PRD on the default branch no longer equals the requested version"
            )
        branch = self.api(
            f"{self._project_endpoint(project_id)}/repository/branches/"
            f"{quote(default_branch, safe='')}"
        )
        assert isinstance(branch, dict)
        base_sha = str(branch["commit"]["id"])

        identity = f"{blob.hostname}|{project_id}|{prd_path}|{prd_sha}".encode()
        digest = hashlib.sha256(identity).digest()
        encoded = base64.b32encode(digest).decode("ascii").lower().rstrip("=")
        run_key = f"hollysys-{encoded[:20]}"
        repo_slug = str(project["path"]).lower()
        prd_name = Path(prd_path).stem.lower()
        safe_name = re.sub(r"[^a-z0-9._-]+", "-", prd_name).strip("-") or "prd"
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
                prd_blob_sha=str(requested_file["blob_id"]),
                prd_blob_url=prd_blob_url,
                prd_mr_url=prd_mr_url,
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
            raise TypeError(f"unexpected file response for {path}")
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
            clone_url = f"https://{run.project.host}/{run.project.project_path}.git"
            self._git(
                checkout.parent,
                ["clone", clone_url, str(checkout)],
            )
        origin = self._git(checkout, ["remote", "get-url", "origin"]).stdout.strip()
        expected_suffix = f"/{run.project.project_path}.git"
        parsed_origin = urlparse(origin)
        if (
            parsed_origin.hostname != run.project.host
            or not parsed_origin.path.rstrip("/").endswith(expected_suffix.rstrip("/"))
            or parsed_origin.username
            or parsed_origin.password
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
        if worktree.exists():
            actual = self._git(worktree, ["branch", "--show-current"]).stdout.strip()
            if actual != run.workspace.branch:
                raise ValueError(
                    f"existing worktree uses {actual!r}, expected {run.workspace.branch!r}"
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
                    "GIT_TERMINAL_PROMPT": "0",
                    "HOLLYSYS_GIT_ASKPASS_TOKEN": env["GITLAB_TOKEN"],
                }
            )
            result = subprocess.run(
                ["git", *args],
                cwd=cwd,
                env=env,
                text=True,
                capture_output=True,
                timeout=self.config.command_timeout_seconds,
                check=False,
            )
        if result.returncode != 0 and not tolerate:
            raise CommandError(["git", *args], result.returncode, result.stderr)
        return result

    def delivery_mr(self, run: RunRecord, mr_iid: int | None = None) -> dict | None:
        project = self._project_endpoint(run.project.project_id)
        if mr_iid is not None:
            result = self.api(f"{project}/merge_requests/{mr_iid}")
            return result if isinstance(result, dict) else None
        results = self.api(
            f"{project}/merge_requests?source_branch="
            f"{quote(run.workspace.branch, safe='')}&scope=all&per_page=20"
        )
        if not isinstance(results, list):
            return None
        matches = [
            item
            for item in results
            if isinstance(item, dict)
            and item.get("source_branch") == run.workspace.branch
            and item.get("target_branch") == run.workspace.target_branch
        ]
        if len(matches) > 1:
            raise RuntimeError("more than one delivery MR exists for the run branch")
        return matches[0] if matches else None

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
        actual_paths = self.artifact_paths(
            run.project.project_id,
            metadata.artifact_commit_sha,
            self.config.artifact_patterns.get(stage_for_patterns.value, []),
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
                self.config.gitlab_host,
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
        patterns = self.config.artifact_patterns.get(pattern_stage.value, [])
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
            self.config.artifact_patterns.get(pattern_stage.value, []),
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
            raise ValueError("delivery MR does not exist")
        if mr.get("state") == "merged":
            return mr, str(mr.get("merge_commit_sha") or "")
        if mr.get("draft") or mr.get("work_in_progress"):
            raise ValueError("delivery MR is still draft")
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
        merge_status = str(mr.get("detailed_merge_status") or mr.get("merge_status"))
        if merge_status not in {"mergeable", "can_be_merged"}:
            raise ValueError(f"MR is not mergeable: {merge_status}")
        if self.config.required_pipeline:
            pipelines = self.api(
                f"{self._project_endpoint(run.project.project_id)}/pipelines"
                f"?sha={head}&per_page=1"
            )
            if (
                not isinstance(pipelines, list)
                or not pipelines
                or pipelines[0].get("status") != "success"
            ):
                raise ValueError("required pipeline is not successful for checked head")
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
                    raise ValueError("MR has unresolved blocking discussions")
        return mr, head

    def merge(self, run: RunRecord, mr_iid: int, checked_head: str) -> dict:
        mr = self.delivery_mr(run, mr_iid)
        if mr and mr.get("state") == "merged":
            return mr
        try:
            result = self.api(
                f"{self._project_endpoint(run.project.project_id)}/merge_requests/"
                f"{mr_iid}/merge",
                method="PUT",
                fields={"sha": checked_head},
            )
        except CommandError as exc:
            # The merge may have committed even if the client lost the
            # response. Re-read GitLab before deciding whether to retry.
            current = self.delivery_mr(run, mr_iid)
            if current and current.get("state") == "merged":
                return current
            if current and str(current.get("sha") or "") != checked_head:
                raise CheckedHeadConflict(
                    "MR head changed during checked-head merge"
                ) from exc
            raise
        if not isinstance(result, dict) or result.get("state") != "merged":
            raise RuntimeError("GitLab did not confirm the checked-head merge")
        return result

    def health(self) -> dict:
        user = self.api("user")
        return {
            "ok": isinstance(user, dict) and bool(user.get("id")),
            "user_id": user.get("id") if isinstance(user, dict) else None,
            "username": user.get("username") if isinstance(user, dict) else None,
        }
