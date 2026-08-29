from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from pathlib import Path

import yaml

from openmapstack.cli import main
from openmapstack.integrity import canonical_file_set_hash, declared_input_paths, file_inventory
from openmapstack.validation import validate_project


class OpenMapStackCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="openmapstack-cli-test-")
        self.root = Path(self.tempdir.name)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def write_project(self, project: dict | None = None, *, artifacts: bool = False) -> Path:
        project = deepcopy(project or valid_manifest())
        path = self.root / "project.yaml"
        path.write_text(yaml.safe_dump(project, sort_keys=False), encoding="utf-8")
        (self.root / "README.md").write_text("# Test project\n", encoding="utf-8")
        (self.root / "pipeline.py").write_text(PIPELINE, encoding="utf-8")
        if artifacts:
            materialize_artifacts(self.root)
        return path

    def symlinked_project(self) -> tuple[Path, Path]:
        """A project directory plus a symlink pointing at it."""
        real = self.root / "real"
        real.mkdir()
        link = self.root / "link"
        link.symlink_to(real, target_is_directory=True)
        (real / "project.yaml").write_text(
            yaml.safe_dump(valid_manifest(), sort_keys=False), encoding="utf-8"
        )
        (real / "README.md").write_text("# Test project\n", encoding="utf-8")
        (real / "pipeline.py").write_text(PIPELINE, encoding="utf-8")
        return real, link

    def test_complete_project_passes(self) -> None:
        path = self.write_project(artifacts=True)
        result = validate_project(path)
        self.assertEqual(result.status, "passed", [check.to_dict() for check in result.checks])
        self.assertTrue(result.ok())

    def test_preflight_allows_not_yet_generated_artifacts(self) -> None:
        path = self.write_project(artifacts=False)
        result = validate_project(path, artifacts=False)
        self.assertEqual(result.status, "passed", [check.to_dict() for check in result.checks])

    def test_invalid_graph_and_license_fail(self) -> None:
        project = valid_manifest()
        project["sources"]["parcels"]["license"] = {}
        project["processing"]["steps"][1]["input"] = "never_produced"
        path = self.write_project(project, artifacts=True)
        result = validate_project(path)
        failed = {check.id for check in result.checks if check.status == "failed"}
        self.assertIn("source.license", failed)
        self.assertIn("processing.graph", failed)
        self.assertEqual(result.status, "failed")

    def test_report_status_cannot_launder_warning(self) -> None:
        path = self.write_project(artifacts=True)
        report_path = self.root / "validation" / "latest-report.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["checks"][0]["status"] = "warning"
        report["status"] = "passed"
        report_path.write_text(json.dumps(report), encoding="utf-8")
        result = validate_project(path)
        report_check = next(check for check in result.checks if check.id == "validation.report")
        self.assertEqual(report_check.status, "failed")
        self.assertIn("expected 'warning'", report_check.message)

    def test_failed_domain_report_fails_cli_validation(self) -> None:
        project = valid_manifest()
        project["project"]["status"] = "failed"
        project["runs"]["latest"]["status"] = "failed"
        path = self.write_project(project, artifacts=True)
        report_path = self.root / "validation" / "latest-report.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["checks"][0]["status"] = "failed"
        report["status"] = "failed"
        report_path.write_text(json.dumps(report), encoding="utf-8")
        run_path = self.root / "runs" / "run-20260826-000000.json"
        run = json.loads(run_path.read_text(encoding="utf-8"))
        run["status"] = "failed"
        run_path.write_text(json.dumps(run), encoding="utf-8")
        result = validate_project(path)
        self.assertEqual(result.status, "failed")
        report_check = next(check for check in result.checks if check.id == "validation.report")
        self.assertEqual(report_check.status, "failed")
        self.assertIn("failed checks", report_check.message)

    def test_path_escape_is_rejected(self) -> None:
        project = valid_manifest()
        project["outputs"]["candidate"]["path"] = "../outside.json"
        path = self.write_project(project, artifacts=True)
        result = validate_project(path)
        declaration = next(check for check in result.checks if check.id == "outputs.declaration")
        self.assertEqual(declaration.status, "failed")
        self.assertIn("escapes", declaration.message)

    def test_runtime_dependencies_must_exist_inside_project(self) -> None:
        project = valid_manifest()
        project["runtime"]["implementation"]["dependencies"] = ["../outside.lock"]
        path = self.write_project(project, artifacts=False)
        result = validate_project(path, artifacts=False)
        dependency_check = next(
            check for check in result.checks if check.id == "runtime.dependencies"
        )
        self.assertEqual(dependency_check.status, "failed")
        self.assertIn("safe project-relative path", dependency_check.message)

    def test_run_output_hash_is_verified_against_file(self) -> None:
        path = self.write_project(artifacts=True)
        output = self.root / "data" / "derived" / "candidate.json"
        output.write_text('{"type":"FeatureCollection","features":[{}]}', encoding="utf-8")
        result = validate_project(path)
        run_check = next(check for check in result.checks if check.id == "runs.latest")
        self.assertEqual(run_check.status, "failed")
        self.assertIn("hash mismatch", run_check.message)

    def test_matching_but_invented_aggregate_hashes_fail(self) -> None:
        path = self.write_project(artifacts=True)
        invented = "sha256:" + "f" * 64
        project = yaml.safe_load(path.read_text(encoding="utf-8"))
        project["runs"]["latest"]["outputs_hash"] = invented
        path.write_text(yaml.safe_dump(project, sort_keys=False), encoding="utf-8")
        report_path = self.root / "validation/latest-report.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["outputs_hash"] = invented
        report_path.write_text(json.dumps(report), encoding="utf-8")
        run_path = self.root / "runs/run-20260826-000000.json"
        run = json.loads(run_path.read_text(encoding="utf-8"))
        run["outputs_hash"] = invented
        run_path.write_text(json.dumps(run), encoding="utf-8")
        result = validate_project(path)
        run_check = next(check for check in result.checks if check.id == "runs.latest")
        self.assertEqual(run_check.status, "failed")
        self.assertIn("real canonical file-set hash", run_check.message)

    def test_missing_input_inventory_fails(self) -> None:
        path = self.write_project(artifacts=True)
        run_path = self.root / "runs/run-20260826-000000.json"
        run = json.loads(run_path.read_text(encoding="utf-8"))
        del run["inputs"]
        run_path.write_text(json.dumps(run), encoding="utf-8")
        result = validate_project(path)
        run_check = next(check for check in result.checks if check.id == "runs.latest")
        self.assertEqual(run_check.status, "failed")
        self.assertIn("input inventory is missing", run_check.message)

    def test_formal_schema_rejects_missing_analysis_crs(self) -> None:
        project = valid_manifest()
        del project["processing"]["analysis_crs"]
        path = self.write_project(project, artifacts=False)
        result = validate_project(path, artifacts=False)
        schema_check = next(check for check in result.checks if check.id == "manifest.json_schema")
        self.assertEqual(schema_check.status, "failed")
        self.assertIn("analysis_crs", schema_check.message)

    def test_declared_output_missing_from_run_hash_inventory_fails(self) -> None:
        project = valid_manifest()
        project["outputs"]["extra"] = {
            "path": "data/derived/extra.json",
            "format": "GeoJSON",
            "generated_by": "export",
        }
        path = self.write_project(project, artifacts=True)
        extra_output = self.root / "data" / "derived" / "extra.json"
        extra_output.write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")
        result = validate_project(path)
        run_check = next(check for check in result.checks if check.id == "runs.latest")
        self.assertEqual(run_check.status, "failed")
        self.assertIn("do not participate in run output hashing", run_check.message)
        self.assertIn("data/derived/extra.json", run_check.message)

    def test_undeclared_derived_file_is_a_warning_not_a_silent_pass(self) -> None:
        path = self.write_project(artifacts=True)
        stray = self.root / "data" / "derived" / "undeclared.json"
        stray.write_text("{}", encoding="utf-8")
        result = validate_project(path)
        undeclared_check = next(
            check for check in result.checks if check.id == "outputs.undeclared_derived_files"
        )
        self.assertEqual(undeclared_check.status, "warning")
        self.assertIn("data/derived/undeclared.json", undeclared_check.message)

    def test_run_executes_canonical_pipeline_then_validates(self) -> None:
        path = self.write_project(artifacts=False)
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = main(["run", str(path)])
        self.assertEqual(exit_code, 0, stdout.getvalue())
        self.assertTrue((self.root / "data" / "derived" / "candidate.json").is_file())
        self.assertIn("PASSED", stdout.getvalue())

    def test_run_dry_run_does_not_execute(self) -> None:
        path = self.write_project(artifacts=False)
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = main(["run", str(path), "--dry-run", "--pipeline-arg=--sample"])
        self.assertEqual(exit_code, 0)
        self.assertIn("Would run:", stdout.getvalue())
        self.assertIn("--sample", stdout.getvalue())
        self.assertFalse((self.root / "data" / "derived" / "candidate.json").exists())

    def test_validate_json_and_inspect_json_are_machine_readable(self) -> None:
        path = self.write_project(artifacts=True)
        for argv, expected_schema in (
            (["validate", str(path), "--json"], "openmapstack-validation-result/v1"),
            (["inspect", str(path), "--json"], "openmapstack-inspection/v1"),
        ):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(argv)
            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["schema"], expected_schema)

    def test_pipeline_failure_is_reported_without_post_validation(self) -> None:
        project = valid_manifest()
        path = self.write_project(project, artifacts=False)
        (self.root / "pipeline.py").write_text("raise SystemExit(7)\n", encoding="utf-8")
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = main(["run", str(path), "--json"])
        self.assertEqual(exit_code, 1)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["phase"], "execute")
        self.assertEqual(payload["returncode"], 7)

    def test_validation_works_through_a_symlinked_project_root(self) -> None:
        """End-to-end guard that a symlinked root validates cleanly.

        This passes even with the resolve bug present, because
        resolve_project_file() already resolves the manifest before the
        validator sees it. It guards that normalization staying in place;
        test_declared_input_paths_accepts_an_unresolved_root is the one that
        reproduces the underlying defect.
        """
        real, link = self.symlinked_project()
        materialize_artifacts(real, source_files=["data/source/roads.geojson"])

        result = validate_project(link / "project.yaml")
        self.assertEqual(
            result.status, "passed", [check.to_dict() for check in result.checks]
        )

    def test_declared_input_paths_accepts_an_unresolved_root(self) -> None:
        """Reproduces the resolved-child versus unresolved-root ValueError.

        project_path() resolves what it returns, so a relative_to() measured
        against an unresolved root raises. Callers outside the CLI reach this
        helper directly with whatever root they hold — the eval assertions do —
        so the helper cannot assume a normalized argument. data/source must be
        populated or the walk that normalizes input paths never runs.
        """
        real, link = self.symlinked_project()
        materialize_artifacts(real, source_files=["data/source/roads.geojson"])
        project = yaml.safe_load((real / "project.yaml").read_text(encoding="utf-8"))

        # Project-relative results, whichever spelling of the root is supplied.
        self.assertIn("data/source/roads.geojson", declared_input_paths(link, project))
        self.assertEqual(
            declared_input_paths(link, project),
            declared_input_paths(real, project),
        )


def valid_manifest() -> dict:
    return {
        "schema": "openmapstack-project/v1",
        "project": {
            "id": "cli-test",
            "title": "CLI test",
            "question": "Which parcel qualifies?",
            "created_at": "2026-08-26T00:00:00Z",
            "updated_at": "2026-08-26T00:00:00Z",
            "status": "validated",
        },
        "interpretation": {
            "objective": "Select a deterministic fixture parcel.",
            "assumptions": [
                {"id": "A1", "statement": "Area is metric.", "rationale": "The threshold is in square metres."}
            ],
        },
        "sources": {
            "parcels": {
                "role": "authoritative_input",
                "provider": "Fixture authority",
                "dataset": "Fixture parcels",
                "source_url": "https://data.example.test/parcels.geojson",
                "access": {"method": "local", "retrieved_at": "2026-08-26T00:00:00Z"},
                "version": {"identifier": "fixture-1", "published_at": "2026-08-26"},
                "selection": {"filter": "id = 'P1'"},
                "license": {"name": "CC0-1.0", "url": "https://creativecommons.org/publicdomain/zero/1.0/"},
                "rationale": "Small deterministic test fixture.",
            }
        },
        "overrides": [],
        "processing": {
            "analysis_crs": "EPSG:3301",
            "storage_crs": "EPSG:4326",
            "steps": [
                {"id": "load", "operation": "read", "source": "parcels", "output": "parcels_raw"},
                {"id": "export", "operation": "export", "input": "parcels_raw", "output": "candidate"},
            ],
        },
        "outputs": {
            "candidate": {
                "path": "data/derived/candidate.json",
                "format": "GeoJSON",
                "generated_by": "export",
            }
        },
        "validation": {
            "required": ["geometry_valid", "manifest_graph_resolves"],
            "domain_checks": [],
        },
        "presentation": {
            "intent": "report",
            "primary_view": "report",
            "layout": {"type": "report"},
            "map": {
                "engine_preference": "maplibre",
                "basemap": {
                    "id": "osm-standard",
                    "kind": "raster-xyz",
                    "tiles": ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
                    "attribution": "© OpenStreetMap contributors",
                },
                "layer_groups": [],
                "layers": [],
            },
            "provenance_ui": {"show_assumptions": True},
        },
        "runtime": {
            "implementation": {"preferred_engine": "python", "pipeline": "pipeline.py"},
            "environment": {"python": "3.11"},
        },
        "runs": {
            "latest": {
                "id": "run-20260826-000000",
                "started_at": "2026-08-26T00:00:00Z",
                "completed_at": "2026-08-26T00:00:01Z",
                "status": "passed",
                "inputs_hash": "sha256:input",
                "outputs_hash": "sha256:output",
                "validation_report": {"path": "validation/latest-report.json"},
            }
        },
    }


def materialize_artifacts(root: Path, *, source_files: list[str] | None = None) -> None:
    output = root / "data" / "derived" / "candidate.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")
    for relative in source_files or []:
        source = root / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")
    inputs = sorted(["pipeline.py", *(source_files or [])])
    outputs = ["data/derived/candidate.json"]
    inputs_hash = canonical_file_set_hash(root, inputs)
    outputs_hash = canonical_file_set_hash(root, outputs)
    project_path = root / "project.yaml"
    project = yaml.safe_load(project_path.read_text(encoding="utf-8"))
    project["runs"]["latest"]["inputs_hash"] = inputs_hash
    project["runs"]["latest"]["outputs_hash"] = outputs_hash
    project_path.write_text(yaml.safe_dump(project, sort_keys=False), encoding="utf-8")
    report = {
        "run_id": "run-20260826-000000",
        "started_at": "2026-08-26T00:00:00Z",
        "completed_at": "2026-08-26T00:00:01Z",
        "status": "passed",
        "checks": [
            {"id": "geometry_valid", "status": "passed", "features_checked": 0},
            {"id": "manifest_graph_resolves", "status": "passed", "steps_checked": 2},
        ],
        "inputs_hash": inputs_hash,
        "outputs_hash": outputs_hash,
    }
    report_path = root / "validation" / "latest-report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report), encoding="utf-8")
    run = {
        "run_id": "run-20260826-000000",
        "started_at": "2026-08-26T00:00:00Z",
        "completed_at": "2026-08-26T00:00:01Z",
        "status": "passed",
        "inputs_hash": inputs_hash,
        "outputs_hash": outputs_hash,
        "inputs": file_inventory(root, inputs),
        "outputs": file_inventory(root, outputs),
        "environment": {"python": "test"},
    }
    run_path = root / "runs" / "run-20260826-000000.json"
    run_path.parent.mkdir(parents=True, exist_ok=True)
    run_path.write_text(json.dumps(run), encoding="utf-8")


PIPELINE = """\
from pathlib import Path
import hashlib
import json
import yaml

root = Path(__file__).resolve().parent
output = root / "data" / "derived" / "candidate.json"
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")

def file_hash(path):
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()

def set_hash(paths):
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda value: value.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(path.read_bytes())
    return "sha256:" + digest.hexdigest()

input_paths = [root / "pipeline.py"]
output_paths = [output]
inputs_hash = set_hash(input_paths)
outputs_hash = set_hash(output_paths)
report = {
    "run_id": "run-20260826-000000",
    "started_at": "2026-08-26T00:00:00Z",
    "completed_at": "2026-08-26T00:00:01Z",
    "status": "passed",
    "checks": [
        {"id": "geometry_valid", "status": "passed", "features_checked": 0},
        {"id": "manifest_graph_resolves", "status": "passed", "steps_checked": 2},
    ],
    "inputs_hash": inputs_hash,
    "outputs_hash": outputs_hash,
}
report_path = root / "validation" / "latest-report.json"
report_path.parent.mkdir(parents=True, exist_ok=True)
report_path.write_text(json.dumps(report), encoding="utf-8")
run = {
    "run_id": "run-20260826-000000",
    "started_at": "2026-08-26T00:00:00Z",
    "completed_at": "2026-08-26T00:00:01Z",
    "status": "passed",
    "inputs_hash": inputs_hash,
    "outputs_hash": outputs_hash,
    "inputs": [{"path": "pipeline.py", "sha256": file_hash(root / "pipeline.py")}],
    "outputs": [{"path": "data/derived/candidate.json", "sha256": file_hash(output)}],
    "environment": {"python": "test"},
}
run_path = root / "runs" / "run-20260826-000000.json"
run_path.parent.mkdir(parents=True, exist_ok=True)
run_path.write_text(json.dumps(run), encoding="utf-8")
project_path = root / "project.yaml"
project = yaml.safe_load(project_path.read_text(encoding="utf-8"))
project["runs"]["latest"]["inputs_hash"] = inputs_hash
project["runs"]["latest"]["outputs_hash"] = outputs_hash
project_path.write_text(yaml.safe_dump(project, sort_keys=False), encoding="utf-8")
"""


if __name__ == "__main__":
    unittest.main()
