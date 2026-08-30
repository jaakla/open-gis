"""Tests for `openmapstack verify`.

Every capability here gets a deliberate-defect test as well as a positive
one, on the same discipline the mutation cases enforce for the eval suite: a
check that has never been observed to fail is not evidence.
"""

from __future__ import annotations

import io
import json
import shutil
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import yaml

from openmapstack.cli import main
from openmapstack.verify import verify_project

from tests.evals.helpers import make_workspace, minimal_project, write_project

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "tartu-development"


def _status_of(result, name: str) -> str | None:
    for run in result.checks:
        if run.name == name:
            return run.result.status
    return None


class VerifyPlanTests(unittest.TestCase):
    def test_manifest_checks_run_without_any_optional_dependency(self) -> None:
        workspace = make_workspace()
        write_project(workspace, minimal_project())
        result = verify_project(workspace / "project.yaml")
        self.assertIsNotNone(_status_of(result, "project.conforms_to_schema"))
        self.assertIsNotNone(_status_of(result, "provenance.every_source_pinned"))
        self.assertIsNotNone(_status_of(result, "rerun.no_chat_dependency"))

    def test_declared_outputs_drive_the_geodata_plan(self) -> None:
        # The plan is derived from the manifest, so a project cannot opt out
        # of a check by omitting it: a declared output is a checked output.
        workspace = make_workspace()
        project = minimal_project()
        project["outputs"] = {
            "result": {"path": "data/derived/result.parquet", "format": "GeoParquet (EPSG:3301)"}
        }
        write_project(workspace, project)
        result = verify_project(workspace / "project.yaml")
        planned = [r for r in result.checks if r.args.get("path") == "data/derived/result.parquet"]
        self.assertTrue(planned, "declared output produced no geodata checks")
        self.assertIn("geodata.dataset_crs_is", {r.name for r in planned})

    def test_output_without_declared_epsg_is_not_testable_not_passed(self) -> None:
        workspace = make_workspace()
        project = minimal_project()
        project["outputs"] = {"result": {"path": "data/derived/result.parquet", "format": "GeoParquet"}}
        write_project(workspace, project)
        result = verify_project(workspace / "project.yaml")
        crs = [r for r in result.checks if r.name == "geodata.dataset_crs_is"]
        self.assertEqual(len(crs), 1)
        self.assertEqual(crs[0].result.status, "not_testable")
        self.assertEqual(crs[0].result.data.get("code"), "crs_undeclared")

    def test_unreadable_output_format_is_not_testable_not_skipped(self) -> None:
        workspace = make_workspace()
        project = minimal_project()
        project["outputs"] = {"report": {"path": "data/derived/summary.pdf", "format": "PDF"}}
        write_project(workspace, project)
        result = verify_project(workspace / "project.yaml")
        geo = [r for r in result.checks if r.args.get("path") == "data/derived/summary.pdf"]
        self.assertEqual(len(geo), 1)
        self.assertEqual(geo[0].result.status, "not_testable")
        self.assertEqual(geo[0].result.data.get("code"), "unsupported_format")

    def test_a_raising_check_is_not_testable_not_a_crash(self) -> None:
        # One broken check must not take down a report the user is relying on
        # for everything else -- but it must never read as a pass either.
        workspace = make_workspace()
        write_project(workspace, minimal_project())
        from unittest.mock import patch

        with patch(
            "openmapstack.checks.project.graph_resolves",
            side_effect=RuntimeError("boom"),
        ):
            result = verify_project(workspace / "project.yaml")
        run = next(r for r in result.checks if r.name == "project.graph_resolves")
        self.assertEqual(run.result.status, "not_testable")
        self.assertEqual(run.result.data.get("code"), "check_error")


class VerifyStatusTests(unittest.TestCase):
    def test_not_testable_alone_never_reports_passed(self) -> None:
        workspace = make_workspace()
        write_project(workspace, minimal_project())
        result = verify_project(workspace / "project.yaml")
        for run in result.checks:
            run.result.status = "not_testable"
        self.assertEqual(result.status, "not_testable")
        self.assertTrue(result.ok())
        self.assertFalse(result.ok(strict=True))

    def test_any_failure_makes_the_run_fail(self) -> None:
        workspace = make_workspace()
        write_project(workspace, minimal_project())
        result = verify_project(workspace / "project.yaml")
        result.checks[0].result.status = "failed"
        self.assertEqual(result.status, "failed")
        self.assertFalse(result.ok())


class VerifyCliTests(unittest.TestCase):
    def test_missing_project_is_exit_two_not_a_traceback(self) -> None:
        with redirect_stderr(io.StringIO()):
            self.assertEqual(main(["verify", str(make_workspace() / "absent.yaml")]), 2)

    def test_json_output_is_machine_readable_and_written(self) -> None:
        workspace = make_workspace()
        write_project(workspace, minimal_project())
        out = workspace / "report" / "verify.json"
        with redirect_stdout(io.StringIO()):
            main(["verify", str(workspace / "project.yaml"), "--json", "--output", str(out)])
        payload = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema"], "openmapstack-verify-result/v1")
        self.assertIn("counts", payload)
        self.assertTrue(payload["checks"])


@unittest.skipUnless(EXAMPLE.is_dir(), "worked example is not present")
class VerifyWorkedExampleTests(unittest.TestCase):
    """The example is the only real end-to-end project in the repository."""

    def test_geodata_checks_run_against_the_real_outputs(self) -> None:
        result = verify_project(EXAMPLE)
        geometry = [r for r in result.checks if r.name == "geodata.geometry_all_valid"]
        self.assertTrue(geometry)
        # The example writes its geometry column as `geometry`, not `geom`.
        # These checks used to hardcode `geom` and reported not_testable on
        # exactly the naming real GeoParquet uses.
        self.assertTrue(
            any(r.result.status == "passed" for r in geometry),
            [r.result.detail for r in geometry],
        )

    def test_a_corrupted_output_is_detected(self) -> None:
        workspace = make_workspace() / "project"
        shutil.copytree(EXAMPLE, workspace)
        manifest = yaml.safe_load((workspace / "project.yaml").read_text(encoding="utf-8"))
        target = next(
            spec["path"]
            for spec in manifest["outputs"].values()
            if str(spec.get("path", "")).endswith(".parquet")
        )
        (workspace / target).write_bytes(b"not a parquet file")
        result = verify_project(workspace)
        run = next(
            r for r in result.checks
            if r.name == "geodata.geometry_all_valid" and r.args.get("path") == target
        )
        self.assertNotEqual(run.result.status, "passed")

    def test_a_declared_output_that_does_not_exist_is_detected(self) -> None:
        workspace = make_workspace() / "project"
        shutil.copytree(EXAMPLE, workspace)
        manifest = yaml.safe_load((workspace / "project.yaml").read_text(encoding="utf-8"))
        target = next(iter(manifest["outputs"].values()))["path"]
        (workspace / target).unlink()
        result = verify_project(workspace)
        self.assertEqual(_status_of(result, "project.declared_files_exist"), "failed")


if __name__ == "__main__":
    unittest.main()
