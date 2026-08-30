from __future__ import annotations

import json
import unittest

from .helpers import make_workspace, minimal_project, write_json, write_project

from openmapstack.checks import validation as validation_assertions  # noqa: E402
from openmapstack.integrity import canonical_file_set_hash, file_inventory  # noqa: E402


def _write_hashed_run(workspace):
    (workspace / "pipeline.py").write_text("# pipeline\n", encoding="utf-8")
    output = workspace / "data/derived/final.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")
    inputs = ["pipeline.py"]
    outputs = ["data/derived/final.json"]
    inputs_hash = canonical_file_set_hash(workspace, inputs)
    outputs_hash = canonical_file_set_hash(workspace, outputs)
    project = minimal_project()
    project["runs"] = {"latest": {
        "id": "run-1",
        "started_at": "2026-01-01T00:00:00Z",
        "completed_at": "2026-01-01T00:00:01Z",
        "status": "passed",
        "inputs_hash": inputs_hash,
        "outputs_hash": outputs_hash,
        "validation_report": {"path": "validation/latest-report.json"},
    }}
    write_project(workspace, project)
    report = {"run_id": "run-1", "inputs_hash": inputs_hash, "outputs_hash": outputs_hash}
    run = {
        "run_id": "run-1",
        "inputs_hash": inputs_hash,
        "outputs_hash": outputs_hash,
        "inputs": file_inventory(workspace, inputs),
        "outputs": file_inventory(workspace, outputs),
    }
    write_json(workspace, "validation/latest-report.json", report)
    write_json(workspace, "runs/run-1.json", run)
    return report, run


class RequiredAllPresentTests(unittest.TestCase):
    def test_all_present_once_passes(self) -> None:
        workspace = make_workspace()
        write_project(workspace, minimal_project())
        write_json(workspace, "validation/latest-report.json", {
            "status": "passed", "checks": [{"id": "geometry_valid", "status": "passed"}]
        })
        result = validation_assertions.required_all_present(workspace)
        self.assertEqual(result.status, "passed")

    def test_missing_declared_check_fails(self) -> None:
        workspace = make_workspace()
        write_project(workspace, minimal_project())
        write_json(workspace, "validation/latest-report.json", {"status": "passed", "checks": []})
        result = validation_assertions.required_all_present(workspace)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("code"), "declared_check_missing")

    def test_duplicate_check_fails(self) -> None:
        workspace = make_workspace()
        write_project(workspace, minimal_project())
        write_json(workspace, "validation/latest-report.json", {
            "status": "passed",
            "checks": [
                {"id": "geometry_valid", "status": "passed"},
                {"id": "geometry_valid", "status": "passed"},
            ],
        })
        result = validation_assertions.required_all_present(workspace)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("code"), "duplicate_check")

    def test_missing_report_is_not_testable(self) -> None:
        workspace = make_workspace()
        write_project(workspace, minimal_project())
        result = validation_assertions.required_all_present(workspace)
        self.assertEqual(result.status, "not_testable")
        self.assertEqual(result.data.get("code"), "report_missing")


class NoImplicitPassTests(unittest.TestCase):
    def test_explicit_statuses_pass(self) -> None:
        workspace = make_workspace()
        write_json(workspace, "validation/latest-report.json", {
            "checks": [{"id": "a", "status": "passed"}, {"id": "b", "status": "warning"}]
        })
        result = validation_assertions.no_implicit_pass(workspace)
        self.assertEqual(result.status, "passed")

    def test_missing_status_fails(self) -> None:
        workspace = make_workspace()
        write_json(workspace, "validation/latest-report.json", {
            "checks": [{"id": "a"}]
        })
        result = validation_assertions.no_implicit_pass(workspace)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("code"), "implicit_status")


class WarningOrFailedPropagatesTests(unittest.TestCase):
    def test_all_passed_and_status_passed_ok(self) -> None:
        workspace = make_workspace()
        write_json(workspace, "validation/latest-report.json", {
            "status": "passed", "checks": [{"id": "a", "status": "passed"}]
        })
        result = validation_assertions.warning_or_failed_propagates_to_status(workspace)
        self.assertEqual(result.status, "passed")

    def test_warning_check_but_passed_status_is_laundering(self) -> None:
        workspace = make_workspace()
        write_json(workspace, "validation/latest-report.json", {
            "status": "passed", "checks": [{"id": "a", "status": "warning"}]
        })
        result = validation_assertions.warning_or_failed_propagates_to_status(workspace)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("code"), "status_laundering")

    def test_all_passed_but_status_not_passed_fails(self) -> None:
        workspace = make_workspace()
        write_json(workspace, "validation/latest-report.json", {
            "status": "warning", "checks": [{"id": "a", "status": "passed"}]
        })
        result = validation_assertions.warning_or_failed_propagates_to_status(workspace)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("code"), "status_understated")


class RunRecordMatchesTests(unittest.TestCase):
    def test_matching_run_record_passes(self) -> None:
        workspace = make_workspace()
        _write_hashed_run(workspace)
        result = validation_assertions.run_record_matches(workspace)
        self.assertEqual(result.status, "passed")

    def test_missing_run_record_fails(self) -> None:
        workspace = make_workspace()
        write_project(workspace, minimal_project())
        write_json(workspace, "validation/latest-report.json", {"run_id": "run-1"})
        result = validation_assertions.run_record_matches(workspace)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("code"), "run_record_missing")

    def test_hash_mismatch_fails(self) -> None:
        workspace = make_workspace()
        report, _ = _write_hashed_run(workspace)
        report["inputs_hash"] = "sha256:" + "0" * 64
        write_json(workspace, "validation/latest-report.json", report)
        result = validation_assertions.run_record_matches(workspace)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("code"), "hash_mismatch")

    def test_matching_but_invented_hashes_fail(self) -> None:
        workspace = make_workspace()
        report, run = _write_hashed_run(workspace)
        invented = "sha256:" + "f" * 64
        report["outputs_hash"] = invented
        run["outputs_hash"] = invented
        project = minimal_project()
        project["runs"] = {"latest": {
            "id": "run-1", "inputs_hash": report["inputs_hash"], "outputs_hash": invented
        }}
        write_project(workspace, project)
        write_json(workspace, "validation/latest-report.json", report)
        write_json(workspace, "runs/run-1.json", run)
        result = validation_assertions.run_record_matches(workspace)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("code"), "hash_mismatch")

    def test_missing_inventory_fails(self) -> None:
        workspace = make_workspace()
        _, run = _write_hashed_run(workspace)
        del run["inputs"]
        write_json(workspace, "runs/run-1.json", run)
        result = validation_assertions.run_record_matches(workspace)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("code"), "hash_inventory_missing")


class NoProseOnlyValidationTests(unittest.TestCase):
    def test_check_with_evidence_passes(self) -> None:
        workspace = make_workspace()
        write_json(workspace, "validation/latest-report.json", {
            "checks": [{"id": "geometry_valid", "status": "passed", "features_checked": 10}]
        })
        result = validation_assertions.no_prose_only_validation(workspace, check_id="geometry_valid")
        self.assertEqual(result.status, "passed")

    def test_prose_only_check_fails(self) -> None:
        workspace = make_workspace()
        write_json(workspace, "validation/latest-report.json", {
            "checks": [{"id": "geometry_valid", "status": "passed", "reason": "looks fine"}]
        })
        result = validation_assertions.no_prose_only_validation(workspace, check_id="geometry_valid")
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("code"), "prose_only")

    def test_missing_check_fails(self) -> None:
        workspace = make_workspace()
        write_json(workspace, "validation/latest-report.json", {"checks": []})
        result = validation_assertions.no_prose_only_validation(workspace, check_id="geometry_valid")
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("code"), "check_missing")


class ReportEvidenceRecomputesTests(unittest.TestCase):
    def _workspace(self, declared_rows=2):
        workspace = make_workspace()
        target = workspace / "data.geojson"
        target.write_text(json.dumps({
            "type": "FeatureCollection",
            "features": [
                {"type": "Feature", "properties": {"id": "a"}, "geometry": {"type": "Point", "coordinates": [0, 0]}},
                {"type": "Feature", "properties": {"id": "b"}, "geometry": {"type": "Point", "coordinates": [1, 1]}},
            ],
        }), encoding="utf-8")
        write_json(workspace, "validation/latest-report.json", {
            "checks": [{"id": "row_count", "status": "passed", "rows": declared_rows}]
        })
        return workspace

    def test_real_evidence_passes(self) -> None:
        workspace = self._workspace()
        result = validation_assertions.report_evidence_recomputes(workspace, evidence=[{
            "check_id": "row_count", "evidence_field": "rows", "metric": "row_count", "path": "data.geojson"
        }])
        self.assertEqual(result.status, "passed", result.detail)

    def test_invented_evidence_fails(self) -> None:
        workspace = self._workspace(declared_rows=99)
        result = validation_assertions.report_evidence_recomputes(workspace, evidence=[{
            "check_id": "row_count", "evidence_field": "rows", "metric": "row_count", "path": "data.geojson"
        }])
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("code"), "evidence_mismatch")

    def test_evidence_not_testable_without_duckdb(self) -> None:
        from unittest.mock import patch

        workspace = self._workspace()
        with patch("openmapstack.checks.geodata._connect", return_value=None):
            result = validation_assertions.report_evidence_recomputes(workspace, evidence=[{
                "check_id": "row_count", "evidence_field": "rows", "metric": "row_count", "path": "data.geojson"
            }])
        self.assertEqual(result.status, "not_testable")


if __name__ == "__main__":
    unittest.main()
