from __future__ import annotations

import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from .helpers import make_workspace

from openmapstack.checks import qgis as qgis_assertions  # noqa: E402


def _write_qgz(path, datasources, extra_xml=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(f"<datasource>{ds}</datasource>" for ds in datasources)
    xml = f'<?xml version="1.0"?><qgis><projectlayers>{body}</projectlayers>{extra_xml}</qgis>'
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("project.qgs", xml)


class StaticValidTests(unittest.TestCase):
    def test_valid_relative_datasource_passes(self) -> None:
        workspace = make_workspace()
        (workspace / "data").mkdir()
        (workspace / "data" / "layer.geojson").write_text("{}", encoding="utf-8")
        _write_qgz(workspace / "project.qgz", ["./data/layer.geojson"])
        result = qgis_assertions.static_valid(workspace)
        self.assertEqual(result.status, "passed")

    def test_missing_file_fails(self) -> None:
        workspace = make_workspace()
        result = qgis_assertions.static_valid(workspace)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("code"), "file_missing")

    def test_not_a_zip_fails(self) -> None:
        workspace = make_workspace()
        (workspace / "project.qgz").write_text("not a zip", encoding="utf-8")
        result = qgis_assertions.static_valid(workspace)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("code"), "not_a_zip")

    def test_no_qgs_document_fails(self) -> None:
        workspace = make_workspace()
        with zipfile.ZipFile(workspace / "project.qgz", "w") as zf:
            zf.writestr("readme.txt", "hello")
        result = qgis_assertions.static_valid(workspace)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("code"), "no_qgs_document")

    def test_no_layers_fails(self) -> None:
        workspace = make_workspace()
        _write_qgz(workspace / "project.qgz", [])
        result = qgis_assertions.static_valid(workspace)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("code"), "no_layers")

    def test_broken_datasource_path_fails(self) -> None:
        workspace = make_workspace()
        _write_qgz(workspace / "project.qgz", ["./data/does-not-exist.geojson"])
        result = qgis_assertions.static_valid(workspace)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("code"), "broken_datasource")

    def test_geopackage_missing_layername_fails(self) -> None:
        workspace = make_workspace()
        (workspace / "data.gpkg").write_text("fake", encoding="utf-8")
        _write_qgz(workspace / "project.qgz", ["./data.gpkg"])
        result = qgis_assertions.static_valid(workspace)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("code"), "broken_datasource")
        self.assertIn("layername=", result.detail)

    def test_remote_wms_datasource_is_skipped(self) -> None:
        workspace = make_workspace()
        _write_qgz(workspace / "project.qgz", ["crs=EPSG:3301&url=https://example.invalid/wms"])
        result = qgis_assertions.static_valid(workspace)
        self.assertEqual(result.status, "passed")


class RuntimeLoadUnavailableTests(unittest.TestCase):
    def test_missing_pyqgis_is_not_testable(self) -> None:
        workspace = make_workspace()
        _write_qgz(workspace / "project.qgz", ["./missing.geojson"])
        with patch.dict("sys.modules", {"qgis": None, "qgis.core": None}):
            result = qgis_assertions.runtime_load(workspace)
        self.assertEqual(result.status, "not_testable")
        self.assertEqual(result.data.get("code"), "pyqgis_unavailable")


def _pyqgis_available() -> bool:
    try:
        import qgis.core  # noqa: F401
    except ImportError:
        return False
    return True


@unittest.skipUnless(
    _pyqgis_available(),
    "PyQGIS is not importable in this interpreter; see evals/README.md for "
    "the micromamba-based QGIS environment used to exercise this path manually",
)
class RuntimeLoadWithRealPyqgisTests(unittest.TestCase):
    """Only runs when PyQGIS is importable in the active interpreter (e.g. a
    `micromamba run -n qgis python -m unittest ...` invocation). Never runs
    in default fixture CI, which has no PyQGIS dependency.

    Uses the committed examples/tartu-development worked example rather
    than a hand-rolled minimal .qgs: PyQGIS requires the full real
    <maplayers> project structure QGIS itself writes to actually resolve
    layers, not the minimal <datasource>-only XML the static regex check
    in this module accepts.
    """

    WORKED_EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "tartu-development"

    def test_real_worked_example_loads_and_reports_layers(self) -> None:
        if not (self.WORKED_EXAMPLE / "project.qgz").is_file():
            self.skipTest("examples/tartu-development/project.qgz is a regenerable artifact; run run_e2e.py first")
        result = qgis_assertions.runtime_load(self.WORKED_EXAMPLE)
        self.assertEqual(result.status, "passed", result.detail)
        self.assertGreater(len(result.data.get("layers", [])), 0)

    def test_broken_datasource_reports_invalid_layers(self) -> None:
        workspace = make_workspace()
        real_qgz = self.WORKED_EXAMPLE / "project.qgz"
        if not real_qgz.is_file():
            self.skipTest("examples/tartu-development/project.qgz is a regenerable artifact; run run_e2e.py first")
        with zipfile.ZipFile(real_qgz) as source_zip:
            qgs_xml = source_zip.read("project.qgs").decode("utf-8")
        # Break every real datasource path so PyQGIS reports invalid layers,
        # without needing to hand-author a full project structure.
        broken_xml = qgs_xml.replace("./data/", "./nonexistent-data/")
        with zipfile.ZipFile(workspace / "project.qgz", "w") as zf:
            zf.writestr("project.qgs", broken_xml)
        result = qgis_assertions.runtime_load(workspace)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("code"), "invalid_layers")


if __name__ == "__main__":
    unittest.main()
