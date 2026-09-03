"""Sampled runs: the parameter contract, and the non-promotion invariant.

A sampled run exists so a wide-area analysis fails in minutes instead of
hours. The thing worth testing hardest is not that sampling works, but that a
sampled result can never be mistaken for the analysis: `runs.latest` is what
`verify`, the clean-rerun protocol, and every expectation attestation bind to,
so a sampled record reaching it would launder a smoke test into an answer.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import yaml

from openmapstack.api import describe_check, run_check
from openmapstack.cli import main
from openmapstack.parameters import ParameterError, declared_parameters
from openmapstack.sampling import (
    USE_DECLARED,
    SamplingError,
    declared_sample,
    resolve_sample,
    run_mode,
    run_record_errors,
    sampling_parameters,
)
from openmapstack.validation import validate_project
from tests.test_cli import materialize_artifacts, valid_manifest


def _parameters(*entries: dict) -> dict:
    manifest = valid_manifest()
    manifest["runtime"]["implementation"]["parameters"] = list(entries)
    return manifest


AREA = {
    "id": "sample_area",
    "type": "string",
    "canonical": "",
    "role": "sample_area",
    "sample": "26.68,58.35,26.76,58.39",
    "binding": {"argument": "--sample-area"},
}
ROWS = {
    "id": "sample_rows",
    "type": "integer",
    "canonical": 0,
    "role": "sample_rows",
    "binding": {"environment": "OMS_SAMPLE_ROWS"},
}


class SamplingParameterTests(unittest.TestCase):
    def test_role_and_sample_round_trip(self) -> None:
        parameters = declared_parameters(_parameters(AREA))
        self.assertEqual(parameters[0].role, "sample_area")
        self.assertEqual(parameters[0].sample, "26.68,58.35,26.76,58.39")

    def test_two_parameters_cannot_claim_the_same_role(self) -> None:
        second = dict(AREA, id="other_area", binding={"argument": "--other-area"})
        with self.assertRaises(ParameterError) as caught:
            declared_parameters(_parameters(AREA, second))
        self.assertIn("already claimed", str(caught.exception))

    def test_canonical_must_mean_no_sampling(self) -> None:
        """The canonical run passes nothing, so it must process the full input."""
        with self.assertRaises(ParameterError) as caught:
            declared_parameters(_parameters(dict(AREA, canonical="26.0,58.0,27.0,59.0")))
        self.assertIn("samples nothing", str(caught.exception))

    def test_sample_without_a_role_is_unaddressable(self) -> None:
        entry = {
            "id": "threshold",
            "type": "number",
            "canonical": 2000,
            "sample": 500,
            "binding": {"argument": "--threshold"},
        }
        with self.assertRaises(ParameterError) as caught:
            declared_parameters(_parameters(entry))
        self.assertIn("needs a sampling role", str(caught.exception))

    def test_sample_equal_to_canonical_is_rejected(self) -> None:
        with self.assertRaises(ParameterError) as caught:
            declared_parameters(_parameters(dict(AREA, sample="")))
        self.assertIn("would not sample anything", str(caught.exception))

    def test_a_sampling_parameter_cannot_pair_step_and_field(self) -> None:
        """Sampling selects input; it is not a processing threshold, so there is
        no step value for `parameters_match_steps` to agree with."""
        entry = dict(AREA, step="load", field="source")
        with self.assertRaises(ParameterError) as caught:
            declared_parameters(_parameters(entry))
        self.assertIn("must not pair step/field", str(caught.exception))

    def test_sampling_parameters_are_keyed_by_role(self) -> None:
        self.assertEqual(sorted(sampling_parameters(_parameters(AREA, ROWS))), ["sample_area", "sample_rows"])


class ResolveSampleTests(unittest.TestCase):
    def test_explicit_value_binds_to_the_declared_argument(self) -> None:
        sample = resolve_sample(_parameters(AREA), {"sample_area": "1,2,3,4"})
        self.assertEqual(sample.argv, ["--sample-area", "1,2,3,4"])
        self.assertEqual(sample.requested, {"sample_area": "1,2,3,4"})

    def test_declared_default_is_used_for_bare_sample(self) -> None:
        manifest = _parameters(AREA)
        sample = resolve_sample(manifest, declared_sample(manifest))
        self.assertEqual(sample.argv, ["--sample-area", "26.68,58.35,26.76,58.39"])

    def test_a_role_without_a_declared_sample_is_not_a_bare_sample_default(self) -> None:
        """ROWS declares a role but no `sample:`, so bare --sample cannot use it."""
        self.assertEqual(declared_sample(_parameters(ROWS)), {})

    def test_environment_binding_is_honoured(self) -> None:
        sample = resolve_sample(_parameters(ROWS), {"sample_rows": 500})
        self.assertEqual(sample.argv, [])
        self.assertEqual(sample.environment, {"OMS_SAMPLE_ROWS": "500"})

    def test_undeclared_role_names_what_the_manifest_must_add(self) -> None:
        with self.assertRaises(SamplingError) as caught:
            resolve_sample(_parameters(AREA), {"sample_rows": 10})
        message = str(caught.exception)
        self.assertIn("role: sample_rows", message)
        self.assertIn("--sample-rows", message)

    def test_wrong_type_is_refused(self) -> None:
        with self.assertRaises(SamplingError) as caught:
            resolve_sample(_parameters(ROWS), {"sample_rows": "many"})
        self.assertIn("must be a integer", str(caught.exception))

    def test_use_declared_without_a_declared_sample_fails(self) -> None:
        with self.assertRaises(SamplingError) as caught:
            resolve_sample(_parameters(ROWS), {"sample_rows": USE_DECLARED})
        self.assertIn("no sample value", str(caught.exception))


class RunRecordModeTests(unittest.TestCase):
    def test_a_record_without_a_mode_is_canonical(self) -> None:
        self.assertEqual(run_mode({}), "canonical")
        self.assertEqual(run_record_errors({}), [])

    def test_canonical_record_must_not_carry_a_sample(self) -> None:
        errors = run_record_errors({"mode": "canonical", "sample": {"requested": {}}})
        self.assertEqual(len(errors), 1)
        self.assertIn("must not carry a sample descriptor", errors[0])

    def test_sampled_record_must_record_what_it_realized(self) -> None:
        """A requested fraction is a request; TABLESAMPLE only approximates it."""
        errors = run_record_errors(
            {"mode": "sampled", "sample": {"requested": {"sample_fraction": 1.0}}}
        )
        self.assertEqual(len(errors), 1)
        self.assertIn("sample.realized", errors[0])

    def test_a_complete_sampled_record_is_accepted(self) -> None:
        self.assertEqual(
            run_record_errors(
                {
                    "mode": "sampled",
                    "sample": {
                        "requested": {"sample_fraction": 1.0},
                        "realized": {"rows": 987, "resolution_m": 30},
                        "scale_factor": 0.0125,
                    },
                }
            ),
            [],
        )

    def test_impossible_scale_factor_is_rejected(self) -> None:
        errors = run_record_errors(
            {
                "mode": "sampled",
                "sample": {"requested": {"sample_rows": 10}, "realized": {"rows": 10}, "scale_factor": 4},
            }
        )
        self.assertIn("within (0, 1]", errors[0])

    def test_unknown_mode_is_rejected(self) -> None:
        self.assertIn("invalid run mode", run_record_errors({"mode": "moist"})[0])


class SampleIsolationTests(unittest.TestCase):
    """`runs.latest` is the canonical baseline; a sampled run may never hold it."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="openmapstack-sampling-test-")
        self.root = Path(self.tempdir.name)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _project(self) -> Path:
        path = self.root / "project.yaml"
        path.write_text(yaml.safe_dump(valid_manifest(), sort_keys=False), encoding="utf-8")
        (self.root / "README.md").write_text("# Test project\n", encoding="utf-8")
        (self.root / "pipeline.py").write_text("pass\n", encoding="utf-8")
        materialize_artifacts(self.root)
        return path

    def _check(self, path: Path, name: str = "runs.sample_isolation"):
        result = validate_project(path)
        return next(check for check in result.checks if check.id == name)

    def _add_sampled_record(self, run_id: str, sample: dict | None = None) -> Path:
        record = {
            "run_id": run_id,
            "started_at": "2026-08-26T00:00:00Z",
            "completed_at": "2026-08-26T00:00:01Z",
            "status": "passed",
            "mode": "sampled",
            "sample": sample
            if sample is not None
            else {"requested": {"sample_area": "1,2,3,4"}, "realized": {"rows": 12}},
        }
        target = self.root / "runs" / f"{run_id}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(record), encoding="utf-8")
        return target

    def test_a_sampled_record_beside_the_canonical_one_is_fine(self) -> None:
        path = self._project()
        self._add_sampled_record("run-20260826-120000")
        check = self._check(path)
        self.assertEqual(check.status, "passed", check.message)

    def test_a_sampled_run_promoted_to_runs_latest_fails(self) -> None:
        path = self._project()
        manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
        promoted = manifest["runs"]["latest"]["id"]
        record = json.loads((self.root / "runs" / f"{promoted}.json").read_text(encoding="utf-8"))
        record["mode"] = "sampled"
        record["sample"] = {"requested": {"sample_area": "1,2,3,4"}, "realized": {"rows": 12}}
        (self.root / "runs" / f"{promoted}.json").write_text(json.dumps(record), encoding="utf-8")
        check = self._check(path)
        self.assertEqual(check.status, "failed")
        self.assertIn("cannot be the canonical run", check.message)

    def test_a_sampled_record_stating_only_its_request_fails(self) -> None:
        path = self._project()
        self._add_sampled_record(
            "run-20260826-120000", sample={"requested": {"sample_fraction": 1.0}}
        )
        check = self._check(path)
        self.assertEqual(check.status, "failed")
        self.assertIn("sample.realized", check.message)

    def test_a_project_with_no_sampled_records_passes(self) -> None:
        check = self._check(self._project())
        self.assertEqual(check.status, "passed")
        self.assertIn("no sampled run records", check.message)

    # -- the same invariant through the public check API -------------------

    def test_check_api_exposes_the_invariant(self) -> None:
        descriptor = describe_check("validation.sample_run_not_promoted")
        self.assertEqual(descriptor.dimension, "validation_integrity")
        self.assertTrue(descriptor.oracle_free)

    def test_check_api_reports_a_promoted_sampled_run(self) -> None:
        path = self._project()
        manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
        promoted = manifest["runs"]["latest"]["id"]
        record = json.loads((self.root / "runs" / f"{promoted}.json").read_text(encoding="utf-8"))
        record["mode"] = "sampled"
        record["sample"] = {"requested": {"sample_area": "1,2,3,4"}, "realized": {"rows": 12}}
        (self.root / "runs" / f"{promoted}.json").write_text(json.dumps(record), encoding="utf-8")
        result = run_check("validation.sample_run_not_promoted", self.root)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["code"], "sampled_run_promoted")

    def test_check_api_passes_a_healthy_project(self) -> None:
        self._project()
        self._add_sampled_record("run-20260826-120000")
        result = run_check("validation.sample_run_not_promoted", self.root)
        self.assertEqual(result["status"], "passed")

    def test_check_api_pins_a_code_for_an_unrealized_sample(self) -> None:
        """External harnesses grade on the code, so it is part of the contract."""
        self._project()
        self._add_sampled_record(
            "run-20260826-120000", sample={"requested": {"sample_rows": 100}}
        )
        result = run_check("validation.sample_run_not_promoted", self.root)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["code"], "sample_record_invalid")

    def test_check_api_is_not_testable_without_run_records(self) -> None:
        """Absent evidence is never an implicit pass."""
        path = self._project()
        for record in (self.root / "runs").glob("*.json"):
            record.unlink()
        (self.root / "runs").rmdir()
        result = run_check("validation.sample_run_not_promoted", self.root)
        self.assertEqual(result["status"], "not_testable")
        self.assertEqual(result["code"], "runs_dir_missing")
        self.assertTrue(path.is_file())


SAMPLING_PIPELINE = """\
import json
import os
import sys
from pathlib import Path

root = Path(__file__).resolve().parent
argv = sys.argv[1:]
area = argv[argv.index("--sample-area") + 1] if "--sample-area" in argv else None
sampled = area is not None

(root / "received.json").write_text(json.dumps({
    "argv": argv,
    "run_mode": os.environ.get("OPENMAPSTACK_RUN_MODE"),
}), encoding="utf-8")

output = root / "data" / "derived" / "candidate.json"
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(json.dumps({"type": "FeatureCollection", "features": [], "area": area}), encoding="utf-8")

if sampled:
    run_id = "run-20260826-120000"
    record = {
        "run_id": run_id,
        "started_at": "2026-08-26T12:00:00Z",
        "completed_at": "2026-08-26T12:00:01Z",
        "status": "passed",
        "mode": "sampled",
        "sample": {"requested": {"sample_area": area}},
        "environment": {"python": "test"},
    }
    if os.environ.get("EVAL_BAD_SAMPLE") != "1":
        record["sample"]["realized"] = {"rows": 3, "bbox": area}
        record["sample"]["scale_factor"] = 0.01
    run_path = root / "runs" / (run_id + ".json")
    run_path.parent.mkdir(parents=True, exist_ok=True)
    run_path.write_text(json.dumps(record), encoding="utf-8")
    if os.environ.get("EVAL_PROMOTE_SAMPLE") == "1":
        import yaml
        manifest_path = root / "project.yaml"
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        manifest["runs"]["latest"]["id"] = run_id
        manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
"""


class SampledRunCliTests(unittest.TestCase):
    """`openmapstack run --sample*`: bind the knob, then refuse promotion."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="openmapstack-sample-cli-")
        self.root = Path(self.tempdir.name)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _project(self, *parameters: dict) -> Path:
        manifest = valid_manifest()
        if parameters:
            manifest["runtime"]["implementation"]["parameters"] = list(parameters)
        path = self.root / "project.yaml"
        path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
        (self.root / "README.md").write_text("# Test project\n", encoding="utf-8")
        (self.root / "pipeline.py").write_text(SAMPLING_PIPELINE, encoding="utf-8")
        materialize_artifacts(self.root)
        return path

    def _run(self, argv: list[str]) -> tuple[int, dict]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(argv + ["--json"])
        return code, json.loads(stdout.getvalue())

    def _run_with_env(self, argv: list[str], **environment: str) -> tuple[int, dict]:
        """Run with extra variables the fixture pipeline reads to misbehave."""
        previous = {key: os.environ.get(key) for key in environment}
        os.environ.update(environment)
        try:
            return self._run(argv)
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_explicit_sample_area_reaches_the_pipeline(self) -> None:
        path = self._project(AREA)
        code, payload = self._run(["run", str(path), "--sample-area", "1,2,3,4"])
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["mode"], "sampled")
        self.assertEqual(payload["sample"]["requested"], {"sample_area": "1,2,3,4"})
        received = json.loads((self.root / "received.json").read_text(encoding="utf-8"))
        self.assertEqual(received["argv"], ["--sample-area", "1,2,3,4"])
        self.assertEqual(received["run_mode"], "sampled")

    def test_bare_sample_uses_the_declared_value(self) -> None:
        path = self._project(AREA)
        code, payload = self._run(["run", str(path), "--sample"])
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["sample"]["requested"], {"sample_area": AREA["sample"]})

    def test_a_canonical_run_passes_nothing(self) -> None:
        """Declaring a sampling parameter must not leak into the canonical run:
        no argument, no environment variable, no sample descriptor."""
        path = self._project(AREA)
        _, payload = self._run(["run", str(path)])
        self.assertEqual(payload["mode"], "canonical")
        self.assertNotIn("sample", payload)
        received = json.loads((self.root / "received.json").read_text(encoding="utf-8"))
        self.assertEqual(received["argv"], [])
        self.assertIsNone(received["run_mode"])

    def test_sampling_a_manifest_that_declares_no_role_is_refused(self) -> None:
        path = self._project()
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["run", str(path), "--sample-area", "1,2,3,4"])
        self.assertEqual(code, 2)
        self.assertIn("role: sample_area", stderr.getvalue())
        self.assertFalse((self.root / "received.json").exists())

    def test_bare_sample_without_a_declared_default_is_refused(self) -> None:
        path = self._project(ROWS)
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["run", str(path), "--sample"])
        self.assertEqual(code, 2)
        self.assertIn("--sample needs", stderr.getvalue())

    def test_dry_run_shows_the_sampled_command_without_executing(self) -> None:
        path = self._project(AREA)
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = main(["run", str(path), "--sample", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("Would run (sampled):", stdout.getvalue())
        self.assertIn(AREA["sample"], stdout.getvalue())
        self.assertFalse((self.root / "received.json").exists())

    def test_a_pipeline_that_promotes_its_sampled_run_fails_the_command(self) -> None:
        """The strongest guard: it does not rely on the pipeline behaving."""
        path = self._project(AREA)
        code, payload = self._run_with_env(
            ["run", str(path), "--sample-area", "1,2,3,4"], EVAL_PROMOTE_SAMPLE="1"
        )
        self.assertEqual(code, 1)
        self.assertEqual(payload["status"], "failed")
        self.assertTrue(payload["promotion_problems"])
        self.assertIn("cannot become the canonical run", payload["promotion_problems"][0])

    def test_clobbering_the_declared_outputs_is_reported(self) -> None:
        """A sampled run that writes in place leaves the canonical outputs stale;
        say so here rather than letting it surface as a hash mismatch later."""
        path = self._project(AREA)
        code, payload = self._run(["run", str(path), "--sample-area", "1,2,3,4"])
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["canonical_outputs_overwritten"], ["data/derived/candidate.json"])

    def test_strict_turns_a_clobbered_output_into_a_failure(self) -> None:
        path = self._project(AREA)
        code, payload = self._run(["run", str(path), "--sample-area", "1,2,3,4", "--strict"])
        self.assertEqual(code, 1)
        self.assertEqual(payload["canonical_outputs_overwritten"], ["data/derived/candidate.json"])

    def test_a_record_stating_only_its_request_fails_the_command(self) -> None:
        """A requested fraction is a request; the record must say what it got."""
        path = self._project(AREA)
        code, payload = self._run_with_env(
            ["run", str(path), "--sample-area", "1,2,3,4"], EVAL_BAD_SAMPLE="1"
        )
        self.assertEqual(code, 1)
        self.assertTrue(any("sample.realized" in problem for problem in payload["promotion_problems"]))

    def test_a_sampled_run_leaves_runs_latest_alone(self) -> None:
        path = self._project(AREA)
        before = yaml.safe_load(path.read_text(encoding="utf-8"))["runs"]["latest"]["id"]
        self._run(["run", str(path), "--sample-area", "1,2,3,4"])
        after = yaml.safe_load(path.read_text(encoding="utf-8"))["runs"]["latest"]["id"]
        self.assertEqual(before, after)
        self.assertTrue((self.root / "runs" / "run-20260826-120000.json").is_file())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
