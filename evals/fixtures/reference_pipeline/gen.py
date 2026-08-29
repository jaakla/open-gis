#!/usr/bin/env python3
"""Deterministic reference pipeline used to generate/regenerate committed
eval fixtures under evals/cases/*/project/.

This is intentionally small (a minimal, real `openmapstack-project/v1` project)
so eval cases can assert against genuinely executed pipeline output rather
than hand-typed JSON. Generator mode reads the checked-in mini-Tartu fixture.
The copied `pipeline.py` mode reads only the produced project's own
`data/source/` and `data/overrides/`, so it remains runnable after the eval
generator and original conversation are unavailable.

Usage:
    python gen.py <output_dir> [--no-override] [--break=<bug-name>]

--break selects a deliberate defect for negative/adversarial cases:
    hallucinated_feature       add a candidate with fabricated geometry/id
    wrong_crs                  declare analysis_crs as EPSG:4326
    completeness_unknown       (via --uncertain-completeness) POI source completeness cannot be established
    dangling_graph             a step consumes a symbol nothing produces
    override_not_applied       override declared but not reflected in output
    override_from_mismatch     override asserts a `from` value source doesn't have
    unpinned_source            source version.identifier == "latest"
    validation_laundering      drop a required check from the report
    dashboard_only             omit the declared canonical pipeline.py
    qgis_broken_datasource     project.qgz references a missing file
    incomplete_pagination      roads source reports numberMatched > returned
    mutated_source              pipeline rewrites its own copied "immutable" source file
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import platform
import shutil
import sys
import zipfile
from pathlib import Path

import duckdb
import yaml

HERE = Path(__file__).resolve().parent
FIXTURES = HERE.parent / "mini-tartu"
# Pinned, vendored map library. See vendor/*/PROVENANCE.md: a CDN-loaded
# library would make every graded result depend on a third party's uptime.
_MAPLIBRE_VENDOR = HERE.parent / "vendor" / "maplibre-gl-3.6.2"


def _connect_spatial():
    """Load a preinstalled Spatial extension without network access.

    This helper is deliberately self-contained because this file is copied to
    generated projects as ``pipeline.py`` and must remain runnable without the
    eval package.
    """
    config = {}
    extension_dir = os.environ.get("OPENMAPSTACK_SPATIAL_EXTENSION_DIR")
    if extension_dir:
        config["extension_directory"] = str(Path(extension_dir).expanduser().resolve())
    connection = duckdb.connect(config=config)
    try:
        connection.execute("LOAD spatial")
    except Exception as exc:
        connection.close()
        raise RuntimeError(
            "DuckDB Spatial is not preinstalled; prepare it before running this pipeline"
        ) from exc
    return connection


def _run_id() -> str:
    return "run-" + datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")


def _sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _canonical_file_set_hash(root: Path, paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(set(paths), key=lambda value: value.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(path.read_bytes())
    return "sha256:" + digest.hexdigest()


def _inventory(root: Path, paths: list[Path]) -> list[dict[str, str]]:
    return [
        {"path": path.relative_to(root).as_posix(), "sha256": _sha256_bytes(path.read_bytes())}
        for path in sorted(set(paths), key=lambda value: value.relative_to(root).as_posix())
    ]


def build(
    output_dir: Path,
    apply_override: bool,
    break_mode: str | None,
    with_scenario_road: bool = False,
    uncertain_completeness: bool = False,
    source_dir: Path | None = None,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "data" / "source").mkdir(parents=True, exist_ok=True)
    (output_dir / "data" / "derived").mkdir(parents=True, exist_ok=True)
    (output_dir / "data" / "overrides").mkdir(parents=True, exist_ok=True)
    (output_dir / "validation").mkdir(parents=True, exist_ok=True)
    (output_dir / "runs").mkdir(parents=True, exist_ok=True)
    (output_dir / "README.md").write_text(
        "# Mini-Tartu OpenMapStack project\n\nRun `python pipeline.py` to rebuild all artifacts.\n",
        encoding="utf-8",
    )

    input_dir = source_dir or FIXTURES
    for name in ("parcels.geojson", "roads.geojson", "pois.geojson"):
        source = input_dir / name
        destination = output_dir / "data" / "source" / name
        if not source.is_file():
            raise FileNotFoundError(f"required immutable source is missing: {source}")
        if source.resolve() != destination.resolve():
            shutil.copyfile(source, destination)

    if break_mode == "mutated_source":
        # Deliberately violate immutability: rewrite a byte of the copied
        # "immutable" source after it has landed in the project, simulating
        # a pipeline/agent that edits source data in place.
        pois_dest = output_dir / "data" / "source" / "pois.geojson"
        pois_dest.write_text(
            pois_dest.read_text(encoding="utf-8").replace("Test Kiosk", "Renamed Kiosk"),
            encoding="utf-8",
        )

    con = _connect_spatial()

    parcels_path = (output_dir / "data" / "source" / "parcels.geojson").as_posix()
    roads_path = (output_dir / "data" / "source" / "roads.geojson").as_posix()
    pois_path = (output_dir / "data" / "source" / "pois.geojson").as_posix()

    analysis_crs = "EPSG:4326" if break_mode == "wrong_crs" else "EPSG:3301"

    con.execute(f"CREATE TABLE parcels_raw AS SELECT * FROM ST_Read('{parcels_path}')")
    con.execute(
        "CREATE TABLE large_parcels AS SELECT cadastral_id, land_use, municipality, geom, "
        "ST_Area(geom) AS area_m2 FROM parcels_raw "
        "WHERE ST_Area(geom) >= 8000 AND land_use IN ('ARIMAA','MAATULUNDUSMAA','TOOTMISMAA')"
    )
    con.execute(f"CREATE TABLE official_roads AS SELECT road_id, road_class, name, geom FROM ST_Read('{roads_path}')")
    con.execute(f"CREATE TABLE pois_raw AS SELECT poi_id, name, status, geom FROM ST_Read('{pois_path}')")

    scenario_road_path = None
    if with_scenario_road:
        scenario_road_dest = output_dir / "data" / "overrides" / "planned-road.geojson"
        scenario_road_src = input_dir / "planned-road.geojson"
        if scenario_road_dest.is_file():
            scenario_road_src = scenario_road_dest
        if not scenario_road_src.is_file():
            raise FileNotFoundError(f"required scenario override is missing: {scenario_road_src}")
        if scenario_road_src.resolve() != scenario_road_dest.resolve():
            shutil.copyfile(scenario_road_src, scenario_road_dest)
        scenario_road_path = scenario_road_dest.as_posix()
        con.execute(f"CREATE TABLE scenario_roads AS SELECT name, status, geom FROM ST_Read('{scenario_road_path}')")
        con.execute(
            "CREATE TABLE roads AS "
            "SELECT road_id, road_class, name, geom FROM official_roads "
            "UNION ALL BY NAME "
            "SELECT NULL AS road_id, 'scenario' AS road_class, name, geom FROM scenario_roads"
        )
    else:
        con.execute("CREATE TABLE roads AS SELECT * FROM official_roads")

    # Apply the one declared override (modify_attribute: poi-7 status active -> closed)
    override_applied_detail = None
    if apply_override and break_mode != "override_not_applied":
        con.execute(
            "CREATE TABLE pois_effective AS SELECT * REPLACE "
            "(CASE WHEN poi_id = 'poi-7' THEN 'closed' ELSE status END AS status) FROM pois_raw"
        )
        override_applied_detail = "poi-7.status: active -> closed"
        override_status = "applied"
    else:
        con.execute("CREATE TABLE pois_effective AS SELECT * FROM pois_raw")
        override_status = "applied" if apply_override else "not_testable"

    con.execute(
        "CREATE TABLE candidate_parcels AS "
        "SELECT lp.*, MIN(ST_Distance(lp.geom, r.geom)) AS dist_main_road_m "
        "FROM large_parcels lp, roads r "
        "GROUP BY lp.cadastral_id, lp.land_use, lp.municipality, lp.geom, lp.area_m2 "
        "HAVING MIN(ST_Distance(lp.geom, r.geom)) <= 2000 "
        "ORDER BY lp.cadastral_id"
    )

    if break_mode == "hallucinated_feature":
        con.execute(
            "INSERT INTO candidate_parcels BY NAME "
            "SELECT 'FAKE-999' AS cadastral_id, 'ARIMAA' AS land_use, 'Tartu linn' AS municipality, "
            "ST_GeomFromText('POLYGON((999000 6999000,999100 6999000,999100 6999100,999000 6999100,999000 6999000))') AS geom, "
            "12000.0 AS area_m2, 500.0 AS dist_main_road_m"
        )

    candidates_path = output_dir / "data" / "derived" / "candidate-parcels.parquet"
    con.execute(f"COPY candidate_parcels TO '{candidates_path.as_posix()}' (FORMAT PARQUET)")
    candidates_geojson = output_dir / "data" / "derived" / "candidate-parcels.geojson"
    con.execute(f"COPY candidate_parcels TO '{candidates_geojson.as_posix()}' (FORMAT GDAL, DRIVER 'GeoJSON')")

    pois_out_path = output_dir / "data" / "derived" / "education_pois.geojson"
    con.execute(f"COPY pois_effective TO '{pois_out_path.as_posix()}' (FORMAT GDAL, DRIVER 'GeoJSON')")

    # Web-map variants (EPSG:4326) for the interactive dashboard. These are
    # embedded into dashboard.html and written to a scratch directory, so
    # they never appear as undeclared derived files in the project itself.
    wgs_dir = output_dir / ".dashboard-wgs84"
    wgs_dir.mkdir(exist_ok=True)
    for name, table in (
        ("candidate_parcels-wgs84.geojson", "candidate_parcels"),
        ("education_pois-wgs84.geojson", "pois_effective"),
    ):
        con.execute(
            f"COPY (SELECT * EXCLUDE (geom), ST_Transform(geom, 'EPSG:3301', 'EPSG:4326', true) AS geom "
            f"FROM {table}) TO '{(wgs_dir / name).as_posix()}' "
            "(FORMAT GDAL, DRIVER 'GeoJSON')"
        )
    planned_source = FIXTURES / "planned-road.geojson"
    if with_scenario_road and planned_source.is_file():
        con.execute(
            f"COPY (SELECT ST_Transform(geom, 'EPSG:3301', 'EPSG:4326', true) AS geom "
            f"FROM ST_Read('{planned_source.as_posix()}')) TO "
            f"'{(wgs_dir / 'planned-road-wgs84.geojson').as_posix()}' "
            "(FORMAT GDAL, DRIVER 'GeoJSON')"
        )

    row_count = con.execute("SELECT COUNT(*) FROM candidate_parcels").fetchone()[0]
    invalid = con.execute("SELECT SUM(CASE WHEN NOT ST_IsValid(geom) THEN 1 ELSE 0 END) FROM candidate_parcels").fetchone()[0] or 0

    steps = [
        {"id": "load_parcels", "operation": "read", "source": "cadastral_parcels", "output": "parcels_raw"},
        {"id": "filter_large_parcels", "operation": "filter", "input": "parcels_raw",
         "expression": "area_m2 >= 8000 AND land_use IN ('ARIMAA','MAATULUNDUSMAA','TOOTMISMAA')",
         "output": "large_parcels"},
        {"id": "load_roads", "operation": "read", "source": "roads", "output": "official_roads"},
        *([{"id": "load_scenario_road", "operation": "load_scenario_feature", "override": "OVERRIDE-002",
            "geometry_file": "data/overrides/planned-road.geojson", "crs": "EPSG:3301",
            "output": "scenario_roads"},
           {"id": "combine_road_networks", "operation": "union", "inputs": ["official_roads", "scenario_roads"],
            "output": "road_network"}] if with_scenario_road else
          [{"id": "load_roads_alias", "operation": "passthrough", "input": "official_roads", "output": "road_network"}]),
        {"id": "load_pois", "operation": "read", "source": "pois", "output": "pois_raw"},
        {"id": "apply_poi_override", "operation": "apply_override", "input": "pois_raw",
         "override": "OVERRIDE-001", "output": "pois_effective"},
        {"id": "road_distance", "operation": "distance_filter", "input": "large_parcels", "target": "road_network",
         "max_distance_m": 2000, "crs": analysis_crs, "output": "candidate_parcels"},
    ]
    if break_mode == "dangling_graph":
        steps.append({
            "id": "phantom_step", "operation": "filter", "input": "nonexistent_symbol",
            "expression": "1=1", "output": "phantom_output",
        })

    outputs = {
        "candidate_parcels": {
            "path": "data/derived/candidate-parcels.parquet",
            "format": "GeoParquet",
            "generated_by": "road_distance",
        },
        "candidate_parcels_geojson": {
            "path": "data/derived/candidate-parcels.geojson",
            "format": "GeoJSON",
            "generated_by": "road_distance",
        },
        "education_pois": {
            "path": "data/derived/education_pois.geojson",
            "format": "GeoJSON",
            "generated_by": "apply_poi_override",
        },
    }
    override_change_from = True if break_mode != "override_from_mismatch" else False
    overrides = [{
        "id": "OVERRIDE-001",
        "action": "modify_attribute",
        "target": {"source": "pois", "feature_id": "poi-7"},
        "change": {"field": "status", "from": "active" if override_change_from else "closed", "to": "closed"},
        "rationale": "Field survey confirmed the kiosk closed on the reported date.",
        "evidence": [{"type": "field_survey", "value": "Surveyed 2026-08-20 by project analyst; kiosk shuttered"}],
        "created_at": "2026-08-25T08:26:00+03:00",
        "created_by": "analyst",
    }] if apply_override else []

    if with_scenario_road:
        overrides.append({
            "id": "OVERRIDE-002",
            "action": "add_feature",
            "layer": "planned_roads",
            "properties": {"name": "Hypothetical connector", "status": "scenario"},
            "geometry_file": {"path": "data/overrides/planned-road.geojson"},
            "geometry_origin": "scenario",
            "rationale": "Planning document describes a connector absent from machine-readable roads data.",
            "evidence": [{"type": "planning_document", "value": "Municipal masterplan draft, section 4.2"}],
            "created_at": "2026-08-25T08:31:00+03:00",
            "created_by": "analyst",
        })

    source_identifier = "latest" if break_mode == "unpinned_source" else "mini-tartu-fixture-v1"

    project = {
        "schema": "openmapstack-project/v1",
        "project": {
            "id": output_dir.name,
            "title": "Eval fixture: mini-Tartu candidate parcels",
            "question": "Which large parcels near the test main road qualify as development candidates?",
            "created_at": "2026-08-25T08:00:00Z",
            "updated_at": "2026-08-25T08:00:05Z",
            "status": "warning" if break_mode else ("validated" if apply_override else "in_progress"),
        },
        "interpretation": {
            "objective": "Identify large parcels within 2000 m of the test main road.",
            "assumptions": [
                {"id": "A1", "statement": "Distance measured planar in EPSG:3301.",
                 "rationale": "Metric distance requires a projected CRS."},
            ],
        },
        "sources": {
            "cadastral_parcels": {
                "role": "authoritative_input", "provider": "eval-fixture",
                "dataset": "mini-tartu parcels", "source_url": "file://evals/fixtures/mini-tartu/parcels.geojson",
                "access": {"method": "local", "retrieved_at": "2026-08-25T08:00:00Z"},
                "version": {"published_at": "2026-08-25", "identifier": source_identifier},
                "selection": {
                    "filter": "area_m2 >= 8000",
                    "semantic_predicates": [{
                        "field": "land_use",
                        "domain_value": ["ARIMAA", "MAATULUNDUSMAA", "TOOTMISMAA"],
                        "meaning": "Land-use classes eligible for development screening",
                    }],
                },
                "license": {"name": "eval fixture, public domain", "url": "https://example.invalid/license"},
                "rationale": "Small deterministic fixture for CI evals.",
            },
            "roads": {
                "role": "authoritative_input", "provider": "eval-fixture",
                "dataset": "mini-tartu roads", "source_url": "file://evals/fixtures/mini-tartu/roads.geojson",
                "access": {"method": "local", "retrieved_at": "2026-08-25T08:00:00Z"},
                "version": {"published_at": "2026-08-25", "identifier": "mini-tartu-fixture-v1"},
                "selection": ({"completeness": {"matched": 5, "returned": 1, "page_size": 1, "pages": 1}}
                               if break_mode == "incomplete_pagination"
                               else {"completeness": {"matched": 1, "returned": 1}}),
                "license": {"name": "eval fixture, public domain", "url": "https://example.invalid/license"},
                "rationale": "Small deterministic fixture for CI evals.",
            },
            "pois": {
                "role": "authoritative_input", "provider": "eval-fixture",
                "dataset": "mini-tartu pois", "source_url": "file://evals/fixtures/mini-tartu/pois.geojson",
                "access": {"method": "local", "retrieved_at": "2026-08-25T08:00:00Z"},
                "version": {"published_at": "2026-08-25", "identifier": "mini-tartu-fixture-v1"},
                "selection": ({} if uncertain_completeness else {"completeness": {"matched": 2, "returned": 2}}),
                "license": {"name": "eval fixture, public domain", "url": "https://example.invalid/license"},
                "rationale": "Small deterministic fixture for CI evals.",
            },
        },
        "overrides": overrides,
        "processing": {
            "analysis_crs": analysis_crs,
            "storage_crs": "EPSG:4326",
            "steps": steps,
        },
        "outputs": outputs,
        "validation": {
            "required": [
                "geometry_valid", "crs_known", "row_count_gt_zero", "no_duplicate_cadastral_id",
                "no_null_cadastral_id", "manifest_graph_resolves", "overrides_applied",
            ],
            "domain_checks": [
                {"name": "parcel_area_range", "expression": "area_m2 > 0 AND area_m2 < 1000000"},
            ],
        },
        "presentation": {
            "intent": "analytical_workspace",
            "primary_view": "map",
            "layout": {"type": "map_with_sidebar", "sidebar": {"position": "left", "width": "medium",
                       "organization": "tabs", "section_state": "collapsible",
                       "tabs": [{"id": "map", "title": "Map", "sections": ["layer_controls", "basemap"]}]}},
            "controls": {
                "reconfigurable": True,
                "canonical_reset": True,
                "off_canonical_labelling": "required",
                "filters": [{
                    "id": "minimum-parcel-area",
                    "field": "area_m2",
                    "canonical": 8000,
                }],
                "scenarios": ([{
                    "id": "planned-road",
                    "override": "OVERRIDE-002",
                }] if with_scenario_road else []),
            },
            "map": {"engine_preference": "maplibre",
                    "basemap": {
                        "id": "osm-standard",
                        "kind": "raster-xyz",
                        "tiles": ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
                        "attribution": "© OpenStreetMap contributors",
                        "default_visible": True,
                        "note": "Interactive reference/background map; not an analysis input.",
                    },
                    "layer_groups": [{"id": "analysis", "title": "Analysis", "default_open": True},
                                      {"id": "user_overrides", "title": "Manual additions", "default_open": True},
                                      {"id": "basemap", "title": "Background map", "default_open": True}],
                    "layers": [
                        {"source": "candidate_parcels", "group": "analysis", "semantic_role": "primary_result",
                         "geometry": "polygon"},
                        {"source": "education_pois", "group": "user_overrides", "semantic_role": "user_override",
                         "geometry": "point"},
                        *([{"source": "planned_roads", "group": "user_overrides", "semantic_role": "planned",
                            "geometry": "line"}] if with_scenario_road else []),
                    ]},
            "legend": {"visible": True, "mode": "semantic"},
            "provenance_ui": {"feature_source_on_click": True, "show_source_timestamp": True,
                               "show_override_badge": True, "show_assumptions": True},
            "editing": {"allow_draw_geometry": False, "allow_attribute_override": True,
                        "allow_hide_source_feature": False, "allow_add_annotation": False,
                        "draft_persistence": "local_storage", "export_format": "openmapstack-override-bundle/v1",
                        "canonical_application": "pipeline_required",
                        "targets": {"pois": {"label": "POI", "source": "pois", "id_field": "poi_id",
                                              "label_field": "name",
                                              "fields": [{"view_field": "status", "source_field": "status",
                                                          "label": "Status", "type": "text"}]}}},
        },
        "warnings": [],
        # vendor/maplibre-gl is a declared dependency, not a derived artifact:
        # the dashboard loads it locally, so a clean rerun must carry it or
        # the rebuilt project would silently lose its map library.
        "runtime": {"implementation": {"preferred_engine": "duckdb-spatial", "pipeline": "pipeline.py",
                                       "dependencies": ["README.md", "vendor/maplibre-gl"]},
                    "environment": {"python": "3.12", "duckdb": duckdb.__version__}},
    }

    if break_mode:
        project["warnings"].append({
            "id": "EVAL-BREAK", "severity": "high", "layer": "n/a", "issue": break_mode,
            "statement": f"Deliberately injected defect for negative eval case: {break_mode}",
            "mitigation": "n/a — this project intentionally demonstrates a rejected workflow.",
        })

    if uncertain_completeness:
        project["project"]["status"] = "warning"
        project["validation"]["required"].append("poi_completeness")
        project["warnings"].append({
            "id": "DATA-001", "severity": "medium", "layer": "pois", "issue": "completeness_unknown",
            "statement": "The eval-fixture POI source does not publish a completeness baseline "
                         "(no matched/returned counts); this project may omit facilities.",
            "mitigation": "Verify against an authoritative completeness baseline before consequential use.",
        })

    if break_mode != "dashboard_only":
        pipeline_src = Path(__file__).resolve()
        pipeline_dest = output_dir / "pipeline.py"
        if pipeline_src.resolve() != pipeline_dest.resolve():
            shutil.copyfile(pipeline_src, pipeline_dest)

    # The vendored map library is a declared runtime dependency, so it must
    # be in place and hashed with the other inputs before the run record is
    # written -- a swapped library is then as visible as a swapped source.
    _copy_vendored_maplibre(output_dir)

    input_paths = [
        path
        for directory in (output_dir / "data" / "source", output_dir / "data" / "overrides")
        for path in directory.rglob("*")
        if path.is_file()
    ]
    input_paths.extend(
        path
        for path in (output_dir / "pipeline.py", output_dir / "README.md")
        if path.is_file()
    )
    input_paths.extend(
        path for path in (output_dir / "vendor" / "maplibre-gl").rglob("*") if path.is_file()
    )
    output_paths = [
        output_dir / definition["path"]
        for definition in outputs.values()
        if (output_dir / definition["path"]).is_file()
    ]
    run_id = "run-20260825-080000"
    inputs_hash = _canonical_file_set_hash(output_dir, input_paths)
    outputs_hash = _canonical_file_set_hash(output_dir, output_paths)

    project["runs"] = {"latest": {"id": run_id, "started_at": "2026-08-25T08:00:00Z",
                                   "completed_at": "2026-08-25T08:00:05Z",
                                   "status": "warning" if (break_mode or uncertain_completeness) else "passed",
                                   "inputs_hash": inputs_hash, "outputs_hash": outputs_hash,
                                   "validation_report": {"path": "validation/latest-report.json"}}}

    (output_dir / "project.yaml").write_text(yaml.dump(project, sort_keys=False, allow_unicode=True), encoding="utf-8")

    checks = [
        {"id": "geometry_valid", "status": "passed" if not invalid else "failed",
         "features_checked": row_count, "invalid_count": int(invalid)},
        {"id": "crs_known", "status": "passed", "expected": analysis_crs, "actual": analysis_crs},
        {"id": "row_count_gt_zero", "status": "passed" if row_count > 0 else "failed", "rows": row_count},
        {"id": "no_duplicate_cadastral_id", "status": "passed", "duplicates": 0},
        {"id": "no_null_cadastral_id", "status": "passed", "nulls": 0},
        {"id": "manifest_graph_resolves", "status": "failed" if break_mode == "dangling_graph" else "passed",
         "steps_checked": len(steps),
         "errors": ["phantom_step input=nonexistent_symbol resolves to neither a source nor a prior output"]
                    if break_mode == "dangling_graph" else []},
        {"id": "overrides_applied", "status": "passed" if apply_override else "not_testable",
         "declared": [o["id"] for o in overrides],
         "results": (
             ([{"id": "OVERRIDE-001", "status": override_status, "detail": override_applied_detail,
                "target": "poi-7", "field": "status", "from": "active", "to": "closed"}] if apply_override else [])
             + ([{"id": "OVERRIDE-002", "status": "applied",
                  "detail": "1 scenario road geometry loaded from data/overrides/planned-road.geojson"}]
                if with_scenario_road else [])
         )},
        {"id": "parcel_area_range", "status": "passed", "out_of_range_count": 0},
    ]

    if uncertain_completeness:
        checks.append({
            "id": "poi_completeness", "status": "warning",
            "reason": "No authoritative completeness baseline available for the POI source",
        })

    if break_mode == "validation_laundering":
        checks = [c for c in checks if c["id"] != "no_null_cadastral_id"]

    overall_status = "passed"
    if break_mode in ("wrong_crs", "hallucinated_feature") :
        overall_status = "failed"
    if any(c["status"] in ("failed",) for c in checks):
        overall_status = "failed"
    elif any(c["status"] in ("warning", "not_testable") for c in checks):
        overall_status = "warning"

    report = {
        "run_id": run_id,
        "schema": "openmapstack-project/v1",
        "status": overall_status,
        "checks": checks,
        "inputs_hash": inputs_hash,
        "outputs_hash": outputs_hash,
    }
    (output_dir / "validation" / "latest-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    run_record = {
        "run_id": run_id, "started_at": "2026-08-25T08:00:00Z", "completed_at": "2026-08-25T08:00:05Z",
        "status": overall_status, "inputs_hash": inputs_hash, "outputs_hash": outputs_hash,
        "environment": {"python": platform.python_version(), "duckdb": duckdb.__version__},
        "inputs": _inventory(output_dir, input_paths),
        "outputs": _inventory(output_dir, output_paths),
    }
    (output_dir / "runs" / f"{run_id}.json").write_text(json.dumps(run_record, indent=2), encoding="utf-8")

    if break_mode == "qgis_broken_datasource":
        qgs_xml = (
            '<?xml version="1.0"?><qgis><projectlayers>'
            '<datasource>./data/derived/does-not-exist.gpkg|layername=missing</datasource>'
            '</projectlayers></qgis>'
        )
    else:
        qgs_xml = _build_qgs_xml(output_dir, project, break_mode)
    qgz_path = output_dir / "project.qgz"
    with zipfile.ZipFile(qgz_path, "w") as zf:
        zf.writestr("project.qgs", qgs_xml)

    dashboard_html = _build_dashboard_html(output_dir, project, break_mode)
    (output_dir / "dashboard.html").write_text(dashboard_html, encoding="utf-8")
    shutil.rmtree(output_dir / ".dashboard-wgs84", ignore_errors=True)

    con.close()


# Layer definitions shared by the QGIS project and the dashboard: each entry
# is (id, title, relative file path, ogr layer name, geometry family,
# manifest layer-group id, manifest source key). The .qgs layer tree must
# mirror the dashboard's group hierarchy (project-spec.md s. 6).
def _project_layers(output_dir: Path, project: dict) -> list[dict]:
    with_scenario_road = any(o.get("id") == "OVERRIDE-002" for o in project.get("overrides") or [])
    layers = [
        {"id": "candidate-parcels", "title": "Candidate parcels",
         "file": "data/derived/candidate-parcels.geojson", "layername": "candidate-parcels",
         "geometry": "polygon", "group": "analysis", "source": "candidate_parcels",
         "color": "34,160,107,255"},
        {"id": "education-pois", "title": "Education POIs",
         "file": "data/derived/education_pois.geojson", "layername": "education_pois",
         "geometry": "point", "group": "user_overrides", "source": "education_pois",
         "color": "29,78,216,255"},
    ]
    if with_scenario_road:
        layers.append({"id": "planned-road", "title": "Hypothetical connector (scenario)",
                        "file": "data/overrides/planned-road.geojson", "layername": "planned-road",
                        "geometry": "line", "group": "user_overrides", "source": "planned_roads",
                        "color": "217,131,36,255"})
    return [entry for entry in layers if (output_dir / entry["file"]).is_file()]


_EPSG_3301_SRS = """<srs>
          <spatialrefsys nativeFormat="Wkt">
           <wkt>PROJCS["EST97 / Estonia 1997",GEOGCS["EST97",DATUM["Estonia_1997",SPHEROID["GRS 1980",6378137,298.257222101,AUTHORITY["EPSG","7019"]],TOWGS84[0,0,0,0,0,0,0],AUTHORITY["EPSG","6180"]],PRIMEM["Greenwich",0,AUTHORITY["EPSG","8901"]],UNIT["degree",0.0174532925199433,AUTHORITY["EPSG","9122"]],AUTHORITY["EPSG","4180"]],PROJECTION["Lambert_Conformal_Conic_2SP"],PARAMETER["standard_parallel_1",59.33333333333334],PARAMETER["standard_parallel_2",58],PARAMETER["latitude_of_origin",57.51755393055556],PARAMETER["central_meridian",24],PARAMETER["false_easting",500000],PARAMETER["false_northing",1000000],UNIT["metre",1,AUTHORITY["EPSG","9001"]],AXIS["Northing",NORTH],AXIS["Easting",EAST],AUTHORITY["EPSG","3301"]]</wkt>
           <proj4>+proj=lcc +lat_0=57.5175539305556 +lon_0=24 +lat_1=59.3333333333333 +lat_2=58 +x_0=500000 +y_0=1000000 +ellps=GRS80 +towgs84=0,0,0,0,0,0,0 +units=m +no_defs</proj4>
           <srsid>2417</srsid>
           <srid>3301</srid>
           <authid>EPSG:3301</authid>
           <description>EST97 / Estonia 1997</description>
           <projectionacronym>lcc</projectionacronym>
           <ellipsoidacronym>GRS80</ellipsoidacronym>
           <geographicflag>false</geographicflag>
          </spatialrefsys>
         </srs>"""


# The tiled basemap is served in Web Mercator. Omitting this made QGIS
# assume the project CRS for it and skip reprojection entirely, placing an
# Estonian project's background map ~1500 km away in the Ardennes -- a
# confidently wrong map, which is worse than none.
_EPSG_3857_SRS = """<srs>
          <spatialrefsys nativeFormat="Wkt">
           <proj4>+proj=merc +a=6378137 +b=6378137 +lat_ts=0 +lon_0=0 +x_0=0 +y_0=0 +k=1 +units=m +nadgrids=@null +wktext +no_defs</proj4>
           <srsid>3857</srsid>
           <srid>3857</srid>
           <authid>EPSG:3857</authid>
           <description>WGS 84 / Pseudo-Mercator</description>
           <projectionacronym>merc</projectionacronym>
           <ellipsoidacronym>EPSG:7030</ellipsoidacronym>
           <geographicflag>false</geographicflag>
          </spatialrefsys>
         </srs>"""


_WKB_TYPES = {"polygon": ("MultiPolygon", "Polygon"), "point": ("MultiPoint", "Point"), "line": ("MultiLineString", "LineString")}


def _symbol_xml(geometry: str, color: str) -> str:
    if geometry == "polygon":
        return (
            '<symbol type="fill" frame_rate="10" name="0" clip_to_extent="1" force_rhr="0" alpha="1" is_animated="0">'
            '<layer class="SimpleFill" locked="0" pass="0" enabled="1">'
            '<Option type="Map">'
            f'<Option type="QString" name="color" value="{color}"/>'
            '<Option type="QString" name="outline_color" value="45,70,60,255"/>'
            '<Option type="QString" name="outline_style" value="solid"/>'
            '<Option type="QString" name="outline_width" value="0.4"/>'
            '<Option type="QString" name="outline_width_unit" value="MM"/>'
            '<Option type="QString" name="style" value="solid"/>'
            '</Option>'
            '</layer>'
            '</symbol>'
        )
    if geometry == "point":
        return (
            '<symbol type="marker" frame_rate="10" name="0" clip_to_extent="1" force_rhr="0" alpha="1" is_animated="0">'
            '<layer class="SimpleMarker" locked="0" pass="0" enabled="1">'
            '<Option type="Map">'
            f'<Option type="QString" name="color" value="{color}"/>'
            '<Option type="QString" name="name" value="circle"/>'
            '<Option type="QString" name="outline_color" value="20,40,90,255"/>'
            '<Option type="QString" name="outline_style" value="solid"/>'
            '<Option type="QString" name="outline_width" value="0.4"/>'
            '<Option type="QString" name="size" value="3.4"/>'
            '<Option type="QString" name="size_unit" value="MM"/>'
            '</Option>'
            '</layer>'
            '</symbol>'
        )
    return (
        '<symbol type="line" frame_rate="10" name="0" clip_to_extent="1" force_rhr="0" alpha="1" is_animated="0">'
        '<layer class="SimpleLine" locked="0" pass="0" enabled="1">'
        '<Option type="Map">'
        f'<Option type="QString" name="line_color" value="{color}"/>'
        '<Option type="QString" name="line_style" value="solid"/>'
        '<Option type="QString" name="line_width" value="0.8"/>'
        '<Option type="QString" name="line_width_unit" value="MM"/>'
        '<Option type="QString" name="capstyle" value="flat"/>'
        '<Option type="QString" name="joinstyle" value="round"/>'
        '</Option>'
        '</layer>'
        '</symbol>'
    )


def _build_qgs_xml(output_dir: Path, project: dict, break_mode: str | None) -> str:
    """A minimal but genuine QGIS project: real maplayers with datasources,
    CRS, declared renderers, and a layer tree whose groups mirror the
    manifest's presentation.map.layer_groups — including the tiled basemap
    layer the skill requires (project-spec.md s. 5.2).

    The two renderers stack in opposite directions: `presentation.map.layers`
    is ordered bottom-to-top (MapLibre paints later style layers on top),
    while a QGIS layer tree paints its *first* entry on top. Emitting the
    manifest order verbatim therefore inverts the hierarchy — it buried the
    POI markers under the opaque parcel fill — so the tree is built in
    reverse, with the basemap last (bottom) in both products.
    """
    layers = _project_layers(output_dir, project)
    group_ids = [g["id"] for g in project["presentation"]["map"]["layer_groups"]]
    basemap = project["presentation"]["map"].get("basemap")
    include_basemap = basemap is not None and break_mode != "dashboard_no_basemap"

    def tree_layer(source: str, name: str, layer_id: str, checked: str = "Qt::Checked") -> str:
        return (
            f'<layer-tree-layer source="{source}" '
            f'name="{name}" id="{layer_id}" checked="{checked}" expanded="1"/>'
        )

    tree_parts = []
    for group_id in reversed([group_id for group_id in group_ids if group_id != "basemap"]):
        children = "".join(
            tree_layer(f'./{entry["file"]}|layername={entry["layername"]}', entry["title"], entry["id"])
            for entry in reversed([entry for entry in layers if entry["group"] == group_id])
        )
        tree_parts.append(
            f'<layer-tree-group name="{group_id}" checked="Qt::Checked" expanded="1" mutually-exclusive="0">{children}</layer-tree-group>'
        )
    if include_basemap:
        source = f"type=xyz&amp;url={basemap['tiles'][0]}&amp;zmax=19&amp;zmin=0"
        tree_parts.append(
            f'<layer-tree-group name="basemap" checked="Qt::Checked" expanded="1" mutually-exclusive="0">'
            f'{tree_layer(source, "OpenStreetMap (background)", "basemap-osm")}</layer-tree-group>'
        )

    layer_parts = []
    for entry in layers:
        wkb_type, geometry_attr = _WKB_TYPES[entry["geometry"]]
        layer_parts.append(
            f'''<maplayer autoRefreshEnabled="0" autoRefreshTime="0" blendMode="0" constraintsEnabled="0" geometry="{geometry_attr}" insertDefaultStyles="0" labelsEnabled="0" layerType="vector" legendPlaceholderImage="" maxScale="0" minScale="100000000" readOnly="0" refreshOnNotifyEnabled="0" refreshOnNotifyMessage="" skipFeatureCount="0" type="vector" wkbType="{wkb_type}">
      <id>{entry["id"]}</id>
      <layername>{entry["title"]}</layername>
      <title>{entry["title"]}</title>
      <datasource>./{entry["file"]}|layername={entry["layername"]}</datasource>
      <provider encoding="UTF-8">ogr</provider>
      <layer_opacities>1</layer_opacities>
      {_EPSG_3301_SRS}
      <renderer-v2 type="singleSymbol" enableorderby="0" symbollevels="0" forceraster="0" referencescale="-1">
       <symbols>{_symbol_xml(entry["geometry"], entry["color"])}</symbols>
      </renderer-v2>
     </maplayer>'''
        )
    if include_basemap:
        source = f"type=xyz&amp;url={basemap['tiles'][0]}&amp;zmax=19&amp;zmin=0"
        layer_parts.append(
            f'''<maplayer autoRefreshEnabled="0" autoRefreshTime="0" blendMode="0" constraintsEnabled="0" insertDefaultStyles="0" labelsEnabled="0" layerType="raster" legendPlaceholderImage="" maxScale="0" minScale="100000000" readOnly="0" refreshOnNotifyEnabled="0" refreshOnNotifyMessage="" skipFeatureCount="0" type="raster">
      <id>basemap-osm</id>
      <layername>OpenStreetMap (background)</layername>
      <title>OpenStreetMap (background)</title>
      <datasource>{source}</datasource>
      <provider>wms</provider>
      <layer_opacities>1</layer_opacities>
      {_EPSG_3857_SRS}
      <rasterbands/>
     </maplayer>'''
        )

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE qgis PUBLIC \'http://mrcc.com/qgis.dtd\' \'SYSTEM\'>\n'
        '<qgis version="3.44.0" projectname="">\n'
        ' <homePath path=""/>\n'
        f' <layer-tree-group>{"".join(tree_parts)}</layer-tree-group>\n'
        f' <projectlayers>{"".join(layer_parts)}</projectlayers>\n'
        '</qgis>\n'
    )


def _copy_vendored_maplibre(output_dir: Path) -> None:
    """Place the pinned map library beside the dashboard that loads it.

    The generated project must open and behave identically with no network:
    a CDN-hosted library would make every graded visual result depend on a
    third party's uptime rather than on the project under test. When the
    project is regenerated by its own copied pipeline.py the vendor
    directory is already in place, so a missing source directory is not an
    error -- only a missing library at the end would be.
    """
    destination = output_dir / "vendor" / "maplibre-gl"
    if not _MAPLIBRE_VENDOR.is_dir():
        if (destination / "maplibre-gl.js").is_file():
            return
        raise FileNotFoundError(f"vendored map library is missing: {_MAPLIBRE_VENDOR}")
    destination.mkdir(parents=True, exist_ok=True)
    for name in ("maplibre-gl.js", "maplibre-gl.css"):
        shutil.copyfile(_MAPLIBRE_VENDOR / name, destination / name)


def _esc(value: str) -> str:
    return (str(value).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _bbox_of(feature_collection: dict) -> tuple[float, float, float, float] | None:
    xs: list[float] = []
    ys: list[float] = []

    def visit(coords) -> None:
        if isinstance(coords[0], (int, float)):
            xs.append(float(coords[0]))
            ys.append(float(coords[1]))
        else:
            for child in coords:
                visit(child)

    for feature in feature_collection.get("features") or []:
        visit(feature.get("geometry", {}).get("coordinates") or [])
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def _feature_collection(output_dir: Path, relative: str) -> dict:
    path = output_dir / relative
    if not path.is_file():
        return {"type": "FeatureCollection", "features": []}
    return json.loads(path.read_text(encoding="utf-8"))


def _build_dashboard_html(output_dir: Path, project: dict, break_mode: str | None) -> str:
    """A deterministic, genuinely interactive dashboard: MapLibre GL with an
    OSM raster basemap (attribution rendered by the map) and the project's
    declared layers as GeoJSON overlays. No generated timestamps, so the
    file is byte-identical across reruns. Break mode
    ``dashboard_no_basemap`` reproduces the most common agent failure the
    skill warns about: an analysis overlay with no background map."""
    layers = _project_layers(output_dir, project)
    groups = project["presentation"]["map"]["layer_groups"]
    scenarios = project["presentation"]["controls"]["scenarios"]
    warnings = project.get("warnings") or []
    sources = project.get("sources") or {}
    basemap = project["presentation"]["map"].get("basemap")

    show_basemap = basemap is not None and break_mode != "dashboard_no_basemap"

    # WGS84 scratch files are keyed by the manifest source key.
    wgs_feature_collections = {
        "candidate_parcels": "candidate_parcels-wgs84.geojson",
        "education_pois": "education_pois-wgs84.geojson",
        "planned_roads": "planned-road-wgs84.geojson",
    }
    view = {
        "title": project["project"]["title"],
        "status": project["project"]["status"],
        "warnings": warnings,
        "sources": [
            {"key": key, "provider": src.get("provider"), "license": (src.get("license") or {}).get("name"),
             "version": (src.get("version") or {}).get("identifier"),
             "retrieved_at": (src.get("access") or {}).get("retrieved_at")}
            for key, src in sources.items()
        ],
        "layerGroups": groups,
        "scenarios": scenarios,
        "basemap": basemap if show_basemap else None,
        "layers": [
            {"id": entry["id"], "title": entry["title"], "group": entry["group"],
             "geometry": entry["geometry"], "source": entry["source"],
             "features": _feature_collection(output_dir / ".dashboard-wgs84", wgs_feature_collections.get(entry["source"], ""))}
            for entry in layers
        ],
    }

    all_features = {"type": "FeatureCollection",
                    "features": [f for layer in view["layers"] for f in layer["features"].get("features") or []]}
    bbox = _bbox_of(all_features) or (0.0, 0.0, 1.0, 1.0)
    view["bbox"] = [round(v, 7) for v in bbox]

    # Map the manifest's interactive semantics onto concrete MapLibre layer
    # ids so the toggles operate on real style layers.
    group_layers: dict[str, list[str]] = {}
    scenario_layers: dict[str, list[str]] = {}
    style_layers: list[dict] = []
    if show_basemap:
        group_layers.setdefault("basemap", []).append("basemap-raster")
        style_layers.append({"id": "basemap-raster", "type": "raster", "source": "basemap-src"})
    for layer in view["layers"]:
        rgb = next((entry["color"] for entry in layers if entry["source"] == layer["source"]), "90,90,90")
        if layer["geometry"] == "polygon":
            style_layers.append({"id": f"{layer['id']}-fill", "type": "fill", "source": f"src-{layer['id']}",
                                 "paint": {"fill-color": f"rgb({rgb})", "fill-opacity": 0.55}})
            style_layers.append({"id": f"{layer['id']}-outline", "type": "line", "source": f"src-{layer['id']}",
                                 "paint": {"line-color": f"rgb({rgb})", "line-width": 1.5}})
            group_layers.setdefault(layer["group"], []).extend([f"{layer['id']}-fill", f"{layer['id']}-outline"])
        elif layer["geometry"] == "point":
            style_layers.append({"id": layer["id"], "type": "circle", "source": f"src-{layer['id']}",
                                 "paint": {"circle-radius": 7, "circle-color": f"rgb({rgb})",
                                           "circle-stroke-color": "rgb(20,40,90)", "circle-stroke-width": 1.5}})
            group_layers.setdefault(layer["group"], []).append(layer["id"])
        else:
            style_layers.append({"id": layer["id"], "type": "line", "source": f"src-{layer['id']}",
                                 "paint": {"line-color": f"rgb({rgb})", "line-width": 4}})
            group_layers.setdefault(layer["group"], []).append(layer["id"])
    for scenario in scenarios:
        layer_key = next(
            (o.get("layer") for o in (project.get("overrides") or []) if o.get("id") == scenario.get("override")),
            None,
        )
        style_layer = next((l for l in view["layers"] if l["source"] == layer_key), None)
        if style_layer:
            scenario_layers[scenario["id"]] = [style_layer["id"]]

    geojson_sources = {
        f"src-{layer['id']}": {"type": "geojson", "data": layer["features"]} for layer in view["layers"]
    }
    basemap_source = (
        {"basemap-src": {"type": "raster", "tiles": basemap["tiles"], "tileSize": 256,
                          "attribution": basemap["attribution"], "maxzoom": 19}}
        if show_basemap else {}
    )

    show_warnings_panel = break_mode != "dashboard_silent_warnings"
    script_extra = "throw new Error('eval break: dashboard_broken_script');\n" if break_mode == "dashboard_broken_script" else ""

    warnings_html = (
        '<section data-testid="warnings" class="panel"><h2>Warnings</h2><ul>'
        + "".join(f'<li><b>{_esc(w.get("id"))}</b> ({_esc(w.get("severity"))}): {_esc(w.get("issue"))} — {_esc(w.get("statement"))}</li>' for w in warnings)
        + '</ul></section>'
        if warnings and show_warnings_panel else ""
    )

    style = """
body { font-family: sans-serif; margin: 0; display: flex; height: 100vh; }
#sidebar { width: 340px; overflow-y: auto; padding: 12px; background: #f4f6f9; box-sizing: border-box; }
#mapwrap { flex: 1; position: relative; }
#map { position: absolute; inset: 0; }
.panel { background: #fff; border: 1px solid #dde2ea; border-radius: 8px; padding: 10px; margin-bottom: 10px; }
.panel h2 { font-size: 13px; margin: 0 0 8px; }
li { margin-bottom: 6px; font-size: 12px; }
label { display: block; font-size: 13px; margin: 4px 0; }
.legend-swatch { display: inline-block; width: 12px; height: 12px; margin-right: 6px; vertical-align: middle; border-radius: 2px; }
"""

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(view["title"])} — OpenMapStack project view</title>
<link href="./vendor/maplibre-gl/maplibre-gl.css" rel="stylesheet">
<style>{style}</style>
</head>
<body>
<div id="sidebar">
 <div class="panel"><h2>{_esc(view["title"])}</h2><p style="font-size:12px">status: {_esc(view["status"])}</p></div>
 <section class="panel" data-testid="layer-controls"><h2>Layers</h2>
  {''.join(f'<label><input type="checkbox" data-layer-group="{_esc(g["id"])}" checked> {_esc(g["title"])}</label>' for g in groups)}
  {''.join(f'<label><input type="checkbox" data-scenario="{_esc(s["id"])}" checked> Scenario: {_esc(s["id"])}</label>' for s in scenarios)}
  <button data-testid="canonical-reset" id="reset" type="button">Reset to canonical</button>
 </section>
 <section data-testid="legend" class="panel"><h2>Legend</h2>
  {''.join(f'<div><span class="legend-swatch" style="background:rgb({_esc(entry["color"].rsplit(",", 1)[0])})"></span>{_esc(entry["title"])} <em>({entry["group"]})</em></div>' for entry in layers)}
 </section>
 <section data-testid="provenance" class="panel"><h2>Provenance</h2>
  {''.join(f'<div style="font-size:12px;margin-bottom:6px"><b>{_esc(s["key"])}</b> — {_esc(s["provider"])}; license: {_esc(s["license"])}; version: {_esc(s["version"])}; retrieved: {_esc(s["retrieved_at"])}</div>' for s in view["sources"])}
 </section>
 {warnings_html}
</div>
<div id="mapwrap"><div id="map" data-testid="map"></div></div>
<script src="./vendor/maplibre-gl/maplibre-gl.js"></script>
<script>
const VIEW = {json.dumps(view, ensure_ascii=False)};
const GROUP_LAYERS = {json.dumps(group_layers)};
const SCENARIO_LAYERS = {json.dumps(scenario_layers)};
{script_extra}const MAP = new maplibregl.Map({{
  container: "map",
  style: {{
    version: 8,
    sources: {json.dumps({**basemap_source, **geojson_sources}, ensure_ascii=False)},
    layers: {json.dumps(style_layers, ensure_ascii=False)},
  }},
  bounds: VIEW.bbox,
  fitBoundsOptions: {{padding: 40, duration: 0}},
  attributionControl: {json.dumps(show_basemap)},
}});
function applyVisibility() {{
  if (!MAP.isStyleLoaded()) return;
  for (const cb of document.querySelectorAll('input[type="checkbox"][data-layer-group]')) {{
    for (const layerId of GROUP_LAYERS[cb.dataset.layerGroup] || []) {{
      if (MAP.getLayer(layerId)) MAP.setLayoutProperty(layerId, "visibility", cb.checked ? "visible" : "none");
    }}
  }}
  for (const cb of document.querySelectorAll('input[type="checkbox"][data-scenario]')) {{
    for (const layerId of SCENARIO_LAYERS[cb.dataset.scenario] || []) {{
      if (MAP.getLayer(layerId)) MAP.setLayoutProperty(layerId, "visibility", cb.checked ? "visible" : "none");
    }}
  }}
}}
const initialStates = {{}};
document.querySelectorAll('input[type="checkbox"]').forEach((cb) => {{
  initialStates[cb.dataset.layerGroup || cb.dataset.scenario || cb.id || cb.name || ""] = cb.checked;
  cb.addEventListener("change", applyVisibility);
}});
document.getElementById("reset").addEventListener("click", () => {{
  document.querySelectorAll('input[type="checkbox"]').forEach((cb) => {{
    const key = cb.dataset.layerGroup || cb.dataset.scenario || cb.id || cb.name || "";
    if (key in initialStates) cb.checked = initialStates[key];
  }});
  applyVisibility();
}});
MAP.on("load", applyVisibility);
</script>
</body>
</html>
'''

def main() -> int:
    # A generated project receives a copy of this file as its canonical
    # pipeline. In that role it must rebuild from the project's own immutable
    # inputs, with no dependency on this eval generator or the original chat.
    if Path(__file__).name == "pipeline.py":
        output_dir = Path(__file__).resolve().parent
        project_path = output_dir / "project.yaml"
        if not project_path.is_file():
            raise FileNotFoundError(f"project manifest is missing: {project_path}")
        project = yaml.safe_load(project_path.read_text(encoding="utf-8"))
        overrides = project.get("overrides") or [] if isinstance(project, dict) else []
        source_selection = (
            ((project.get("sources") or {}).get("pois") or {}).get("selection") or {}
            if isinstance(project, dict)
            else {}
        )
        build(
            output_dir,
            apply_override=any(item.get("id") == "OVERRIDE-001" for item in overrides),
            break_mode=None,
            with_scenario_road=any(item.get("id") == "OVERRIDE-002" for item in overrides),
            uncertain_completeness="completeness" not in source_selection,
            source_dir=output_dir / "data" / "source",
        )
        print(f"rebuilt {output_dir} from local project inputs")
        return 0

    parser = argparse.ArgumentParser()
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--no-override", action="store_true")
    parser.add_argument("--break", dest="break_mode", default=None)
    parser.add_argument("--scenario-road", action="store_true", help="add OVERRIDE-002 planned connector road")
    parser.add_argument("--uncertain-completeness", action="store_true",
                         help="drop POI completeness counts and add a completeness warning")
    args = parser.parse_args()

    if args.output_dir.exists():
        shutil.rmtree(args.output_dir)
    build(args.output_dir, apply_override=not args.no_override, break_mode=args.break_mode,
          with_scenario_road=args.scenario_road, uncertain_completeness=args.uncertain_completeness)
    print(f"wrote {args.output_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
