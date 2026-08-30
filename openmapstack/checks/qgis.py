"""QGIS project static-validity assertions.

See references/project-spec.md section 5. PyQGIS runtime validation is out
of scope for fixture CI (heavy dependency); this module implements the
static minimum described in the spec — never an implicit pass when runtime
validation isn't available.
"""

from __future__ import annotations

import re
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from . import AssertionResult, failed, get_in, load_project_yaml, not_testable, passed, project_root


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
        render_target = None
        if render_png:
            # Resolve relative render paths against the project, not the
            # runner's cwd, so case definitions stay cwd-independent.
            render_target = Path(render_png)
            if not render_target.is_absolute():
                render_target = project_root(workspace, project_dir) / render_png
            render_error = _render_extent_png(project, layers, render_target)

        if render_error:
            return failed(
                f"loaded but render failed: {render_error}",
                code="render_failed",
                layers=layer_details,
            )

        # A loadable, "valid" project that paints nothing is still broken:
        # analyze the rendered snapshot for the empty-map failure mode
        # (missing layers, collapsed extent, gross CRS displacement).
        if render_target is not None:
            from .visual import _is_blank, image_stats  # local import: stdlib-only module

            try:
                stats = image_stats(render_target)
            except ValueError as exc:
                return failed(
                    f"rendered snapshot is not decodable: {exc}",
                    code="render_undecodable",
                    layers=layer_details,
                )
            if _is_blank(stats):
                return failed(
                    "project renders to a blank image "
                    f"({stats['modal_color_fraction']:.1%} one color) — layers may be missing, "
                    "the extent collapsed, or the CRS is displaced",
                    code="blank_render",
                    stats=stats,
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


def _render_extent_png(
    project: Any,
    layers: dict,
    output_path: Path,
    *,
    include_basemap: bool = True,
    extent_layers: dict | None = None,
) -> str | None:
    """Render the project to a fixed-size PNG in its own layer-tree order.
    Returns an error string on failure, or None on success. Isolated so a
    rendering backend problem never masks the load/validity result above it.

    The controlled extent is derived only from local vector/raster data
    layers (never basemap XYZ/WMS tile layers, whose reported extent is the
    whole world and would zoom out past anything meaningful), reprojected
    into the destination CRS so mismatched-CRS layers cannot silently
    collapse the frame.

    Draw order comes from the project's layer tree, not from
    ``mapLayers()``, whose ordering is an incidental artifact of layer ids:
    it sorted the opaque parcel fill above the POI markers and painted them
    out of the snapshot entirely.

    ``include_basemap=False`` renders local data only. Per-layer visibility
    comparisons use it so their result cannot turn on whether a tile server
    answered, and pass ``extent_layers`` to pin the frame to the full layer
    set: recomputing the extent from a reduced set would move the whole
    image, so every comparison would "differ" and a layer that paints
    nothing would look like one that paints.
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
        from qgis.PyQt.QtGui import QColor  # type: ignore
    except ImportError as exc:  # noqa: BLE001
        return f"render dependencies unavailable: {exc}"

    try:
        # The layer tree is the authored draw order (its first entry paints
        # on top); mapLayers() is an id-keyed mapping with no visual meaning.
        ordered = [lyr for lyr in project.layerTreeRoot().layerOrder() if lyr is not None]
        known = set(layers.values())
        ordered = [lyr for lyr in ordered if lyr in known] or list(layers.values())
        valid_layers = [lyr for lyr in ordered if lyr.isValid()]
        if not valid_layers:
            return "no valid layers to render"

        def is_tile_layer(lyr: Any) -> bool:
            return str(lyr.dataProvider().name() if lyr.dataProvider() else "") in {"wms", "xyz"}

        # The extent never comes from tile layers, whose reported extent is
        # the whole world; it comes from the local data under test.
        data_layers = [lyr for lyr in valid_layers if not is_tile_layer(lyr)]
        if not data_layers:
            return "no local data layers to render (only remote basemap layers present)"
        # The basemap is part of what the reader sees, so it belongs in the
        # snapshot -- but a tile server that does not answer must degrade to
        # a bare background, never to a failed render.
        render_layers = valid_layers if include_basemap else data_layers

        destination_authid = next(
            (lyr.crs().authid() for lyr in data_layers if lyr.crs().authid()), None
        ) or project.crs().authid()
        if not destination_authid:
            return "no layer or project declares a usable CRS"
        # Re-constructing CRS objects from their authid string sidesteps a
        # PyQGIS quirk where layer-attached CRS/transform objects report
        # isValid()==False even though the authid itself is well-formed.
        destination_crs = QgsCoordinateReferenceSystem(destination_authid)

        extent_source = data_layers
        if extent_layers is not None:
            extent_source = [
                lyr for lyr in extent_layers.values() if lyr.isValid() and not is_tile_layer(lyr)
            ] or data_layers

        extent = None
        for lyr in extent_source:
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
        settings.setLayers(render_layers)
        settings.setDestinationCrs(destination_crs)
        settings.setExtent(extent)
        settings.setOutputSize(QSize(800, 600))
        settings.setBackgroundColor(QColor(255, 255, 255))
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


def _qgs_xml(workspace: Path, path: str, project_dir: str) -> tuple[str | None, Path | None, str | None]:
    """Extracted .qgs XML plus a stable failure code when extraction is
    impossible: ``file_missing`` (caller usually reports first), or
    ``not_a_zip``. ``None`` XML with a ``None`` code means the container
    opened but holds no .qgs document (caller reports ``no_qgs_document``)."""
    qgz_path = project_root(workspace, project_dir) / path
    if not qgz_path.exists():
        return None, qgz_path, "file_missing"
    try:
        with zipfile.ZipFile(qgz_path):
            pass
    except zipfile.BadZipFile:
        return None, qgz_path, "not_a_zip"
    return _extract_qgs_xml(qgz_path), qgz_path, None


def styles_declared(workspace: Path, path: str = "project.qgz", project_dir: str = ".") -> AssertionResult:
    """Every map layer in the .qgs document declares a non-empty renderer
    (a style). A layer without one renders as an invisible default and the
    map silently shows less than the manifest claims."""
    xml, _qgz_path, error = _qgs_xml(workspace, path, project_dir)
    if error == "file_missing":
        return failed(f"{path} does not exist", code="file_missing")
    if error == "not_a_zip":
        return failed(f"{path} is not a valid zip archive", code="not_a_zip")
    if xml is None:
        return failed(f"{path} does not contain a .qgs document", code="no_qgs_document")
    maplayers = re.findall(r"<maplayer[ >].*?</maplayer>", xml, re.DOTALL)
    if not maplayers:
        return failed(f"{path} declares no map layers", code="no_layers")
    unstyled = []
    checked = 0
    for layer_xml in maplayers:
        name_match = re.search(r"<layername>(.*?)</layername>", layer_xml, re.DOTALL)
        name = name_match.group(1) if name_match else "?"
        # Raster layers (tiled basemaps) carry no renderer-v2; their styling
        # is intrinsic to the tile source.
        if re.search(r'<maplayer[^>]*type="raster"', layer_xml):
            continue
        checked += 1
        renderer = re.search(r"<renderer-v2\s([^>]*)>", layer_xml)
        if renderer is None or not renderer.group(1).strip():
            unstyled.append(name)
    if unstyled:
        return failed(
            f"map layers without a declared renderer/style: {unstyled}",
            code="missing_layer_style",
            unstyled=unstyled,
        )
    return passed(f"all {checked} vector map layers declare a renderer/style")


def _normalize_group_name(value: Any) -> str:
    """Fold a layer-group id or title to a comparable form: lowercase, with
    spaces, underscores and hyphens all treated as the same separator."""
    return re.sub(r"[\s_-]+", " ", str(value or "")).strip().lower()


def groups_match_manifest(workspace: Path, path: str = "project.qgz", project_dir: str = ".") -> AssertionResult:
    """The .qgz layer tree must mirror the manifest's
    ``presentation.map.layer_groups`` — the spec requires the QGIS project
    to be a layer-tree mirror of the web dashboard's visual hierarchy.

    A tree group matches a declared group when its ``name`` is either the
    group's ``id`` or its human ``title``, compared case- and
    separator-insensitively. QGIS layer trees are authored for readers, and
    the spec's own examples name groups by title ("Analysis Results"), so
    requiring the raw id would fail projects that follow the spec.
    """
    proj = load_project_yaml(workspace, project_dir)
    if proj is None:
        return failed("project.yaml missing", code="manifest_missing")
    declared = get_in(proj, "presentation.map.layer_groups", []) or []
    if not declared:
        return passed("manifest declares no layer groups (vacuously true)")

    xml, _qgz_path, error = _qgs_xml(workspace, path, project_dir)
    if error == "file_missing":
        return failed(f"{path} does not exist", code="file_missing")
    if error == "not_a_zip":
        return failed(f"{path} is not a valid zip archive", code="not_a_zip")
    if xml is None:
        return failed(f"{path} does not contain a .qgs document", code="no_qgs_document")
    tree_groups = re.findall(r'<layer-tree-group[^>]*\bname="([^"]+)"', xml)
    present = {_normalize_group_name(name) for name in tree_groups}
    missing = [
        group.get("id")
        for group in declared
        if not ({_normalize_group_name(group.get("id")), _normalize_group_name(group.get("title"))} - {""}) & present
    ]
    if missing:
        return failed(
            f"manifest layer groups absent from the .qgz layer tree: {missing} "
            f"(found: {tree_groups})",
            code="layer_group_missing_from_qgis",
            missing=missing,
            found=tree_groups,
        )
    return passed(f"all {len(declared)} manifest layer groups present in the .qgz layer tree")


def _is_remote_basemap_source(source: str) -> bool:
    """Tiled XYZ/WMS basemap layers (declared via presentation.map.basemap)
    are background references, not undeclared data layers.

    QGIS raster provider URIs are ``key=value`` pairs in no guaranteed
    order, so a real WMS layer starts ``crs=...`` as often as ``url=...``.
    Match on the keys that identify a remote tile/service source wherever
    they appear rather than on the first one only.
    """
    lowered = source.lower()
    if lowered.startswith(("type=xyz", "url=http")):
        return True
    return any(marker in lowered for marker in ("type=xyz", "&url=http", "contextualwmslegend", "tilematrixset="))


_OUTPUT_FORMAT_SUFFIXES = (
    "_geojson", "_parquet", "_gpkg", "_geoparquet", "_fgb", "_csv", "_pmtiles", "_json",
)


def _manifest_layer_files(proj: dict[str, Any]) -> dict[str, list[str]]:
    """Map manifest presentation layer ``source`` keys to project-relative
    file paths. A source key is either an output key (``outputs.*.path``;
    an output may have several format variants) or an override layer id
    (``overrides[].layer`` → geometry_file path)."""
    resolved: dict[str, list[str]] = {}
    for key, output in (proj.get("outputs") or {}).items():
        if not (isinstance(output, dict) and output.get("path")):
            continue
        resolved.setdefault(key, []).append(output["path"])
        # Format variants convention: the same dataset may be emitted under
        # sibling output keys like ``<key>_geojson`` / ``<key>_parquet``.
        # Only strip a known format suffix -- splitting on the last
        # underscore would alias ``education_pois`` to ``education`` and
        # could match a manifest layer that means something else entirely.
        for suffix in _OUTPUT_FORMAT_SUFFIXES:
            if key.endswith(suffix) and len(key) > len(suffix):
                resolved.setdefault(key[: -len(suffix)], []).append(output["path"])
                break
    for override in proj.get("overrides") or []:
        layer = override.get("layer")
        geometry_file = (override.get("geometry_file") or {}).get("path") if isinstance(override.get("geometry_file"), dict) else None
        if layer and geometry_file:
            resolved.setdefault(layer, []).append(geometry_file)
    return resolved


def layers_match_manifest(workspace: Path, path: str = "project.qgz", project_dir: str = ".") -> AssertionResult:
    """Runtime check (requires PyQGIS) that every layer the manifest's
    ``presentation.map.layers`` claims is actually loaded in the .qgz with a
    known CRS and the declared geometry family. Manifest claims absent from
    the QGIS product fail; extra undeclared data layers are reported as a
    warning."""
    try:
        from qgis.core import QgsProject  # type: ignore
    except ImportError:
        return not_testable(
            "PyQGIS is not installed in this execution environment", code="pyqgis_unavailable"
        )

    proj = load_project_yaml(workspace, project_dir)
    if proj is None:
        return failed("project.yaml missing", code="manifest_missing")
    manifest_layers = get_in(proj, "presentation.map.layers", []) or []
    if not manifest_layers:
        return passed("manifest declares no map layers (vacuously true)")

    qgz_path = project_root(workspace, project_dir) / path
    if not qgz_path.exists():
        return failed(f"{path} does not exist", code="file_missing")

    _qgis_application()
    project = QgsProject.instance()
    project.clear()
    try:
        if not project.read(str(qgz_path)):
            return failed(f"{path} failed to load in PyQGIS", code="load_failed")

        layers_by_file: dict[str, Any] = {}
        for lyr in project.mapLayers().values():
            source_file = str(lyr.source()).split("|", 1)[0]
            layers_by_file[Path(source_file).name] = lyr

        root = project_root(workspace, project_dir)
        expected_files = _manifest_layer_files(proj)
        errors: list[str] = []
        matched: list[str] = []
        for layer in manifest_layers:
            key = layer.get("source")
            candidates = expected_files.get(key) or []
            if not candidates:
                errors.append(
                    f"manifest layer {key!r} does not resolve to any output or override geometry file"
                )
                continue
            filenames = [Path(relative).name for relative in candidates]
            lyr = next((layers_by_file[name] for name in filenames if name in layers_by_file), None)
            if lyr is None:
                errors.append(
                    f"manifest layer {key!r} ({filenames[0]}) is not loaded in {path}"
                )
                continue
            if not lyr.isValid():
                errors.append(f"manifest layer {key!r} ({Path(lyr.source()).name}) is invalid under PyQGIS")
                continue
            if not lyr.crs().authid():
                errors.append(f"manifest layer {key!r} ({Path(lyr.source()).name}) has no resolvable CRS")
            declared_geometry = (layer.get("geometry") or "").lower()
            if declared_geometry:
                # QgsGeometryType: Point=0, Line=1, Polygon=2; multi-ness is
                # carried separately, so only the base family must match.
                expected_type = {"point": 0, "line": 1, "polygon": 2}.get(declared_geometry)
                if expected_type is not None and int(lyr.geometryType()) != expected_type:
                    errors.append(
                        f"manifest layer {key!r} declares {declared_geometry} geometry "
                        f"but the QGIS layer {Path(lyr.source()).name} is geometryType={int(lyr.geometryType())}"
                    )
            matched.append(key)

        declared_files = {
            Path(candidate).name
            for manifest_layer in manifest_layers
            for candidate in expected_files.get(manifest_layer.get("source")) or []
        }
        undeclared = sorted(
            name for name, lyr in layers_by_file.items()
            if name not in declared_files
            and not _is_remote_basemap_source(str(lyr.source()))
        )
        if errors:
            return failed("; ".join(errors), code="manifest_layer_mismatch", errors=errors, matched=matched)
        return passed(
            f"all {len(manifest_layers)} manifest layers load in {path} with resolvable CRS and matching geometry",
            matched=matched,
            undeclared_layers=undeclared,
        )
    finally:
        project.clear()


def every_declared_layer_renders(
    workspace: Path,
    path: str = "project.qgz",
    project_dir: str = ".",
) -> AssertionResult:
    """Every layer the manifest declares must contribute visible pixels to
    the rendered map, not merely load.

    `runtime_load` proves layers are valid and the render is not blank, and
    `layers_match_manifest` proves they are present with the right CRS and
    geometry -- yet a layer can satisfy all of that and still be invisible:
    buried under an opaque fill by layer-tree order, styled with zero
    opacity, or scale-limited out of the frame. That is how the reference
    project's POI markers were absent from every QGIS snapshot while all
    three checks passed.

    Each declared layer is removed from an otherwise identical render and
    the result must differ. The frame is pinned to the full layer set and
    the basemap is excluded, so the only thing that can change is the layer
    under test.
    """
    try:
        from qgis.core import QgsProject  # type: ignore
    except ImportError:
        return not_testable(
            "PyQGIS is not installed in this execution environment", code="pyqgis_unavailable"
        )

    from .visual import images_differ  # local import: stdlib-only module

    proj = load_project_yaml(workspace, project_dir)
    if proj is None:
        return failed("project.yaml missing", code="manifest_missing")
    manifest_layers = get_in(proj, "presentation.map.layers", []) or []
    if not manifest_layers:
        return passed("manifest declares no map layers (vacuously true)")

    qgz_path = project_root(workspace, project_dir) / path
    if not qgz_path.exists():
        return failed(f"{path} does not exist", code="file_missing")

    _qgis_application()
    project = QgsProject.instance()
    project.clear()
    try:
        if not project.read(str(qgz_path)):
            return failed(f"{path} failed to load in PyQGIS", code="load_failed")

        by_filename: dict[str, Any] = {}
        for lyr in project.mapLayers().values():
            by_filename[Path(str(lyr.source()).split("|", 1)[0]).name] = lyr

        expected_files = _manifest_layer_files(proj)
        targets: dict[str, Any] = {}
        for layer in manifest_layers:
            key = layer.get("source")
            for relative in expected_files.get(key) or []:
                found = by_filename.get(Path(relative).name)
                if found is not None:
                    targets[key] = found
                    break
        if not targets:
            return failed(
                "no manifest layer resolves to a layer loaded in the QGIS project",
                code="manifest_layer_mismatch",
            )

        all_layers = {lyr.id(): lyr for lyr in project.mapLayers().values()}
        with tempfile.TemporaryDirectory(prefix="openmapstack-qgis-render-") as tmp:
            tmp_dir = Path(tmp)
            baseline = tmp_dir / "baseline.png"
            error = _render_extent_png(
                project, all_layers, baseline, include_basemap=False, extent_layers=all_layers
            )
            if error:
                return failed(f"baseline render failed: {error}", code="render_failed")

            invisible: list[str] = []
            fractions: dict[str, float] = {}
            for key, target in targets.items():
                without = {lid: lyr for lid, lyr in all_layers.items() if lid != target.id()}
                candidate = tmp_dir / f"without-{target.id()}.png"
                error = _render_extent_png(
                    project, without, candidate, include_basemap=False, extent_layers=all_layers
                )
                if error:
                    return failed(
                        f"render without manifest layer {key!r} failed: {error}", code="render_failed"
                    )
                differs, fraction = images_differ(baseline, candidate)
                fractions[key] = fraction
                if not differs:
                    invisible.append(key)

        if invisible:
            return failed(
                "manifest layers load but paint nothing in the rendered map "
                f"(hidden by draw order, styling, or scale limits): {invisible}",
                code="declared_layer_not_visible",
                invisible=invisible,
                render_diff_fraction=fractions,
            )
        return passed(
            f"all {len(targets)} manifest layers contribute visible content to the rendered map",
            render_diff_fraction=fractions,
        )
    finally:
        project.clear()


def every_layer_declares_crs(
    workspace: Path,
    path: str = "project.qgz",
    project_dir: str = ".",
) -> AssertionResult:
    """Every map layer in the .qgs, raster basemaps included, must declare a
    CRS authority id.

    A layer with no declared CRS is assumed to be in the project CRS and is
    never reprojected. For a Web Mercator tile basemap in a project using a
    national grid, that silently paints the background map thousands of
    kilometres from the data: the reference project rendered Estonian
    parcels over the Belgian Ardennes while every other check passed. A
    confidently wrong map is worse than a missing one, and this is a static
    check so it gates on every PR rather than on the weekly render.
    """
    xml, _qgz_path, error = _qgs_xml(workspace, path, project_dir)
    if error == "file_missing":
        return failed(f"{path} does not exist", code="file_missing")
    if error == "not_a_zip":
        return failed(f"{path} is not a valid zip archive", code="not_a_zip")
    if xml is None:
        return failed(f"{path} does not contain a .qgs document", code="no_qgs_document")

    maplayers = re.findall(r"<maplayer[ >].*?</maplayer>", xml, re.DOTALL)
    if not maplayers:
        return failed(f"{path} declares no map layers", code="no_layers")

    missing: list[str] = []
    declared: dict[str, str] = {}
    for layer_xml in maplayers:
        name_match = re.search(r"<layername>(.*?)</layername>", layer_xml, re.DOTALL)
        name = name_match.group(1) if name_match else "?"
        authid = re.search(r"<authid>(.*?)</authid>", layer_xml, re.DOTALL)
        if authid is None or not authid.group(1).strip():
            missing.append(name)
        else:
            declared[name] = authid.group(1).strip()
    if missing:
        return failed(
            f"map layers with no declared CRS (they will be assumed to be in the project CRS "
            f"and never reprojected): {missing}",
            code="layer_crs_undeclared",
            missing=missing,
            declared=declared,
        )
    return passed(f"all {len(declared)} map layers declare a CRS", declared=declared)
