"""Reusable, semantic assertion functions for OpenMapStack eval cases.

Every assertion function has the signature:

    def fn(workspace: Path, **args) -> AssertionResult

and inspects real files in `workspace` (a copy of a generated/reference
project) rather than assistant prose. Assertions never raise for expected
"could not check" conditions — they return `not_testable` instead, matching
the four-state vocabulary (`passed | failed | warning | not_testable`)
required of `validation/latest-report.json` itself in
`references/project-spec.md` section 6.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

STATUSES = ("passed", "failed", "warning", "not_testable")


@dataclass
class AssertionResult:
    status: str  # passed | failed | warning | not_testable
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"invalid assertion status: {self.status!r}")

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "detail": self.detail, **self.data}


def passed(detail: str = "", **data: Any) -> AssertionResult:
    return AssertionResult("passed", detail, data)


def failed(detail: str = "", *, code: str | None = None, **data: Any) -> AssertionResult:
    """``code`` is a stable, machine-readable failure identifier (e.g.
    ``feature_present``). Mutation cases can require the *specific* failure
    they inject, not merely status ``failed``, via ``expect_code`` in
    ``expected.yaml``."""
    if code is not None:
        data["code"] = code
    return AssertionResult("failed", detail, data)


def warning(detail: str = "", *, code: str | None = None, **data: Any) -> AssertionResult:
    if code is not None:
        data["code"] = code
    return AssertionResult("warning", detail, data)


def not_testable(detail: str = "", *, code: str | None = None, **data: Any) -> AssertionResult:
    if code is not None:
        data["code"] = code
    return AssertionResult("not_testable", detail, data)


def load_project_yaml(workspace: Path, project_dir: str = ".") -> dict[str, Any] | None:
    path = workspace / project_dir / "project.yaml"
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            value = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError):
        return None
    return value if isinstance(value, dict) else None


def load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def project_root(workspace: Path, project_dir: str = ".") -> Path:
    return workspace / project_dir


def get_in(data: dict, dotted: str, default: Any = None) -> Any:
    """Fetch a nested dict value using a dotted path, e.g. 'project.status'."""
    cur: Any = data
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur
