from __future__ import annotations

import json
import unittest

from .helpers import make_workspace, minimal_project, write_project

from assertions import overrides as overrides_assertions  # noqa: E402


def _override_project(**override_fields):
    project = minimal_project()
    base_override = {
        "id": "OVERRIDE-001",
        "action": "modify_attribute",
        "target": {"source": "test_source", "feature_id": "f1"},
        "change": {"field": "status", "from": "active", "to": "closed"},
        "rationale": "field survey",
        "evidence": [{"type": "field_survey", "value": "surveyed 2026-01-01"}],
        "created_at": "2026-01-01T00:00:00Z",
        "created_by": "analyst",
    }
    base_override.update(override_fields)
    project["overrides"] = [base_override]
    return project


class DeclaredCountTests(unittest.TestCase):
    def test_matching_count_passes(self) -> None:
        workspace = make_workspace()
        write_project(workspace, _override_project())
        result = overrides_assertions.declared_count(workspace, count=1)
        self.assertEqual(result.status, "passed")

    def test_mismatched_count_fails(self) -> None:
        workspace = make_workspace()
        write_project(workspace, _override_project())
        result = overrides_assertions.declared_count(workspace, count=2)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("code"), "override_count_mismatch")


class ProvenanceTests(unittest.TestCase):
    def test_complete_provenance_passes(self) -> None:
        workspace = make_workspace()
        write_project(workspace, _override_project())
        result = overrides_assertions.every_override_has_provenance(workspace)
        self.assertEqual(result.status, "passed")

    def test_missing_rationale_fails(self) -> None:
        workspace = make_workspace()
        write_project(workspace, _override_project(rationale=""))
        result = overrides_assertions.every_override_has_provenance(workspace)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("code"), "override_missing_provenance")

    def test_vacuous_pass_with_no_overrides(self) -> None:
        workspace = make_workspace()
        write_project(workspace, minimal_project())
        result = overrides_assertions.every_override_has_provenance(workspace)
        self.assertEqual(result.status, "passed")


class EvidenceNotPlaceholderTests(unittest.TestCase):
    def test_real_evidence_passes(self) -> None:
        workspace = make_workspace()
        write_project(workspace, _override_project())
        result = overrides_assertions.evidence_not_placeholder(workspace)
        self.assertEqual(result.status, "passed")

    def test_placeholder_evidence_fails(self) -> None:
        workspace = make_workspace()
        write_project(workspace, _override_project(evidence=[{"type": "url", "value": "TBD"}]))
        result = overrides_assertions.evidence_not_placeholder(workspace)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("code"), "placeholder_evidence")


class ApplicationStatusTests(unittest.TestCase):
    def test_matching_status_passes(self) -> None:
        workspace = make_workspace()
        write_project(workspace, _override_project())
        report_path = workspace / "validation" / "latest-report.json"
        report_path.parent.mkdir(parents=True)
        report_path.write_text(json.dumps({
            "checks": [{"id": "overrides_applied", "results": [{"id": "OVERRIDE-001", "status": "applied"}]}]
        }), encoding="utf-8")
        result = overrides_assertions.application_status(workspace, id="OVERRIDE-001", status="applied")
        self.assertEqual(result.status, "passed")

    def test_missing_report_is_not_testable(self) -> None:
        workspace = make_workspace()
        write_project(workspace, _override_project())
        result = overrides_assertions.application_status(workspace, id="OVERRIDE-001", status="applied")
        self.assertEqual(result.status, "not_testable")
        self.assertEqual(result.data.get("code"), "report_missing")

    def test_mismatched_status_fails(self) -> None:
        workspace = make_workspace()
        write_project(workspace, _override_project())
        report_path = workspace / "validation" / "latest-report.json"
        report_path.parent.mkdir(parents=True)
        report_path.write_text(json.dumps({
            "checks": [{"id": "overrides_applied", "results": [{"id": "OVERRIDE-001", "status": "rejected"}]}]
        }), encoding="utf-8")
        result = overrides_assertions.application_status(workspace, id="OVERRIDE-001", status="applied")
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("code"), "override_status_mismatch")


class FromValueMatchesSourceTests(unittest.TestCase):
    def _write_source(self, workspace, value="active"):
        source = workspace / "data" / "source" / "features.geojson"
        source.parent.mkdir(parents=True)
        source.write_text(json.dumps({
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature",
                "properties": {"feature_id": "f1", "status": value},
                "geometry": {"type": "Point", "coordinates": [0, 0]},
            }],
        }), encoding="utf-8")

    def test_matching_from_value_passes(self) -> None:
        workspace = make_workspace()
        write_project(workspace, _override_project())
        self._write_source(workspace, "active")
        result = overrides_assertions.from_value_matches_source(
            workspace, id="OVERRIDE-001", source_path="data/source/features.geojson", id_field="feature_id"
        )
        self.assertEqual(result.status, "passed")

    def test_mismatched_from_value_fails(self) -> None:
        workspace = make_workspace()
        write_project(workspace, _override_project())
        self._write_source(workspace, "closed")  # source already says closed, override asserts active
        result = overrides_assertions.from_value_matches_source(
            workspace, id="OVERRIDE-001", source_path="data/source/features.geojson", id_field="feature_id"
        )
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("code"), "from_value_mismatch")

    def test_missing_source_file_is_not_testable(self) -> None:
        workspace = make_workspace()
        write_project(workspace, _override_project())
        result = overrides_assertions.from_value_matches_source(
            workspace, id="OVERRIDE-001", source_path="data/source/missing.geojson", id_field="feature_id"
        )
        self.assertEqual(result.status, "not_testable")
        self.assertEqual(result.data.get("code"), "source_missing")

    def test_non_modify_attribute_action_is_vacuously_passed(self) -> None:
        workspace = make_workspace()
        write_project(workspace, _override_project(action="add_feature"))
        result = overrides_assertions.from_value_matches_source(
            workspace, id="OVERRIDE-001", source_path="data/source/features.geojson", id_field="feature_id"
        )
        self.assertEqual(result.status, "passed")


class SourceFilesByteIdenticalTests(unittest.TestCase):
    def test_matching_baseline_passes(self) -> None:
        workspace = make_workspace()
        target = workspace / "data" / "source" / "file.txt"
        target.parent.mkdir(parents=True)
        target.write_text("original", encoding="utf-8")
        import hashlib

        baseline = {"data/source/file.txt": "sha256:" + hashlib.sha256(b"original").hexdigest()}
        result = overrides_assertions.source_files_byte_identical(
            workspace, paths=["data/source/file.txt"], hashes_before=baseline
        )
        self.assertEqual(result.status, "passed")

    def test_mutated_file_fails(self) -> None:
        workspace = make_workspace()
        target = workspace / "data" / "source" / "file.txt"
        target.parent.mkdir(parents=True)
        target.write_text("mutated", encoding="utf-8")
        import hashlib

        baseline = {"data/source/file.txt": "sha256:" + hashlib.sha256(b"original").hexdigest()}
        result = overrides_assertions.source_files_byte_identical(
            workspace, paths=["data/source/file.txt"], hashes_before=baseline
        )
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("code"), "source_mutated")

    def test_missing_baseline_is_not_testable(self) -> None:
        workspace = make_workspace()
        target = workspace / "data" / "source" / "file.txt"
        target.parent.mkdir(parents=True)
        target.write_text("original", encoding="utf-8")
        result = overrides_assertions.source_files_byte_identical(
            workspace, paths=["data/source/file.txt"], hashes_before=None
        )
        self.assertEqual(result.status, "not_testable")
        self.assertEqual(result.data.get("code"), "baseline_missing")

    def test_rerun_workspace_comparison(self) -> None:
        workspace = make_workspace()
        rerun = make_workspace()
        for root in (workspace, rerun):
            target = root / "data" / "source" / "file.txt"
            target.parent.mkdir(parents=True)
            target.write_text("same content", encoding="utf-8")
        result = overrides_assertions.source_files_byte_identical(
            workspace, paths=["data/source/file.txt"], rerun_workspace=str(rerun)
        )
        self.assertEqual(result.status, "passed")

    def test_rerun_workspace_mismatch_fails(self) -> None:
        workspace = make_workspace()
        rerun = make_workspace()
        (workspace / "data" / "source").mkdir(parents=True)
        (workspace / "data" / "source" / "file.txt").write_text("A", encoding="utf-8")
        (rerun / "data" / "source").mkdir(parents=True)
        (rerun / "data" / "source" / "file.txt").write_text("B", encoding="utf-8")
        result = overrides_assertions.source_files_byte_identical(
            workspace, paths=["data/source/file.txt"], rerun_workspace=str(rerun)
        )
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("code"), "source_mutated")

    def test_no_paths_declared_is_not_testable(self) -> None:
        workspace = make_workspace()
        result = overrides_assertions.source_files_byte_identical(workspace, paths=[])
        self.assertEqual(result.status, "not_testable")
        self.assertEqual(result.data.get("code"), "no_paths_declared")


if __name__ == "__main__":
    unittest.main()
