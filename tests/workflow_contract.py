"""Test-only helpers for the Hermes one-PRD/one-MR workflow contract."""

from __future__ import annotations

import base64
import hashlib
import re
import shlex
from dataclasses import dataclass, field
from urllib.parse import quote, unquote, urlparse


PRD_PATH_RE = re.compile(r"^docs/prds/prd-[a-z0-9]+(?:-[a-z0-9]+)*\.md$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")

STAGE_ASSIGNEES = {
    "spec-write": "spec-writer",
    "spec-review": "spec-reviewer",
    "spec-rework": "spec-writer",
    "plan-write": "planner",
    "plan-review": "plan-reviewer",
    "plan-rework": "planner",
    "tasks-write": "tasker",
    "tasks-review": "task-reviewer",
    "tasks-rework": "tasker",
    "implement": "coder",
    "test": "tester",
    "code-review": "code-reviewer",
    "code-rework": "coder",
    "merge": "dispatcher",
}
CONTINUATION_STAGES = {
    "spec-write": "spec-gate",
    "spec-review": "spec-gate",
    "spec-rework": "spec-gate",
    "plan-write": "plan-gate",
    "plan-review": "plan-gate",
    "plan-rework": "plan-gate",
    "tasks-write": "tasks-gate",
    "tasks-review": "tasks-gate",
    "tasks-rework": "tasks-gate",
    "implement": "code-gate",
    "test": "test-gate",
    "code-review": "code-gate",
    "code-rework": "code-gate",
    "merge": "run-complete",
}


class ContractError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PrdBlobUrl:
    host: str
    project_path: str
    commit_sha: str
    prd_path: str


@dataclass(frozen=True)
class PrdMrUrl:
    host: str
    project_path: str
    iid: int


@dataclass
class SimCard:
    card_id: str
    idempotency_key: str
    stage: str
    assignee: str
    created_by: str
    parent_ids: set[str] = field(default_factory=set)
    status: str = "blocked"
    attempts: int = 0
    predicted_live_facts: dict[str, str | None] = field(default_factory=dict)


class KanbanSimulation:
    """Small contract model for dispatcher-only, idempotent card promotion."""

    def __init__(self) -> None:
        self.cards: dict[str, SimCard] = {}
        self.by_key: dict[str, str] = {}

    def create_card(
        self,
        *,
        actor: str,
        idempotency_key: str,
        stage: str,
        assignee: str,
        parent_ids: set[str],
        created_by: str = "dispatcher",
        predicted_live_facts: dict[str, str | None] | None = None,
    ) -> SimCard:
        if actor != "dispatcher":
            raise ContractError("dispatcher_required", "只有 dispatcher 可以创建 Kanban card")
        existing_id = self.by_key.get(idempotency_key)
        if existing_id:
            existing = self.cards[existing_id]
            immutable = (existing.stage, existing.assignee, existing.parent_ids, existing.created_by)
            requested = (stage, assignee, parent_ids, created_by)
            if immutable != requested:
                raise ContractError("idempotency_conflict", "稳定 key 已绑定到不同 card")
            return existing
        if any(parent_id not in self.cards for parent_id in parent_ids):
            raise ContractError("missing_parent", "父卡不存在")
        card_id = f"t_{len(self.cards) + 1}"
        ready = not parent_ids or all(self.cards[parent_id].status == "complete" for parent_id in parent_ids)
        card = SimCard(
            card_id=card_id,
            idempotency_key=idempotency_key,
            stage=stage,
            assignee=assignee,
            created_by=created_by,
            parent_ids=set(parent_ids),
            status="ready" if ready else "blocked",
            predicted_live_facts=dict(predicted_live_facts or {}),
        )
        self.cards[card_id] = card
        self.by_key[idempotency_key] = card_id
        return card

    def link(self, *, actor: str, parent_id: str, child_id: str) -> None:
        if actor != "dispatcher":
            raise ContractError("dispatcher_required", "只有 dispatcher 可以链接 Kanban card")
        if parent_id not in self.cards or child_id not in self.cards:
            raise ContractError("missing_card", "链接的 card 不存在")
        self.cards[child_id].parent_ids.add(parent_id)

    def create_dispatch_pair(
        self,
        *,
        actor: str,
        current_gate_id: str,
        run_key: str,
        next_stage: str,
        iteration: int,
    ) -> tuple[SimCard, SimCard]:
        if next_stage not in STAGE_ASSIGNEES or iteration < 1:
            raise ContractError("invalid_transition", "未知阶段或迭代号")
        transition_key = f"{run_key}:{next_stage}:{iteration}"
        worker = self.create_card(
            actor=actor,
            idempotency_key=f"{transition_key}:work",
            stage=next_stage,
            assignee=STAGE_ASSIGNEES[next_stage],
            parent_ids={current_gate_id},
        )
        continuation = self.create_card(
            actor=actor,
            idempotency_key=f"{transition_key}:continue",
            stage=CONTINUATION_STAGES[next_stage],
            assignee="dispatcher",
            parent_ids={worker.card_id},
            predicted_live_facts={
                "head_sha": None,
                "artifact_digest": None,
                "result": None,
            },
        )
        return worker, continuation

    def complete(self, card_id: str) -> None:
        self.cards[card_id].status = "complete"
        for card in self.cards.values():
            if card.status == "blocked" and card.parent_ids and all(
                self.cards[parent_id].status == "complete" for parent_id in card.parent_ids
            ):
                card.status = "ready"

    def fail_attempt(self, card_id: str, *, failure_limit: int = 3) -> None:
        card = self.cards[card_id]
        card.attempts += 1
        card.status = "blocked" if card.attempts >= failure_limit else "ready"

    def restore_card(self, card: SimCard) -> None:
        """Load an existing database row, including legacy/untrusted provenance."""
        self.cards[card.card_id] = card
        self.by_key[card.idempotency_key] = card.card_id

    def dispatchable_ready_cards(self) -> list[SimCard]:
        return [
            card
            for card in self.cards.values()
            if card.status == "ready" and card.created_by == "dispatcher"
        ]


def _split_gitlab_url(url: str) -> tuple[str, str, str]:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ContractError("invalid_url", "URL 必须是无内嵌凭据的 HTTPS GitLab URL")
    decoded = unquote(parsed.path).strip("/")
    if "/-/" not in decoded:
        raise ContractError("invalid_url", "URL 不是可识别的 GitLab 项目 URL")
    project_path, suffix = decoded.split("/-/", 1)
    if not project_path or "/" not in project_path:
        raise ContractError("invalid_url", "URL 缺少明确的 GitLab 群组/项目")
    return parsed.hostname.lower(), project_path, suffix


def parse_prd_blob_url(url: str) -> PrdBlobUrl:
    host, project_path, suffix = _split_gitlab_url(url)
    parts = suffix.split("/", 2)
    if len(parts) != 3 or parts[0] not in {"blob", "raw"} or not SHA_RE.fullmatch(parts[1]):
        raise ContractError("invalid_prd_url", "PRD URL 必须使用精确 40 位 commit 的 blob/raw 地址")
    prd_path = parts[2]
    if not PRD_PATH_RE.fullmatch(prd_path):
        raise ContractError("invalid_prd_path", "PRD 路径必须是 docs/prds/prd-<key>.md")
    return PrdBlobUrl(host, project_path, parts[1], prd_path)


def parse_prd_mr_url(url: str) -> PrdMrUrl:
    host, project_path, suffix = _split_gitlab_url(url)
    match = re.fullmatch(r"merge_requests/([1-9][0-9]*)/?", suffix)
    if not match:
        raise ContractError("invalid_mr_url", "第二个 URL 必须是 GitLab PRD MR 地址")
    return PrdMrUrl(host, project_path, int(match.group(1)))


def parse_start_command(message: str) -> tuple[PrdBlobUrl, PrdMrUrl]:
    try:
        parts = shlex.split(message)
    except ValueError as exc:
        raise ContractError("invalid_command", "启动命令格式不完整") from exc
    if len(parts) != 4 or parts[:2] != ["实现", "PRD"]:
        raise ContractError(
            "missing_or_extra_url",
            "请提供：实现 PRD <精确 PRD blob/raw URL> <已合并 PRD MR URL>",
        )
    blob = parse_prd_blob_url(parts[2])
    mr = parse_prd_mr_url(parts[3])
    if (blob.host, blob.project_path) != (mr.host, mr.project_path):
        raise ContractError("project_mismatch", "PRD URL 与 PRD MR URL 不属于同一项目")
    return blob, mr


def group_allowed(project_path: str, allowed_groups: list[str]) -> bool:
    return any(group and project_path.startswith(group.rstrip("/") + "/") for group in allowed_groups)


def validate_intake(
    blob: PrdBlobUrl,
    mr: PrdMrUrl,
    *,
    expected_host: str,
    allowed_groups: list[str],
    project_accessible: bool,
    file_exists: bool,
    archived: bool,
    mr_state: str,
    mr_target_branch: str,
    default_branch: str,
    mr_merge_commit_sha: str | None,
    mr_contains_prd: bool,
    current_prd_blob_sha: str | None,
    requested_prd_blob_sha: str | None,
) -> None:
    if blob.host != expected_host.lower() or mr.host != expected_host.lower() or not group_allowed(blob.project_path, allowed_groups):
        raise ContractError("outside_allowlist", "GitLab host 或群组不在允许范围")
    if not project_accessible:
        raise ContractError("project_unreadable", "项目不存在或当前身份无访问权限")
    if not file_exists:
        raise ContractError("file_missing", "文件不存在")
    if archived:
        raise ContractError("archived", "项目已归档，不允许修改")
    if mr_state != "merged" or not mr_merge_commit_sha:
        raise ContractError("mr_not_merged", "PRD MR 尚未合并")
    if mr_target_branch != default_branch:
        raise ContractError("wrong_target", "PRD MR 未合入项目当前默认分支")
    if blob.commit_sha != mr_merge_commit_sha or not mr_contains_prd:
        raise ContractError("wrong_prd_revision", "PRD URL 不是该 MR 的有效合入版本")
    if not current_prd_blob_sha or current_prd_blob_sha != requested_prd_blob_sha:
        raise ContractError("stale_prd", "PRD 已有更新；该 URL 不是当前有效版本")


def derive_run_key(host: str, project_id: int, prd_path: str, prd_commit_sha: str) -> str:
    canonical = f"{host.lower()}|{project_id}|{prd_path}|{prd_commit_sha}".encode()
    token = base64.b32encode(hashlib.sha256(canonical).digest()).decode().lower().rstrip("=")
    return f"sdd-{token[:20]}"


def reconcile_run(existing_status: str | None, delivery_mr_state: str | None) -> str:
    if delivery_mr_state == "merged" or existing_status == "complete":
        return "complete"
    if existing_status in {"running", "ready", "blocked", "paused"}:
        return "resume"
    return "start"


def workspace_reconcile_action(
    *,
    checkout_exists: bool,
    checkout_is_git: bool,
    origin_matches: bool,
    worktree_exists: bool,
    worktree_branch_matches: bool,
) -> str:
    """Return the idempotent workspace action before any filesystem write."""
    if not checkout_exists:
        return "clone"
    if not checkout_is_git:
        raise ContractError("checkout_not_git", "既有 checkout 路径不是 Git 仓库")
    if not origin_matches:
        raise ContractError("origin_mismatch", "既有 checkout origin 与项目身份不一致")
    if worktree_exists and not worktree_branch_matches:
        raise ContractError("worktree_branch_mismatch", "既有 run worktree 分支不一致")
    return "reuse" if worktree_exists else "create_worktree"


def artifact_digest(path_blob_pairs: list[tuple[str, str]]) -> str:
    if not path_blob_pairs:
        raise ContractError("empty_artifact_set", "工件集合不能为空")
    ordered = sorted(path_blob_pairs)
    if len({path for path, _ in ordered}) != len(ordered):
        raise ContractError("duplicate_artifact_path", "工件路径不能重复")
    payload = b"".join(f"{path}\0{blob_sha}\n".encode() for path, blob_sha in ordered)
    return hashlib.sha256(payload).hexdigest()


def validate_one_to_one_artifact_keys(
    spec_paths: list[str], plan_paths: list[str], task_paths: list[str]
) -> list[str]:
    def keys(paths: list[str], prefix: str) -> set[str]:
        result = set()
        pattern = re.compile(rf"^{prefix}-(.+)\.md$")
        for path in paths:
            match = pattern.fullmatch(path.rsplit("/", 1)[-1])
            if not match or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", match.group(1)):
                raise ContractError("invalid_artifact_name", f"工件命名不符合 {prefix}-<key>.md：{path}")
            result.add(match.group(1))
        return result

    spec_keys = keys(spec_paths, "spec")
    plan_keys = keys(plan_paths, "plan")
    task_keys = keys(task_paths, "task")
    if not spec_keys or spec_keys != plan_keys or spec_keys != task_keys:
        raise ContractError("artifact_key_mismatch", "SPEC、PLAN、TASKS key 集合必须完整且一一对应")
    return sorted(spec_keys)


def invalidated_gates(changed_stage: str) -> set[str]:
    invalidation = {
        "spec": {"spec", "plan", "tasks", "test", "code-review"},
        "plan": {"plan", "tasks", "test", "code-review"},
        "tasks": {"tasks", "test", "code-review"},
        "code": {"test", "code-review"},
    }
    try:
        return invalidation[changed_stage]
    except KeyError as exc:
        raise ContractError("invalid_stage", f"未知变更阶段：{changed_stage}") from exc


def design_rework_target(stage: str, result: str, scope_owner: str | None = None) -> str | None:
    producers = {"spec": "spec-writer", "plan": "planner", "tasks": "tasker"}
    if result == "pass":
        return None
    if result == "fail":
        if stage not in producers:
            raise ContractError("invalid_stage", f"未知设计阶段：{stage}")
        return producers[stage]
    if result == "scope_gap" and scope_owner in producers:
        return producers[scope_owner]
    raise ContractError("invalid_rework", "scope_gap 必须明确归属 SPEC、PLAN 或 TASKS")


def code_gates_match(current_head: str, tester_head: str | None, reviewer_head: str | None) -> bool:
    return bool(SHA_RE.fullmatch(current_head)) and tester_head == current_head == reviewer_head


def checked_head_for_merge(
    *,
    current_head: str,
    expected_head: str,
    mr_ready: bool,
    mergeable: bool,
    required_pipeline_status: str,
    blocking_discussions: int,
    artifact_gates_valid: bool,
    tester_head: str | None,
    reviewer_head: str | None,
) -> str:
    if current_head != expected_head or not code_gates_match(current_head, tester_head, reviewer_head):
        raise ContractError("head_drift", "MR head 漂移；必须重新测试和代码评审")
    if not mr_ready or not mergeable:
        raise ContractError("mr_not_mergeable", "MR 尚未 ready 或不可合并")
    if required_pipeline_status != "success":
        raise ContractError("pipeline_not_successful", "必需 pipeline 未成功")
    if blocking_discussions:
        raise ContractError("blocking_discussions", "仍有未解决的阻塞 discussion")
    if not artifact_gates_valid:
        raise ContractError("artifact_gate_stale", "设计工件门禁已失效")
    return current_head


def validate_merge_completion(*, head_sha: str, checked_head: str, merge_api_sha: str) -> None:
    if not all(SHA_RE.fullmatch(value) for value in (head_sha, checked_head, merge_api_sha)):
        raise ContractError("invalid_merge_binding", "merge 必须绑定完整 SHA")
    if head_sha != checked_head or checked_head != merge_api_sha:
        raise ContractError("unchecked_merge", "completion head、checked_head 与 merge API sha 必须相同")


def permanent_blob_links(project_url: str, merge_sha: str, paths: list[str]) -> list[str]:
    if not SHA_RE.fullmatch(merge_sha):
        raise ContractError("invalid_merge_sha", "永久链接必须使用完整 merge commit SHA")
    base = project_url.rstrip("/")
    return [f"{base}/-/blob/{merge_sha}/{quote(path, safe='/')}" for path in sorted(paths)]
