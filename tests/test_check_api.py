"""The versioned check API an external harness (OpenMapBench) consumes.

``ConsumerFixtureTests`` plays the role of that harness: it uses only the
public API, the CLI, and the packaged JSON schemas -- never
``openmapstack.checks`` directly -- and proves it can list, negotiate, run,
and validate a result without vendoring a single check.
"""

from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from openmapstack import __version__, api
from openmapstack.cli import main
from openmapstack.schema import validation_errors
from openmapstack.verify import verify_project
from tests.evals.helpers import make_workspace, minimal_project, write_project

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = REPO_ROOT / "openmapstack" / "schemas"


def _schema(name: str) -> dict:
    return json.loads((SCHEMAS / name).read_text(encoding="utf-8"))


class CatalogueTests(unittest.TestCase):
    def test_every_public_check_is_listed_with_its_parameters(self) -> None:
        names = {descriptor.name for descriptor in api.list_checks()}
        for expected in (
            "project.conforms_to_schema", "provenance.every_source_pinned", "geodata.geometry_all_valid",
            "validation.run_record_matches", "qgis.static_valid", "rerun.clean_execution_succeeded",
            "metamorphic.relation_holds", "presentation.controls_match_pipeline",
        ):
            self.assertIn(expected, names)
        descriptor = api.describe_check("geodata.dataset_crs_is")
        by_name = {parameter.name: parameter for parameter in descriptor.parameters}
        self.assertTrue(by_name["path"].required)
        self.assertTrue(by_name["expected"].required)
        self.assertFalse(by_name["project_dir"].required)
        self.assertEqual(by_name["project_dir"].default, ".")

    def test_known_answer_checks_are_marked_and_everything_else_is_oracle_free(self) -> None:
        catalogue = {descriptor.name: descriptor for descriptor in api.list_checks()}
        for name in api.KNOWN_ANSWER_CHECKS:
            self.assertFalse(catalogue[name].oracle_free, name)
        self.assertTrue(catalogue["geodata.geometry_all_valid"].oracle_free)
        self.assertEqual(catalogue["metamorphic.relation_holds"].dimension, "metamorphic_evidence")
        self.assertEqual(catalogue["visual.render_substantive"].dimension, "visual_judgement")
        self.assertEqual(catalogue["geodata.row_count"].dimension, "gis_correctness")

    def test_private_helpers_are_not_exposed(self) -> None:
        names = {descriptor.name for descriptor in api.list_checks()}
        self.assertFalse(any(".‗" in name or "._" in name for name in names))
        with self.assertRaises(api.CheckAPIError):
            api.describe_check("geodata._read")
        with self.assertRaises(api.CheckAPIError):
            api.describe_check("spatial.connect_spatial")

    def test_eval_runner_uses_the_api_dimensions(self) -> None:
        spec = importlib.util.spec_from_file_location("openmapstack_eval_runner_api_test", REPO_ROOT / "evals" / "run.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        self.assertIs(module.DIMENSIONS, api.DIMENSIONS)


class RunCheckTests(unittest.TestCase):
    def test_result_validates_and_carries_stable_codes(self) -> None:
        workspace = make_workspace()
        project = minimal_project()
        project["sources"]["test_source"]["version"] = {"identifier": "latest"}
        write_project(workspace, project)
        record = api.run_check("provenance.every_source_pinned", workspace)
        self.assertEqual(validation_errors(record, _schema("check-result-v1.schema.json")), [])
        self.assertEqual((record["status"], record["code"]), ("failed", "source_unpinned"))
        self.assertEqual(record["dimension"], "provenance")
        self.assertEqual(record["api_version"], api.CHECK_API_VERSION)

    def test_passed_results_have_no_code(self) -> None:
        workspace = make_workspace()
        write_project(workspace, minimal_project())
        record = api.run_check("project.parses", workspace)
        self.assertEqual(record["status"], "passed")
        self.assertIsNone(record["code"])
        self.assertEqual(validation_errors(record, _schema("check-result-v1.schema.json")), [])

    def test_unknown_check_and_bad_args_are_consumer_errors(self) -> None:
        workspace = make_workspace()
        with self.assertRaises(api.CheckAPIError):
            api.run_check("geodata.nope", workspace)
        with self.assertRaises(api.CheckAPIError):
            api.run_check("geodata.dataset_crs_is", workspace, {"path": "x"})  # missing expected
        with self.assertRaises(api.CheckAPIError):
            api.run_check("project.parses", workspace, {"bogus": 1})

    def test_a_raising_check_is_not_testable_never_passed(self) -> None:
        from unittest.mock import patch

        workspace = make_workspace()
        write_project(workspace, minimal_project())
        def boom(workspace, project_dir="."):
            raise RuntimeError("boom")

        boom.__module__ = "openmapstack.checks.project"
        with patch("openmapstack.checks.project.graph_resolves", new=boom):
            record = api.run_check("project.graph_resolves", workspace)
        self.assertEqual((record["status"], record["code"]), ("not_testable", "check_error"))
        self.assertEqual(validation_errors(record, _schema("check-result-v1.schema.json")), [])


class NegotiationTests(unittest.TestCase):
    def test_compatible_when_api_version_and_checks_match(self) -> None:
        answer = api.negotiate(min_package_version="0.1.0", required_checks=["project.parses"])
        self.assertTrue(answer["compatible"], answer)

    def test_incompatible_answers_say_why(self) -> None:
        answer = api.negotiate(required_api="openmapstack-check-api/v2", min_package_version="99.0.0", required_checks=["geodata.magic"])
        self.assertFalse(answer["compatible"])
        self.assertEqual(len(answer["problems"]), 3)
        with self.assertRaises(api.CheckAPIError):
            api.negotiate(min_package_version="latest")

    def test_api_info_describes_the_installation(self) -> None:
        info = api.api_info()
        self.assertEqual(info["schema"], api.API_INFO_SCHEMA)
        self.assertEqual(info["package_version"], __version__)
        self.assertEqual(info["statuses"], ["passed", "failed", "warning", "not_testable"])
        self.assertIn("metamorphic_evidence", info["dimensions"])


class VerifyResultSchemaTests(unittest.TestCase):
    def test_verify_json_validates_against_the_packaged_schema(self) -> None:
        workspace = make_workspace()
        write_project(workspace, minimal_project())
        payload = verify_project(workspace / "project.yaml").to_dict()
        self.assertEqual(api.validate_verify_result(payload), [])


class ConsumerFixtureTests(unittest.TestCase):
    """A harness that vendors nothing: CLI + JSON schemas only."""

    def _cli(self, *argv: str) -> tuple[int, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(list(argv))
        return code, out.getvalue()

    def test_list_negotiate_run_validate(self) -> None:
        code, text = self._cli("api-info", "--json", "--require-api", api.CHECK_API_VERSION, "--min-version", "0.2.0", "--require-check", "validation.run_record_matches")
        self.assertEqual(code, 0, text)
        info = json.loads(text)
        self.assertTrue(info["negotiation"]["compatible"])

        code, text = self._cli("checks", "--json")
        self.assertEqual(code, 0)
        catalogue = json.loads(text)
        names = {entry["name"] for entry in catalogue["checks"]}
        self.assertIn("geodata.dataset_crs_is", names)

        workspace = make_workspace()
        project = minimal_project()
        project["outputs"] = {"final": {"path": "data/derived/final.json", "format": "GeoJSON", "generated_by": "export"}}
        write_project(workspace, project)
        code, text = self._cli("check", "project.declared_files_exist", str(workspace), "--arg", 'files=["data/derived/final.json"]', "--json")
        record = json.loads(text)
        self.assertEqual(code, 1)
        self.assertEqual(record["status"], "failed")
        self.assertEqual(validation_errors(record, _schema("check-result-v1.schema.json")), [])

        code, text = self._cli("api-info", "--json", "--require-api", "openmapstack-check-api/v9")
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(text)["negotiation"]["compatible"])

    def test_consumer_error_is_exit_two_not_a_graded_result(self) -> None:
        code, text = self._cli("check", "geodata.nope", str(make_workspace()), "--json")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(text)["code"], "consumer_error")

    def test_the_cli_is_reachable_as_a_subprocess(self) -> None:
        completed = subprocess.run([sys.executable, "-m", "openmapstack", "api-info", "--json"], capture_output=True, text=True, cwd=REPO_ROOT, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["check_api_version"], api.CHECK_API_VERSION)


if __name__ == "__main__":
    unittest.main()
