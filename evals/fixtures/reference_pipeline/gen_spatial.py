#!/usr/bin/env python3
"""Deterministic scenario generator for PR 8 GIS coverage.

The main reference generator (``gen.py``) covers one spatial-analysis family
in depth. This generator produces small, focused projects for the *other*
failure modes the epic lists: mixed-CRS sources, many-to-many spatial joins
with duplicate identifiers, boundary semantics and polygon holes, invalid
and null geometry handling, multi-format output parity, and ordered /
conflicting overrides.

Every scenario's correct answer is independently hand-derivable from its
tiny synthetic input (the case YAMLs pin exact ids, counts and areas), so
the cases are true oracles, not self-consistency checks.

Usage:
    python gen_spatial.py <output_dir> --scenario=<name> [--break=<bug>]

Scenarios: boundary | join | crs | health | formats | overrides
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

import duckdb
import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from gen import _canonical_file_set_hash, _connect_spatial, _inventory  # noqa: E402

SCENARIOS_DIR = HERE.parent / "spatial-scenarios"
RUN_ID = "run-20260829-120000"
FIXED_TIME = "2026-08-29T12:00:00Z"
ANALYSIS_CRS = "EPSG:3301"


def _copy_inputs(scenario: str, output_dir: Path) -> dict[str, int]:
    """Copy the scenario's immutable inputs into data/source; the runner's
    byte-identity check compares them against the checked-in originals."""
    source_dir = SCENARIOS_DIR / scenario
    (output_dir / "data" / "derived").mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    for path in sorted(source_dir.glob("*.geojson")):
        dest = output_dir / "data" / "source" / path.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, dest)
        data = json.loads(path.read_text(encoding="utf-8"))
        counts[path.name] = len(data.get("features") or [])
    return counts


def _source_entry(name: str, dataset: str, features: int, rationale: str) -> dict:
    return {
        "role": "authoritative_input",
        "provider": "eval-fixture",
        "dataset": dataset,
        "source_url": f"file://evals/fixtures/spatial-scenarios/{name}",
        "access": {"method": "local", "retrieved_at": FIXED_TIME},
        "version": {"published_at": "2026-08-29", "identifier": "spatial-scenarios-v1"},
        "selection": {"completeness": {"matched": features, "returned": features}},
        "license": {"name": "eval fixture, public domain", "url": "https://example.invalid/license"},
        "rationale": rationale,
    }


def _project(output_dir: Path, *, title: str, question: str, sources: dict,
             steps: list, outputs: dict, overrides: list | None = None,
             assumptions: list | None = None,
             required_checks: list | None = None) -> dict:
    return {
        "schema": "openmapstack-project/v1",
        "project": {
            "id": output_dir.name,
            "title": title,
            "question": question,
            "created_at": FIXED_TIME,
            "updated_at": FIXED_TIME,
            "status": "validated",
        },
        "interpretation": {
            "objective": question,
            "assumptions": assumptions or [
                {"id": "A1", "statement": "Synthetic fixture geometry; distances and areas are exact by construction.",
                 "rationale": "Metric measurement requires the projected analysis CRS."},
            ],
        },
        "sources": sources,
        "overrides": overrides or [],
        "processing": {"analysis_crs": ANALYSIS_CRS, "steps": steps},
        "outputs": outputs,
        "validation": {
            "required": required_checks or [
                "geometry_valid", "crs_known", "row_count_gt_zero",
                "manifest_graph_resolves",
            ],
            "domain_checks": [],
        },
        "presentation": {
            "intent": "analytical_workspace",
            "primary_view": "map",
        },
        "warnings": [],
        "runtime": {"implementation": {"preferred_engine": "duckdb-spatial",
                                        "pipeline": "pipeline.py",
                                        "dependencies": ["README.md", "gen.py"]},
                    "environment": {"python": "3.12", "duckdb": duckdb.__version__}},
    }


def _finish(output_dir: Path, project: dict, checks: list) -> None:
    """Write manifest, run record and validation report with real hashes —
    the same parity contract the main reference generator satisfies."""
    # The declared canonical pipeline ships as real, runnable code: this
    # generator plus the shared helper module it imports. Written first so
    # the input inventory below includes them.
    shutil.copyfile(HERE / "gen_spatial.py", output_dir / "pipeline.py")
    shutil.copyfile(HERE / "gen.py", output_dir / "gen.py")
    input_paths = [
        path
        for directory in (output_dir / "data" / "source", output_dir / "data" / "overrides")
        for path in directory.rglob("*")
        if path.is_file()
    ]
    # The declared canonical pipeline and its helper module are canonical
    # inputs too (integrity.declared_input_paths requires them).
    input_paths.extend(
        path for path in (output_dir / "pipeline.py", output_dir / "gen.py", output_dir / "README.md")
        if path.is_file()
    )
    output_paths = [
        output_dir / definition["path"]
        for definition in (project.get("outputs") or {}).values()
        if (output_dir / definition["path"]).is_file()
    ]
    (output_dir / "validation").mkdir(exist_ok=True)
    (output_dir / "runs").mkdir(exist_ok=True)
    inputs_hash = _canonical_file_set_hash(output_dir, input_paths)
    outputs_hash = _canonical_file_set_hash(output_dir, output_paths)

    overall = "passed"
    if any(check.get("status") == "failed" for check in checks):
        overall = "failed"
    elif any(check.get("status") in ("warning", "not_testable") for check in checks):
        overall = "warning"

    project["runs"] = {"latest": {"id": RUN_ID, "started_at": FIXED_TIME,
                                   "completed_at": FIXED_TIME,
                                   "status": overall,
                                   "inputs_hash": inputs_hash, "outputs_hash": outputs_hash,
                                   "validation_report": {"path": "validation/latest-report.json"}}}
    (output_dir / "project.yaml").write_text(
        yaml.dump(project, sort_keys=False, allow_unicode=True), encoding="utf-8")

    report = {"run_id": RUN_ID, "schema": "openmapstack-project/v1", "status": overall,
              "checks": checks, "inputs_hash": inputs_hash, "outputs_hash": outputs_hash}
    (output_dir / "validation" / "latest-report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8")

    run_record = {"run_id": RUN_ID, "started_at": FIXED_TIME, "completed_at": FIXED_TIME,
                  "status": overall, "inputs_hash": inputs_hash, "outputs_hash": outputs_hash,
                  "environment": {"python": "3.12", "duckdb": duckdb.__version__},
                  "inputs": _inventory(output_dir, input_paths),
                  "outputs": _inventory(output_dir, output_paths)}
    (output_dir / "runs" / f"{RUN_ID}.json").write_text(
        json.dumps(run_record, indent=2), encoding="utf-8")



def _standard_checks(row_count: int, extra: list | None = None) -> list:
    return [
        {"id": "geometry_valid", "status": "passed", "features_checked": row_count, "invalid_count": 0},
        {"id": "crs_known", "status": "passed", "expected": ANALYSIS_CRS, "actual": ANALYSIS_CRS},
        {"id": "row_count_gt_zero", "status": "passed", "rows": row_count},
        {"id": "manifest_graph_resolves", "status": "passed", "steps_checked": True},
    ] + (extra or [])


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

def build_boundary(output_dir: Path, break_mode: str | None) -> None:
    """Candidates = parcels intersecting a 100 m road buffer. P-A only
    *touches* the buffer boundary (intersects, not contains); P-D is a
    donut whose hole must reduce its measured area."""
    _copy_inputs("boundary", output_dir)
    con = _connect_spatial()
    try:
        if break_mode == "contains-vs-intersects":
            predicate = "ST_Contains(road_buffer.geom, p.geom)"
        else:
            predicate = "ST_Intersects(p.geom, road_buffer.geom)"
        con.execute(f"""
            CREATE TABLE road_buffer AS
            SELECT ST_Buffer(geom, 100) AS geom FROM ST_Read('{(output_dir / "data/source/road.geojson").as_posix()}')
        """)
        if break_mode == "hole-filled":
            # The defect: rebuild the parcel from its exterior ring only,
            # silently filling the hole and overstating the usable area.
            area_expr = "ROUND(ST_Area(ST_MakePolygon(ST_ExteriorRing(p.geom))), 1)"
            geom_expr = "ST_MakePolygon(ST_ExteriorRing(p.geom)) AS geom"
        else:
            area_expr = "ROUND(ST_Area(p.geom), 1)"
            geom_expr = "p.geom AS geom"
        con.execute(f"""
            CREATE TABLE candidates AS
            SELECT p.parcel_id, {area_expr} AS area_m2, {geom_expr}
            FROM ST_Read('{(output_dir / "data/source/parcels.geojson").as_posix()}') p, road_buffer
            WHERE {predicate}
        """)
        candidates = output_dir / "data" / "derived" / "candidates.parquet"
        con.execute(f"COPY candidates TO '{candidates.as_posix()}' (FORMAT PARQUET)")
        count = con.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
    finally:
        con.close()

    outputs = {"candidates": {"path": "data/derived/candidates.parquet",
                               "format": "GeoParquet", "generated_by": "select_candidates"}}
    project = _project(
        output_dir,
        title="Boundary semantics and polygon holes fixture",
        question="Which parcels intersect the 100 m road buffer, and what are their true (hole-aware) areas?",
        sources={
            "parcels": _source_entry("boundary/parcels.geojson", "synthetic parcels incl. boundary-touch and donut", 4,
                                     "P-A touches the buffer boundary only; P-D is a donut."),
            "road": _source_entry("boundary/road.geojson", "synthetic road centreline", 1, "Buffer source."),
        },
        steps=[
            {"id": "load_parcels", "operation": "read", "source": "parcels", "output": "parcels_raw"},
            {"id": "load_road", "operation": "read", "source": "road", "output": "road_raw"},
            {"id": "buffer_road", "operation": "buffer", "input": "road_raw", "distance_m": 100,
             "crs": ANALYSIS_CRS, "output": "road_buffer"},
            {"id": "select_candidates", "operation": "filter", "input": "parcels_raw",
             "expression": "ST_Intersects(parcels_raw.geom, road_buffer.geom)",
             "crs": ANALYSIS_CRS, "output": "candidates"},
        ],
        outputs=outputs,
        assumptions=[
            {"id": "A1", "statement": "Selection uses ST_Intersects: a parcel merely touching the buffer boundary qualifies.",
             "rationale": "Accessibility to the edge of the corridor; ST_Contains would wrongly exclude it."},
            {"id": "A2", "statement": "Parcel area is measured on the polygon with its hole (ST_Area subtracts interior rings).",
             "rationale": "A filled donut overstates the usable area."},
        ],
    )
    _finish(output_dir, project, _standard_checks(count))


def build_join(output_dir: Path, break_mode: str | None) -> None:
    """POIs within 300 m of each parcel. poi-x and poi-y fall within range
    of TWO parcels (many-to-many); poi-z appears twice in the input with the
    same identifier — a correct join dedupes by pair."""
    _copy_inputs("join", output_dir)
    con = _connect_spatial()
    try:
        distinct = "" if break_mode == "double-count" else "DISTINCT"
        con.execute(f"""
            CREATE TABLE join_pairs AS
            SELECT {distinct} p.parcel_id, q.poi_id,
                   p.parcel_id || '|' || q.poi_id AS pair_id
            FROM ST_Read('{(output_dir / "data/source/parcels.geojson").as_posix()}') p,
                 ST_Read('{(output_dir / "data/source/pois.geojson").as_posix()}') q
            WHERE ST_DWithin(p.geom, q.geom, 300)
            ORDER BY pair_id
        """)
        pairs = output_dir / "data" / "derived" / "join-pairs.parquet"
        con.execute(f"COPY join_pairs TO '{pairs.as_posix()}' (FORMAT PARQUET)")
        count = con.execute("SELECT COUNT(*) FROM join_pairs").fetchone()[0]
    finally:
        con.close()

    outputs = {"join_pairs": {"path": "data/derived/join-pairs.parquet",
                               "format": "GeoParquet", "generated_by": "spatial_join"}}
    project = _project(
        output_dir,
        title="Many-to-many spatial join integrity fixture",
        question="Which POIs lie within 300 m of each parcel, without double counting duplicated inputs?",
        sources={
            "parcels": _source_entry("join/parcels.geojson", "synthetic parcels", 3, "Join left side."),
            "pois": _source_entry("join/pois.geojson", "synthetic POIs incl. one duplicate identifier", 5,
                                   "poi-x and poi-y are within range of two parcels; poi-z is duplicated."),
        },
        steps=[
            {"id": "load_parcels", "operation": "read", "source": "parcels", "output": "parcels_raw"},
            {"id": "load_pois", "operation": "read", "source": "pois", "output": "pois_raw"},
            {"id": "spatial_join", "operation": "spatial_join", "inputs": ["parcels_raw", "pois_raw"],
             "expression": "ST_DWithin(parcels_raw.geom, pois_raw.geom, 300)",
             "dedupe_by": "pair_id", "crs": ANALYSIS_CRS, "output": "join_pairs"},
        ],
        outputs=outputs,
        assumptions=[
            {"id": "A1", "statement": "A many-to-many join yields one row per (parcel, POI) pair; duplicated input rows with the same identifier collapse to one pair.",
             "rationale": "Duplicate POI records must not inflate proximity counts."},
        ],
    )
    _finish(output_dir, project, _standard_checks(count))


def build_crs(output_dir: Path, break_mode: str | None) -> None:
    """The road is legitimately stored in EPSG:4326; metrics are computed in
    EPSG:3301 after reprojection. The mutant lies about the output CRS:
    candidates.geojson declares EPSG:4326 while its coordinates are still
    EPSG:3301 metres."""
    _copy_inputs("crs", output_dir)
    con = _connect_spatial()
    try:
        con.execute(f"""
            CREATE TABLE road_3301 AS
            SELECT ST_Transform(geom, 'EPSG:4326', 'EPSG:3301', true) AS geom, road_id
            FROM ST_Read('{(output_dir / "data/source/road-4326.geojson").as_posix()}')
        """)
        con.execute(f"""
            CREATE TABLE candidates AS
            SELECT p.parcel_id,
                   ROUND(ST_Distance(p.geom, r.geom), 2) AS dist_m,
                   ROUND(ST_Area(p.geom), 1) AS area_m2,
                   p.geom
            FROM ST_Read('{(output_dir / "data/source/parcels-3301.geojson").as_posix()}') p,
                 road_3301 r
            WHERE ST_Distance(p.geom, r.geom) <= 200
            ORDER BY parcel_id
        """)
        parquet = output_dir / "data" / "derived" / "candidates.parquet"
        con.execute(f"COPY candidates TO '{parquet.as_posix()}' (FORMAT PARQUET)")
        geojson = output_dir / "data" / "derived" / "candidates.geojson"
        con.execute(f"COPY candidates TO '{geojson.as_posix()}' (FORMAT GDAL, DRIVER 'GeoJSON')")
        count = con.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
    finally:
        con.close()

    if break_mode == "crs-metadata-mismatch":
        # Relabel, don't transform: the coordinates stay EPSG:3301 metres
        # while the file claims EPSG:4326 — the metadata-mismatch defect.
        data = json.loads(geojson.read_text(encoding="utf-8"))
        data["crs"] = {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::4326"}}
        geojson.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")

    outputs = {
        "candidates": {"path": "data/derived/candidates.parquet", "format": "GeoParquet",
                        "crs": ANALYSIS_CRS, "generated_by": "distance_filter"},
        "candidates_geojson": {"path": "data/derived/candidates.geojson", "format": "GeoJSON",
                                "crs": ANALYSIS_CRS, "generated_by": "distance_filter"},
    }
    project = _project(
        output_dir,
        title="Mixed-CRS reprojection fixture",
        question="Which parcels lie within 200 m of the road when the road is stored in WGS84 and metrics run in EPSG:3301?",
        sources={
            "parcels": _source_entry("crs/parcels-3301.geojson", "synthetic parcels (EPSG:3301)", 3,
                                      "Analysis side, already projected."),
            "road": _source_entry("crs/road-4326.geojson", "synthetic road (EPSG:4326 storage)", 1,
                                   "Legitimate geographic storage; must be reprojected before metric use."),
        },
        steps=[
            {"id": "load_parcels", "operation": "read", "source": "parcels", "output": "parcels_raw"},
            {"id": "load_road", "operation": "read", "source": "road", "output": "road_wgs84"},
            {"id": "reproject_road", "operation": "reproject", "input": "road_wgs84",
             "from_crs": "EPSG:4326", "to_crs": ANALYSIS_CRS, "output": "road_3301"},
            {"id": "distance_filter", "operation": "distance_filter", "input": "parcels_raw",
             "target": "road_3301", "max_distance_m": 200, "crs": ANALYSIS_CRS,
             "output": "candidates,candidates_geojson"},
        ],
        outputs=outputs,
        assumptions=[
            {"id": "A1", "statement": "Distances are valid only after reprojection; geographic degrees are never used as metres.",
             "rationale": "Reprojecting the WGS84 road to EPSG:3301 recovers the original line to sub-millimetre accuracy."},
        ],
    )
    extra = {"id": "reprojection_roundtrip", "status": "passed",
             "detail": "EPSG:4326 road reprojected to EPSG:3301 matches the surveyed line to sub-mm"}
    _finish(output_dir, project, _standard_checks(count, [extra]))


def build_health(output_dir: Path, break_mode: str | None) -> None:
    """Input mixes a valid polygon, a self-intersecting bow tie, a null
    geometry with valid attributes, and a second valid polygon. Declared
    handling: invalid and null-geometry features are excluded from the
    spatial output and reported."""
    _copy_inputs("health", output_dir)
    con = _connect_spatial()
    try:
        if break_mode == "invalid-accepted":
            # The defect: silently "repair" the bow tie and ship it as if
            # the source had been valid all along.
            geom_expr = "ST_MakeValid(geom)"
            where = "geom IS NOT NULL"
        else:
            geom_expr = "geom"
            where = "geom IS NOT NULL AND ST_IsValid(geom)"
        con.execute(f"""
            CREATE TABLE valid_parcels AS
            SELECT parcel_id, {geom_expr} AS geom
            FROM ST_Read('{(output_dir / "data/source/parcels.geojson").as_posix()}')
            WHERE {where}
        """)
        out = output_dir / "data" / "derived" / "valid-parcels.parquet"
        con.execute(f"COPY valid_parcels TO '{out.as_posix()}' (FORMAT PARQUET)")
        count = con.execute("SELECT COUNT(*) FROM valid_parcels").fetchone()[0]
    finally:
        con.close()

    outputs = {"valid_parcels": {"path": "data/derived/valid-parcels.parquet",
                                   "format": "GeoParquet", "generated_by": "geometry_health"}}
    project = _project(
        output_dir,
        title="Invalid and null geometry handling fixture",
        question="Which parcels are geometrically usable, and what happened to the ones that are not?",
        sources={
            "parcels": _source_entry("health/parcels.geojson", "synthetic parcels incl. bow tie and null geometry", 4,
                                      "H-B is self-intersecting; H-C has a null geometry."),
        },
        steps=[
            {"id": "load_parcels", "operation": "read", "source": "parcels", "output": "parcels_raw"},
            {"id": "geometry_health", "operation": "filter", "input": "parcels_raw",
             "expression": "geom IS NOT NULL AND ST_IsValid(geom)",
             "crs": ANALYSIS_CRS, "output": "valid_parcels"},
        ],
        outputs=outputs,
        assumptions=[
            {"id": "A1", "statement": "Invalid geometries are excluded and reported, never silently repaired into the result.",
             "rationale": "A repaired bow tie changes the feature the source describes; the correction must be a declared override, not a pipeline side effect."},
        ],
    )
    extra = {"id": "invalid_input_excluded", "status": "passed",
             "excluded": [{"id": "H-B", "reason": "self-intersecting"}, {"id": "H-C", "reason": "null geometry"}]}
    _finish(output_dir, project, _standard_checks(count, [extra]))


def build_formats(output_dir: Path, break_mode: str | None) -> None:
    """One analysis result delivered as GeoPackage, GeoParquet and GeoJSON —
    every declared format must carry the same features."""
    _copy_inputs("formats", output_dir)
    con = _connect_spatial()
    try:
        con.execute(f"""
            CREATE TABLE result AS
            SELECT parcel_id, zone, ROUND(ST_Area(geom), 1) AS area_m2, geom
            FROM ST_Read('{(output_dir / "data/source/parcels.geojson").as_posix()}')
            ORDER BY parcel_id
        """)
        gpkg = output_dir / "data" / "derived" / "result.gpkg"
        parquet = output_dir / "data" / "derived" / "result.parquet"
        geojson = output_dir / "data" / "derived" / "result.geojson"
        if break_mode == "format-drift":
            con.execute("CREATE TABLE drifted AS SELECT * FROM result WHERE parcel_id = 'F-A'")
            con.execute(f"COPY drifted TO '{gpkg.as_posix()}' (FORMAT GDAL, DRIVER 'GPKG')")
        else:
            con.execute(f"COPY result TO '{gpkg.as_posix()}' (FORMAT GDAL, DRIVER 'GPKG')")
        con.execute(f"COPY result TO '{parquet.as_posix()}' (FORMAT PARQUET)")
        con.execute(f"COPY result TO '{geojson.as_posix()}' (FORMAT GDAL, DRIVER 'GeoJSON')")
        count = con.execute("SELECT COUNT(*) FROM result").fetchone()[0]
    finally:
        con.close()

    outputs = {
        "result_gpkg": {"path": "data/derived/result.gpkg", "format": "GeoPackage",
                          "crs": ANALYSIS_CRS, "generated_by": "classify"},
        "result_parquet": {"path": "data/derived/result.parquet", "format": "GeoParquet",
                            "crs": ANALYSIS_CRS, "generated_by": "classify"},
        "result_geojson": {"path": "data/derived/result.geojson", "format": "GeoJSON",
                            "crs": ANALYSIS_CRS, "generated_by": "classify"},
    }
    project = _project(
        output_dir,
        title="Multi-format output parity fixture",
        question="Deliver the same classified parcels as GeoPackage, GeoParquet and GeoJSON without format drift.",
        sources={
            "parcels": _source_entry("formats/parcels.geojson", "synthetic parcels", 2, "Format-parity input."),
        },
        steps=[
            {"id": "load_parcels", "operation": "read", "source": "parcels", "output": "parcels_raw"},
            {"id": "classify", "operation": "passthrough", "input": "parcels_raw",
             "crs": ANALYSIS_CRS, "output": "result,result_gpkg,result_parquet,result_geojson"},
        ],
        outputs=outputs,
        assumptions=[
            {"id": "A1", "statement": "Every declared storage format carries the identical feature set; a format that silently drops rows fails.",
             "rationale": "Consumers pick a format and must not inherit a filtered subset."},
        ],
    )
    _finish(output_dir, project, _standard_checks(count))


def build_overrides(output_dir: Path, break_mode: str | None) -> None:
    """Ordered attribute-override chain, a conflicting override that must be
    rejected against the immutable source, and a hide_feature override."""
    _copy_inputs("overrides", output_dir)
    con = _connect_spatial()
    try:
        con.execute(f"""
            CREATE TABLE pois AS
            SELECT poi_id, status, geom
            FROM ST_Read('{(output_dir / "data/source/pois.geojson").as_posix()}')
        """)
        if break_mode == "conflict-ignored":
            con.execute("UPDATE pois SET status = 'open' WHERE poi_id = 'poi-2'")  # O-002 applied despite mismatch
        if break_mode == "ordered-swapped":
            # O-005 executes before O-004: its declared `from` (in_renovation)
            # does not match the source value (active), so it must be rejected
            # and poi-4 stays in_renovation only if O-004 ran. Swapping means
            # O-004 runs last, leaving in_renovation.
            con.execute("UPDATE pois SET status = 'in_renovation' WHERE poi_id = 'poi-4'")
        else:
            con.execute("UPDATE pois SET status = 'in_renovation' WHERE poi_id = 'poi-4'")   # O-004
            con.execute("UPDATE pois SET status = 'closed' WHERE poi_id = 'poi-4'")          # O-005
        con.execute("UPDATE pois SET status = 'closed' WHERE poi_id = 'poi-1'")              # O-001
        con.execute("DELETE FROM pois WHERE poi_id = 'poi-3'")                                # O-003 hide
        out = output_dir / "data" / "derived" / "pois-effective.geojson"
        con.execute(f"COPY pois TO '{out.as_posix()}' (FORMAT GDAL, DRIVER 'GeoJSON')")
        count = con.execute("SELECT COUNT(*) FROM pois").fetchone()[0]
    finally:
        con.close()

    overrides = [
        {"id": "O-001", "action": "modify_attribute",
         "target": {"source": "pois", "feature_id": "poi-1"},
         "change": {"field": "status", "from": "active", "to": "closed"},
         "rationale": "Field survey confirmed closure.",
         "evidence": [{"type": "field_survey", "value": "Surveyed 2026-08-20; kiosk shuttered"}],
         "created_at": FIXED_TIME, "created_by": "analyst"},
        {"id": "O-002", "action": "modify_attribute",
         "target": {"source": "pois", "feature_id": "poi-2"},
         "change": {"field": "status", "from": "closed", "to": "open"},
         "rationale": "Stale planning claim: asserts a prior value the source does not have.",
         "evidence": [{"type": "planning_document", "value": "Register entry predates the current source"}],
         "created_at": FIXED_TIME, "created_by": "analyst",
         "expected_outcome": "rejected"},
        {"id": "O-003", "action": "hide_feature",
         "target": {"source": "pois", "feature_id": "poi-3"},
         "rationale": "Duplicate record of poi-2; hidden from the effective view.",
         "evidence": [{"type": "field_survey", "value": "On-site check: same kiosk as poi-2"}],
         "created_at": FIXED_TIME, "created_by": "analyst"},
        {"id": "O-004", "action": "modify_attribute",
         "target": {"source": "pois", "feature_id": "poi-4"},
         "change": {"field": "status", "from": "active", "to": "in_renovation"},
         "rationale": "Renovation works started; first stage of a two-step ordered change.",
         "evidence": [{"type": "field_survey", "value": "Scaffolding observed 2026-08-25"}],
         "created_at": FIXED_TIME, "created_by": "analyst"},
        {"id": "O-005", "action": "modify_attribute",
         "target": {"source": "pois", "feature_id": "poi-4"},
         "change": {"field": "status", "from": "in_renovation", "to": "closed"},
         "rationale": "Ordered after O-004: closure only takes effect once the renovation stage is recorded.",
         "evidence": [{"type": "field_survey", "value": "Renovation completed 2026-08-28"}],
         "created_at": FIXED_TIME, "created_by": "analyst"},
    ]

    outputs = {"pois_effective": {"path": "data/derived/pois-effective.geojson",
                                    "format": "GeoJSON", "crs": ANALYSIS_CRS,
                                    "generated_by": "apply_overrides"}}
    project = _project(
        output_dir,
        title="Ordered and conflicting override semantics fixture",
        question="Apply the declared overrides in order: an ordered two-stage change, a prior-value conflict that must be rejected, and a hidden duplicate.",
        sources={
            "pois": _source_entry("overrides/pois.geojson", "synthetic POIs", 4,
                                   "Override targets; the immutable source is never modified."),
        },
        steps=[
            {"id": "load_pois", "operation": "read", "source": "pois", "output": "pois_raw"},
            {"id": "apply_overrides", "operation": "apply_override", "input": "pois_raw",
             "override": "O-001", "output": "pois_step1"},
            {"id": "apply_o002", "operation": "apply_override", "input": "pois_step1",
             "override": "O-002", "output": "pois_step2"},
            {"id": "apply_o003", "operation": "apply_override", "input": "pois_step2",
             "override": "O-003", "output": "pois_step3"},
            {"id": "apply_o004", "operation": "apply_override", "input": "pois_step3",
             "override": "O-004", "output": "pois_step4"},
            {"id": "apply_o005", "operation": "apply_override", "input": "pois_step4",
             "override": "O-005", "output": "pois_effective"},
        ],
        outputs=outputs,
        overrides=overrides,
        assumptions=[
            {"id": "A1", "statement": "Overrides apply strictly in declared order; a modify_attribute whose prior value does not match the immutable source is rejected, never forced.",
             "rationale": "Forcing a stale prior value would launder an unverified claim into the published result."},
        ],
    )
    # The report records what the run ACTUALLY did — including the mutant's
    # defect — so assertions grading override outcomes grade real behavior.
    if break_mode == "conflict-ignored":
        o002 = {"id": "O-002", "status": "applied",
                "detail": "prior-value mismatch IGNORED: forced poi-2 status active -> open"}
        o005 = {"id": "O-005", "status": "applied", "detail": "poi-4 status in_renovation -> closed"}
    elif break_mode == "ordered-swapped":
        o002 = {"id": "O-002", "status": "rejected",
                "detail": "declared from 'closed' but immutable source has 'active'; no source mutation"}
        o005 = {"id": "O-005", "status": "rejected",
                "detail": "executed before O-004: declared from 'in_renovation' does not match source 'active'"}
    else:
        o002 = {"id": "O-002", "status": "rejected",
                "detail": "declared from 'closed' but immutable source has 'active'; no source mutation"}
        o005 = {"id": "O-005", "status": "applied", "detail": "poi-4 status in_renovation -> closed"}
    extra = {"id": "overrides_applied", "status": "passed",
             "declared": [o["id"] for o in overrides],
             "results": [
                 {"id": "O-001", "status": "applied", "detail": "poi-1 status active -> closed"},
                 o002,
                 {"id": "O-003", "status": "applied", "detail": "poi-3 hidden from effective output"},
                 {"id": "O-004", "status": "applied", "detail": "poi-4 status active -> in_renovation"},
                 o005,
             ]}
    project["validation"]["required"].append("overrides_applied")
    _finish(output_dir, project, _standard_checks(count, [extra]))


BUILDERS = {
    "boundary": build_boundary,
    "join": build_join,
    "crs": build_crs,
    "health": build_health,
    "formats": build_formats,
    "overrides": build_overrides,
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--scenario", required=True, choices=sorted(BUILDERS))
    parser.add_argument("--break", dest="break_mode", default=None)
    args = parser.parse_args()

    output_dir = args.output_dir
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)
    BUILDERS[args.scenario](output_dir, args.break_mode)
    print(f"wrote {output_dir} (scenario={args.scenario})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
