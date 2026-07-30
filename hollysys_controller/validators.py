from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

VALIDATOR_VERSION = "tasks-graph/v1"

TASK_HEADER_RE = re.compile(
    r"(?m)^- \[ \] (?P<task_id>T[0-9]{3,})\b[^\n]*$"
)
TASK_DEPENDENCY_RE = re.compile(
    r"(?m)^\s+- depends_on[：:]\s*\[(?P<dependencies>[^\]]*)\]\s*$"
)
TASK_ACTION_RE = re.compile(
    r"(?m)^\s+- 动作[：:]\s*(?P<action>reuse|modify|extend|create)\s*$"
)


@dataclass(frozen=True)
class ValidationResult:
    validator: str
    validator_version: str
    input_digest: str
    passed: bool
    error_codes: tuple[str, ...]
    result_digest: str

    def as_dict(self) -> dict:
        return {
            "validator": self.validator,
            "validator_version": self.validator_version,
            "input_digest": self.input_digest,
            "passed": self.passed,
            "error_codes": list(self.error_codes),
            "result_digest": self.result_digest,
        }


def _digest_documents(documents: list[str]) -> str:
    encoded = json.dumps(
        documents,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _result(
    input_digest: str,
    errors: set[str],
) -> ValidationResult:
    error_codes = tuple(sorted(errors))
    result_digest = hashlib.sha256(
        json.dumps(
            {
                "validator": VALIDATOR_VERSION,
                "input_digest": input_digest,
                "error_codes": error_codes,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return ValidationResult(
        validator="tasks-graph",
        validator_version=VALIDATOR_VERSION,
        input_digest=input_digest,
        passed=not error_codes,
        error_codes=error_codes,
        result_digest=result_digest,
    )


def _looks_like_frozen_upstream(path: str) -> bool:
    normalized = path.strip().lstrip("./").lower()
    return any(
        part in normalized
        for part in (
            "/prds/",
            "/specs/",
            "/plans/",
            "docs/prd",
            "docs/spec",
            "docs/plan",
        )
    )


def validate_task_documents(documents: list[str]) -> ValidationResult:
    input_digest = _digest_documents(documents)
    errors: set[str] = set()
    tasks: dict[str, set[str]] = {}
    duplicate_ids: set[str] = set()

    for document in documents:
        headers = list(TASK_HEADER_RE.finditer(document))
        for index, header in enumerate(headers):
            task_id = header.group("task_id")
            if task_id in tasks:
                duplicate_ids.add(task_id)
                errors.add("duplicate_id")
            end = (
                headers[index + 1].start()
                if index + 1 < len(headers)
                else len(document)
            )
            block = document[header.start() : end]
            dependencies = list(TASK_DEPENDENCY_RE.finditer(block))
            dependency_ids: set[str] = set()
            if len(dependencies) != 1:
                errors.add("depends_on_count")
            else:
                dependency_text = dependencies[0].group(
                    "dependencies"
                ).strip()
                dependency_ids = set(
                    re.findall(r"\bT[0-9]{3,}\b", dependency_text)
                )
                residue = re.sub(
                    r"\bT[0-9]{3,}\b", "", dependency_text
                )
                residue = re.sub(r"[\s,，、]+", "", residue)
                if residue:
                    errors.add("malformed_dependency")
            actions = list(TASK_ACTION_RE.finditer(block))
            if len(actions) != 1:
                errors.add("action_count")
            else:
                action = actions[0].group("action")
                target_paths = re.findall(r"`([^`\n]+)`", block)
                if action != "reuse" and any(
                    _looks_like_frozen_upstream(path)
                    for path in target_paths
                ):
                    errors.add("frozen_file_modified")
            if task_id not in duplicate_ids:
                tasks[task_id] = dependency_ids

    if not tasks:
        errors.add("no_tasks")
        return _result(input_digest, errors)

    for task_id, dependencies in tasks.items():
        if dependencies - tasks.keys():
            errors.add("missing_dependency")
        if task_id in dependencies:
            errors.add("self_dependency")

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_id: str) -> None:
        if task_id in visiting:
            errors.add("dependency_cycle")
            return
        if task_id in visited:
            return
        visiting.add(task_id)
        for dependency in tasks[task_id]:
            if dependency in tasks:
                visit(dependency)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in tasks:
        visit(task_id)
    return _result(input_digest, errors)
