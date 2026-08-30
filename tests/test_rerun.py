from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = REPO_ROOT / "evals" / "run.py"
SPEC = importlib.util.spec_from_file_location("openmapstack_clean_rerun_tests", RUNNER_PATH)
assert SPEC and SPEC.loader
eval_runner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = eval_runner
SPEC.loader.exec_module(eval_runner)

from openmapstack import rerun as openmapstack_rerun  # noqa: E402
from openmapstack.checks import rerun as rerun_assertions  # noqa: E402


class FakeValidation:
    @staticmethod
    def ok() -> bool:
        return True

    @staticmethod
    def to_dict() -> dict[str, object]:
        return {"schema": "test", "status": "passed", "checks": []}


class CleanRerunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="openmapstack-clean-rerun-test-")
        self.root = Path(self.tempdir.name)
        self.project = self.root / "original"
        self.rerun = self.root / "rerun"
        self.project.mkdir()
        self.rerun.mkdir()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def write_manifest(
        self,
        *,
        pipeline: str = "pipeline.py",
        dependencies: list[str] | None = None,
        command: list[str] | None = None,
    ) -> None:
        implementation: dict[str, object] = {"pipeline": pipeline}
        if dependencies is not None:
            implementation["dependencies"] = dependencies
        if command is not None:
            implementation = {"command": command}
            if dependencies is not None:
                implementation["dependencies"] = dependencies
        manifest = {"runtime": {"implementation": implementation}}
        (self.project / "project.yaml").write_text(
            yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8"
        )

    def test_clean_workspace_preserves_only_inputs_entrypoint_and_declared_dependencies(self) -> None:
        self.write_manifest(dependencies=["config.json"])
        (self.project / "pipeline.py").write_text(
            "from pathlib import Path\n"
            "root = Path(__file__).resolve().parent\n"
            "assert (root / 'config.json').is_file()\n"
            "assert (root / 'data/source/input.txt').is_file()\n"
            "(root / 'rebuilt.txt').write_text('rebuilt')\n",
            encoding="utf-8",
        )
        (self.project / "config.json").write_text("{}\n", encoding="utf-8")
        for relative in (
            "data/source/input.txt",
            "data/overrides/override.geojson",
            "data/derived/stale.json",
            "validation/latest-report.json",
            "runs/old.json",
            "prompt.md",
            "project.qgz",
        ):
            path = self.project / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("stale\n", encoding="utf-8")

        with patch.object(openmapstack_rerun, "validate_project", return_value=FakeValidation()):
            evidence = eval_runner.perform_clean_rerun(self.project, self.rerun, 10)

        self.assertEqual(evidence["status"], "passed", evidence)
        self.assertTrue((self.rerun / "rebuilt.txt").is_file())
        self.assertEqual(
            evidence["preserved_paths"],
            ["config.json", "data/overrides", "data/source", "pipeline.py", "project.yaml"],
        )
        for excluded in (
            "data/derived/stale.json",
            "validation/latest-report.json",
            "runs/old.json",
            "prompt.md",
            "project.qgz",
        ):
            self.assertFalse((self.rerun / excluded).exists(), excluded)

    def test_missing_canonical_entrypoint_is_a_graded_rerun_failure(self) -> None:
        self.write_manifest(pipeline="missing.py")
        evidence = eval_runner.perform_clean_rerun(self.project, self.rerun, 10)
        self.assertEqual(evidence["status"], "failed")
        self.assertEqual(evidence["stage"], "preparation")
        self.assertIn("canonical pipeline does not exist", evidence["error"])
        assertion = rerun_assertions.clean_execution_succeeded(self.project, str(self.rerun))
        self.assertEqual(assertion.status, "failed")

    def test_missing_declared_dependency_fails_before_execution(self) -> None:
        self.write_manifest(dependencies=["missing-config.json"])
        (self.project / "pipeline.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
        evidence = eval_runner.perform_clean_rerun(self.project, self.rerun, 10)
        self.assertEqual(evidence["stage"], "preparation")
        self.assertIn("declared clean-rerun dependency does not exist", evidence["error"])
        self.assertNotIn("execution", evidence)

    def test_pipeline_that_only_reuses_existing_output_fails_clean_rerun(self) -> None:
        self.write_manifest()
        stale = self.project / "data/derived/result.json"
        stale.parent.mkdir(parents=True)
        stale.write_text('{"result": "looks valid"}\n', encoding="utf-8")
        (self.project / "pipeline.py").write_text(
            "from pathlib import Path\n"
            "output = Path(__file__).resolve().parent / 'data/derived/result.json'\n"
            "raise SystemExit(0 if output.exists() else 9)\n",
            encoding="utf-8",
        )
        evidence = eval_runner.perform_clean_rerun(self.project, self.rerun, 10)
        self.assertEqual(evidence["status"], "failed")
        self.assertEqual(evidence["stage"], "canonical_execution")
        self.assertEqual(evidence["execution"]["returncode"], 9)
        self.assertFalse((self.rerun / "data/derived/result.json").exists())

    def test_pipeline_that_mutates_immutable_source_fails_clean_rerun(self) -> None:
        self.write_manifest()
        source_file = self.project / "data/source/input.txt"
        source_file.parent.mkdir(parents=True)
        source_file.write_text("original\n", encoding="utf-8")
        (self.project / "pipeline.py").write_text(
            "from pathlib import Path\n"
            "root = Path(__file__).resolve().parent\n"
            "(root / 'data/source/input.txt').write_text('mutated by pipeline\\n')\n"
            "raise SystemExit(0)\n",
            encoding="utf-8",
        )
        with patch.object(openmapstack_rerun, "validate_project", return_value=FakeValidation()):
            evidence = eval_runner.perform_clean_rerun(self.project, self.rerun, 10)
        self.assertEqual(evidence["status"], "failed", evidence)
        self.assertEqual(evidence["stage"], "source_integrity")
        self.assertIn("data/source/input.txt", evidence["mutated_source_files"])
        assertion = rerun_assertions.clean_execution_succeeded(self.project, str(self.rerun))
        self.assertEqual(assertion.status, "failed")

    def test_command_cannot_escape_to_eval_generator(self) -> None:
        self.write_manifest(command=["python3", "../../evals/fixtures/reference_pipeline/gen.py"])
        evidence = eval_runner.perform_clean_rerun(
            self.project,
            self.rerun,
            10,
            forbidden_fragments=eval_runner.eval_forbidden_rerun_fragments(),
        )
        self.assertEqual(evidence["stage"], "preparation")
        self.assertIn("excluded machinery", evidence["error"])

    def test_case005_reruns_the_generated_canonical_pipeline(self) -> None:
        case_dir = REPO_ROOT / "evals/cases/005-reproducible-rerun"
        result = eval_runner.run_case(case_dir, "fixture", timeout_s=30)
        self.assertEqual(result["status"], "passed", result)
        self.assertIsNone(result["rerun_generator"])
        clean_rerun = result["clean_rerun"]
        self.assertEqual(clean_rerun["status"], "passed", clean_rerun)
        self.assertEqual(clean_rerun["command"][1], "pipeline.py")
        self.assertNotIn("reference_pipeline", json.dumps(clean_rerun["command"]))
        self.assertNotIn("data/derived", clean_rerun["preserved_paths"])


class RerunNormalizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="openmapstack-rerun-normalize-test-")
        self.original = Path(self.tempdir.name) / "original"
        self.rerun = Path(self.tempdir.name) / "rerun"
        self.original.mkdir()
        self.rerun.mkdir()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def write_json_pair(self, relative: str, original: object, rerun: object) -> None:
        for root, value in ((self.original, original), (self.rerun, rerun)):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(value), encoding="utf-8")

    def test_geojson_feature_order_ring_representation_and_declared_timestamp_are_normalized(self) -> None:
        feature_a = {
            "type": "Feature",
            "properties": {"id": "a", "generated_at": "first"},
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[0, 0], [2, 0], [2, 2], [0, 0]]],
            },
        }
        feature_b = {
            "type": "Feature",
            "properties": {"id": "b"},
            "geometry": {"type": "Point", "coordinates": [3, 4]},
        }
        rerun_a = {
            "type": "Feature",
            "properties": {"generated_at": "second", "id": "a"},
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[2, 2], [2, 0], [0, 0], [2, 2]]],
            },
        }
        self.write_json_pair(
            "data/result.geojson",
            {"type": "FeatureCollection", "features": [feature_a, feature_b]},
            {"type": "FeatureCollection", "features": [feature_b, rerun_a]},
        )
        result = rerun_assertions.outputs_semantically_equal(
            self.original,
            str(self.rerun),
            ["data/result.geojson"],
            ignored_fields=["generated_at"],
        )
        self.assertEqual(result.status, "passed", result.detail)

    def test_semantically_changed_output_fails(self) -> None:
        self.write_json_pair("result.json", {"count": 3}, {"count": 4})
        result = rerun_assertions.outputs_semantically_equal(
            self.original, str(self.rerun), ["result.json"]
        )
        self.assertEqual(result.status, "failed")

    def test_byte_stability_passes_fails_and_reports_missing_output(self) -> None:
        original = self.original / "result.bin"
        rerun = self.rerun / "result.bin"
        original.write_bytes(b"same")
        rerun.write_bytes(b"same")
        result = rerun_assertions.outputs_hash_stable(
            self.original, str(self.rerun), ["result.bin"]
        )
        self.assertEqual(result.status, "passed")

        rerun.write_bytes(b"changed")
        result = rerun_assertions.outputs_hash_stable(
            self.original, str(self.rerun), ["result.bin"]
        )
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("code"), "output_hash_changed")

        rerun.unlink()
        result = rerun_assertions.outputs_hash_stable(
            self.original, str(self.rerun), ["result.bin"]
        )
        self.assertEqual(result.status, "not_testable")
        self.assertEqual(result.data.get("code"), "output_missing")

    def test_chat_dependency_positive_negative_and_undeclared(self) -> None:
        manifest = {
            "runtime": {
                "implementation": {
                    "pipeline": "pipeline.py",
                    "dependencies": ["config.txt"],
                }
            }
        }
        (self.original / "project.yaml").write_text(
            yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8"
        )
        (self.original / "pipeline.py").write_text("print('offline')\n", encoding="utf-8")
        (self.original / "config.txt").write_text("deterministic=true\n", encoding="utf-8")
        result = rerun_assertions.no_chat_dependency(self.original)
        self.assertEqual(result.status, "passed")

        (self.original / "config.txt").write_text("chat_history=state.json\n", encoding="utf-8")
        result = rerun_assertions.no_chat_dependency(self.original)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("code"), "chat_dependency_found")

        (self.original / "project.yaml").write_text("runtime: {}\n", encoding="utf-8")
        result = rerun_assertions.no_chat_dependency(self.original)
        self.assertEqual(result.status, "not_testable")
        self.assertEqual(result.data.get("code"), "dependencies_undeclared")

    def test_validation_timestamps_hashes_and_order_are_ignored_but_status_is_not(self) -> None:
        base_checks = [
            {"id": "geometry_valid", "status": "passed", "count": 3},
            {"id": "crs_known", "status": "passed", "actual": "EPSG:3301"},
        ]
        original = {
            "status": "passed",
            "run_id": "run-one",
            "started_at": "2026-01-01T00:00:00Z",
            "outputs_hash": "sha256:one",
            "checks": base_checks,
        }
        rerun = {
            "status": "passed",
            "run_id": "run-two",
            "started_at": "2026-02-01T00:00:00Z",
            "outputs_hash": "sha256:two",
            "checks": [dict(check) for check in reversed(base_checks)],
        }
        self.write_json_pair("validation/latest-report.json", original, rerun)
        result = rerun_assertions.validation_report_reproducible(
            self.original, str(self.rerun)
        )
        self.assertEqual(result.status, "passed", result.detail)

        rerun["checks"][0]["status"] = "failed"
        self.write_json_pair("validation/latest-report.json", original, rerun)
        result = rerun_assertions.validation_report_reproducible(
            self.original, str(self.rerun)
        )
        self.assertEqual(result.status, "failed")


if __name__ == "__main__":
    unittest.main()
