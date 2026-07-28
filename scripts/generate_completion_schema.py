#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hollysys_controller.models import CompletionMetadata


def generated_schema() -> dict:
    schema = CompletionMetadata.model_json_schema(mode="validation")
    schema["allOf"] = [
        {
            "if": {
                "properties": {"outcome": {"const": "scope_gap"}},
                "required": ["outcome"],
            },
            "then": {
                "required": ["scope_gap_target", "issues"],
                "properties": {
                    "scope_gap_target": {
                        "enum": ["spec-write", "plan-write", "tasks-write"]
                    },
                    "issues": {"type": "array", "minItems": 1},
                },
            },
        },
        {
            "if": {
                "properties": {"outcome": {"enum": ["pass", "fail", "cancelled"]}},
                "required": ["outcome"],
            },
            "then": {"properties": {"scope_gap_target": {"type": "null"}}},
        },
        {
            "if": {
                "properties": {
                    "stage": {
                        "enum": [
                            "spec-review",
                            "plan-review",
                            "tasks-review",
                        ]
                    },
                    "outcome": {"const": "pass"},
                },
                "required": ["stage", "outcome"],
            },
            "then": {
                "required": [
                    "artifact_paths",
                    "artifact_digest",
                    "review_commit_sha",
                ],
                "properties": {
                    "artifact_paths": {"type": "array", "minItems": 1},
                    "artifact_digest": {
                        "type": "string",
                        "pattern": "^[0-9a-f]{64}$",
                    },
                    "review_commit_sha": {
                        "type": "string",
                        "pattern": "^[0-9a-f]{40}$",
                    },
                },
            },
        },
        {
            "if": {
                "properties": {
                    "stage": {"enum": ["test", "code-review"]},
                    "outcome": {"const": "pass"},
                },
                "required": ["stage", "outcome"],
            },
            "then": {
                "required": ["mr_iid", "mr_url", "head_sha"],
                "properties": {
                    "mr_iid": {"type": "integer", "minimum": 1},
                    "mr_url": {"type": "string", "format": "uri"},
                    "head_sha": {
                        "type": "string",
                        "pattern": "^[0-9a-f]{40}$",
                    },
                },
            },
        },
    ]
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://hollysys.example/schemas/card-completion-v3.json",
        **schema,
    }


def main() -> None:
    destination = ROOT / "schemas" / "card-completion.schema.json"
    destination.write_text(
        json.dumps(generated_schema(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
