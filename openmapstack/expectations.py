"""Attested, project-specific expectations for user-data verification.

An expectation supplies the golden answer that a generic checker cannot know.
The pipeline being checked must not certify that answer itself, so execution is
licensed only by an attestation bound to both the check/arguments and the
project's current input hash.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .checks import AssertionResult, failed, not_testable, passed, warning
from .checks import geodata as geodata_checks
from .integrity import normalize_digest, sha256_file
from .project import get_in, project_path

ExpectationCheck = Callable[..., AssertionResult]

EXPECTATION_CHECKS: dict[str, ExpectationCheck] = {
    "geodata.row_count": geodata_checks.row_count,
    "geodata.feature_present": geodata_checks.feature_present,
    "geodata.feature_absent": geodata_checks.feature_absent,
    "geodata.feature_field_equals": geodata_checks.feature_field_equals,
    "geodata.field_range": geodata_checks.field_range,
}

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_RESERVED_ARGS = {"workspace", "project_dir"}


def expectation_digest(check: str, args: dict[str, Any]) -> str:
    """Bind an attestation to the exact check and arguments it reviewed."""
    canonical = json.dumps(
        {"check": check, "args": args},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def _evidence(
    evidence_class: str,
    *,
    attestation: dict[str, Any] | None,
    expected_expectation_hash: str,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "class": evidence_class,
        "expected_expectation_sha256": expected_expectation_hash,
    }
    if isinstance(attestation, dict):
        for key in ("verified_by", "verified_against", "verified_at", "evidence_sha256"):
            value = attestation.get(key)
            if value not in (None, ""):
                result[key] = value
    return result


def _validate_args(
    root: Path,
    check: str,
    args: object,
) -> tuple[dict[str, Any] | None, AssertionResult | None]:
    if not isinstance(args, dict):
        return None, failed("expectation args must be a mapping", code="expectation_args_invalid")
    if _RESERVED_ARGS & set(args):
        return None, failed(
            f"expectation args contain reserved names: {sorted(_RESERVED_ARGS & set(args))}",
            code="expectation_args_invalid",
        )
    rules: dict[str, tuple[set[str], set[str]]] = {
        "geodata.row_count": (
            {"path", "equals", "at_least", "at_most"},
            {"path"},
        ),
        "geodata.feature_present": ({"path", "id_field", "id"}, {"path", "id_field", "id"}),
        "geodata.feature_absent": ({"path", "id_field", "id"}, {"path", "id_field", "id"}),
        "geodata.feature_field_equals": (
            {"path", "id_field", "id", "field", "equals"},
            {"path", "id_field", "id", "field", "equals"},
        ),
        "geodata.field_range": ({"path", "field", "min", "max"}, {"path", "field"}),
    }
    allowed, required = rules[check]
    extra = set(args) - allowed
    missing = required - set(args)
    if extra or missing:
        return None, failed(
            f"expectation args have extra={sorted(extra)} missing={sorted(missing)}",
            code="expectation_args_invalid",
        )
    if check == "geodata.row_count":
        constraints = [args.get(key) for key in ("equals", "at_least", "at_most") if key in args]
        if not constraints or any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in constraints):
            return None, failed(
                "row_count requires at least one non-negative integer constraint",
                code="expectation_args_invalid",
            )
    if check == "geodata.field_range":
        bounds = [args.get(key) for key in ("min", "max") if key in args]
        if not bounds or any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in bounds):
            return None, failed(
                "field_range requires at least one numeric min/max bound",
                code="expectation_args_invalid",
            )
    path = args.get("path")
    if project_path(root, path) is None:
        return None, failed(
            "expectation path must be safe and project-relative",
            code="expectation_path_unsafe",
        )
    for key in ("field", "id_field"):
        value = args.get(key)
        if value is not None and (not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None):
            return None, failed(
                f"expectation {key} must be a simple identifier",
                code="expectation_args_invalid",
            )
    return dict(args), None


def evaluate_expectation(
    root: Path,
    manifest: dict[str, Any],
    expectation: object,
) -> tuple[AssertionResult, dict[str, Any]]:
    """Validate an attestation, then execute its allowlisted checker.

    Returns the assertion result plus structured evidence for the verification
    report. Unverified, incomplete, or stale attestations are warnings and do
    not execute model- or user-supplied expected values.
    """
    if not isinstance(expectation, dict):
        return failed("expectation must be a mapping", code="expectation_invalid"), {
            "class": "invalid"
        }

    check = expectation.get("check")
    args = expectation.get("args")
    expectation_id = expectation.get("id")
    if not isinstance(expectation_id, str) or not expectation_id.strip():
        return failed("expectation id is required", code="expectation_invalid"), {
            "class": "invalid"
        }
    if not isinstance(check, str) or check not in EXPECTATION_CHECKS:
        return failed(
            f"expectation {expectation_id!r} uses an unsupported check {check!r}",
            code="expectation_check_unsupported",
        ), {"class": "invalid"}
    if not isinstance(args, dict):
        return failed(
            f"expectation {expectation_id!r} args must be a mapping",
            code="expectation_args_invalid",
        ), {"class": "invalid"}

    expected_hash = expectation_digest(check, args)
    attestation = expectation.get("attestation")
    if not isinstance(attestation, dict) or attestation.get("status") != "verified":
        evidence = _evidence(
            "unverified",
            attestation=attestation if isinstance(attestation, dict) else None,
            expected_expectation_hash=expected_hash,
        )
        return warning(
            f"expectation {expectation_id!r} is unverified; independent review must bind "
            f"expectation_sha256 to {expected_hash}",
            code="expectation_unverified",
        ), evidence

    required_attestation = (
        "verified_by",
        "verified_against",
        "verified_at",
        "evidence_sha256",
        "expectation_sha256",
        "inputs_hash",
    )
    missing = [key for key in required_attestation if not attestation.get(key)]
    if missing:
        evidence = _evidence(
            "unverified",
            attestation=attestation,
            expected_expectation_hash=expected_hash,
        )
        return warning(
            f"expectation {expectation_id!r} has incomplete verification evidence: {missing}",
            code="expectation_attestation_incomplete",
        ), evidence

    if normalize_digest(attestation.get("expectation_sha256")) != expected_hash:
        evidence = _evidence(
            "stale_attestation",
            attestation=attestation,
            expected_expectation_hash=expected_hash,
        )
        return warning(
            f"expectation {expectation_id!r} changed after review; expected digest is {expected_hash}",
            code="expectation_changed",
        ), evidence

    current_inputs_hash = normalize_digest(get_in(manifest, "runs", "latest", "inputs_hash"))
    attested_inputs_hash = normalize_digest(attestation.get("inputs_hash"))
    if current_inputs_hash is None or attested_inputs_hash != current_inputs_hash:
        evidence = _evidence(
            "stale_attestation",
            attestation=attestation,
            expected_expectation_hash=expected_hash,
        )
        evidence["current_inputs_hash"] = current_inputs_hash
        return warning(
            f"expectation {expectation_id!r} was not verified against the current project inputs",
            code="expectation_inputs_changed",
        ), evidence

    normalized_evidence_hash = normalize_digest(attestation.get("evidence_sha256"))
    if normalized_evidence_hash is None:
        return warning(
            f"expectation {expectation_id!r} has a malformed evidence digest",
            code="expectation_attestation_incomplete",
        ), _evidence(
            "unverified",
            attestation=attestation,
            expected_expectation_hash=expected_hash,
        )

    evidence_path = attestation.get("evidence_path")
    if evidence_path is not None:
        resolved_evidence = project_path(root, evidence_path)
        if resolved_evidence is None:
            return failed(
                f"expectation {expectation_id!r} evidence_path is unsafe",
                code="expectation_evidence_path_unsafe",
            ), {"class": "invalid", "expected_expectation_sha256": expected_hash}
        if not resolved_evidence.is_file():
            return warning(
                f"expectation {expectation_id!r} evidence file is unavailable",
                code="expectation_evidence_unavailable",
            ), _evidence(
                "stale_attestation",
                attestation=attestation,
                expected_expectation_hash=expected_hash,
            )
        if sha256_file(resolved_evidence) != normalized_evidence_hash:
            return warning(
                f"expectation {expectation_id!r} evidence file changed after review",
                code="expectation_evidence_changed",
            ), _evidence(
                "stale_attestation",
                attestation=attestation,
                expected_expectation_hash=expected_hash,
            )

    checked_args, arg_error = _validate_args(root, check, args)
    if arg_error is not None:
        return arg_error, {
            "class": "invalid",
            "expected_expectation_sha256": expected_hash,
        }
    if checked_args is None:  # defensive: _validate_args returns one or the other
        return failed("expectation args are invalid", code="expectation_args_invalid"), {
            "class": "invalid",
            "expected_expectation_sha256": expected_hash,
        }

    try:
        result = EXPECTATION_CHECKS[check](root, **checked_args)
    except Exception as exc:  # noqa: BLE001 - one expectation must not abort the report
        result = not_testable(f"{type(exc).__name__}: {exc}", code="check_error")
    evidence = _evidence(
        "attested",
        attestation=attestation,
        expected_expectation_hash=expected_hash,
    )
    if result.status == "passed":
        result = passed(f"attested expectation {expectation_id!r} satisfied: {result.detail}")
    return result, evidence
