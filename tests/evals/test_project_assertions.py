from __future__ import annotations

import unittest
from copy import deepcopy

from .helpers import make_workspace, minimal_project, write_project

from assertions import project as project_assertions  # noqa: E402


class ExistsTests(unittest.TestCase):
    def test_present_file_passes(self) -> None:
        workspace = make_workspace()
        (workspace / "marker.txt").write_text("ok", encoding="utf-8")
        result = project_assertions.exists(workspace, path="marker.txt")
        self.assertEqual(result.status, "passed")

    def test_missing_file_fails_with_code(self) -> None:
        workspace = make_workspace()
        result = project_assertions.exists(workspace, path="missing.txt")
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("code"), "file_missing")


class ParsesTests(unittest.TestCase):
    def test_valid_mapping_passes(self) -> None:
        workspace = make_workspace()
        write_project(workspace, minimal_project())
        result = project_assertions.parses(workspace)
        self.assertEqual(result.status, "passed")

    def test_missing_or_malformed_manifest_fails_without_raising(self) -> None:
        for content in (None, "schema: [unterminated\n"):
            with self.subTest(content=content):
                workspace = make_workspace()
                if content is not None:
                    (workspace / "project.yaml").write_text(content, encoding="utf-8")
                result = project_assertions.parses(workspace)
                self.assertEqual(result.status, "failed")
                self.assertEqual(result.data.get("code"), "manifest_missing")


class SchemaIsTests(unittest.TestCase):
    def test_matching_schema_passes(self) -> None:
        workspace = make_workspace()
        write_project(workspace, minimal_project())
        result = project_assertions.schema_is(workspace, schema="openmapstack-project/v1")
        self.assertEqual(result.status, "passed")

    def test_mismatched_schema_fails(self) -> None:
        workspace = make_workspace()
        write_project(workspace, minimal_project(schema="something-else/v1"))
        result = project_assertions.schema_is(workspace, schema="openmapstack-project/v1")
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("code"), "schema_mismatch")

    def test_missing_manifest_fails(self) -> None:
        workspace = make_workspace()
        result = project_assertions.schema_is(workspace, schema="openmapstack-project/v1")
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("code"), "manifest_missing")


class ConformsToSchemaTests(unittest.TestCase):
    def test_complete_manifest_passes(self) -> None:
        workspace = make_workspace()
        project = minimal_project()
        project["runs"] = {"latest": {
            "id": "run-1",
            "started_at": "2026-01-01T00:00:00Z",
            "completed_at": "2026-01-01T00:00:01Z",
            "status": "passed",
            "inputs_hash": "sha256:" + "0" * 64,
            "outputs_hash": "sha256:" + "0" * 64,
            "validation_report": {"path": "validation/latest-report.json"},
        }}
        write_project(workspace, project)
        result = project_assertions.conforms_to_schema(workspace)
        self.assertEqual(result.status, "passed", result.detail)

    def test_missing_analysis_crs_fails(self) -> None:
        workspace = make_workspace()
        project = minimal_project()
        project["runs"] = {"latest": {
            "id": "run-1",
            "started_at": "2026-01-01T00:00:00Z",
            "completed_at": "2026-01-01T00:00:01Z",
            "status": "passed",
            "inputs_hash": "sha256:" + "0" * 64,
            "outputs_hash": "sha256:" + "0" * 64,
            "validation_report": {"path": "validation/latest-report.json"},
        }}
        del project["processing"]["analysis_crs"]
        write_project(workspace, project)
        result = project_assertions.conforms_to_schema(workspace)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("code"), "manifest_schema_invalid")

    def test_map_without_basemap_fails(self) -> None:
        """A map with no declared background map is unreadable — the reader
        cannot tell which town, or whether the CRS is displaced. The manifest
        must declare it so the built product can be checked against it."""
        workspace = make_workspace()
        project = minimal_project()
        project["runs"] = {"latest": {
            "id": "run-1",
            "started_at": "2026-01-01T00:00:00Z",
            "completed_at": "2026-01-01T00:00:01Z",
            "status": "passed",
            "inputs_hash": "sha256:" + "0" * 64,
            "outputs_hash": "sha256:" + "0" * 64,
            "validation_report": {"path": "validation/latest-report.json"},
        }}
        del project["presentation"]["map"]["basemap"]
        write_project(workspace, project)
        result = project_assertions.conforms_to_schema(workspace)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("code"), "manifest_schema_invalid")
        self.assertIn("basemap", result.detail)

    def test_basemap_without_tiles_or_url_fails(self) -> None:
        workspace = make_workspace()
        project = minimal_project()
        project["runs"] = {"latest": {
            "id": "run-1",
            "started_at": "2026-01-01T00:00:00Z",
            "completed_at": "2026-01-01T00:00:01Z",
            "status": "passed",
            "inputs_hash": "sha256:" + "0" * 64,
            "outputs_hash": "sha256:" + "0" * 64,
            "validation_report": {"path": "validation/latest-report.json"},
        }}
        del project["presentation"]["map"]["basemap"]["tiles"]
        write_project(workspace, project)
        result = project_assertions.conforms_to_schema(workspace)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("code"), "manifest_schema_invalid")

    def test_missing_manifest_fails(self) -> None:
        result = project_assertions.conforms_to_schema(make_workspace())
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("code"), "manifest_missing")

class StatusAssertionsTests(unittest.TestCase):
    def test_status_is_matches(self) -> None:
        workspace = make_workspace()
        write_project(workspace, minimal_project())
        result = project_assertions.status_is(workspace, status="validated")
        self.assertEqual(result.status, "passed")

    def test_status_is_mismatch(self) -> None:
        workspace = make_workspace()
        write_project(workspace, minimal_project())
        result = project_assertions.status_is(workspace, status="failed")
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("code"), "status_mismatch")

    def test_status_in_set(self) -> None:
        workspace = make_workspace()
        write_project(workspace, minimal_project())
        self.assertEqual(
            project_assertions.status_in(workspace, statuses=["validated", "warning"]).status, "passed"
        )
        self.assertEqual(
            project_assertions.status_in(workspace, statuses=["failed"]).data.get("code"),
            "status_not_in_set",
        )


class StatusAgreesWithReportTests(unittest.TestCase):
    def test_validated_without_report_fails(self) -> None:
        workspace = make_workspace()
        write_project(workspace, minimal_project())
        result = project_assertions.status_agrees_with_validation_report(workspace)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("code"), "validated_without_report")

    def test_no_report_and_not_validated_is_not_testable(self) -> None:
        workspace = make_workspace()
        write_project(workspace, minimal_project(project={
            **minimal_project()["project"], "status": "in_progress",
        }))
        result = project_assertions.status_agrees_with_validation_report(workspace)
        self.assertEqual(result.status, "not_testable")

    def test_validated_status_laundering_fails(self) -> None:
        from .helpers import write_json

        workspace = make_workspace()
        write_project(workspace, minimal_project())
        write_json(workspace, "validation/latest-report.json", {"status": "warning", "checks": []})
        result = project_assertions.status_agrees_with_validation_report(workspace)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("code"), "status_laundering")

    def test_validated_agrees_with_passed_report(self) -> None:
        from .helpers import write_json

        workspace = make_workspace()
        write_project(workspace, minimal_project())
        write_json(workspace, "validation/latest-report.json", {"status": "passed", "checks": []})
        result = project_assertions.status_agrees_with_validation_report(workspace)
        self.assertEqual(result.status, "passed")


class GraphResolvesTests(unittest.TestCase):
    def test_resolving_graph_passes(self) -> None:
        workspace = make_workspace()
        write_project(workspace, minimal_project())
        result = project_assertions.graph_resolves(workspace)
        self.assertEqual(result.status, "passed")

    def test_dangling_input_fails(self) -> None:
        workspace = make_workspace()
        project = minimal_project()
        project["processing"]["steps"][1]["input"] = "nonexistent_symbol"
        write_project(workspace, project)
        result = project_assertions.graph_resolves(workspace)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("code"), "graph_unresolved")

    def test_duplicate_step_id_fails(self) -> None:
        workspace = make_workspace()
        project = minimal_project()
        project["processing"]["steps"].append(deepcopy(project["processing"]["steps"][0]))
        write_project(workspace, project)
        result = project_assertions.graph_resolves(workspace)
        self.assertEqual(result.status, "failed")
        self.assertIn("duplicate step ids", result.detail)

    def test_duplicate_output_symbol_fails(self) -> None:
        workspace = make_workspace()
        project = minimal_project()
        project["processing"]["steps"][1]["output"] = "raw"  # collides with step 0's output
        write_project(workspace, project)
        result = project_assertions.graph_resolves(workspace)
        self.assertEqual(result.status, "failed")
        self.assertIn("duplicate symbol", result.detail)

    def test_unknown_override_reference_fails(self) -> None:
        workspace = make_workspace()
        project = minimal_project()
        project["processing"]["steps"][1]["override"] = "OVERRIDE-DOES-NOT-EXIST"
        write_project(workspace, project)
        result = project_assertions.graph_resolves(workspace)
        self.assertEqual(result.status, "failed")
        self.assertIn("override", result.detail)

    def test_dangling_generated_by_fails(self) -> None:
        workspace = make_workspace()
        project = minimal_project()
        project["outputs"]["final"]["generated_by"] = "step_that_does_not_exist"
        write_project(workspace, project)
        result = project_assertions.graph_resolves(workspace)
        self.assertEqual(result.status, "failed")


class DeclaredFilesExistTests(unittest.TestCase):
    def test_all_present_passes(self) -> None:
        workspace = make_workspace()
        (workspace / "a.txt").write_text("x", encoding="utf-8")
        (workspace / "b.txt").write_text("x", encoding="utf-8")
        result = project_assertions.declared_files_exist(workspace, files=["a.txt", "b.txt"])
        self.assertEqual(result.status, "passed")

    def test_missing_file_fails(self) -> None:
        workspace = make_workspace()
        (workspace / "a.txt").write_text("x", encoding="utf-8")
        result = project_assertions.declared_files_exist(workspace, files=["a.txt", "missing.txt"])
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("code"), "declared_files_missing")


class AssumptionsHaveRationaleTests(unittest.TestCase):
    def test_present_and_complete_passes(self) -> None:
        workspace = make_workspace()
        write_project(workspace, minimal_project())
        result = project_assertions.assumptions_have_rationale(workspace)
        self.assertEqual(result.status, "passed")

    def test_no_assumptions_warns(self) -> None:
        workspace = make_workspace()
        project = minimal_project()
        project["interpretation"]["assumptions"] = []
        write_project(workspace, project)
        result = project_assertions.assumptions_have_rationale(workspace)
        self.assertEqual(result.status, "warning")
        self.assertEqual(result.data.get("code"), "no_assumptions_declared")

    def test_missing_rationale_fails(self) -> None:
        workspace = make_workspace()
        project = minimal_project()
        project["interpretation"]["assumptions"] = [{"id": "A1", "statement": "x"}]
        write_project(workspace, project)
        result = project_assertions.assumptions_have_rationale(workspace)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("code"), "assumption_missing_rationale")


class OneCanonicalPipelineTests(unittest.TestCase):
    def test_missing_pipeline_fails(self) -> None:
        workspace = make_workspace()
        result = project_assertions.one_canonical_pipeline(workspace)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("code"), "pipeline_missing")

    def test_wrapper_importing_pipeline_passes(self) -> None:
        workspace = make_workspace()
        (workspace / "pipeline.py").write_text("def main():\n    pass\n", encoding="utf-8")
        (workspace / "wrapper.py").write_text("import pipeline\npipeline.main()\n", encoding="utf-8")
        result = project_assertions.one_canonical_pipeline(workspace, wrapper_paths=["wrapper.py"])
        self.assertEqual(result.status, "passed")

    def test_wrapper_duplicating_logic_fails(self) -> None:
        workspace = make_workspace()
        (workspace / "pipeline.py").write_text("def main():\n    pass\n", encoding="utf-8")
        (workspace / "wrapper.py").write_text("def main():\n    pass\n", encoding="utf-8")
        result = project_assertions.one_canonical_pipeline(workspace, wrapper_paths=["wrapper.py"])
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("code"), "duplicated_pipeline_logic")


if __name__ == "__main__":
    unittest.main()
