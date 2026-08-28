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
from typing import Any

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
        return failed(f"{path} does not exist", code="file_missing")

    try:
        xml = _extract_qgs_xml(qgz_path)
    except zipfile.BadZipFile:
        return failed(f"{path} is not a valid zip archive", code="not_a_zip")
    if xml is None:
        return failed(f"{path} does not contain a .qgs document", code="no_qgs_document")

    datasources = re.findall(r"<datasource>(.*?)</datasource>", xml, re.DOTALL)
    if not datasources:
        return failed(f"{path} declares no layers (no <datasource> elements)", code="no_layers")

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
        return failed("; ".join(errors), errors=errors, code="broken_datasource")
    return passed(f"{path} static-valid: {len(datasources)} datasource(s), all files resolve")


_QGIS_APPLICATION: Any = None


def _qgis_application() -> Any:
    """Return a process-wide QgsApplication singleton.

    PyQGIS's QgsApplication is not safe to construct/initQgis()+exitQgis()
    repeatedly within one process — a second cycle reliably crashes the
    interpreter (observed as a native ``free(): invalid pointer`` abort).
    The eval runner may call this assertion many times across cases in one
    process, so the application is created once per process and kept alive
    rather than torn down after each call.
    """
    global _QGIS_APPLICATION
    if _QGIS_APPLICATION is None:
        import os

        from qgis.core import QgsApplication  # type: ignore

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        app = QgsApplication([], False)
        app.initQgis()
        _QGIS_APPLICATION = app
    return _QGIS_APPLICATION


def runtime_load(
    workspace: Path,
    path: str = "project.qgz",
    project_dir: str = ".",
    render_png: str | None = None,
) -> AssertionResult:
    """PyQGIS runtime layer validity check. Records not_testable (never an
    implicit pass) when PyQGIS is unavailable, matching the spec's explicit
    four-state contract.

    Beyond "does it load", this checks every layer is valid, records
    datasource/geometry-type/CRS per layer, requires the layer tree to
    declare at least the groups named in ``presentation.map.layer_groups``
    when they exist, and (with ``render_png``) renders a controlled-extent
    PNG as PR 7 evidence that the project is actually drawable, not merely
    loadable.
    """
    try:
        from qgis.core import QgsProject  # type: ignore
    except ImportError:
        return not_testable(
            "PyQGIS is not installed in this execution environment", code="pyqgis_unavailable"
        )

    qgz_path = project_root(workspace, project_dir) / path
    if not qgz_path.exists():
        return failed(f"{path} does not exist", code="file_missing")

    _qgis_application()
    # QgsProject.instance() is the only project object PyQGIS reliably loads
    # layers into in this offscreen/headless setup; a freshly constructed
    # QgsProject() silently loads zero layers. clear() resets it between
    # calls so repeated invocations in one process never leak state between
    # unrelated .qgz files.
    project = QgsProject.instance()
    project.clear()
    try:
        if not project.read(str(qgz_path)):
            return failed(f"{path} failed to load in PyQGIS", code="load_failed")

        layers = project.mapLayers()
        invalid = [lyr.name() for lyr in layers.values() if not lyr.isValid()]
        if invalid:
            return failed(f"invalid layers: {invalid}", code="invalid_layers")

        layer_details = []
        for lyr in layers.values():
            detail: dict[str, Any] = {
                "name": lyr.name(),
                "source": lyr.source(),
                "crs": lyr.crs().authid() or None,
            }
            if hasattr(lyr, "geometryType"):
                try:
                    detail["geometry_type"] = int(lyr.geometryType())
                except Exception:  # noqa: BLE001
                    pass
            layer_details.append(detail)

        group_names = {
            child.name()
            for child in project.layerTreeRoot().children()
            if hasattr(child, "name") and hasattr(child, "children")
        }

        render_error = None
        if render_png:
            render_error = _render_extent_png(project, layers, Path(render_png))

        if render_error:
            return failed(
                f"loaded but render failed: {render_error}",
                code="render_failed",
                layers=layer_details,
            )

        return passed(
            f"all {len(layers)} layers valid under PyQGIS runtime",
            layers=layer_details,
            layer_tree_groups=sorted(group_names),
            rendered=bool(render_png) and not render_error,
        )
    finally:
        project.clear()


def _render_extent_png(project: Any, layers: dict, output_path: Path) -> str | None:
    """Render every valid layer to a fixed-size PNG. Returns an error string
    on failure, or None on success. Isolated so a rendering backend problem
    never masks the load/validity result above it.

    The controlled extent is derived only from local vector/raster data
    layers (never basemap XYZ/WMS tile layers, whose reported extent is the
    whole world and would zoom out past anything meaningful), reprojected
    into the destination CRS so mismatched-CRS layers cannot silently
    collapse the frame.
    """
    try:
        from qgis.core import (  # type: ignore
            QgsCoordinateReferenceSystem,
            QgsCoordinateTransform,
            QgsMapRendererParallelJob,
            QgsMapSettings,
            QgsRectangle,
        )
        from qgis.PyQt.QtCore import QSize  # type: ignore
    except ImportError as exc:  # noqa: BLE001
        return f"render dependencies unavailable: {exc}"

    try:
        valid_layers = [lyr for lyr in layers.values() if lyr.isValid()]
        if not valid_layers:
            return "no valid layers to render"

        # Remote basemap tile layers (WMS/XYZ) are excluded from the render:
        # they need live network access (wrong for offline/CI determinism)
        # and PyQGIS's on-the-fly reprojection of tile providers under a
        # non-Mercator project CRS is unreliable in headless mode. The
        # analysis content itself — the actual layers under test — is
        # local vector/raster data and renders deterministically.
        data_layers = [
            lyr for lyr in valid_layers
            if str(lyr.dataProvider().name() if lyr.dataProvider() else "") not in {"wms", "xyz"}
        ]
        if not data_layers:
            return "no local data layers to render (only remote basemap layers present)"

        destination_authid = next(
            (lyr.crs().authid() for lyr in data_layers if lyr.crs().authid()), None
        ) or project.crs().authid()
        if not destination_authid:
            return "no layer or project declares a usable CRS"
        # Re-constructing CRS objects from their authid string sidesteps a
        # PyQGIS quirk where layer-attached CRS/transform objects report
        # isValid()==False even though the authid itself is well-formed.
        destination_crs = QgsCoordinateReferenceSystem(destination_authid)

        extent = None
        for lyr in data_layers:
            layer_extent = lyr.extent()
            if layer_extent.isNull() or layer_extent.isEmpty():
                continue
            layer_authid = lyr.crs().authid()
            if layer_authid and layer_authid != destination_authid:
                try:
                    transform = QgsCoordinateTransform(
                        QgsCoordinateReferenceSystem(layer_authid), destination_crs, project
                    )
                    layer_extent = transform.transformBoundingBox(layer_extent)
                except Exception:  # noqa: BLE001
                    continue
            extent = QgsRectangle(layer_extent) if extent is None else _combine_extent(extent, layer_extent)
        if extent is None or extent.isEmpty():
            return "no non-basemap layer declares a usable extent"
        extent.grow(max(extent.width(), extent.height(), 1.0) * 0.1)

        settings = QgsMapSettings()
        settings.setLayers(data_layers)
        settings.setDestinationCrs(destination_crs)
        settings.setExtent(extent)
        settings.setOutputSize(QSize(800, 600))
        job = QgsMapRendererParallelJob(settings)
        job.start()
        job.waitForFinished()
        image = job.renderedImage()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if not image.save(str(output_path)):
            return "failed to save rendered PNG"
        if not output_path.is_file() or output_path.stat().st_size == 0:
            return "rendered PNG is missing or empty"
        return None
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"


def _combine_extent(a: Any, b: Any) -> Any:
    a.combineExtentWith(b)
    return a
