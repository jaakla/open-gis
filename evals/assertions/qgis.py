"""QGIS project static-validity assertions.

See references/project-spec.md section 5. PyQGIS runtime validation is out
of scope for fixture CI (heavy dependency); this module implements the
static minimum described in the spec — never an implicit pass when runtime
validation isn't available.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

from . import AssertionResult, failed, not_testable, passed, project_root


def _extract_qgs_xml(qgz_path: Path) -> str | None:
    if qgz_path.suffix == ".qgs":
        return qgz_path.read_text(encoding="utf-8", errors="ignore")
    if qgz_path.suffix == ".qgz":
        with zipfile.ZipFile(qgz_path) as zf:
            qgs_names = [n for n in zf.namelist() if n.endswith(".qgs")]
            if not qgs_names:
                return None
            return zf.read(qgs_names[0]).decode("utf-8", errors="ignore")
    return None


def static_valid(workspace: Path, path: str = "project.qgz", project_dir: str = ".") -> AssertionResult:
    """The .qgz opens as a zip containing a .qgs, every <datasource> referencing
    a relative file path resolves on disk, and GeoPackage datasources declare
    a layername= (otherwise GDAL silently loads a non-spatial attribute table)."""
    qgz_path = project_root(workspace, project_dir) / path
    if not qgz_path.exists():
        return failed(f"{path} does not exist")

    try:
        xml = _extract_qgs_xml(qgz_path)
    except zipfile.BadZipFile:
        return failed(f"{path} is not a valid zip archive")
    if xml is None:
        return failed(f"{path} does not contain a .qgs document")

    datasources = re.findall(r"<datasource>(.*?)</datasource>", xml, re.DOTALL)
    if not datasources:
        return failed(f"{path} declares no layers (no <datasource> elements)")

    errors: list[str] = []
    root = project_root(workspace, project_dir)
    for ds in datasources:
        ds = ds.strip()
        # remote/WMS/WFS datasources use key=value query strings, not file paths.
        if ds.startswith(("http", "type=xyz", "contextualWMSLegend", "crs=")) or "url=" in ds:
            continue
        raw_path = ds.split("|", 1)[0]
        if raw_path.lower().endswith(".gpkg") and "layername=" not in ds:
            errors.append(f"GeoPackage datasource missing layername=: {ds}")
        resolved = (root / raw_path).resolve() if raw_path.startswith("./") or not raw_path.startswith("/") else Path(raw_path)
        if raw_path and not str(raw_path).startswith(("http",)) and not resolved.exists():
            errors.append(f"datasource file does not exist: {raw_path}")

    if errors:
        return failed("; ".join(errors), errors=errors)
    return passed(f"{path} static-valid: {len(datasources)} datasource(s), all files resolve")


def runtime_load(workspace: Path, path: str = "project.qgz", project_dir: str = ".") -> AssertionResult:
    """PyQGIS runtime layer validity check. Records not_testable (never an
    implicit pass) when PyQGIS is unavailable, matching the spec's explicit
    four-state contract."""
    try:
        from qgis.core import QgsApplication, QgsProject  # type: ignore
    except ImportError:
        return not_testable("PyQGIS is not installed in this execution environment")

    qgz_path = project_root(workspace, project_dir) / path
    if not qgz_path.exists():
        return failed(f"{path} does not exist")

    qgs = QgsApplication([], False)
    qgs.initQgis()
    try:
        project = QgsProject.instance()
        if not project.read(str(qgz_path)):
            return failed(f"{path} failed to load in PyQGIS")
        invalid = [lyr.name() for lyr in project.mapLayers().values() if not lyr.isValid()]
        if invalid:
            return failed(f"invalid layers: {invalid}")
        return passed(f"all {len(project.mapLayers())} layers valid under PyQGIS runtime")
    finally:
        qgs.exitQgis()
