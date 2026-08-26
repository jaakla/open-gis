"""Project loading and path helpers for ``open-gis-project/v1``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml


class ProjectError(Exception):
    """A project manifest cannot be loaded safely."""


def resolve_project_file(value: str | Path) -> Path:
    """Resolve a manifest argument, accepting either a file or project directory."""
    path = Path(value).expanduser()
    if path.is_dir():
        path = path / "project.yaml"
    return path.resolve()


def load_project(value: str | Path) -> tuple[Path, dict[str, Any]]:
    path = resolve_project_file(value)
    if not path.is_file():
        raise ProjectError(f"project manifest does not exist: {path}")
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ProjectError(f"cannot parse {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ProjectError(f"project manifest must contain a YAML mapping: {path}")
    return path, loaded


def load_json(path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProjectError(f"cannot parse JSON file {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ProjectError(f"JSON document must contain an object: {path}")
    return loaded


def project_path(root: Path, value: object) -> Path | None:
    """Resolve a project-relative path without allowing escape from the project."""
    if not isinstance(value, str) or not value.strip():
        return None
    relative = Path(value)
    if relative.is_absolute():
        return None
    target = (root / relative).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError:
        return None
    return target


def get_in(value: object, *keys: str, default: Any = None) -> Any:
    current = value
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current


def step_outputs(step: object) -> list[str]:
    if not isinstance(step, dict):
        return []
    raw = step.get("output")
    if isinstance(raw, str):
        return [part.strip() for part in raw.split(",") if part.strip()]
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    return []
