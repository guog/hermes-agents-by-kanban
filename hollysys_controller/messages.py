from __future__ import annotations

import html
import re
from collections.abc import Iterable, Sequence
from typing import Literal
from urllib.parse import urlsplit

from .models import FeishuOrigin

MessageFormat = Literal["text", "markdown"]
MessageField = tuple[str, str]
MessageSection = tuple[str, Sequence[str]]

MESSAGE_TEMPLATE = "{mention}**{icon} {title}**\n\n{body}"
FIELD_TEMPLATE = "**{label}：** {value}"
SECTION_TEMPLATE = "**{label}：**\n{items}"
LIST_ITEM_TEMPLATE = "- {value}"

STAGE_LABELS = {
    "run-init": "run-init（运行初始化）",
    "spec-write": "spec-write（编写 SPEC）",
    "spec-review": "spec-review（审查 SPEC）",
    "plan-write": "plan-write（编写 PLAN）",
    "plan-review": "plan-review（审查 PLAN）",
    "tasks-write": "tasks-write（拆分 TASKS）",
    "tasks-review": "tasks-review（审查 TASKS）",
    "implement": "implement（代码实现）",
    "test": "test（测试）",
    "code-review": "code-review（代码审查）",
    "merge-wait": "merge-wait（等待合并）",
    "exception": "exception（异常处理）",
}

AGENT_LABELS = {
    "dispatcher": "Dispatcher",
    "prd-writer": "PRD Writer",
    "spec-writer": "SPEC Writer",
    "spec-reviewer": "SPEC Reviewer",
    "planner": "Planner",
    "plan-reviewer": "PLAN Reviewer",
    "tasker": "Tasker",
    "task-reviewer": "TASKS Reviewer",
    "coder": "Coder",
    "tester": "Tester",
    "code-reviewer": "Code Reviewer",
    "fde": "FDE",
}

OUTCOME_LABELS = {
    "pass": "pass（通过）",
    "fail": "fail（未通过）",
    "cancelled": "cancelled（已取消）",
    "accepted": "accepted（已接受）",
    "rejected": "rejected（已拒绝）",
    "skipped_unavailable": "skipped_unavailable（条件不可用，已跳过）",
}

EVENT_LABELS = {
    "blocked": "blocked（已阻塞）",
    "crashed": "crashed（进程崩溃）",
    "rate_limited": "rate_limited（外部依赖暂不可用，冷却后重试）",
    "timed_out": "timed_out（执行超时）",
    "gave_up": "gave_up（已停止重试）",
    "spawn_auto_blocked": "spawn_auto_blocked（自动启动受阻）",
}

_WHITESPACE = re.compile(r"\s+")
_GITLAB_MR_PATH = re.compile(r"/-/merge_requests/([1-9][0-9]*)(?:/|$)")
_GITLAB_PIPELINE_PATH = re.compile(r"/-/pipelines/([1-9][0-9]*)(?:/|$)")
_GITLAB_JOB_PATH = re.compile(r"/-/jobs/([1-9][0-9]*)(?:/|$)")
_GITLAB_COMMIT_PATH = re.compile(r"/-/commits?/([0-9a-fA-F]{7,40})(?:/|$)")


def escape_markdown(value: object, *, limit: int | None = None) -> str:
    """Render untrusted text as one safe Feishu Markdown line."""
    normalized = _WHITESPACE.sub(" ", str(value)).strip()
    if limit is not None:
        normalized = normalized[:limit]
    escaped = html.escape(normalized, quote=False)
    escaped = escaped.replace("\\", "\\\\")
    for token in ("`", "*", "_", "[", "]"):
        escaped = escaped.replace(token, f"\\{token}")
    return escaped or "无"


def human_summary(value: object, *, limit: int = 180) -> str:
    """Render one concise, safe human-facing summary with a visible ellipsis."""
    normalized = _WHITESPACE.sub(" ", str(value)).strip()
    if len(normalized) <= limit:
        return escape_markdown(normalized)
    if limit < 2:
        return "…"
    candidate = normalized[: limit - 1].rstrip()
    lower_bound = max(1, limit // 2)
    boundary = max(
        (
            candidate.rfind(token, lower_bound)
            for token in ("。", "；", ";", "，", ",", " ")
        ),
        default=-1,
    )
    if boundary >= lower_bound:
        candidate = candidate[:boundary].rstrip()
    candidate = candidate.rstrip("。；;，,:：")
    return escape_markdown(f"{candidate}…")


def inline_code(value: object) -> str:
    normalized = _WHITESPACE.sub(" ", str(value)).strip()
    normalized = normalized.replace("`", "ˋ")
    return f"`{normalized or 'unknown'}`"


def markdown_link(label: object, url: object) -> str:
    raw_url = str(url).strip()
    parsed = urlsplit(raw_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return escape_markdown(label)
    safe_url = raw_url.replace(" ", "%20").replace(")", "%29")
    return f"[{escape_markdown(label)}]({safe_url})"


def gitlab_link_label(
    url: object,
    *,
    default: str = "查看 GitLab 证据",
) -> str:
    """Give a GitLab URL a stable label that tells a human what it opens."""
    parsed = urlsplit(str(url).strip())
    mr_match = _GITLAB_MR_PATH.search(parsed.path)
    if mr_match:
        mr_label = f"MR !{mr_match.group(1)}"
        if parsed.fragment.startswith("note_"):
            return f"查看 {mr_label} 审查记录"
        if parsed.path.rstrip("/").endswith("/diffs"):
            return f"查看 {mr_label} 代码变更"
        return f"查看 {mr_label}"
    pipeline_match = _GITLAB_PIPELINE_PATH.search(parsed.path)
    if pipeline_match:
        return f"查看流水线 #{pipeline_match.group(1)}"
    job_match = _GITLAB_JOB_PATH.search(parsed.path)
    if job_match:
        return f"查看 Job #{job_match.group(1)}"
    commit_match = _GITLAB_COMMIT_PATH.search(parsed.path)
    if commit_match:
        return f"查看提交 {commit_match.group(1)[:12]}"
    return default


def format_stage(stage: object) -> str:
    raw = str(getattr(stage, "value", stage))
    return STAGE_LABELS.get(raw, escape_markdown(raw))


def format_agent(agent: object) -> str:
    raw = str(agent or "unknown")
    return AGENT_LABELS.get(raw, escape_markdown(raw))


def format_outcome(outcome: object) -> str:
    raw = str(getattr(outcome, "value", outcome))
    return OUTCOME_LABELS.get(raw, escape_markdown(raw))


def format_event(event: object) -> str:
    raw = str(event)
    return EVENT_LABELS.get(raw, escape_markdown(raw))


def format_duration(seconds: int | None) -> str:
    if seconds is None:
        return "未知"
    remaining = max(0, int(seconds))
    hours, remaining = divmod(remaining, 3600)
    minutes, seconds_part = divmod(remaining, 60)
    if hours:
        return f"{hours}小时{minutes:02d}分{seconds_part:02d}秒"
    if minutes:
        return f"{minutes}分{seconds_part:02d}秒"
    return f"{seconds_part}秒"


def format_attempt(attempt: object, redispatch_limit: int) -> str:
    total = max(1, int(redispatch_limit) + 1)
    try:
        current = int(attempt)
    except (TypeError, ValueError):
        return f"未知/{total}"
    if current < 1:
        return f"未知/{total}"
    return f"{current}/{total}"


def short_sha(value: object) -> str:
    raw = str(value or "").strip()
    return raw[:12] if raw else "unknown"


def render_message(
    *,
    mention: str,
    icon: str,
    title: str,
    fields: Iterable[MessageField],
    sections: Iterable[MessageSection] = (),
) -> str:
    body: list[str] = [
        FIELD_TEMPLATE.format(label=label, value=value)
        for label, value in fields
        if value
    ]
    for label, values in sections:
        items = [
            LIST_ITEM_TEMPLATE.format(value=value)
            for value in values
            if value
        ]
        if not items:
            continue
        if body:
            body.append("")
        body.append(
            SECTION_TEMPLATE.format(label=label, items="\n".join(items))
        )
    return MESSAGE_TEMPLATE.format(
        mention=mention,
        icon=icon,
        title=escape_markdown(title),
        body="\n".join(body),
    )


def markdown_payload(origin: FeishuOrigin, content: str) -> dict:
    return {
        "origin": origin.model_dump(mode="json"),
        "format": "markdown",
        "content": content,
    }
