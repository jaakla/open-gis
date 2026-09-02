"""The versioned check API that external harnesses consume.

OpenMapBench (and any other benchmark or CI system) grades produced projects
with this package's checks. It must be able to do so without vendoring the
check implementations and without depending on module layout: the surface
it may rely on is exactly

- ``CHECK_API_VERSION`` and ``api_info()`` for negotiation;
- ``list_checks()`` for the catalogue of check names and their parameters;
- ``run_check()`` for one check, returning a record that validates against
  ``openmapstack-check-result/v1``;
- ``openmapstack verify --json``, returning ``openmapstack-verify-result/v1``;
- the JSON schemas packaged under ``openmapstack/schemas/``.

Everything else in ``openmapstack.checks`` is implementation.

Versioning: ``CHECK_API_VERSION`` follows ``<name>/v<major>``. A new check,
a new optional parameter, or a new result field is additive and does not
change the major. Renaming or removing a check, changing a parameter's
meaning, or changing the four-state status vocabulary does. A consumer
pins the major and the minimum package version it was tested against, and
``negotiate()`` answers whether the installed package satisfies both.

Result semantics that a consumer may rely on:

- ``status`` is one of ``passed | failed | warning | not_testable`` and a
  check that could not establish its predicate is never ``passed``;
- ``code`` is a stable machine-readable identifier when the status is not
  ``passed``; consumers grade on ``status`` and, for mutation-style
  expectations, ``code`` -- never on ``detail`` text;
- ``dimension`` is the reporting bucket the check belongs to; buckets are
  reported separately and must not be collapsed into one score;
- ``oracle_free`` is ``false`` for the checks that need a known answer.
  Those transfer to arbitrary data only through attested expectations.

Setup failures (a check that raises) are reported as ``not_testable`` with
``code: check_error`` by ``run_check`` so a broken environment cannot
produce either a pass or a graded failure; benchmark harnesses keep them
out of scored denominators.
"""

from __future__ import annotations

import importlib
import inspect
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import __version__
from .checks import STATUSES, AssertionResult, not_testable
from .schema import validation_errors

CHECK_API_VERSION = "openmapstack-check-api/v1"
CHECK_RESULT_SCHEMA = "openmapstack-check-result/v1"
API_INFO_SCHEMA = "openmapstack-api-info/v1"
VERIFY_RESULT_SCHEMA = "openmapstack-verify-result/v1"
PROJECT_SCHEMA = "openmapstack-project/v1"

CHECK_MODULES = (
    "project",
    "provenance",
    "overrides",
    "validation",
    "geodata",
    "presentation",
    "qgis",
    "visual",
    "rerun",
    "metamorphic",
)

# Reporting buckets. Shared with evals/run.py, which asserts equality in
# tests so the two cannot drift apart.
DIMENSIONS = {
    "project": "reproducibility_compliance",
    "overrides": "override_handling",
    "provenance": "provenance",
    "geodata": "gis_correctness",
    "validation": "validation_integrity",
    "qgis": "presentation_contract",
    "presentation": "presentation_contract",
    "visual": "visual_judgement",
    "rerun": "rerun_success",
    "metamorphic": "metamorphic_evidence",
}

# The checks that need a known answer. They are reachable on user data
# only through validation.expectations[] attestations.
KNOWN_ANSWER_CHECKS = frozenset(
    {
        "geodata.row_count",
        "geodata.feature_present",
        "geodata.feature_absent",
        "geodata.feature_field_equals",
        "geodata.field_range",
    }
)

_SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"
_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)")


class CheckAPIError(ValueError):
    """The consumer asked for something the API does not provide."""


@dataclass(frozen=True)
class CheckParameter:
    name: str
    required: bool
    default: Any = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"name": self.name, "required": self.required}
        if not self.required:
            payload["default"] = self.default
        return payload


@dataclass(frozen=True)
class CheckDescriptor:
    name: str
    module: str
    dimension: str
    oracle_free: bool
    summary: str
    parameters: tuple[CheckParameter, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "module": self.module,
            "dimension": self.dimension,
            "oracle_free": self.oracle_free,
            "summary": self.summary,
            "parameters": [parameter.to_dict() for parameter in self.parameters],
        }


def _load_schema(name: str) -> dict[str, Any]:
    return json.loads((_SCHEMA_DIR / name).read_text(encoding="utf-8"))


def _describe(module_name: str, function_name: str, function: Any) -> CheckDescriptor:
    signature = inspect.signature(function)
    parameters: list[CheckParameter] = []
    for index, parameter in enumerate(signature.parameters.values()):
        if index == 0:  # workspace
            continue
        if parameter.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        required = parameter.default is inspect.Parameter.empty
        default = None if required else parameter.default
        if isinstance(default, tuple):
            default = list(default)
        parameters.append(CheckParameter(parameter.name, required, default))
    doc = inspect.getdoc(function) or ""
    summary = doc.strip().splitlines()[0].strip() if doc.strip() else ""
    name = f"{module_name}.{function_name}"
    return CheckDescriptor(
        name=name,
        module=module_name,
        dimension=DIMENSIONS.get(module_name, "other"),
        oracle_free=name not in KNOWN_ANSWER_CHECKS,
        summary=summary,
        parameters=tuple(parameters),
    )


def _is_check(function: Any) -> bool:
    if not inspect.isfunction(function) or function.__name__.startswith("_"):
        return False
    try:
        parameters = list(inspect.signature(function).parameters.values())
    except (TypeError, ValueError):
        return False
    return bool(parameters) and parameters[0].name == "workspace"


def list_checks() -> list[CheckDescriptor]:
    """Every public check, discovered from the shipped modules."""
    descriptors: list[CheckDescriptor] = []
    for module_name in CHECK_MODULES:
        module = importlib.import_module(f"openmapstack.checks.{module_name}")
        for function_name, function in sorted(vars(module).items()):
            if getattr(function, "__module__", None) != module.__name__:
                continue
            if _is_check(function):
                descriptors.append(_describe(module_name, function_name, function))
    return descriptors


def describe_check(name: str) -> CheckDescriptor:
    module_name, _, function_name = name.partition(".")
    function = _resolve(name)
    if getattr(function, "__module__", None) != f"openmapstack.checks.{module_name}":
        raise CheckAPIError(f"unknown check {name!r}; see list_checks()")
    return _describe(module_name, function_name, function)


def _resolve(name: str) -> Any:
    module_name, _, function_name = name.partition(".")
    if module_name not in CHECK_MODULES or not function_name:
        raise CheckAPIError(f"unknown check {name!r}; see list_checks()")
    module = importlib.import_module(f"openmapstack.checks.{module_name}")
    function = getattr(module, function_name, None)
    if function is None or not _is_check(function):
        raise CheckAPIError(f"unknown check {name!r}; see list_checks()")
    return function


def run_check(name: str, workspace: str | Path, args: dict[str, Any] | None = None) -> dict[str, Any]:
    """Execute one check and return an ``openmapstack-check-result/v1`` record.

    Unknown check names and malformed arguments raise ``CheckAPIError``
    (a consumer configuration error). A check that raises while running is
    reported as ``not_testable`` with ``code: check_error`` -- never as a
    pass, and never as a graded failure.
    """
    descriptor = describe_check(name)
    function = _resolve(name)
    args = dict(args or {})
    declared = {parameter.name for parameter in descriptor.parameters}
    unknown = sorted(set(args) - declared)
    missing = sorted(parameter.name for parameter in descriptor.parameters if parameter.required and parameter.name not in args)
    if unknown or missing:
        raise CheckAPIError(f"{name}: unknown args {unknown}, missing required args {missing}")
    try:
        result = function(Path(workspace), **args)
    except Exception as exc:  # noqa: BLE001 - a check must never take a harness down
        result = not_testable(f"{type(exc).__name__}: {exc}", code="check_error")
    if not isinstance(result, AssertionResult) or result.status not in STATUSES:
        result = not_testable("check returned a malformed result", code="check_error")
    data = {key: value for key, value in result.data.items() if key != "code"}
    record: dict[str, Any] = {
        "schema": CHECK_RESULT_SCHEMA,
        "api_version": CHECK_API_VERSION,
        "package_version": __version__,
        "check": name,
        "dimension": descriptor.dimension,
        "oracle_free": descriptor.oracle_free,
        "args": args,
        "status": result.status,
        "code": result.data.get("code"),
        "detail": result.detail,
        "data": json.loads(json.dumps(data, default=str)),
    }
    errors = validation_errors(record, _load_schema("check-result-v1.schema.json"))
    if errors:  # pragma: no cover - the record is built here; a failure is a bug
        raise CheckAPIError(f"internal: check result does not validate: {errors}")
    return record


def api_info() -> dict[str, Any]:
    """What this installation offers, for a consumer to negotiate against."""
    checks = list_checks()
    return {
        "schema": API_INFO_SCHEMA,
        "package": "openmapstack",
        "package_version": __version__,
        "check_api_version": CHECK_API_VERSION,
        "project_schema": PROJECT_SCHEMA,
        "result_schemas": {
            "check": CHECK_RESULT_SCHEMA,
            "verify": VERIFY_RESULT_SCHEMA,
        },
        "statuses": list(STATUSES),
        "dimensions": sorted(set(DIMENSIONS.values())),
        "checks": len(checks),
        "oracle_free_checks": sum(descriptor.oracle_free for descriptor in checks),
        "known_answer_checks": sorted(KNOWN_ANSWER_CHECKS),
    }


def _parse_version(text: str) -> tuple[int, int, int]:
    match = _VERSION.match(text or "")
    if match is None:
        raise CheckAPIError(f"not a semantic version: {text!r}")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def negotiate(
    *,
    required_api: str = CHECK_API_VERSION,
    min_package_version: str | None = None,
    required_checks: list[str] | None = None,
) -> dict[str, Any]:
    """Answer whether this installation satisfies a consumer's requirements.

    A consumer states the API major it was built for, the oldest package
    version it was tested against, and the checks it needs. The answer
    lists every unmet requirement so a harness can report *why* it is
    refusing to grade rather than grading with a checker it does not
    understand.
    """
    problems: list[str] = []
    if required_api != CHECK_API_VERSION:
        problems.append(f"check API {required_api!r} is not provided; this package offers {CHECK_API_VERSION!r}")
    if min_package_version is not None and _parse_version(__version__) < _parse_version(min_package_version):
        problems.append(f"package version {__version__} is older than the required {min_package_version}")
    available = {descriptor.name for descriptor in list_checks()}
    missing = sorted(name for name in (required_checks or []) if name not in available)
    if missing:
        problems.append(f"checks not provided: {missing}")
    return {
        "schema": "openmapstack-api-negotiation/v1",
        "compatible": not problems,
        "package_version": __version__,
        "check_api_version": CHECK_API_VERSION,
        "problems": problems,
    }


def validate_verify_result(payload: dict[str, Any]) -> list[str]:
    """Schema-validate an ``openmapstack verify --json`` document."""
    return validation_errors(payload, _load_schema("verify-result-v1.schema.json"))
