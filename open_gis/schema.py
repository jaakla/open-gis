"""JSON Schema loading and validation helpers."""

from __future__ import annotations

import json
from datetime import date, datetime
from importlib.resources import files
from typing import Any

from jsonschema import Draft202012Validator


def _json_value(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    return value


def load_packaged_schema(name: str) -> dict[str, Any]:
    resource = files("open_gis.schemas").joinpath(name)
    return json.loads(resource.read_text(encoding="utf-8"))


def validation_errors(instance: Any, schema: dict[str, Any]) -> list[str]:
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(_json_value(instance)), key=lambda item: list(item.path))
    formatted: list[str] = []
    for error in errors:
        location = ".".join(str(part) for part in error.absolute_path) or "$"
        formatted.append(f"{location}: {error.message}")
    return formatted


def project_schema_errors(project: Any) -> list[str]:
    return validation_errors(project, load_packaged_schema("project-v1.schema.json"))
