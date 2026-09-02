"""`openmapstack verify` on projects whose data this repository has never seen.

Issue #13's definition of done asks for verify to run against at least two
projects absent from the fixtures and emit stable text and JSON reports.
The two projects here are built from scratch by pure-Python pipelines over
invented geodata (a river-crossing screening and a facility catchment
count); nothing under evals/ is read. The reports are compared against
committed goldens after path and timing normalisation, so an unintended
change in plan composition, ordering, wording, or JSON shape shows up as a
diff rather than passing unnoticed.
"""

from __future__ import annotations

import io
import json
import re
import subprocess
import sys
import textwrap
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import yaml

from openmapstack.api import validate_verify_result
from openmapstack.cli import main
from tests.evals.helpers import make_workspace

GOLDEN_DIR = Path(__file__).resolve().parent / "goldens" / "verify"


def _crossings_project(root: Path) -> None:
    """Project A: which planned trails cross a river (line/line predicate)."""
    (root / "data/source").mkdir(parents=True)
    trails = {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "properties": {"trail_id": f"T{i}", "name": f"Trail {i}"},
             "geometry": {"type": "LineString", "coordinates": [[i * 100.0, 0.0], [i * 100.0, 1000.0 if i % 2 else 400.0]]}}
            for i in range(1, 7)
        ],
    }
    river = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"river_id": "R1"},
         "geometry": {"type": "LineString", "coordinates": [[0.0, 500.0], [1000.0, 500.0]]}}
    ]}
    (root / "data/source/trails.geojson").write_text(json.dumps(trails), encoding="utf-8")
    (root / "data/source/river.geojson").write_text(json.dumps(river), encoding="utf-8")
    (root / "pipeline.py").write_text(textwrap.dedent('''
        import json, hashlib, sys
        from pathlib import Path
        ROOT = Path(__file__).resolve().parent
        argv = sys.argv[1:]
        crossing_y = float(argv[argv.index("--river-y") + 1]) if "--river-y" in argv else 500.0
        trails = json.loads((ROOT / "data/source/trails.geojson").read_text())["features"]
        crossing = []
        for feature in sorted(trails, key=lambda f: f["properties"]["trail_id"]):
            (x0, y0), (x1, y1) = feature["geometry"]["coordinates"]
            if min(y0, y1) <= crossing_y <= max(y0, y1):
                crossing.append({"type": "Feature", "properties": dict(feature["properties"]), "geometry": feature["geometry"]})
        (ROOT / "data/derived").mkdir(exist_ok=True)
        out = ROOT / "data/derived/crossing-trails.geojson"
        # GeoJSON without a crs member reads as WGS84; the output is in the
        # analysis CRS, so say so -- verify cross-checks this against the data.
        crs = {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::3301"}}
        out.write_text(json.dumps({"type": "FeatureCollection", "crs": crs, "features": crossing}))
        def h(p):
            return "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest()
        inputs = sorted(p for p in (ROOT / "data/source").rglob("*") if p.is_file()) + [ROOT / "pipeline.py"]
        def agg(paths):
            d = hashlib.sha256()
            for p in sorted(paths, key=lambda p: p.relative_to(ROOT).as_posix()):
                rel = p.relative_to(ROOT).as_posix().encode()
                d.update(len(rel).to_bytes(8, "big")); d.update(rel); d.update(p.read_bytes())
            return "sha256:" + d.hexdigest()
        report = {"run_id": "run-20260901-000000", "status": "passed", "checks": [
            {"id": "geometry_valid", "status": "passed", "features_checked": len(crossing)},
            {"id": "manifest_graph_resolves", "status": "passed"},
        ], "inputs_hash": agg(inputs), "outputs_hash": agg([out])}
        (ROOT / "validation").mkdir(exist_ok=True)
        (ROOT / "validation/latest-report.json").write_text(json.dumps(report, indent=2))
        (ROOT / "runs").mkdir(exist_ok=True)
        (ROOT / "runs/run-20260901-000000.json").write_text(json.dumps({
            "run_id": "run-20260901-000000", "started_at": "2026-09-01T00:00:00Z", "completed_at": "2026-09-01T00:00:01Z",
            "status": "passed", "inputs_hash": report["inputs_hash"], "outputs_hash": report["outputs_hash"],
            "inputs": [{"path": p.relative_to(ROOT).as_posix(), "sha256": h(p)} for p in inputs],
            "outputs": [{"path": out.relative_to(ROOT).as_posix(), "sha256": h(out)}],
        }, indent=2))
    ''').lstrip(), encoding="utf-8")
    subprocess.run([sys.executable, "pipeline.py"], cwd=root, check=True)
    report = json.loads((root / "validation/latest-report.json").read_text())
    manifest = {
        "schema": "openmapstack-project/v1",
        "project": {"id": "river-crossings", "title": "Planned trails crossing the river", "question": "Which planned trails cross the river?",
                    "created_at": "2026-09-01T00:00:00Z", "updated_at": "2026-09-01T00:00:00Z", "status": "validated"},
        "interpretation": {"objective": "Select trails whose line intersects the river centreline.",
                           "assumptions": [{"id": "A1", "statement": "Crossing means the trail line intersects the river line.", "rationale": "No bridge data is available."}]},
        "sources": {
            "trails": {"role": "authoritative_input", "provider": "Trail planning office", "dataset": "planned trails", "source_url": "https://example.invalid/trails",
                       "access": {"method": "local", "retrieved_at": "2026-09-01T00:00:00Z"}, "version": {"identifier": "plan-2026-09", "published_at": "2026-09-01"},
                       "selection": {"filter": "all"}, "license": {"name": "CC BY 4.0", "url": "https://example.invalid/license"}, "rationale": "Only planning dataset available."},
            "river": {"role": "context", "provider": "Hydrology agency", "dataset": "river centreline", "source_url": "https://example.invalid/river",
                      "access": {"method": "local", "retrieved_at": "2026-09-01T00:00:00Z"}, "version": {"identifier": "hydro-2026", "published_at": "2026-08-01"},
                      "selection": {"filter": "river_id = 'R1'"}, "license": {"name": "CC BY 4.0", "url": "https://example.invalid/license"}, "rationale": "Authoritative centreline."},
        },
        "overrides": [],
        "processing": {"analysis_crs": "EPSG:3301", "storage_crs": "EPSG:3301", "steps": [
            {"id": "load_trails", "operation": "read", "source": "trails", "output": "trails_raw"},
            {"id": "load_river", "operation": "read", "source": "river", "output": "river_raw"},
            {"id": "select_crossings", "operation": "intersects_filter", "input": "trails_raw", "target": "river_raw", "crs": "EPSG:3301", "output": "crossing_trails"},
        ]},
        "outputs": {"crossing_trails": {"path": "data/derived/crossing-trails.geojson", "format": "GeoJSON (EPSG:3301)", "generated_by": "select_crossings"}},
        "validation": {"required": ["geometry_valid", "manifest_graph_resolves"], "domain_checks": [],
                       "metamorphic": [{"id": "trail-order", "relation": "input_permutation_invariance", "source": {"path": "data/source/trails.geojson"},
                                        "outputs": ["crossing_trails"], "key": "trail_id", "preconditions": {"tie_break": "keyed by trail_id; sorted output"}}]},
        "presentation": {"intent": "report", "primary_view": "report", "layout": {"type": "report"},
                         "map": {"engine_preference": "maplibre", "basemap": {"id": "osm-standard", "kind": "raster-xyz", "tiles": ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"], "attribution": "© OpenStreetMap contributors"},
                                 "layer_groups": [{"id": "analysis", "title": "Analysis"}],
                                 "layers": [{"source": "crossing_trails", "group": "analysis", "semantic_role": "primary_result", "geometry": "line"}]}},
        "warnings": [],
        "runtime": {"implementation": {"preferred_engine": "python", "pipeline": "pipeline.py",
                                       "parameters": [{"id": "river_y", "type": "number", "canonical": 500, "binding": {"argument": "--river-y"}}]},
                    "environment": {"python": "3.12"}},
        "runs": {"latest": {"id": "run-20260901-000000", "started_at": "2026-09-01T00:00:00Z", "completed_at": "2026-09-01T00:00:01Z", "status": "passed",
                            "inputs_hash": report["inputs_hash"], "outputs_hash": report["outputs_hash"], "validation_report": {"path": "validation/latest-report.json"}}},
    }
    (root / "project.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")


def _catchments_project(root: Path) -> None:
    """Project B: facilities per district (point-in-polygon count), with an
    unverified expectation and an override, exercising more of the plan."""
    (root / "data/source").mkdir(parents=True)
    (root / "data/overrides").mkdir()
    districts = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"district_id": "D1"}, "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [500, 0], [500, 500], [0, 500], [0, 0]]]}},
        {"type": "Feature", "properties": {"district_id": "D2"}, "geometry": {"type": "Polygon", "coordinates": [[[500, 0], [1000, 0], [1000, 500], [500, 500], [500, 0]]]}},
    ]}
    facilities = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"facility_id": f"F{i}", "status": "open"}, "geometry": {"type": "Point", "coordinates": [x, 250.0]}}
        for i, x in enumerate((100.0, 200.0, 700.0, 900.0, 950.0), start=1)
    ]}
    (root / "data/source/districts.geojson").write_text(json.dumps(districts), encoding="utf-8")
    (root / "data/source/facilities.geojson").write_text(json.dumps(facilities), encoding="utf-8")
    (root / "pipeline.py").write_text(textwrap.dedent('''
        import json, hashlib
        from pathlib import Path
        ROOT = Path(__file__).resolve().parent
        districts = json.loads((ROOT / "data/source/districts.geojson").read_text())["features"]
        facilities = json.loads((ROOT / "data/source/facilities.geojson").read_text())["features"]
        closed = {"F5"}  # OVERRIDE-001
        counts = []
        for d in sorted(districts, key=lambda f: f["properties"]["district_id"]):
            xs = [pt[0] for pt in d["geometry"]["coordinates"][0]]
            n = sum(1 for f in facilities if f["properties"]["facility_id"] not in closed and min(xs) <= f["geometry"]["coordinates"][0] < max(xs))
            counts.append({"type": "Feature", "properties": {"district_id": d["properties"]["district_id"], "facility_count": n}, "geometry": d["geometry"]})
        (ROOT / "data/derived").mkdir(exist_ok=True)
        out = ROOT / "data/derived/district-counts.geojson"
        out.write_text(json.dumps({"type": "FeatureCollection", "features": counts}))
        def h(p):
            return "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest()
        inputs = sorted(p for d in ("data/source", "data/overrides") for p in (ROOT / d).rglob("*") if p.is_file()) + [ROOT / "pipeline.py"]
        def agg(paths):
            d = hashlib.sha256()
            for p in sorted(paths, key=lambda p: p.relative_to(ROOT).as_posix()):
                rel = p.relative_to(ROOT).as_posix().encode()
                d.update(len(rel).to_bytes(8, "big")); d.update(rel); d.update(p.read_bytes())
            return "sha256:" + d.hexdigest()
        report = {"run_id": "run-20260901-000001", "status": "warning", "checks": [
            {"id": "geometry_valid", "status": "passed", "features_checked": 2},
            {"id": "overrides_applied", "status": "passed", "results": [{"id": "OVERRIDE-001", "status": "applied"}]},
            {"id": "facility_completeness", "status": "warning", "reason": "no completeness baseline"},
        ], "inputs_hash": agg(inputs), "outputs_hash": agg([out])}
        (ROOT / "validation").mkdir(exist_ok=True)
        (ROOT / "validation/latest-report.json").write_text(json.dumps(report, indent=2))
        (ROOT / "runs").mkdir(exist_ok=True)
        (ROOT / "runs/run-20260901-000001.json").write_text(json.dumps({
            "run_id": "run-20260901-000001", "started_at": "2026-09-01T00:00:00Z", "completed_at": "2026-09-01T00:00:01Z",
            "status": "warning", "inputs_hash": report["inputs_hash"], "outputs_hash": report["outputs_hash"],
            "inputs": [{"path": p.relative_to(ROOT).as_posix(), "sha256": h(p)} for p in inputs],
            "outputs": [{"path": out.relative_to(ROOT).as_posix(), "sha256": h(out)}],
        }, indent=2))
    ''').lstrip(), encoding="utf-8")
    (root / "data/overrides/closures.json").write_text(json.dumps({"closed": ["F5"]}), encoding="utf-8")
    subprocess.run([sys.executable, "pipeline.py"], cwd=root, check=True)
    report = json.loads((root / "validation/latest-report.json").read_text())
    manifest = {
        "schema": "openmapstack-project/v1",
        "project": {"id": "district-facilities", "title": "Open facilities per district", "question": "How many open facilities does each district have?",
                    "created_at": "2026-09-01T00:00:00Z", "updated_at": "2026-09-01T00:00:00Z", "status": "warning"},
        "interpretation": {"objective": "Count open facilities inside each district polygon.",
                           "assumptions": [{"id": "A1", "statement": "A facility on the shared boundary belongs to the western district.", "rationale": "Half-open interval avoids double counting."}]},
        "sources": {
            "districts": {"role": "authoritative_input", "provider": "City", "dataset": "districts", "source_url": "https://example.invalid/districts",
                          "access": {"method": "local", "retrieved_at": "2026-09-01T00:00:00Z"}, "version": {"identifier": "districts-2026", "published_at": "2026-01-01"},
                          "selection": {"filter": "all"}, "license": {"name": "CC0", "url": "https://example.invalid/cc0"}, "rationale": "Official boundaries."},
            "facilities": {"role": "authoritative_input", "provider": "City", "dataset": "facilities", "source_url": "https://example.invalid/facilities",
                           "access": {"method": "local", "retrieved_at": "2026-09-01T00:00:00Z"}, "version": {"identifier": "facilities-2026-08", "published_at": "2026-08-01"},
                           "selection": {"filter": "status = 'open'"}, "license": {"name": "CC0", "url": "https://example.invalid/cc0"}, "rationale": "Official register."},
        },
        "overrides": [{"id": "OVERRIDE-001", "action": "modify_attribute", "target": {"source": "facilities", "feature_id": "F5"},
                       "change": {"field": "status", "from": "open", "to": "closed"}, "rationale": "Closed after the register was published.",
                       "evidence": [{"type": "url", "value": "https://example.invalid/notice/F5"}], "created_at": "2026-09-01T00:00:00Z", "created_by": "analyst"}],
        "processing": {"analysis_crs": "EPSG:3301", "storage_crs": "EPSG:3301", "steps": [
            {"id": "load_districts", "operation": "read", "source": "districts", "output": "districts_raw"},
            {"id": "load_facilities", "operation": "read", "source": "facilities", "output": "facilities_raw"},
            {"id": "apply_closures", "operation": "apply_override", "input": "facilities_raw", "override": "OVERRIDE-001", "output": "facilities_effective"},
            {"id": "count_per_district", "operation": "point_in_polygon_count", "inputs": ["districts_raw", "facilities_effective"], "crs": "EPSG:3301", "output": "district_counts"},
        ]},
        "outputs": {"district_counts": {"path": "data/derived/district-counts.geojson", "format": "GeoJSON", "generated_by": "count_per_district"}},
        "validation": {"required": ["geometry_valid", "overrides_applied"], "domain_checks": [{"name": "facility_completeness", "expression": "count >= 0"}],
                       "expectations": [{"id": "d1-count", "check": "geodata.feature_field_equals",
                                         "args": {"path": "data/derived/district-counts.geojson", "id_field": "district_id", "id": "D1", "field": "facility_count", "equals": 2},
                                         "attestation": {"status": "unverified", "reason": "awaiting register comparison"}}]},
        "presentation": {"intent": "analytical_workspace", "primary_view": "map", "layout": {"type": "map_with_sidebar"},
                         "map": {"engine_preference": "maplibre", "basemap": {"id": "osm-standard", "kind": "raster-xyz", "tiles": ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"], "attribution": "© OpenStreetMap contributors"},
                                 "layer_groups": [{"id": "analysis", "title": "Analysis"}, {"id": "user_overrides", "title": "Corrections"}],
                                 "layers": [{"source": "district_counts", "group": "analysis", "semantic_role": "primary_result", "geometry": "polygon"},
                                            {"source": "facilities", "group": "user_overrides", "semantic_role": "user_override", "geometry": "point"}]}},
        "warnings": [{"id": "DATA-001", "severity": "medium", "layer": "facilities", "issue": "completeness_unknown", "statement": "The register may omit facilities.", "mitigation": "Verify before decisions."}],
        "runtime": {"implementation": {"preferred_engine": "python", "pipeline": "pipeline.py"}, "environment": {"python": "3.12"}},
        "runs": {"latest": {"id": "run-20260901-000001", "started_at": "2026-09-01T00:00:00Z", "completed_at": "2026-09-01T00:00:01Z", "status": "warning",
                            "inputs_hash": report["inputs_hash"], "outputs_hash": report["outputs_hash"], "validation_report": {"path": "validation/latest-report.json"}}},
    }
    (root / "project.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True), encoding="utf-8")


def _normalise_text(text: str, root: Path) -> str:
    return text.replace(str(root), "$PROJECT")


def _normalise_json(payload: dict, root: Path) -> dict:
    text = json.dumps(payload, sort_keys=True)
    text = text.replace(str(root), "$PROJECT")
    payload = json.loads(text)
    for check in payload.get("checks", []):
        evidence = check.get("evidence")
        if isinstance(evidence, dict):
            for volatile in ("duration_s", "removed_environment_keys", "command", "preserved_paths"):
                evidence.pop(volatile, None)
            for volatile in ("expected_expectation_sha256", "current_inputs_hash"):
                if volatile in evidence:
                    evidence[volatile] = "$DIGEST"
        if "message" in check:
            check["message"] = re.sub(r"sha256:[0-9a-f]{64}", "sha256:$DIGEST", check["message"])
    return payload


class ForeignProjectVerifyTests(unittest.TestCase):
    maxDiff = None

    def _run(self, name: str, builder) -> tuple[str, dict]:
        root = make_workspace() / name
        root.mkdir()
        builder(root)
        text = io.StringIO()
        with redirect_stdout(text):
            main(["verify", str(root / "project.yaml"), "--verbose", "--metamorphic"])
        json_out = io.StringIO()
        with redirect_stdout(json_out):
            main(["verify", str(root / "project.yaml"), "--json", "--metamorphic"])
        payload = json.loads(json_out.getvalue())
        self.assertEqual(validate_verify_result(payload), [])
        return _normalise_text(text.getvalue(), root), _normalise_json(payload, root)

    def _assert_golden(self, name: str, text: str, payload: dict) -> None:
        golden_text = GOLDEN_DIR / f"{name}.txt"
        golden_json = GOLDEN_DIR / f"{name}.json"
        if not golden_text.exists() or not golden_json.exists():
            GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
            golden_text.write_text(text, encoding="utf-8")
            golden_json.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            self.fail(f"goldens for {name} were missing and have been written; re-run to compare")
        self.assertEqual(text, golden_text.read_text(encoding="utf-8"))
        self.assertEqual(payload, json.loads(golden_json.read_text(encoding="utf-8")))

    def test_river_crossings_project_reports_stably(self) -> None:
        text, payload = self._run("river-crossings", _crossings_project)
        statuses = {check["check"]: check["status"] for check in payload["checks"]}
        self.assertEqual(statuses["metamorphic.trail-order"], "passed")
        self.assertEqual(statuses["project.parameters_match_steps"], "passed")
        self.assertEqual(statuses["validation.run_record_matches"], "passed")
        self._assert_golden("river-crossings", text, payload)

    def test_district_facilities_project_reports_stably(self) -> None:
        text, payload = self._run("district-facilities", _catchments_project)
        statuses = {check["check"]: check["status"] for check in payload["checks"]}
        self.assertEqual(statuses["expectation.d1-count"], "warning")
        self.assertEqual(statuses["validation.warning_or_failed_propagates_to_status"], "passed")
        self.assertEqual(payload["status"], "warning")
        self._assert_golden("district-facilities", text, payload)


if __name__ == "__main__":
    unittest.main()
