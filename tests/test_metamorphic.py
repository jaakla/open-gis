"""Metamorphic relations: positive, deliberate-defect, and invalid-precondition paths.

Each relation is exercised three ways, on the same discipline as the mutation
cases: it must hold on a healthy pipeline, fail on a pipeline with exactly the
defect it exists to catch, and refuse (``not_testable`` or a declaration
failure) when its preconditions do not hold -- because a relation asserted
where it is not valid is a false failure, and a relation skipped silently is
a false pass.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from pathlib import Path

import yaml

from openmapstack.checks import metamorphic as metamorphic_checks
from openmapstack.checks.project import parameters_match_steps
from openmapstack.cli import main
from openmapstack.metamorphic import DeclarationError, parse_declaration, run_relation
from openmapstack.parameters import ParameterError, declared_parameters
from openmapstack.schema import project_schema_errors
from openmapstack.verify import verify_project
from tests.evals.helpers import make_workspace, minimal_project, write_project

# A deliberately boring pipeline: select points with x <= max_x, keyed by
# pid, deduplicated, written in pid order. ``MODE`` injects one defect.
PIPELINE = textwrap.dedent(
    '''
    import json, os, sys, time
    from pathlib import Path

    ROOT = Path(__file__).resolve().parent
    MODE = {mode!r}
    max_x = float(os.environ.get("OMS_MAX_X", "10"))
    argv = sys.argv[1:]
    if "--max-x" in argv:
        max_x = float(argv[argv.index("--max-x") + 1])
    if MODE == "crash":
        raise SystemExit(3)
    if MODE == "slow":
        time.sleep(5)
    if MODE == "reach_back":
        target = Path((ROOT / "data/source/origin.txt").read_text().strip())
        with target.open("a") as fh:
            fh.write("touched\\n")
    doc = json.loads((ROOT / "data/source/points.geojson").read_text())
    selected = {{}}
    for index, feature in enumerate(doc["features"]):
        x = feature["geometry"]["coordinates"][0]
        keep = x >= max_x if MODE == "inverted" else x <= max_x
        if not keep:
            continue
        pid = feature["properties"]["pid"]
        props = dict(feature["properties"])
        if MODE == "order_dependent":
            props["rank"] = index
        out = {{"type": "Feature", "properties": props, "geometry": feature["geometry"]}}
        if MODE == "duplicate_sensitive":
            selected[(pid, index)] = out
        else:
            selected.setdefault(pid, out)
    features = [selected[key] for key in sorted(selected, key=lambda k: str(k))]
    (ROOT / "data/derived").mkdir(parents=True, exist_ok=True)
    (ROOT / "data/derived/selected.geojson").write_text(
        json.dumps({{"type": "FeatureCollection", "features": features}})
    )
    '''
)

POINTS = {
    "type": "FeatureCollection",
    "features": [
        {"type": "Feature", "properties": {"pid": f"p{i}"}, "geometry": {"type": "Point", "coordinates": [float(i), 0.0]}}
        for i in range(1, 16)
    ],
}


def _permutation(**extra):
    declaration = {
        "id": "point-order",
        "relation": "input_permutation_invariance",
        "source": {"path": "data/source/points.geojson"},
        "outputs": ["selected"],
        "key": "pid",
        "preconditions": {"tie_break": "selection is keyed by pid; output sorted by pid"},
    }
    declaration.update(extra)
    return declaration


def _duplicates(**extra):
    declaration = {
        "id": "point-duplicates",
        "relation": "duplicate_resistance",
        "source": {"path": "data/source/points.geojson"},
        "outputs": ["selected"],
        "key": "pid",
        "preconditions": {"dedup_key": "pid", "measure": "set"},
    }
    declaration.update(extra)
    return declaration


def _monotonic(**extra):
    declaration = {
        "id": "max-x-monotonic",
        "relation": "positive_buffer_monotonicity",
        "parameter": "max_x",
        "variant": {"multiply": 1.5},
        "outputs": ["selected"],
        "key": "pid",
        "preconditions": {"predicate": "within_distance", "expected": "superset"},
    }
    declaration.update(extra)
    return declaration


def _parameter(**extra):
    parameter = {
        "id": "max_x",
        "type": "number",
        "canonical": 10,
        "binding": {"argument": "--max-x"},
        "step": "select",
        "field": "max_x",
    }
    parameter.update(extra)
    return parameter


class _ProjectMixin:
    def build(self, *, mode: str = "healthy", relations=None, parameters=None, run: bool = True):
        workspace = make_workspace()
        project = minimal_project()
        project["processing"]["steps"] = [
            {"id": "load", "operation": "read", "source": "test_source", "output": "points"},
            {"id": "select", "operation": "distance_filter", "input": "points", "max_x": 10, "crs": "EPSG:3301", "output": "selected"},
        ]
        project["outputs"] = {"selected": {"path": "data/derived/selected.geojson", "format": "GeoJSON", "generated_by": "select"}}
        project["runtime"]["implementation"]["parameters"] = parameters if parameters is not None else [_parameter()]
        project["validation"]["metamorphic"] = relations if relations is not None else []
        write_project(workspace, project)
        (workspace / "pipeline.py").write_text(PIPELINE.format(mode=mode), encoding="utf-8")
        (workspace / "data/source").mkdir(parents=True)
        (workspace / "data/source/points.geojson").write_text(json.dumps(POINTS), encoding="utf-8")
        if run:
            import subprocess, sys

            subprocess.run([sys.executable, "pipeline.py"], cwd=workspace, check=True)
        return workspace, project

    def relation(self, workspace, project, declaration):
        return run_relation(workspace, project, declaration)


class PermutationInvarianceTests(_ProjectMixin, unittest.TestCase):
    def test_holds_on_a_keyed_pipeline(self) -> None:
        workspace, project = self.build()
        result, evidence = self.relation(workspace, project, _permutation())
        self.assertEqual(result.status, "passed", result.detail)
        self.assertEqual(evidence["variant"]["transformation"], "permute_features")

    def test_detects_an_order_dependent_pipeline(self) -> None:
        workspace, project = self.build(mode="order_dependent")
        result, _ = self.relation(workspace, project, _permutation())
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data["code"], "permutation_changed_output")

    def test_requires_a_declared_tie_break_rule(self) -> None:
        workspace, project = self.build()
        result, evidence = self.relation(workspace, project, _permutation(preconditions={}))
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data["code"], "metamorphic_declaration_invalid")
        self.assertEqual(evidence["class"], "invalid")
        self.assertIn("tie_break", result.detail)

    def test_non_unique_output_key_is_not_testable(self) -> None:
        # Set semantics cannot be asserted over a key that repeats.
        workspace, project = self.build(mode="duplicate_sensitive")
        doc = json.loads((workspace / "data/source/points.geojson").read_text())
        doc["features"].append(deepcopy(doc["features"][0]))
        (workspace / "data/source/points.geojson").write_text(json.dumps(doc))
        import subprocess, sys

        subprocess.run([sys.executable, "pipeline.py"], cwd=workspace, check=True)
        result, _ = self.relation(workspace, project, _permutation())
        self.assertEqual(result.status, "not_testable")
        self.assertEqual(result.data["code"], "precondition_unmet")


class DuplicateResistanceTests(_ProjectMixin, unittest.TestCase):
    def test_holds_on_a_deduplicating_pipeline(self) -> None:
        workspace, project = self.build()
        result, evidence = self.relation(workspace, project, _duplicates())
        self.assertEqual(result.status, "passed", result.detail)
        self.assertEqual(evidence["variant"]["duplicated"], len(POINTS["features"]))

    def test_detects_a_duplicate_sensitive_pipeline(self) -> None:
        workspace, project = self.build(mode="duplicate_sensitive")
        result, _ = self.relation(workspace, project, _duplicates())
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data["code"], "duplicates_changed_output")

    def test_rejects_count_and_sum_semantics(self) -> None:
        # Counts legitimately double when rows are duplicated; declaring the
        # relation for them is invalid use, not a relation that happens to fail.
        workspace, project = self.build()
        result, _ = self.relation(
            workspace, project, _duplicates(preconditions={"dedup_key": "pid", "measure": "count"})
        )
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data["code"], "metamorphic_declaration_invalid")
        self.assertIn("set semantics", result.detail)

    def test_source_that_already_has_duplicates_is_not_testable(self) -> None:
        workspace, project = self.build(run=False)
        doc = json.loads((workspace / "data/source/points.geojson").read_text())
        doc["features"].append(deepcopy(doc["features"][0]))
        (workspace / "data/source/points.geojson").write_text(json.dumps(doc))
        import subprocess, sys

        subprocess.run([sys.executable, "pipeline.py"], cwd=workspace, check=True)
        result, _ = self.relation(workspace, project, _duplicates())
        self.assertEqual(result.status, "not_testable")
        self.assertEqual(result.data["code"], "precondition_unmet")
        self.assertIn("already has duplicate", result.detail)


class BufferMonotonicityTests(_ProjectMixin, unittest.TestCase):
    def test_holds_when_a_larger_threshold_keeps_every_baseline_feature(self) -> None:
        workspace, project = self.build()
        result, evidence = self.relation(workspace, project, _monotonic())
        self.assertEqual(result.status, "passed", result.detail)
        self.assertEqual(evidence["parameter"], {"id": "max_x", "canonical": 10, "variant": 15.0})
        self.assertEqual(evidence["command"][-2:], ["--max-x", "15"])
        self.assertEqual(evidence["counts"]["selected"], {"baseline": 10, "variant": 15})

    def test_detects_an_inverted_predicate(self) -> None:
        workspace, project = self.build(mode="inverted")
        result, _ = self.relation(workspace, project, _monotonic())
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data["code"], "monotonicity_violated")
        self.assertIn("lost", result.detail)

    def test_environment_binding_is_honoured(self) -> None:
        workspace, project = self.build(parameters=[_parameter(binding={"environment": "OMS_MAX_X"})])
        result, evidence = self.relation(workspace, project, _monotonic())
        self.assertEqual(result.status, "passed", result.detail)
        self.assertEqual(evidence["variant"]["environment"], ["OMS_MAX_X"])

    def test_undeclared_parameter_is_a_declaration_failure(self) -> None:
        workspace, project = self.build(parameters=[])
        result, _ = self.relation(workspace, project, _monotonic())
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data["code"], "metamorphic_declaration_invalid")

    def test_non_numeric_parameter_is_not_testable(self) -> None:
        workspace, project = self.build(
            parameters=[{"id": "max_x", "type": "string", "canonical": "ten", "binding": {"argument": "--max-x"}}]
        )
        result, _ = self.relation(workspace, project, _monotonic())
        self.assertEqual(result.status, "not_testable")
        self.assertEqual(result.data["code"], "precondition_unmet")

    def test_variant_must_strictly_grow(self) -> None:
        with self.assertRaises(DeclarationError):
            parse_declaration(_monotonic(variant={"multiply": 1}))
        with self.assertRaises(DeclarationError):
            parse_declaration(_monotonic(variant={"add": -5}))
        with self.assertRaises(DeclarationError):
            parse_declaration(_monotonic(preconditions={"predicate": "outside_distance"}))


class RelationSafetyTests(_ProjectMixin, unittest.TestCase):
    def test_unknown_relation_is_rejected_not_skipped(self) -> None:
        with self.assertRaises(DeclarationError):
            parse_declaration(_permutation(relation="crs_round_trip_stability"))

    def test_unsupported_source_format_is_not_testable(self) -> None:
        workspace, project = self.build()
        (workspace / "data/source/points.gpkg").write_bytes(b"not really")
        result, _ = self.relation(workspace, project, _permutation(source={"path": "data/source/points.gpkg"}))
        self.assertEqual(result.status, "not_testable")
        self.assertEqual(result.data["code"], "unsupported_format")

    def test_source_outside_immutable_trees_is_rejected(self) -> None:
        with self.assertRaises(DeclarationError):
            parse_declaration(_permutation(source={"path": "data/derived/selected.geojson"}))

    def test_oversize_source_is_a_resource_limit(self) -> None:
        workspace, project = self.build()
        result, _ = self.relation(workspace, project, _permutation(limits={"max_source_bytes": 10}))
        self.assertEqual(result.status, "not_testable")
        self.assertEqual(result.data["code"], "resource_limit")

    def test_missing_baseline_output_is_not_testable(self) -> None:
        workspace, project = self.build(run=False)
        result, _ = self.relation(workspace, project, _permutation())
        self.assertEqual(result.status, "not_testable")
        self.assertEqual(result.data["code"], "baseline_missing")

    def test_crashing_variant_is_a_failure(self) -> None:
        workspace, project = self.build()
        (workspace / "pipeline.py").write_text(PIPELINE.format(mode="crash"), encoding="utf-8")
        result, evidence = self.relation(workspace, project, _permutation())
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data["code"], "variant_execution_failed")
        self.assertIn("stderr_tail", evidence)

    def test_timeout_is_not_testable_and_the_variant_is_removed(self) -> None:
        workspace, project = self.build()
        (workspace / "pipeline.py").write_text(PIPELINE.format(mode="slow"), encoding="utf-8")
        before = {p.name for p in Path(tempfile.gettempdir()).iterdir() if p.name.startswith("openmapstack-metamorphic-")}
        result, _ = self.relation(workspace, project, _permutation(limits={"timeout_s": 0.5}))
        after = {p.name for p in Path(tempfile.gettempdir()).iterdir() if p.name.startswith("openmapstack-metamorphic-")}
        self.assertEqual(result.status, "not_testable")
        self.assertEqual(result.data["code"], "variant_timeout")
        self.assertEqual(after - before, set())

    def test_variant_that_mutates_the_original_source_fails(self) -> None:
        workspace, project = self.build()
        origin = workspace / "data/source/origin.txt"
        origin.write_text(str(workspace / "data/source/points.geojson"))
        (workspace / "pipeline.py").write_text(PIPELINE.format(mode="reach_back"), encoding="utf-8")
        result, _ = self.relation(workspace, project, _permutation())
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data["code"], "original_source_mutated")

    def test_variant_workspace_is_removed_after_success(self) -> None:
        workspace, project = self.build()
        before = {p.name for p in Path(tempfile.gettempdir()).iterdir() if p.name.startswith("openmapstack-metamorphic-")}
        self.relation(workspace, project, _permutation())
        after = {p.name for p in Path(tempfile.gettempdir()).iterdir() if p.name.startswith("openmapstack-metamorphic-")}
        self.assertEqual(after - before, set())
        # And the original workspace still has exactly what it started with.
        self.assertEqual(json.loads((workspace / "data/source/points.geojson").read_text()), POINTS)


class ParameterContractTests(_ProjectMixin, unittest.TestCase):
    def test_parameters_parse_and_bind(self) -> None:
        manifest = {"runtime": {"implementation": {"parameters": [_parameter()]}},
                    "processing": {"steps": [{"id": "select", "max_x": 10}]}}
        [parameter] = declared_parameters(manifest)
        self.assertEqual(parameter.bind(15.0), (["--max-x", "15"], {}))
        self.assertEqual(parameter.bind(12.5), (["--max-x", "12.5"], {}))

    def test_drift_between_canonical_and_step_is_rejected(self) -> None:
        manifest = {"runtime": {"implementation": {"parameters": [_parameter(canonical=2000)]}},
                    "processing": {"steps": [{"id": "select", "max_x": 10}]}}
        with self.assertRaises(ParameterError) as caught:
            declared_parameters(manifest)
        self.assertIn("!= processing step", str(caught.exception))

    def test_malformed_bindings_and_types_are_rejected(self) -> None:
        for bad in (
            _parameter(binding={}),
            _parameter(binding={"argument": "max-x"}),
            _parameter(binding={"environment": "lower"}),
            _parameter(binding={"argument": "--max-x", "environment": "OMS_MAX_X"}),
            _parameter(type="number", canonical=True),
            _parameter(type="integer", canonical=1.5),
            {k: v for k, v in _parameter().items() if k != "field"},
            _parameter(step="missing", field="max_x"),
            _parameter(id="not an identifier"),
        ):
            manifest = {"runtime": {"implementation": {"parameters": [bad]}},
                        "processing": {"steps": [{"id": "select", "max_x": 10}]}}
            with self.assertRaises(ParameterError, msg=bad):
                declared_parameters(manifest)

    def test_project_check_reports_drift_and_absence(self) -> None:
        workspace, _ = self.build(run=False)
        self.assertEqual(parameters_match_steps(workspace).status, "passed")
        workspace, _ = self.build(parameters=[_parameter(canonical=99)], run=False)
        result = parameters_match_steps(workspace)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data["code"], "parameters_invalid")
        workspace, _ = self.build(parameters=[], run=False)
        self.assertEqual(parameters_match_steps(workspace).status, "not_testable")

    def test_schema_accepts_the_contract_and_rejects_unknown_relations(self) -> None:
        _, project = self.build(relations=[_permutation(), _duplicates(), _monotonic()], run=False)
        project["runs"] = {"latest": {"id": "run-1", "started_at": "x", "completed_at": "x", "status": "passed",
                                      "inputs_hash": "sha256:" + "0" * 64, "outputs_hash": "sha256:" + "0" * 64,
                                      "validation_report": {"path": "validation/latest-report.json"}}}
        self.assertEqual(project_schema_errors(project), [])
        broken = deepcopy(project)
        broken["validation"]["metamorphic"][0]["relation"] = "subset_additivity"
        self.assertTrue(project_schema_errors(broken))
        broken = deepcopy(project)
        broken["runtime"]["implementation"]["parameters"][0]["binding"] = {"argument": "bad flag"}
        self.assertTrue(project_schema_errors(broken))


class VerifyIntegrationTests(_ProjectMixin, unittest.TestCase):
    def test_declarations_are_checked_statically_and_executed_only_on_request(self) -> None:
        workspace, _ = self.build(relations=[_permutation(), _monotonic()])
        static = verify_project(workspace / "project.yaml")
        names = [run.name for run in static.checks]
        self.assertIn("metamorphic.declarations_valid", names)
        self.assertIn("project.parameters_match_steps", names)
        self.assertNotIn("metamorphic.point-order", names)

        executed = verify_project(workspace / "project.yaml", metamorphic=True)
        by_name = {run.name: run for run in executed.checks}
        self.assertEqual(by_name["metamorphic.point-order"].result.status, "passed")
        self.assertEqual(by_name["metamorphic.max-x-monotonic"].result.status, "passed")
        payload = executed.to_dict()
        entry = next(item for item in payload["checks"] if item["check"] == "metamorphic.point-order")
        self.assertEqual(entry["evidence"]["relation"], "input_permutation_invariance")

    def test_invalid_declaration_fails_the_static_plan(self) -> None:
        workspace, _ = self.build(relations=[_duplicates(preconditions={"dedup_key": "pid", "measure": "count"})])
        result = verify_project(workspace / "project.yaml")
        run = next(r for r in result.checks if r.name == "metamorphic.declarations_valid")
        self.assertEqual(run.result.status, "failed")
        self.assertEqual(result.status, "failed")

    def test_cli_flag_runs_relations(self) -> None:
        workspace, _ = self.build(mode="inverted", relations=[_monotonic()])
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["verify", str(workspace / "project.yaml"), "--metamorphic"])
        self.assertEqual(code, 1)
        self.assertIn("FAIL metamorphic.max-x-monotonic", out.getvalue())
        self.assertIn("lost 5 baseline feature(s)", out.getvalue())

    def test_check_library_entry_matches_declared_id(self) -> None:
        workspace, _ = self.build(relations=[_permutation()])
        self.assertEqual(metamorphic_checks.relation_holds(workspace, id="point-order").status, "passed")
        missing = metamorphic_checks.relation_holds(workspace, id="nope")
        self.assertEqual(missing.status, "failed")
        self.assertEqual(missing.data["code"], "metamorphic_relation_undeclared")
        self.assertEqual(metamorphic_checks.declarations_valid(workspace).status, "passed")
        (workspace / "project.yaml").write_text(
            yaml.safe_dump(minimal_project(), sort_keys=False), encoding="utf-8"
        )
        self.assertEqual(metamorphic_checks.declarations_valid(workspace).status, "not_testable")


if __name__ == "__main__":
    unittest.main()
