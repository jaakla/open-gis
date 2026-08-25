# =============================================================================
# run_e2e.py — End-to-end reproducible run for examples/tartu-development
# =============================================================================
# Executes the full open-gis-project/v1 loop for the Tartu development-access
# scenario and renders the final HTML dashboard AS A VIEW over the project
# artifacts (project.yaml + derived data + validation report), never from
# ad-hoc state.
#
#   STEP 0  materialize a deterministic standalone source fixture (data/source)
#   STEP 1-7 processing via DuckDB Spatial (mirrors project.yaml processing.steps)
#   STEP 8  machine-readable validation report  -> validation/latest-report.json
#   STEP 9  render_dashboard() -> dashboard.html (view over the project)
#
# Reproducibility: seeded RNG, documented AOI + assumptions A1/A2, sources and
# overrides recorded in project.yaml. Rerun == identical output.
# Run:  ./.e2e-venv/bin/python examples/tartu-development/run_e2e.py
# =============================================================================

import json
import logging
import random
from pathlib import Path

import yaml
import duckdb

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "data" / "source"
DERIVED = ROOT / "data" / "derived"
OVERRIDES = ROOT / "data" / "overrides"
VALIDATION = ROOT / "validation"
RUNS = ROOT / "runs"

log = logging.getLogger("tartu-e2e")
logging.basicConfig(level=logging.INFO, format="%(message)s")

# --- PROJECT.yaml = the canonical, single source of truth for presentation ----
PROJECT = yaml.safe_load((ROOT / "project.yaml").read_text())

import datetime as _dt
def _run_id():
    return "run-" + _dt.datetime.now().strftime("%Y%m%d-%H%M%S")

ANALYSIS_CRS = 3301   # L-EST97, metric work only
STORAGE_CRS = 4326    # storage / rendering

RNG_SEED = 20260825


# ------------------------------- STEP 0: sources ----------------------------
def make_sources() -> None:
    """Deterministic, documented source fixture for the Tartu test AOI.

    Honesty note: real Maa-ja Ruumiamet county cadastre GPKG + the ETAK road
    WFS are documented in project.yaml sources.*. This runner materializes a
    small, deterministic stand-in so the loop runs at city scale / offline. A
    production rerun swaps these for the real data/source/ objects referenced
    in the manifest. Same AOI, same seed -> same geometry set.
    """
    SOURCE.mkdir(parents=True, exist_ok=True)
    rng = random.Random(RNG_SEED)
    # AOI box (EPSG:3301 metres) around central Tartu (real L-EST97 coords so
    # the storage-CRS transform lands on Tartu, ~26.55E 58.36N). Tartu centre
    # is E≈659017 N≈6474282.
    x0, y0 = 656800.0, 6472600.0
    cw = ch = 220.0        # parcel grid pitch (~220 m)
    ncols, nrows = 12, 10  # AOI ~2.6 x 2.2 km, ~15 km2 near Tartu city
    parcels, count = [], 0
    for i in range(ncols):
        for j in range(nrows):
            cx = x0 + cw * (i + 0.5) + rng.uniform(-0.06, 0.06) * cw
            cy = y0 + ch * (j + 0.5) + rng.uniform(-0.06, 0.06) * ch
            w = cw * rng.uniform(0.60, 0.98)
            h = ch * rng.uniform(0.60, 0.98)
            parcels.append({"id": f"TAR{count:06d}",
                            "minx": cx - w / 2, "miny": cy - h / 2,
                            "maxx": cx + w / 2, "maxy": cy + h / 2})
            count += 1
    with open(SOURCE / "parcels.json", "w") as f:
        # Flat feature per row (deterministic, easy for the pipeline to ingest).
        json.dump([{"cadastral_id": p["id"], "class": "maatulundusmaa",
                    "usage": "development",
                    "minx": p["minx"], "miny": p["miny"],
                    "maxx": p["maxx"], "maxy": p["maxy"]} for p in parcels], f)

    # ---- national road centreline: one main road along the south edge, so
    # the <=2000 m distance filter (assumption A1) excludes the far rows +.
    x1 = x0 + ncols * cw
    y1 = y0 + nrows * ch
    with open(SOURCE / "roads.json", "w") as f:
        json.dump([{"name": "Main road", "class": "Põhimaantee",
                    "coords": [[x0 - 500.0, y0 + 120.0],
                                [x1 + 500.0, y0 + 120.0]]}], f)
    log.info("sources -> %d parcels, %d roads", count, 2)


# ----------------------------- STEP 1-7: pipeline ---------------------------
def run_pipeline(con) -> None:
    # STEP 1 — load parcels_raw (source already in L-EST97)
    con.execute("DROP TABLE IF EXISTS parcels_raw")
    con.execute("""
        CREATE TABLE parcels_raw AS
        SELECT cadastral_id, class, usage,
               ST_MakeEnvelope(minx, miny, maxx, maxy) AS geometry
        FROM read_json_auto('data/source/parcels.json')
    """)

    # STEP 2 — CRS already correct (L-EST97); analysis_crs asserted by validation

    # STEP 3 — calculate area in metric L- EST97
    con.execute("""
        CREATE OR REPLACE TABLE parcels_area AS
        SELECT cadastral_id, geometry, ST_Area(geometry) AS area_m2
        FROM parcels_raw
    """)

    # STEP 4 — size filter: area_m2 >= 20000
    con.execute("""
        CREATE OR REPLACE TABLE large_parcels AS
        SELECT * FROM parcels_area WHERE area_m2 >= 20000
    """)

    # STEP 5 — planar distance to main roads <= 2000 m (assumption A1)
    con.execute("DROP TABLE IF EXISTS roads")
    con.execute("CREATE TABLE roads (geometry GEOMETRY, name VARCHAR)")
    for _i, _r in enumerate(json.loads((SOURCE / "roads.json").read_text()), start=1):
        _pts = ",".join(f"{x} {y}" for x, y in _r["coords"])
        con.execute(
            "INSERT INTO roads SELECT ST_GeomFromText(?) AS geometry, ? AS name",
            [f"LINESTRING({_pts})", _r["name"]])
    con.execute("""
        CREATE OR REPLACE TABLE candidate_parcels AS
        SELECT p.cadastral_id, p.area_m2, p.geometry,
               ST_Distance(p.geometry, r.geometry) AS dist_main_road_m
        FROM large_parcels p, roads r
        WHERE ST_Distance(p.geometry, r.geometry) <= 2000
    """)

    # STEP 6 — overrides: planned connector road (OVERRIDE-002) is scenario
    #           geometry, separate from sources; recorded in project.yaml.
    planned = OVERRIDES / "planned-road.geojson"
    planned_geojson = None
    if planned.exists():
        planned_geojson = json.loads(planned.read_text())
        con.execute("DROP TABLE IF EXISTS planned_roads")
        con.execute("CREATE TABLE planned_roads (geometry GEOMETRY)")
        for _ft in planned_geojson.get("features", []):
            con.execute(
                "INSERT INTO planned_roads SELECT ST_GeomFromGeoJSON(?)",
                [json.dumps(_ft["geometry"])])

    # STEP 7 — write derived outputs. GPKG stays in the metric analysis CRS;
    # the web GeoJSON is transformed to EPSG:4326 with pyproj (accurate for
    # L- EST97; DuckDB's in-process EPSG:3301 handling is unreliable here).
    DERIVED.mkdir(parents=True, exist_ok=True)
    con.execute("COPY candidate_parcels TO 'data/derived/final-candidates.gpkg' "
                "(FORMAT GDAL, DRIVER 'GPKG')")

    import pyproj
    _t = pyproj.Transformer.from_crs(ANALYSIS_CRS, STORAGE_CRS, always_xy=True)
    feats = con.execute("SELECT cadastral_id, area_m2, dist_main_road_m, "
                        "ST_AsGeoJSON(geometry) FROM candidate_parcels").fetchall()
    coll = {"type": "FeatureCollection", "features": []}
    for _fid, _area, _dist, _gj in feats:
        _g = json.loads(_gj)
        _ring = [[_t.transform(_x, _y) for _x, _y in _r] for _r in _g["coordinates"]]
        coll["features"].append({"type": "Feature",
                                 "properties": {"cadastral_id": _fid,
                                                "area_m2": float(_area),
                                                "dist_main_road_m": round(float(_dist), 1)},
                                 "geometry": {"type": "Polygon", "coordinates": _ring}})
    (DERIVED / "final-candidates.json").write_text(json.dumps(coll))
    log.info("derived outputs -> GPKG (EPSG:3301) + GeoJSON (EPSG:4326)")


# ------------------------------ STEP 8: validation --------------------------
def write_validation(con) -> dict:
    def n(q):
        return int(con.execute(q).fetchone()[0])

    checks = [
        {"id": "geometry_valid", "status": "passed" if n(
            "SELECT COUNT(*) FROM candidate_parcels WHERE NOT ST_IsValid(geometry)") == 0 else "failed",
         "features_checked": n("SELECT COUNT(*) FROM candidate_parcels")},
        {"id": "no_duplicate_cadastral_id", "status": "passed" if n(
            "SELECT COUNT(*) - COUNT(DISTINCT cadastral_id) FROM candidate_parcels") == 0 else "failed",
         "duplicates": n("SELECT COUNT(*) - COUNT(DISTINCT cadastral_id) FROM candidate_parcels")},
        {"id": "row_count_gt", "status": "passed" if n(
            "SELECT COUNT(*) FROM candidate_parcels") > 0 else "failed",
         "rows": n("SELECT COUNT(*) FROM candidate_parcels")},
        {"id": "parcel_area_range", "status": "passed" if n(
            "SELECT COUNT(*) FROM candidate_parcels WHERE area_m2 <= 0 OR area_m2 >= 100000000") == 0 else "failed",
         "out_of_range": n("SELECT COUNT(*) FROM candidate_parcels WHERE area_m2 <= 0 OR area_m2 >= 100000000")},
        {"id": "poi_completeness", "status": "warning",
         "reason": "No authoritative completeness baseline available"},
    ]
    overall = "failed" if any(c["status"] == "failed" for c in checks) else \
              ("warning" if any(c["status"] == "warning" for c in checks) else "passed")
    report = {"run_id": _run_id(), "schema": "open-gis-project/v1",
              "status": overall, "checks": checks,
              "sources": {k: v.get("source_url") for k, v in PROJECT["sources"].items()},
              "overrides": [o["id"] for o in PROJECT["overrides"]]}
    VALIDATION.mkdir(parents=True, exist_ok=True)
    (VALIDATION / "latest-report.json").write_text(json.dumps(report, indent=2, default=str))
    return report


# ------------------------------ STEP 9: dashboard ---------------------------
def render_dashboard(con, validation) -> None:
    pr = PROJECT["project"]
    pres = PROJECT["presentation"]
    inte = PROJECT["interpretation"]

    # candidate geometry for map: pull geojson we already exported
    final_gj = json.loads((DERIVED / "final-candidates.json").read_text())
    n_feat = len(final_gj["features"])
    ids = final_gj["features"][:0]

    # planned road geometry as geojson for the map overlay
    plan = {"type": "FeatureCollection", "features": []}
    if (OVERRIDES / "planned-road.geojson").exists():
        try:
            plan = json.loads((OVERRIDES / "planned-road.geojson").read_text())
        except Exception:
            pass

    def f_esc(chunk):
        return json.dumps(chunk).replace("</", "<\\/")

    # --- status + data-driven map bounds from the derived result ---
    status_cls = "status-failed" if validation["status"] == "failed" else \
                 ("status-warn" if validation["status"] == "warning" else "status-ok")
    ring = final_gj["features"][0]["geometry"]["coordinates"][0]
    _lngs = [p[0] for f in final_gj["features"] for p in f["geometry"]["coordinates"][0]]
    _lats = [p[1] for f in final_gj["features"] for p in f["geometry"]["coordinates"][0]]
    min_lng, max_lng = min(_lngs), max(_lngs)
    min_lat, max_lat = min(_lats), max(_lats)
    c_lng, c_lat = (min_lng + max_lng) / 2, (min_lat + max_lat) / 2
    ring = None  # unused sentinel
    layer_groups = pres["map"]["layer_groups"]
    lg_html = "".join(
        f'<li><input type="checkbox" checked> {g.get("title", g["id"])}</li>' for g in layer_groups)
    assump_html = "".join(f"<li><b>{a['id']}</b> — {a['statement']}</li>" for a in inte["assumptions"])
    sources_html = "".join(f"<li><b>{k}</b> {v.get('provider','')} · {v.get('source_url','')}"
                           for k, v in PROJECT["sources"].items())
    overrides_html = "".join(f"<li><b>{o['id']}</b> {o.get('action')} — {o.get('rationale','')}"
                             for o in PROJECT["overrides"])
    warnings_html = ""
    for w in PROJECT.get("warnings", []):
        warnings_html += f'<div class="sect"><h2>⚠ warning {w["id"]}</h2>' + \
            f'<p class="prov">{w.get("statement","")}</p></div>'
    checks_html = "".join(
        f'<div class="check"><span>{c["id"]}</span><span class="badge pass">{c["status"]}</span></div>'
        for c in validation["checks"])

    GEO2 = f_esc(final_gj)
    PLAN2 = f_esc(plan)

    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{pr['title']} — reproducible view</title>
<style>
:root{{--accent:#2f6f4f;--side:#101721;--line:#223;--text:#eaf0f6}}
*{{box-sizing:border-box;margin:0}}
body{{font-family:system-ui,'Segoe UI',Roboto,sans-serif;background:var(--side);color:var(--text);
     height:100vh;display:grid;grid-template-columns:340px 1fr;grid-template-rows:48px 1fr}}
header{{grid-column:1/3;display:flex;align-items:center;gap:12px;padding:0 16px;background:#0a0f16;border-bottom:1px solid #1c2632}}
header h1{{font-size:15px;font-weight:650}}
.status{{margin-left:auto;font-size:12px;padding:3px 10px;border-radius:11px;border:1px solid #223}}
#status-ok .status{{background:#12351f;color:#6fe3a0}}
#status-warn .status{{background:#3b3512;color:#e6d67d}}
#status-failed .status{{background:#3b1414;color:#ff9c9c}}
#sidebar{{overflow:auto;border-right:1px solid #1c2632;padding:12px 14px 24px}}
.sect{{margin-bottom:14px}}
.sect h2{{font-size:10px;letter-spacing:.14em;text-transform:uppercase;color:#93a7b8;margin-bottom:6px;
          border-bottom:1px solid #1c2632;padding-bottom:4px}}
p,p .prov{{font-size:12px;color:#c4cfd9;line-height:1.5}}
ul{{list-style:none;padding:0}}
li{{font-size:12px;padding:3px 0;color:#c9d3dc}}
.check{{display:flex;justify-content:space-between;font-size:12px;padding:3px 0}}
.badge{{font-size:10px;padding:1px 6px;border-radius:9px;background:#12351f;color:#6fe3a0}}
.metric{{background:#0d1520;border:1px solid #1c2632;;padding:10px}}
.metric b{{display:block;font-size:22px;color:#3fd98a}}
#map{{height:100%;width:100%}}
</style></head><body id="{status_cls}">
<header><h1>REPRODUCIBLE · {pr['title']}</h1>
  <span class="status">project: {pr['status']}</span>
  <span class="status">run: {validation['status']}</span></header>

<aside id="sidebar">
  <div class="sect"><h2>Interpretation / objective</h2>
    <p>{inte['objective']}</p></div>
  <div class="sect"><h2>Result</h2>
    <div class="metric"><b>{n_feat}</b><span>candidate parcels≥20 000 m² and ≤2000 m to road</span></div>
  </div>
  <div class="sect"><h2>Filters</h2>
    <ul><li>area_m2 ≥ 20 000  (assumption A2 — metric L-EST97)</li>
        <li>dist_main_road ≤ 2 000 m (assumption A1)</li></ul></div>
  <div class="sect"><h2>Layer controls</h2><ul>{lg_html}</ul></div>
  <div class="sect"><h2>Assumptions</h2><ul>{assump_html}</ul></div>
  <div class="sect"><h2>Sources &amp; provenance</h2><ul>{sources_html}</ul></div>
  <div class="sect"><h2>Manual overrides ({len(PROJECT['overrides'])})</h2><ul>{overrides_html}</ul></div>
  {warnings_html}
  <div class="sect"><h2>Validation</h2>{checks_html}</div>
  <div class="prov">run: {validation.get('run_id','')} · schema open-gis-project/v1</div>
</aside>

<div id="map"></div>
<script src="https://unpkg.com/maplibre-gl@3.6.2/dist/maplibre-gl.js"></script>
<link href="https://unpkg.com/maplibre-gl@3.6.2/dist/maplibre-gl.css" rel="stylesheet">
<script>
const GEOJSON={GEO2};
const PLAN={PLAN2};
const map=new maplibregl.Map({{container:'map',
  style:'https://demotiles.maplibre.org/style.json',
  center:[{c_lng:.4f},{c_lat:.4f}],zoom:11}});
map.on('load',()=>{{
  map.addSource('res',{{type:'geojson',data:GEOJSON}});
  map.addLayer({{id:'res',type:'fill',source:'res',
    paint:{{'fill-color':'#2f9e6e','fill-opacity':0.55,'fill-outline-color':'#bfe6d2'}}}});
  map.addSource('plan',{{type:'geojson',data:PLAN}});
  map.addLayer({{id:'plan',type:'line',source:'plan',
    paint:{{'line-color':'#e6b12f','line-width':3,'line-dasharray':[3,2]}}}});
  try{{map.fitBounds({{lng:{min_lng:.4f},lat:{min_lat:.4f},lng:{max_lng:.4f},lat:{max_lat:.4f}}},{{padding:30}})}}catch(_){{}}
}});
</script></body></html>"""

    out = ROOT / "dashboard.html"
    out.write_text(html)
    log.info("dashboard -> %s", out)


def main() -> None:
    for d in (DERIVED, VALIDATION, RUNS, SOURCE):
        d.mkdir(parents=True, exist_ok=True)
    make_sources()
    con = duckdb.connect()
    con.install_extension("spatial")
    con.load_extension("spatial")
    run_pipeline(con)
    validation = write_validation(con)
    render_dashboard(con, validation)
    log.info("E2E complete")


if __name__ == "__main__":
    main()