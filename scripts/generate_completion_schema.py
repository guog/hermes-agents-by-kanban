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
                "properties": {"outcome": {"const": "fail"}},
                "required": ["outcome"],
            },
            "then": {
                "required": ["issues"],
                "properties": {
                    "issues": {"type": "array", "minItems": 1},
                },
            },
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
                    "outcome": {"enum": ["pass", "fail"]},
                },
                "required": ["stage", "outcome"],
            },
            "then": {
                "required": [
                    "artifact_paths",
                    "artifact_digest",
                    "artifact_commit_sha",
                ],
                "properties": {
                    "artifact_paths": {"type": "array", "minItems": 1},
                    "artifact_digest": {
                        "type": "string",
                        "pattern": "^[0-9a-f]{64}$",
                    },
                    "artifact_commit_sha": {
                        "type": "string",
                        "pattern": "^[0-9a-f]{40}$",
                    },
                },
            },
        },
        {
            "if": {
                "properties": {
                    "stage": {
                        "enum": [
                            "spec-write",
                            "plan-write",
                            "tasks-write",
                            "implement",
                        ]
                    },
                    "outcome": {"const": "pass"},
                },
                "required": ["stage", "outcome"],
            },
            "then": {
                "required": [
                    "mr_iid",
                    "mr_url",
                    "head_sha",
                ],
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
                "required": ["baseline_disposition"],
                "properties": {
                    "baseline_disposition": {"const": "reviewed"},
                },
            },
        },
        {
            "if": {
                "properties": {
                    "stage": {"enum": ["test", "code-review"]},
                    "outcome": {"enum": ["pass", "fail"]},
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
        {
            "if": {
                "properties": {
                    "stage": {"const": "test"},
                    "outcome": {"enum": ["pass", "fail"]},
                },
                "required": ["stage", "outcome"],
            },
            "then": {
                "required": ["test_disposition"],
                "properties": {
                    "test_disposition": {
                        "enum": ["executed", "skipped_unavailable"]
                    },
                },
            },
            "else": {
                "properties": {
                    "test_disposition": {"type": "null"},
                    "skip_reason": {"type": "null"},
                },
            },
        },
        {
            "if": {
                "properties": {
                    "stage": {
                        "enum": [
                            "spec-write",
                            "plan-write",
                            "tasks-write",
                            "implement",
                        ]
                    },
                    "outcome": {"const": "pass"},
                },
                "required": ["stage", "outcome"],
            },
            "then": {
                "required": ["repository_evidence"],
                "properties": {
                    "repository_evidence": {"type": "object"},
                },
            },
        },
        {
            "if": {
                "properties": {
                    "stage": {"const": "test"},
                    "test_disposition": {"const": "skipped_unavailable"},
                },
                "required": ["stage", "test_disposition"],
            },
            "then": {
                "required": [
                    "outcome",
                    "skip_reason",
                    "verification",
                    "residual_risk",
                ],
                "properties": {
                    "outcome": {"const": "pass"},
                    "skip_reason": {"type": "string", "minLength": 1},
                    "verification": {"type": "array", "minItems": 1},
                    "residual_risk": {"type": "array", "minItems": 1},
                },
            },
            "else": {
                "properties": {
                    "skip_reason": {"type": "null"},
                },
            },
        },
        {
            "if": {
                "properties": {
                    "mode": {"const": "finalization"},
                    "outcome": {"const": "pass"},
                },
                "required": ["mode", "outcome"],
            },
            "then": {
                "required": [
                    "artifact_paths",
                    "artifact_digest",
                    "artifact_commit_sha",
                    "baseline_disposition",
                    "forced_advance",
                    "mr_iid",
                    "mr_url",
                ],
                "properties": {
                    "artifact_paths": {"type": "array", "minItems": 1},
                    "baseline_disposition": {
                        "const": "forced_after_review_limit"
                    },
                    "forced_advance": {"type": "object"},
                },
            },
        },
        {
            "if": {
                "required": ["gate_phase"],
                "properties": {
                    "gate_phase": {"type": "string"},
                },
            },
            "then": {
                "required": [
                    "gate_decision",
                    "gate_reviewer",
                    "gate_reviewed_at",
                    "gate_reason",
                    "gate_evidence_refs",
                    "gate_artifact_paths",
                    "gate_artifact_commit_sha",
                    "gate_artifact_digest",
                    "contract_refs",
                    "requirement_ids",
                ],
                "properties": {
                    "gate_evidence_refs": {
                        "type": "array",
                        "minItems": 1,
                        "uniqueItems": True,
                    },
                    "gate_artifact_paths": {
                        "type": "array",
                        "minItems": 1,
                        "uniqueItems": True,
                    },
                    "contract_refs": {
                        "type": "array",
                        "minItems": 1,
                        "uniqueItems": True,
                    },
                    "requirement_ids": {
                        "type": "array",
                        "minItems": 1,
                        "uniqueItems": True,
                    },
                },
            },
        },
        {
            "if": {
                "required": ["stage", "outcome"],
                "properties": {
                    "stage": {"const": "tasks-review"},
                    "outcome": {"const": "pass"},
                },
            },
            "then": {
                "required": ["gate_phase", "gate_decision"],
                "properties": {
                    "gate_phase": {"const": "implementation_entry"},
                    "gate_decision": {"const": "approved"},
                },
            },
        },
        {
            "if": {
                "required": ["stage", "outcome"],
                "properties": {
                    "stage": {"const": "code-review"},
                    "outcome": {"const": "pass"},
                },
            },
            "then": {
                "required": ["gate_phase", "gate_decision"],
                "properties": {
                    "gate_phase": {
                        "const": "implementation_completion"
                    },
                    "gate_decision": {"const": "approved"},
                },
            },
        },
    ]
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://hollysys.example/schemas/card-completion-v8.json",
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
