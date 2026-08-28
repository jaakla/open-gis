#!/usr/bin/env python3
"""Deterministic reference pipeline used to generate/regenerate committed
eval fixtures under evals/cases/*/project/.

This is intentionally small (a minimal, real `open-gis-project/v1` project)
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


def _connect_spatial():
    """Load a preinstalled Spatial extension without network access.

    This helper is deliberately self-contained because this file is copied to
    generated projects as ``pipeline.py`` and must remain runnable without the
    eval package.
    """
    config = {}
    extension_dir = os.environ.get("OPEN_GIS_SPATIAL_EXTENSION_DIR")
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
        "# Mini-Tartu Open-GIS project\n\nRun `python pipeline.py` to rebuild all artifacts.\n",
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
        "schema": "open-gis-project/v1",
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
                    "layer_groups": [{"id": "analysis", "title": "Analysis", "default_open": True},
                                      {"id": "user_overrides", "title": "Manual additions", "default_open": True}],
                    "layers": [
                        {"source": "candidate_parcels", "group": "analysis", "semantic_role": "primary_result",
                         "geometry": "polygon"},
                        {"source": "pois", "group": "user_overrides", "semantic_role": "user_override",
                         "geometry": "point"},
                    ]},
            "legend": {"visible": True, "mode": "semantic"},
            "provenance_ui": {"feature_source_on_click": True, "show_source_timestamp": True,
                               "show_override_badge": True, "show_assumptions": True},
            "editing": {"allow_draw_geometry": False, "allow_attribute_override": True,
                        "allow_hide_source_feature": False, "allow_add_annotation": False,
                        "draft_persistence": "local_storage", "export_format": "open-gis-override-bundle/v1",
                        "canonical_application": "pipeline_required",
                        "targets": {"pois": {"label": "POI", "source": "pois", "id_field": "poi_id",
                                              "label_field": "name",
                                              "fields": [{"view_field": "status", "source_field": "status",
                                                          "label": "Status", "type": "text"}]}}},
        },
        "warnings": [],
        "runtime": {"implementation": {"preferred_engine": "duckdb-spatial", "pipeline": "pipeline.py",
                                       "dependencies": ["README.md"]},
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
        "schema": "open-gis-project/v1",
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
        qgs_xml = (
            '<?xml version="1.0"?><qgis><projectlayers>'
            f'<datasource>./{candidates_geojson.relative_to(output_dir).as_posix()}</datasource>'
            '</projectlayers></qgis>'
        )
    qgz_path = output_dir / "project.qgz"
    with zipfile.ZipFile(qgz_path, "w") as zf:
        zf.writestr("project.qgs", qgs_xml)

    con.close()


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
