#!/usr/bin/env python3
"""Validate Hermes SDD card completion metadata without third-party packages."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


DEFAULT_SCHEMA = Path("/opt/fleet/schemas/card-completion.schema.json")
SUPPORTED_KEYWORDS = {
    "$comment",
    "$id",
    "$schema",
    "additionalProperties",
    "allOf",
    "const",
    "enum",
    "format",
    "if",
    "items",
    "minimum",
    "minItems",
    "minLength",
    "pattern",
    "properties",
    "required",
    "then",
    "title",
    "type",
    "uniqueItems",
}
SUPPORTED_TYPES = {"array", "boolean", "integer", "null", "number", "object", "string"}


class SchemaContractError(ValueError):
    """Raised when the checked-in schema uses an unsupported construct."""


def load_schema(schema_path: str | Path = DEFAULT_SCHEMA) -> dict[str, Any]:
    path = Path(schema_path)
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SchemaContractError(f"cannot load completion schema {path}: {exc}") from exc
    if not isinstance(schema, dict):
        raise SchemaContractError(f"completion schema {path} must be a JSON object")
    _assert_supported_schema(schema)
    return schema


def validate_completion_metadata(
    metadata: Any,
    *,
    task_id: str | None = None,
    schema_path: str | Path = DEFAULT_SCHEMA,
) -> list[str]:
    """Return deterministic validation errors; an empty list means valid."""
    schema = load_schema(schema_path)
    errors = _validate_node(metadata, schema, "$")
    if (
        isinstance(metadata, dict)
        and task_id is not None
        and metadata.get("kanban_card_id") != task_id
    ):
        errors.append(
            "$.kanban_card_id: must equal the completing task id "
            f"{task_id!r}"
        )
    return sorted(set(errors))


def _assert_supported_schema(schema: dict[str, Any], path: str = "$") -> None:
    unknown = sorted(set(schema) - SUPPORTED_KEYWORDS)
    if unknown:
        raise SchemaContractError(
            f"{path}: unsupported JSON Schema keywords: {', '.join(unknown)}"
        )

    declared_type = schema.get("type")
    declared_types = (
        declared_type if isinstance(declared_type, list) else [declared_type]
    )
    unsupported_types = sorted(
        value
        for value in declared_types
        if value is not None and value not in SUPPORTED_TYPES
    )
    if unsupported_types:
        raise SchemaContractError(
            f"{path}: unsupported JSON Schema types: {', '.join(unsupported_types)}"
        )

    declared_format = schema.get("format")
    if declared_format not in (None, "uri"):
        raise SchemaContractError(
            f"{path}: unsupported JSON Schema format: {declared_format!r}"
        )

    properties = schema.get("properties", {})
    if properties is not None:
        if not isinstance(properties, dict):
            raise SchemaContractError(f"{path}.properties must be an object")
        for name, child in properties.items():
            if not isinstance(child, dict):
                raise SchemaContractError(f"{path}.properties.{name} must be an object")
            _assert_supported_schema(child, f"{path}.properties.{name}")

    items = schema.get("items")
    if items is not None:
        if not isinstance(items, dict):
            raise SchemaContractError(f"{path}.items must be an object")
        _assert_supported_schema(items, f"{path}.items")

    for keyword in ("if", "then"):
        child = schema.get(keyword)
        if child is not None:
            if not isinstance(child, dict):
                raise SchemaContractError(f"{path}.{keyword} must be an object")
            _assert_supported_schema(child, f"{path}.{keyword}")

    all_of = schema.get("allOf", [])
    if all_of is not None:
        if not isinstance(all_of, list):
            raise SchemaContractError(f"{path}.allOf must be an array")
        for index, child in enumerate(all_of):
            if not isinstance(child, dict):
                raise SchemaContractError(f"{path}.allOf[{index}] must be an object")
            _assert_supported_schema(child, f"{path}.allOf[{index}]")


def _validate_node(value: Any, schema: dict[str, Any], path: str) -> list[str]:
    errors: list[str] = []

    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: must equal {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: must be one of {schema['enum']!r}")

    declared_type = schema.get("type")
    if declared_type is not None:
        declared_types = declared_type if isinstance(declared_type, list) else [declared_type]
        if not any(_matches_type(value, candidate) for candidate in declared_types):
            expected = "|".join(str(candidate) for candidate in declared_types)
            errors.append(f"{path}: expected {expected}, got {_json_type(value)}")
            return errors

    if isinstance(value, dict):
        required = schema.get("required", [])
        for name in required:
            if name not in value:
                errors.append(f"{path}.{name}: is required")
        for name, child_schema in schema.get("properties", {}).items():
            if name in value:
                errors.extend(_validate_node(value[name], child_schema, f"{path}.{name}"))
        if schema.get("additionalProperties") is False:
            allowed = set(schema.get("properties", {}))
            for name in sorted(set(value) - allowed):
                errors.append(f"{path}.{name}: additional property is not allowed")

    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}: must contain at least {schema['minItems']} item(s)")
        if schema.get("uniqueItems"):
            seen: set[str] = set()
            for index, item in enumerate(value):
                marker = json.dumps(item, sort_keys=True, ensure_ascii=False)
                if marker in seen:
                    errors.append(f"{path}[{index}]: duplicates an earlier item")
                seen.add(marker)
        child_schema = schema.get("items")
        if child_schema:
            for index, item in enumerate(value):
                errors.extend(_validate_node(item, child_schema, f"{path}[{index}]"))

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(
                f"{path}: length must be at least {schema['minLength']}"
            )
        pattern = schema.get("pattern")
        if pattern is not None and re.search(pattern, value) is None:
            errors.append(f"{path}: does not match required pattern {pattern!r}")
        if schema.get("format") == "uri" and not _is_uri(value):
            errors.append(f"{path}: must be an absolute URI")

    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and "minimum" in schema
        and value < schema["minimum"]
    ):
        errors.append(f"{path}: must be >= {schema['minimum']}")

    for condition in schema.get("allOf", []):
        predicate = condition.get("if")
        consequence = condition.get("then")
        if predicate is not None and consequence is not None:
            if not _validate_node(value, predicate, path):
                errors.extend(_validate_node(value, consequence, path))
        else:
            errors.extend(_validate_node(value, condition, path))

    return errors


def _matches_type(value: Any, declared_type: str) -> bool:
    if declared_type == "null":
        return value is None
    if declared_type == "boolean":
        return isinstance(value, bool)
    if declared_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if declared_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if declared_type == "string":
        return isinstance(value, str)
    if declared_type == "array":
        return isinstance(value, list)
    if declared_type == "object":
        return isinstance(value, dict)
    return False


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _is_uri(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return bool(parsed.scheme and (parsed.netloc or parsed.path))


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate a Hermes SDD completion metadata JSON object."
    )
    parser.add_argument(
        "--schema",
        default=str(DEFAULT_SCHEMA),
        help="Path to card-completion.schema.json",
    )
    parser.add_argument(
        "--metadata-file",
        required=True,
        help="JSON file to validate, or - to read stdin",
    )
    parser.add_argument(
        "--task-id",
        default=None,
        help="Require kanban_card_id to match this task id",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    try:
        if args.metadata_file == "-":
            metadata = json.load(sys.stdin)
        else:
            metadata = json.loads(
                Path(args.metadata_file).read_text(encoding="utf-8")
            )
        errors = validate_completion_metadata(
            metadata,
            task_id=args.task_id,
            schema_path=args.schema,
        )
    except (OSError, json.JSONDecodeError, SchemaContractError) as exc:
        print(f"completion metadata validation unavailable: {exc}", file=sys.stderr)
        return 2

    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print("completion metadata: valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
