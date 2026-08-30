"""Attestation and safety tests for project-specific expectations."""

from __future__ import annotations

from copy import deepcopy
from unittest.mock import Mock, patch

from openmapstack.checks import failed, passed
from openmapstack.expectations import (
    EXPECTATION_CHECKS,
    evaluate_expectation,
    expectation_digest,
)
from openmapstack.integrity import sha256_file
from openmapstack.schema import project_schema_errors
from openmapstack.verify import verify_project
from tests.evals.helpers import make_workspace, write_project
from tests.test_cli import valid_manifest

INPUTS_HASH = "sha256:" + "1" * 64
EVIDENCE_HASH = "sha256:" + "2" * 64


def _expectation(
    *,
    check: str = "geodata.row_count",
    args: dict | None = None,
    status: str = "verified",
) -> dict:
    args = deepcopy(args or {"path": "data/derived/result.parquet", "equals": 3})
    attestation: dict = {"status": status}
    if status == "verified":
        attestation.update(
            {
                "verified_by": "Independent reviewer",
                "verified_against": "authority-record-42",
                "verified_at": "2026-08-30T00:00:00Z",
                "evidence_sha256": EVIDENCE_HASH,
                "expectation_sha256": expectation_digest(check, args),
                "inputs_hash": INPUTS_HASH,
            }
        )
    return {"id": "expected-result", "check": check, "args": args, "attestation": attestation}


def _manifest(expectation: dict) -> dict:
    manifest = valid_manifest()
    manifest["runs"]["latest"]["inputs_hash"] = INPUTS_HASH
    manifest["validation"]["expectations"] = [expectation]
    return manifest


def test_schema_accepts_complete_verified_expectation() -> None:
    assert project_schema_errors(_manifest(_expectation())) == []


def test_schema_rejects_unsupported_check_and_unsafe_identifier() -> None:
    unsupported = _manifest(_expectation(check="project.parses", args={"path": "project.yaml"}))
    assert any("project.parses" in error for error in project_schema_errors(unsupported))

    unsafe_field = _manifest(
        _expectation(
            check="geodata.feature_present",
            args={"path": "data/result.parquet", "id_field": 'id" OR TRUE --', "id": "x"},
        )
    )
    assert project_schema_errors(unsafe_field)

    escaped_path = _manifest(_expectation(args={"path": "../outside.parquet", "equals": 3}))
    assert project_schema_errors(escaped_path)


def test_unverified_expectation_warns_without_executing() -> None:
    root = make_workspace()
    expectation = _expectation(status="unverified")
    checker = Mock(return_value=passed())
    with patch.dict(EXPECTATION_CHECKS, {"geodata.row_count": checker}):
        result, evidence = evaluate_expectation(root, _manifest(expectation), expectation)
    assert result.status == "warning"
    assert result.data["code"] == "expectation_unverified"
    assert evidence["class"] == "unverified"
    assert evidence["expected_expectation_sha256"] == expectation_digest(
        expectation["check"], expectation["args"]
    )
    checker.assert_not_called()


def test_changed_expectation_invalidates_attestation_without_executing() -> None:
    root = make_workspace()
    expectation = _expectation()
    expectation["args"]["equals"] = 4
    checker = Mock(return_value=passed())
    with patch.dict(EXPECTATION_CHECKS, {"geodata.row_count": checker}):
        result, evidence = evaluate_expectation(root, _manifest(expectation), expectation)
    assert result.status == "warning"
    assert result.data["code"] == "expectation_changed"
    assert evidence["class"] == "stale_attestation"
    checker.assert_not_called()


def test_changed_inputs_invalidate_attestation_without_executing() -> None:
    root = make_workspace()
    expectation = _expectation()
    manifest = _manifest(expectation)
    manifest["runs"]["latest"]["inputs_hash"] = "sha256:" + "3" * 64
    checker = Mock(return_value=passed())
    with patch.dict(EXPECTATION_CHECKS, {"geodata.row_count": checker}):
        result, evidence = evaluate_expectation(root, manifest, expectation)
    assert result.status == "warning"
    assert result.data["code"] == "expectation_inputs_changed"
    assert evidence["class"] == "stale_attestation"
    checker.assert_not_called()


def test_unsafe_project_path_fails_without_executing() -> None:
    root = make_workspace()
    expectation = _expectation(args={"path": "../outside.parquet", "equals": 3})
    checker = Mock(return_value=passed())
    with patch.dict(EXPECTATION_CHECKS, {"geodata.row_count": checker}):
        result, evidence = evaluate_expectation(root, _manifest(expectation), expectation)
    assert result.status == "failed"
    assert result.data["code"] == "expectation_path_unsafe"
    assert evidence["class"] == "invalid"
    checker.assert_not_called()


def test_verified_expectation_executes_allowlisted_checker() -> None:
    root = make_workspace()
    expectation = _expectation()
    checker = Mock(return_value=passed("row count is 3"))
    with patch.dict(EXPECTATION_CHECKS, {"geodata.row_count": checker}):
        result, evidence = evaluate_expectation(root, _manifest(expectation), expectation)
    assert result.status == "passed"
    assert "attested expectation" in result.detail
    assert evidence["class"] == "attested"
    checker.assert_called_once_with(root, path="data/derived/result.parquet", equals=3)


def test_checker_failure_remains_a_failure() -> None:
    root = make_workspace()
    expectation = _expectation()
    with patch.dict(
        EXPECTATION_CHECKS,
        {"geodata.row_count": Mock(return_value=failed("wrong count", code="row_count_equals"))},
    ):
        result, evidence = evaluate_expectation(root, _manifest(expectation), expectation)
    assert result.status == "failed"
    assert result.data["code"] == "row_count_equals"
    assert evidence["class"] == "attested"


def test_checker_exception_is_not_testable_not_a_crash() -> None:
    root = make_workspace()
    expectation = _expectation()
    with patch.dict(
        EXPECTATION_CHECKS,
        {"geodata.row_count": Mock(side_effect=RuntimeError("boom"))},
    ):
        result, _ = evaluate_expectation(root, _manifest(expectation), expectation)
    assert result.status == "not_testable"
    assert result.data["code"] == "check_error"


def test_local_evidence_file_must_match_attested_digest() -> None:
    root = make_workspace()
    evidence_path = root / "validation" / "authority.json"
    evidence_path.parent.mkdir(parents=True)
    evidence_path.write_text('{"count": 3}', encoding="utf-8")
    expectation = _expectation()
    expectation["attestation"]["evidence_path"] = "validation/authority.json"
    expectation["attestation"]["evidence_sha256"] = "sha256:" + "f" * 64
    result, evidence = evaluate_expectation(root, _manifest(expectation), expectation)
    assert result.status == "warning"
    assert result.data["code"] == "expectation_evidence_changed"
    assert evidence["class"] == "stale_attestation"

    expectation["attestation"]["evidence_sha256"] = sha256_file(evidence_path)
    with patch.dict(
        EXPECTATION_CHECKS,
        {"geodata.row_count": Mock(return_value=passed("matched"))},
    ):
        result, _ = evaluate_expectation(root, _manifest(expectation), expectation)
    assert result.status == "passed"


def test_report_serializes_attestation_class() -> None:
    from openmapstack.verify import CheckRun

    run = CheckRun(
        "expectation.expected-result",
        passed("matched"),
        {"check": "geodata.row_count"},
        {"class": "attested", "verified_by": "Independent reviewer"},
    )
    assert run.to_dict()["evidence"] == {
        "class": "attested",
        "verified_by": "Independent reviewer",
    }


def test_verify_project_plans_expectation_and_serializes_evidence() -> None:
    root = make_workspace()
    expectation = _expectation()
    write_project(root, _manifest(expectation))
    checker = Mock(return_value=passed("matched"))
    with patch.dict(EXPECTATION_CHECKS, {"geodata.row_count": checker}):
        result = verify_project(root)
    run = next(item for item in result.checks if item.name == "expectation.expected-result")
    assert run.result.status == "passed"
    assert run.to_dict()["evidence"]["class"] == "attested"


def test_duplicate_expectation_id_fails_without_executing_twice() -> None:
    root = make_workspace()
    expectation = _expectation()
    manifest = _manifest(expectation)
    manifest["validation"]["expectations"].append(deepcopy(expectation))
    write_project(root, manifest)
    checker = Mock(return_value=passed("matched"))
    with patch.dict(EXPECTATION_CHECKS, {"geodata.row_count": checker}):
        result = verify_project(root)
    runs = [item for item in result.checks if item.name == "expectation.expected-result"]
    assert [item.result.status for item in runs] == ["passed", "failed"]
    assert runs[1].result.data["code"] == "expectation_id_duplicate"
    checker.assert_called_once()
