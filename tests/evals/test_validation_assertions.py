from __future__ import annotations

import unittest

from .helpers import make_workspace, minimal_project, write_json, write_project

from assertions import validation as validation_assertions  # noqa: E402


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
        write_json(workspace, "validation/latest-report.json", {
            "run_id": "run-1", "inputs_hash": "sha256:a", "outputs_hash": "sha256:b"
        })
        write_json(workspace, "runs/run-1.json", {
            "run_id": "run-1", "inputs_hash": "sha256:a", "outputs_hash": "sha256:b"
        })
        result = validation_assertions.run_record_matches(workspace)
        self.assertEqual(result.status, "passed")

    def test_missing_run_record_fails(self) -> None:
        workspace = make_workspace()
        write_json(workspace, "validation/latest-report.json", {"run_id": "run-1"})
        result = validation_assertions.run_record_matches(workspace)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("code"), "run_record_missing")

    def test_hash_mismatch_fails(self) -> None:
        workspace = make_workspace()
        write_json(workspace, "validation/latest-report.json", {
            "run_id": "run-1", "inputs_hash": "sha256:a"
        })
        write_json(workspace, "runs/run-1.json", {
            "run_id": "run-1", "inputs_hash": "sha256:different"
        })
        result = validation_assertions.run_record_matches(workspace)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("code"), "hash_mismatch")


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


if __name__ == "__main__":
    unittest.main()
