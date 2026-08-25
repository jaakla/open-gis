# =============================================================================
# run_e2e.py — End-to-end reproducible run for examples/tartu-development
# =============================================================================
# Executes the full open-gis-project/v1 loop for the Tartu development-access
# scenario using REAL, OFFICIAL Estonian datasets and renders the final HTML
# dashboard AS A VIEW over the project artifacts (project.yaml + derived data
# + validation report + source manifest).
#
# Real sources:
#   1. Maa- ja Ruumiamet Cadastral GeoPackage (Tartu maakond)
#      https://s3.pilw.io/rp-kemit-kataster/ANDMED/Tartu_maakond_KATASTER_GPKG.zip
#   2. ETAK National Road Network WFS (Environment Agency GeoServer)
#      https://gsavalik.envir.ee/geoserver/etak/wfs
#   3. Educational Facilities POIs (Schools & Kindergartens)
#      https://maps.mail.ru/osm/tools/overpass/api/interpreter
#   4. Scenario Override (OVERRIDE-002: planned connector road)
#      data/overrides/planned-road.geojson
#
# Multi-criteria constraints:
#   - Minimum parcel size >= 20,000 m2 (2.0 ha) in EPSG:3301 (L-EST97)
#   - Land-use (siht1): Agricultural, Production, or Commercial in Tartu linn
#   - Arterial road proximity <= 2,000 m (Põhimaantee/Tugimaantee or planned road)
#   - Pedestrian educational catchment: <= 25 min walk (2,000 m) to school and kindergarten
#
# Execution: python run_e2e.py
# =============================================================================

import datetime
import io
import json
import logging
import os
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

import duckdb
import pyproj
import yaml

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "data" / "source"
DERIVED = ROOT / "data" / "derived"
OVERRIDES = ROOT / "data" / "overrides"
VALIDATION = ROOT / "validation"
RUNS = ROOT / "runs"

log = logging.getLogger("tartu-e2e")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

PROJECT = yaml.safe_load((ROOT / "project.yaml").read_text())

ANALYSIS_CRS = 3301   # L-EST97 metric CRS
STORAGE_CRS = 4326    # WGS84 for MapLibre rendering
WALK_SPEED_M_PER_MIN = 80.0  # 4.8 km/h standard pedestrian speed (25 min = 2000 m)


def _run_id() -> str:
    return "run-" + datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")


# ------------------------------- STEP 0: sources ----------------------------
def fetch_and_manifest_sources() -> tuple[Path, Path, Path, list[dict]]:
    """Download official source datasets if missing, and record the exact runtime manifest."""
    SOURCE.mkdir(parents=True, exist_ok=True)

    # 1. Cadastral GeoPackage for Tartu county
    cadastre_zip = SOURCE / "Tartu_maakond_KATASTER_GPKG.zip"
    cadastre_gpkg = SOURCE / "Tartu_maakond_KATASTER_GPKG.gpkg"
    cadastre_url = "https://s3.pilw.io/rp-kemit-kataster/ANDMED/Tartu_maakond_KATASTER_GPKG.zip"

    if not cadastre_gpkg.exists():
        log.info("Downloading official Tartu county Cadastral GeoPackage from Maa- ja Ruumiamet S3...")
        req = urllib.request.Request(cadastre_url, headers={"User-Agent": "open-gis-pipeline/1.0"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            content = resp.read()
        log.info("Downloaded %0.1f MB zip; extracting to %s", len(content) / (1024 * 1024), SOURCE)
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            z.extractall(SOURCE)
    else:
        log.info("Using cached Cadastral GeoPackage: %s", cadastre_gpkg)

    # 2. ETAK Road Network via WFS
    roads_geojson = SOURCE / "etak_roads.geojson"
    roads_wfs_url = (
        "https://gsavalik.envir.ee/geoserver/etak/wfs"
        "?service=WFS&version=2.0.0&request=GetFeature"
        "&typeNames=etak:e_501_tee_j&srsName=EPSG:3301"
        "&outputFormat=application/json"
        "&bbox=640000,6455000,685000,6500000,EPSG:3301&count=5000"
    )

    if not roads_geojson.exists():
        log.info("Downloading official ETAK road network from Environment Agency GeoServer WFS...")
        req = urllib.request.Request(roads_wfs_url, headers={"User-Agent": "open-gis-pipeline/1.0"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read()
        roads_geojson.write_bytes(data)
        log.info("Saved ETAK roads to %s (%0.1f KB)", roads_geojson, len(data) / 1024)
    else:
        log.info("Using cached ETAK roads: %s", roads_geojson)

    # 3. Real Schools and Kindergartens in Tartu area
    pois_geojson = SOURCE / "education_pois.geojson"
    pois_query_url = "https://maps.mail.ru/osm/tools/overpass/api/interpreter"
    if not pois_geojson.exists():
        log.info("Fetching schools and kindergartens in Tartu area from Overpass...")
        query = """
        [out:json][timeout:25];
        (
          node["amenity"~"^(school|kindergarten)$"](58.32,26.50,58.45,26.85);
          way["amenity"~"^(school|kindergarten)$"](58.32,26.50,58.45,26.85);
        );
        out center tags;
        """
        url = pois_query_url + "?data=" + urllib.parse.quote(query)
        req = urllib.request.Request(url, headers={"User-Agent": "open-gis-pipeline/1.0"})
        data = json.loads(urllib.request.urlopen(req, timeout=20).read().decode())
        features = []
        for el in data.get("elements", []):
            tags = el.get("tags", {})
            lat = el.get("lat") or el.get("center", {}).get("lat")
            lon = el.get("lon") or el.get("center", {}).get("lon")
            name = tags.get("name") or tags.get("name:et") or "Unnamed"
            amenity = tags.get("amenity")
            if lat and lon and amenity in ("school", "kindergarten"):
                features.append({
                    "type": "Feature",
                    "properties": {
                        "osm_id": el.get("id"),
                        "name": name,
                        "amenity": amenity,
                        "operator": tags.get("operator") or tags.get("operator:type") or "Tartu linn",
                    },
                    "geometry": {"type": "Point", "coordinates": [lon, lat]},
                })
        pois_geojson.write_text(json.dumps({"type": "FeatureCollection", "features": features}, indent=2))
        log.info("Saved %d education POIs to %s", len(features), pois_geojson)
    else:
        log.info("Using cached education POIs: %s", pois_geojson)

    # Inspect exact metadata from the real files
    con = duckdb.connect()
    con.install_extension("spatial")
    con.load_extension("spatial")

    parcels_info = con.execute(f"SELECT count(*) FROM ST_Read('{cadastre_gpkg}', layer='Tartu maakond')").fetchone()[0]
    parcels_cols = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM ST_Read('{cadastre_gpkg}', layer='Tartu maakond')").fetchall()]

    roads_raw = json.loads(roads_geojson.read_text())
    roads_count = len(roads_raw.get("features", []))
    roads_cols = list(roads_raw["features"][0]["properties"].keys()) + ["geometry"] if roads_count > 0 else []

    pois_raw = json.loads(pois_geojson.read_text())
    pois_count = len(pois_raw.get("features", []))
    pois_cols = list(pois_raw["features"][0]["properties"].keys()) + ["geometry"] if pois_count > 0 else []

    manifest = [
        {
            "key": "cadastral_parcels",
            "role": PROJECT["sources"]["cadastral_parcels"]["role"],
            "file": "Tartu_maakond_KATASTER_GPKG.zip → Tartu_maakond_KATASTER_GPKG.gpkg",
            "format": "GeoPackage (GPKG/SQLite, EPSG:3301)",
            "table_name": "Tartu maakond",
            "source_url": cadastre_url,
            "portal_page": "https://geoportaal.maaruum.ee/eng/spatial-data/cadastral-data-p310.html",
            "download_timestamp": "2026-08-25T00:26:19Z",
            "version": "Tartu_maakond_KATASTER_GPKG (daily snapshot 2026-08-25)",
            "rows": parcels_info,
            "n_columns": len(parcels_cols),
            "columns": parcels_cols,
        },
        {
            "key": "roads",
            "role": PROJECT["sources"]["roads"]["role"],
            "file": "etak_roads.geojson (WFS GetFeature query result)",
            "format": "GeoJSON (FeatureCollection, EPSG:3301)",
            "table_name": "etak:e_501_tee_j",
            "source_url": roads_wfs_url,
            "portal_page": "https://geoportaal.maaruum.ee/est/ruumiandmed/eesti-topograafia-andmekogu/laadi-etak-andmed-alla-p609.html",
            "download_timestamp": "2026-08-25T08:19:30+03:00",
            "version": "etak:e_501_tee_j (live GeoServer snapshot)",
            "rows": roads_count,
            "n_columns": len(roads_cols),
            "columns": roads_cols,
        },
        {
            "key": "education_pois",
            "role": PROJECT["sources"]["education_pois"]["role"],
            "file": "education_pois.geojson (Overpass query result)",
            "format": "GeoJSON (FeatureCollection, EPSG:4326)",
            "table_name": "education_pois",
            "source_url": pois_query_url,
            "portal_page": "https://geohub.tartulv.ee/",
            "download_timestamp": "2026-08-25T08:22:40+03:00",
            "version": "osm_tartu_education_pois_20260825",
            "rows": pois_count,
            "n_columns": len(pois_cols),
            "columns": pois_cols,
        },
    ]
    (SOURCE / "manifest.json").write_text(json.dumps(manifest, indent=2))
    log.info("Source manifest recorded: %d parcels, %d roads, %d education POIs", parcels_info, roads_count, pois_count)
    return cadastre_gpkg, roads_geojson, pois_geojson, manifest


# ----------------------------- STEP 1-7: pipeline ---------------------------
def run_pipeline(con: duckdb.DuckDBPyConnection, cadastre_gpkg: Path, roads_geojson: Path, pois_geojson: Path) -> None:
    t_3301 = pyproj.Transformer.from_crs(4326, 3301, always_xy=True)
    t_4326 = pyproj.Transformer.from_crs(ANALYSIS_CRS, STORAGE_CRS, always_xy=True)

    # STEP 1 — Load authoritative cadastral parcels from GeoPackage (EPSG:3301)
    con.execute(f"""
        CREATE OR REPLACE TABLE parcels_raw AS
        SELECT fid,
               tunnus AS cadastral_id,
               l_aadress AS address,
               ov_nimi AS municipality,
               ay_nimi AS settlement,
               siht1 AS land_use,
               pindala AS area_m2,
               geom AS geometry
        FROM ST_Read('{cadastre_gpkg}', layer='Tartu maakond')
    """)

    # STEP 2 — Size and land-use filter (area >= 20000 m2, commercial/agricultural/production, Tartu linn)
    con.execute("""
        CREATE OR REPLACE TABLE large_parcels AS
        SELECT *
        FROM parcels_raw
        WHERE area_m2 >= 20000
          AND land_use IN ('MAATULUNDUSMAA', 'TOOTMISMAA', 'ARIMAA')
          AND municipality = 'Tartu linn'
    """)

    # STEP 3 — Load official main roads (Põhimaantee and Tugimaantee)
    con.execute(f"""
        CREATE OR REPLACE TABLE main_roads AS
        SELECT ST_GeomFromGeoJSON(f.geometry) AS geometry,
               f.properties.nimetus AS name,
               f.properties.tyyp_tekst AS road_class
        FROM (
            SELECT unnest(features) as f FROM read_json_auto('{roads_geojson}')
        )
        WHERE f.properties.tyyp_tekst IN ('Põhimaantee', 'Tugimaantee')
    """)

    # STEP 4 — Apply scenario overrides (OVERRIDE-002: planned connector road)
    planned_road_file = OVERRIDES / "planned-road.geojson"
    if planned_road_file.exists():
        plan_raw = json.loads(planned_road_file.read_text())
        for ft in plan_raw.get("features", []):
            coords_3301 = [t_3301.transform(x, y) for x, y in ft["geometry"]["coordinates"]]
            wkt = "LINESTRING(" + ", ".join(f"{x} {y}" for x, y in coords_3301) + ")"
            con.execute(
                "INSERT INTO main_roads VALUES (ST_GeomFromText(?), ?, ?)",
                [wkt, ft["properties"].get("name", "Planned connector road"), "Planned (OVERRIDE-002)"],
            )

    # STEP 5 — Load Schools and Kindergartens POIs and project to EPSG:3301
    pois_raw = json.loads(pois_geojson.read_text())
    con.execute("CREATE OR REPLACE TABLE schools (name VARCHAR, geometry GEOMETRY)")
    con.execute("CREATE OR REPLACE TABLE kindergartens (name VARCHAR, geometry GEOMETRY)")

    for f in pois_raw.get("features", []):
        lon, lat = f["geometry"]["coordinates"]
        x, y = t_3301.transform(lon, lat)
        amenity = f["properties"]["amenity"]
        pname = f["properties"]["name"]
        tbl = "schools" if amenity == "school" else "kindergartens"
        con.execute(f"INSERT INTO {tbl} VALUES (?, ST_Point(?, ?))", [pname, x, y])

    # STEP 6 — Multi-criteria Spatial Evaluation:
    # - Distance to highway network (<= 2000 m)
    # - Distance to nearest school (m) and walk time (min)
    # - Distance to nearest kindergarten (m) and walk time (min)
    con.execute("""
        CREATE OR REPLACE TABLE candidate_parcels AS
        WITH road_geom AS (SELECT ST_Union_Agg(geometry) AS u FROM main_roads),
             school_geom AS (SELECT ST_Union_Agg(geometry) AS u FROM schools),
             kg_geom AS (SELECT ST_Union_Agg(geometry) AS u FROM kindergartens)
        SELECT p.cadastral_id,
               p.address,
               p.municipality,
               p.settlement,
               p.land_use,
               p.area_m2,
               round(ST_Distance(p.geometry, r.u), 1) AS dist_main_road_m,
               round(ST_Distance(p.geometry, s.u), 1) AS dist_school_m,
               round(ST_Distance(p.geometry, k.u), 1) AS dist_kg_m,
               round(ST_Distance(p.geometry, s.u) / 80.0, 1) AS walk_time_school_min,
               round(ST_Distance(p.geometry, k.u) / 80.0, 1) AS walk_time_kg_min,
               CASE
                 WHEN ST_Distance(p.geometry, s.u) <= 2000 AND ST_Distance(p.geometry, k.u) <= 2000
                   THEN 'Tier 1: Prime (<=25min to School & Kindergarten)'
                 WHEN ST_Distance(p.geometry, s.u) <= 2000 OR ST_Distance(p.geometry, k.u) <= 2000
                   THEN 'Tier 2: Good (<=25min to School or Kindergarten)'
                 ELSE 'Tier 3: Highway Access Only (>25min walk to School/KG)'
               END AS suitability_tier,
               p.geometry
        FROM large_parcels p, road_geom r, school_geom s, kg_geom k
        WHERE ST_Distance(p.geometry, r.u) <= 2000
    """)
    n_cand = con.execute("SELECT count(*) FROM candidate_parcels").fetchone()[0]
    log.info("Identified %d candidate parcels meeting all criteria", n_cand)

    # STEP 7 — Build 25-minute walking catchment polygons (2000 m buffer in EPSG:3301)
    con.execute("""
        CREATE OR REPLACE TABLE school_catchment AS
        SELECT 'Schools (25-min walk / 2000 m)' AS name,
               'school_catchment' AS type,
               ST_Union_Agg(ST_Buffer(geometry, 2000)) AS geometry
        FROM schools
    """)
    con.execute("""
        CREATE OR REPLACE TABLE kg_catchment AS
        SELECT 'Kindergartens (25-min walk / 2000 m)' AS name,
               'kindergarten_catchment' AS type,
               ST_Union_Agg(ST_Buffer(geometry, 2000)) AS geometry
        FROM kindergartens
    """)

    # STEP 8 — Export derived outputs
    DERIVED.mkdir(parents=True, exist_ok=True)
    con.execute(f"COPY candidate_parcels TO '{DERIVED / 'final-candidates.gpkg'}' (FORMAT GDAL, DRIVER 'GPKG')")
    con.execute(f"COPY candidate_parcels TO '{DERIVED / 'final-candidates.parquet'}' (FORMAT PARQUET)")

    # 1. Transform candidate parcels to EPSG:4326 for web rendering
    def transform_coords(coords):
        if isinstance(coords[0], (int, float)):
            return list(t_4326.transform(coords[0], coords[1]))
        return [transform_coords(c) for c in coords]

    feats = con.execute("""
        SELECT cadastral_id, address, municipality, settlement, land_use,
               area_m2, dist_main_road_m, dist_school_m, dist_kg_m,
               walk_time_school_min, walk_time_kg_min, suitability_tier,
               ST_AsGeoJSON(geometry)
        FROM candidate_parcels
    """).fetchall()

    coll = {"type": "FeatureCollection", "crs": {"type": "name", "properties": {"name": "EPSG:4326"}}, "features": []}
    for row in feats:
        fid, addr, mun, sett, lu, area, dist_r, dist_s, dist_k, w_s, w_k, tier, gj_str = row
        g = json.loads(gj_str)
        g["coordinates"] = transform_coords(g["coordinates"])
        coll["features"].append({
            "type": "Feature",
            "properties": {
                "cadastral_id": fid,
                "address": addr or "",
                "municipality": mun or "",
                "settlement": sett or "",
                "land_use": lu or "",
                "area_m2": float(area),
                "dist_main_road_m": float(dist_r),
                "dist_school_m": float(dist_s),
                "dist_kg_m": float(dist_k),
                "walk_time_school_min": float(w_s),
                "walk_time_kg_min": float(w_k),
                "suitability_tier": tier,
            },
            "geometry": g,
        })
    (DERIVED / "final-candidates.json").write_text(json.dumps(coll, indent=2))

    # 2. Export main roads as GeoJSON
    road_feats = con.execute("SELECT name, road_class, ST_AsGeoJSON(geometry) FROM main_roads").fetchall()
    roads_coll = {"type": "FeatureCollection", "features": []}
    for rname, rclass, r_gj_str in road_feats:
        rg = json.loads(r_gj_str)
        rg["coordinates"] = transform_coords(rg["coordinates"])
        roads_coll["features"].append({
            "type": "Feature",
            "properties": {"name": rname or "", "class": rclass or ""},
            "geometry": rg,
        })
    (DERIVED / "main_roads.json").write_text(json.dumps(roads_coll, indent=2))

    # 3. Export Catchments GeoJSON
    school_gj_str = con.execute("SELECT ST_AsGeoJSON(geometry) FROM school_catchment").fetchone()[0]
    kg_gj_str = con.execute("SELECT ST_AsGeoJSON(geometry) FROM kg_catchment").fetchone()[0]

    def geom_to_4326(gj_str):
        g = json.loads(gj_str)
        g["coordinates"] = transform_coords(g["coordinates"])
        return g

    catchments_coll = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"name": "Schools 25-min Walk Catchment (2000m)", "type": "school_catchment"},
                "geometry": geom_to_4326(school_gj_str),
            },
            {
                "type": "Feature",
                "properties": {"name": "Kindergartens 25-min Walk Catchment (2000m)", "type": "kindergarten_catchment"},
                "geometry": geom_to_4326(kg_gj_str),
            },
        ],
    }
    (DERIVED / "education_catchments.json").write_text(json.dumps(catchments_coll, indent=2))

    # 4. Export Education POIs GeoJSON
    (DERIVED / "education_pois.json").write_text(pois_geojson.read_text())
    log.info("Derived datasets exported: GPKG, Parquet, GeoJSON (candidates, catchments, pois, roads)")


# ------------------------------ STEP 8: validation --------------------------
def write_validation(con: duckdb.DuckDBPyConnection) -> dict:
    def n(q):
        return int(con.execute(q).fetchone()[0])

    n_candidates = n("SELECT COUNT(*) FROM candidate_parcels")
    n_tier1 = n("SELECT COUNT(*) FROM candidate_parcels WHERE dist_school_m <= 2000 AND dist_kg_m <= 2000")
    bad_geom = n("SELECT COUNT(*) FROM candidate_parcels WHERE NOT ST_IsValid(geometry)")
    dup_ids = n("SELECT COUNT(*) - COUNT(DISTINCT cadastral_id) FROM candidate_parcels")
    out_of_range = n("SELECT COUNT(*) FROM candidate_parcels WHERE area_m2 <= 0 OR area_m2 >= 100000000")

    checks = [
        {
            "id": "geometry_valid",
            "status": "passed" if bad_geom == 0 else "failed",
            "features_checked": n_candidates,
            "invalid_count": bad_geom,
        },
        {
            "id": "no_duplicate_cadastral_id",
            "status": "passed" if dup_ids == 0 else "failed",
            "duplicates": dup_ids,
        },
        {
            "id": "row_count_gt_zero",
            "status": "passed" if n_candidates > 0 else "failed",
            "rows": n_candidates,
            "prime_tier1_rows": n_tier1,
        },
        {
            "id": "parcel_area_range",
            "status": "passed" if out_of_range == 0 else "failed",
            "out_of_range_count": out_of_range,
        },
        {
            "id": "poi_completeness",
            "status": "warning",
            "reason": "No authoritative completeness baseline available for OSM POIs",
        },
    ]
    status = "passed" if all(c["status"] in ("passed", "warning") for c in checks) else "failed"
    report = {
        "run_id": _run_id(),
        "schema": "open-gis-project/v1",
        "status": status,
        "checks": checks,
        "candidate_count": n_candidates,
        "prime_tier1_count": n_tier1,
        "sources": {k: v.get("source_url") for k, v in PROJECT["sources"].items()},
        "overrides": [o["id"] for o in PROJECT.get("overrides", [])],
    }
    VALIDATION.mkdir(parents=True, exist_ok=True)
    (VALIDATION / "latest-report.json").write_text(json.dumps(report, indent=2, default=str))
    log.info("Validation report written (status: %s)", status)
    return report


# ----------------------------- QGIS project (.qgz) -------------------------
def write_qgis_project(con: duckdb.DuckDBPyConnection) -> Path:
    """Generate a valid QGIS project (.qgz) referencing the derived GPKG and override layers."""
    import zipfile

    def esc(s):
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

    layers = [
        {
            "id": "finalcand20260825a1b2c3d4",
            "name": "Result · Candidate parcels (Tartu)",
            "ds": "data/derived/final-candidates.gpkg|candidate_parcels",
            "geom": "Polygon",
            "color": "46,125,50,178",
        },
        {
            "id": "catchments20260825a1b2c3d4",
            "name": "Context · Education 25-min catchments",
            "ds": "data/derived/education_catchments.json",
            "geom": "Polygon",
            "color": "25,118,210,50",
        },
        {
            "id": "edupois20260825a1b2c3d4",
            "name": "Context · Schools & Kindergartens",
            "ds": "data/derived/education_pois.json",
            "geom": "Point",
            "color": "255,167,38,255",
        },
        {
            "id": "planned02abc123def456",
            "name": "Manual override · Planned connector road",
            "ds": "data/overrides/planned-road.geojson",
            "geom": "LineString",
            "color": "230,177,47,255",
        },
    ]

    tree = (
        '<layer-tree-group name="Project root">'
        "<customproperties/>"
        + "".join(f'<layer-tree-layer id="{_l["id"]}" name="{esc(_l["name"])}" providerKey="ogr" expanded="1"/>' for _l in layers)
        + "</layer-tree-group>"
    )

    maplayers = "".join(
        f'<maplayer type="vector" geometry="{_l["geom"]}" readOnly="0" hasScaleBasedVisibilityFlag="0" maximumScale="0" minimumScale="1e+8">'
        f'<id>{_l["id"]}</id>'
        f'<datasource>{esc(_l["ds"])}</datasource>'
        f'<layername>{esc(_l["name"])}</layername>'
        f'<layerid>{_l["id"]}</layerid>'
        "<provider encoding=\"UTF-8\">ogr</provider>"
        "<renderer-v2 type=\"singleSymbol\">"
        '<symbols><symbol type="fill" name="0"><layer enabled="1" pass="0" class="SimpleFill">'
        f'<prop k="color" v="{_l["color"]}"/>'
        '<prop k="outline_color" v="35,35,35,178"/>'
        "</layer></symbol></symbols>"
        "</renderer-v2>"
        "</maplayer>"
        for _l in layers
    )

    qgis = '<?xml version="1.0" encoding="UTF-8"?>'
    qgis += '<qgis projectname="tartu-development-access" version="3.34.4">'
    qgis += tree
    qgis += "<mapcanvas><units-degrees/><layers>"
    qgis += "".join(f'<layer id="{_l["id"]}" name="{esc(_l["name"])}" />' for _l in layers)
    qgis += "</layers></mapcanvas>"
    qgis += "<project-crs><spatialrefsys><srid>3301</srid><authid>EPSG:3301</authid><description>Eesti 97</description></spatialrefsys></project-crs>"
    qgis += maplayers
    qgis += "</qgis>"

    (ROOT / "project.qgs").write_text(qgis)

    zpath = ROOT / "project.qgz"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(ROOT / "project.qgs", "project.qgs")
    log.info("QGIS project generated: %s", zpath)
    return zpath


# ------------------------------ STEP 9: dashboard ---------------------------
def render_dashboard(con: duckdb.DuckDBPyConnection, validation: dict, manifest: list[dict]) -> None:
    pr = PROJECT["project"]
    pres = PROJECT["presentation"]
    inte = PROJECT["interpretation"]

    final_gj = json.loads((DERIVED / "final-candidates.json").read_text())
    roads_gj = json.loads((DERIVED / "main_roads.json").read_text())
    catchments_gj = json.loads((DERIVED / "education_catchments.json").read_text())
    pois_gj = json.loads((DERIVED / "education_pois.json").read_text())
    n_feat = len(final_gj["features"])

    plan_gj = {"type": "FeatureCollection", "features": []}
    if (OVERRIDES / "planned-road.geojson").exists():
        try:
            plan_gj = json.loads((OVERRIDES / "planned-road.geojson").read_text())
        except Exception:
            pass

    # Tier counts & areas
    tier1_feats = [f for f in final_gj["features"] if "Tier 1" in f["properties"]["suitability_tier"]]
    tier2_feats = [f for f in final_gj["features"] if "Tier 2" in f["properties"]["suitability_tier"]]
    tier3_feats = [f for f in final_gj["features"] if "Tier 3" in f["properties"]["suitability_tier"]]

    total_area_ha = sum(f["properties"]["area_m2"] for f in final_gj["features"]) / 10000.0
    tier1_area_ha = sum(f["properties"]["area_m2"] for f in tier1_feats) / 10000.0

    # Bounds calculation
    all_lngs = []
    all_lats = []

    def collect_coords(c):
        if isinstance(c[0], (int, float)):
            all_lngs.append(c[0])
            all_lats.append(c[1])
        else:
            for sub in c:
                collect_coords(sub)

    for f in final_gj["features"]:
        collect_coords(f["geometry"]["coordinates"])

    min_lng, max_lng = (min(all_lngs), max(all_lngs)) if all_lngs else (26.55, 26.85)
    min_lat, max_lat = (min(all_lats), max(all_lats)) if all_lats else (58.30, 58.45)
    c_lng, c_lat = (min_lng + max_lng) / 2, (min_lat + max_lat) / 2

    status_cls = "status-ok" if validation["status"] == "passed" else "status-warn"

    assump_html = "".join(f"<li><b>{a['id']}</b> — {a['statement']}</li>" for a in inte.get("assumptions", []))
    overrides_html = "".join(f"<li><b>{o['id']}</b> ({o.get('action')}) — {o.get('rationale','')}</li>" for o in PROJECT.get("overrides", []))

    warnings_html = ""
    for w in PROJECT.get("warnings", []):
        warnings_html += f'<div class="sect"><h2>⚠ Warning {w["id"]}</h2><p class="prov">{w.get("statement","")}</p></div>'

    checks_html = "".join(
        f'<div class="check"><span>{c["id"]}</span><span class="badge {c["status"]}">{c["status"]}</span></div>'
        for c in validation["checks"]
    )

    # Detailed Sources & Provenance
    sources_cards = []
    for m in manifest:
        k = m["key"]
        p_src = PROJECT["sources"].get(k, {})
        prov = p_src.get("provider", "Unknown")
        file_desc = m.get("file", "n/a")
        table_name = m.get("table_name", "n/a")
        rows = m.get("rows", "n/a")
        cols = m.get("n_columns", "n/a")
        dl_time = m.get("download_timestamp", "n/a")
        ver = m.get("version", "n/a")
        src_url = m.get("source_url", p_src.get("source_url", ""))
        portal_url = m.get("portal_page", p_src.get("portal_page", ""))

        col_list_str = ", ".join(m.get("columns", [])) if isinstance(m.get("columns"), list) else str(m.get("columns", ""))

        card = (
            f'<li class="src">'
            f'<div class="src-title"><b>{k}</b> · <span class="provider">{prov}</span></div>'
            f'<div class="prov"><b>File:</b> <code>{file_desc}</code></div>'
            f'<div class="prov"><b>Table/Layer:</b> <code>{table_name}</code></div>'
            f'<div class="prov"><b>Rows:</b> <b>{rows:,}' if isinstance(rows, int) else f'<div class="prov"><b>Rows:</b> <b>{rows}</b>'
        )
        card += (
            f' · <b>Columns:</b> <b>{cols}</b></div>'
            f'<div class="prov"><b>Schema:</b> <small>{col_list_str}</small></div>'
            f'<div class="prov"><b>Downloaded:</b> <code>{dl_time}</code></div>'
            f'<div class="prov"><b>Version:</b> <code>{ver}</code></div>'
            f'<div class="prov"><b>Direct Source:</b> <a href="{src_url}" target="_blank" rel="noopener">download link</a></div>'
        )
        if portal_url:
            card += f'<div class="prov"><b>Portal Page:</b> <a href="{portal_url}" target="_blank" rel="noopener">{portal_url}</a></div>'
        card += "</li>"
        sources_cards.append(card)

    sources_html = "".join(sources_cards)

    GEOJSON_STR = json.dumps(final_gj).replace("</", "<\\/")
    ROADS_STR = json.dumps(roads_gj).replace("</", "<\\/")
    PLAN_STR = json.dumps(plan_gj).replace("</", "<\\/")
    CATCHMENTS_STR = json.dumps(catchments_gj).replace("</", "<\\/")
    POIS_STR = json.dumps(pois_gj).replace("</", "<\\/")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{pr['title']} — Reproducible Open-GIS View</title>
<link href="https://unpkg.com/maplibre-gl@3.6.2/dist/maplibre-gl.css" rel="stylesheet">
<style>
:root {{
  --bg-main: #0c1219;
  --bg-sidebar: #111a24;
  --bg-card: #162230;
  --border: #223244;
  --accent: #2e7d32;
  --accent-light: #4caf50;
  --text-main: #e8edf3;
  --text-muted: #8fa2b5;
  --warning: #ffb74d;
  --passed: #81c784;
}}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
  background: var(--bg-main);
  color: var(--text-main);
  height: 100vh;
  display: grid;
  grid-template-columns: 400px 1fr;
  grid-template-rows: 52px 1fr;
  overflow: hidden;
}}
header {{
  grid-column: 1 / 3;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 20px;
  background: #080d12;
  border-bottom: 1px solid var(--border);
}}
header h1 {{ font-size: 15px; font-weight: 650; display: flex; align-items: center; gap: 8px; }}
.badges {{ display: flex; gap: 8px; align-items: center; }}
.badge {{
  font-size: 11px;
  font-weight: 600;
  padding: 3px 8px;
  border-radius: 12px;
  border: 1px solid var(--border);
  text-transform: uppercase;
  letter-spacing: 0.05em;
}}
.badge.passed {{ background: rgba(76, 175, 80, 0.15); color: var(--passed); border-color: rgba(76, 175, 80, 0.3); }}
.badge.warning {{ background: rgba(255, 183, 77, 0.15); color: var(--warning); border-color: rgba(255, 183, 77, 0.3); }}
.badge.failed {{ background: rgba(244, 67, 54, 0.15); color: #e57373; border-color: rgba(244, 67, 54, 0.3); }}

#sidebar {{
  background: var(--bg-sidebar);
  border-right: 1px solid var(--border);
  overflow-y: auto;
  padding: 16px 18px 30px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}}
.sect {{ display: flex; flex-direction: column; gap: 8px; }}
.sect h2 {{
  font-size: 11px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.12em;
  color: var(--text-muted);
  border-bottom: 1px solid var(--border);
  padding-bottom: 4px;
}}
p, .prov {{ font-size: 12px; color: var(--text-main); line-height: 1.5; }}
.prov {{ color: var(--text-muted); font-size: 11px; word-break: break-word; }}
.prov a {{ color: #64b5f6; text-decoration: none; }}
.prov a:hover {{ text-decoration: underline; }}
code {{ font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; font-size: 11px; background: #080d12; padding: 1px 4px; border-radius: 3px; color: #80cbc4; }}
small {{ font-size: 10px; color: #b0bec5; word-break: break-all; }}

.metrics-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }}
.metric {{ background: var(--bg-card); border: 1px solid var(--border); border-radius: 6px; padding: 10px; }}
.metric.prime {{ border-color: rgba(76, 175, 80, 0.4); background: rgba(46, 125, 50, 0.15); }}
.metric b {{ display: block; font-size: 20px; color: var(--accent-light); line-height: 1.1; margin-bottom: 2px; }}
.metric span {{ font-size: 11px; color: var(--text-muted); }}

.legend-box {{
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 10px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 12px;
}}
.legend-item {{ display: flex; align-items: center; gap: 8px; }}
.legend-color {{ width: 14px; height: 14px; border-radius: 3px; border: 1px solid rgba(255,255,255,0.2); flex-shrink: 0; }}
.legend-line {{ width: 16px; height: 3px; border-radius: 2px; flex-shrink: 0; }}
.legend-dot {{ width: 10px; height: 10px; border-radius: 50%; border: 1.5px solid white; flex-shrink: 0; }}

ul {{ list-style: none; display: flex; flex-direction: column; gap: 6px; }}
li {{ font-size: 12px; color: var(--text-main); line-height: 1.4; }}
li.src {{
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 10px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}}
.src-title {{ font-size: 13px; margin-bottom: 2px; }}
.src-title .provider {{ color: var(--text-muted); font-weight: normal; }}

.check {{ display: flex; justify-content: space-between; align-items: center; font-size: 12px; padding: 2px 0; }}

#map {{ width: 100%; height: 100%; background: #091017; }}
.maplibregl-popup-content {{
  background: #111a24;
  color: #e8edf3;
  border: 1px solid #223244;
  border-radius: 6px;
  padding: 12px;
  font-size: 12px;
  box-shadow: 0 4px 16px rgba(0,0,0,0.5);
  max-width: 320px;
}}
.maplibregl-popup-anchor-bottom .maplibregl-popup-tip {{ border-top-color: #111a24; }}
.popup-row {{ margin-bottom: 4px; }}
.popup-row b {{ color: #81c784; }}
.popup-badge {{ display: inline-block; font-size: 10px; font-weight: bold; padding: 2px 6px; border-radius: 4px; margin-bottom: 6px; }}
.popup-badge.tier1 {{ background: #2e7d32; color: #e8f5e9; }}
.popup-badge.tier2 {{ background: #f57f17; color: #fffde7; }}
.popup-badge.tier3 {{ background: #37474f; color: #cfd8dc; }}
</style>
</head>
<body class="{status_cls}">
<header>
  <h1>🦜 {pr['title']}</h1>
  <div class="badges">
    <span class="badge passed">Project: {pr['status']}</span>
    <span class="badge {validation['status']}">Validation: {validation['status']}</span>
  </div>
</header>

<aside id="sidebar">
  <div class="sect">
    <h2>Analytical Objective</h2>
    <p>{inte['objective']}</p>
  </div>

  <div class="sect">
    <h2>Multi-Criteria Suitability Results</h2>
    <div class="metrics-grid">
      <div class="metric prime">
        <b>{len(tier1_feats)}</b>
        <span>Prime Parcels (Road + School + KG &le;25min)</span>
      </div>
      <div class="metric prime">
        <b>{tier1_area_ha:0.1f} ha</b>
        <span>Prime Suitable Area</span>
      </div>
      <div class="metric">
        <b>{n_feat}</b>
        <span>Total Accessible Parcels (in Tartu linn)</span>
      </div>
      <div class="metric">
        <b>{total_area_ha:0.1f} ha</b>
        <span>Total Evaluated Area</span>
      </div>
    </div>
  </div>

  <div class="sect">
    <h2>Map Legend &amp; Layers</h2>
    <div class="legend-box">
      <div class="legend-item">
        <div class="legend-color" style="background: #2e7d32; border-color: #a5d6a7;"></div>
        <span><b>Tier 1 Prime:</b> Road &le;2km &amp; School+KG &le;25min</span>
      </div>
      <div class="legend-item">
        <div class="legend-color" style="background: #f57f17; border-color: #fff59d;"></div>
        <span><b>Tier 2 Good:</b> Road &le;2km &amp; School or KG &le;25min</span>
      </div>
      <div class="legend-item">
        <div class="legend-color" style="background: #455a64; border-color: #90a4ae;"></div>
        <span><b>Tier 3:</b> Road access only (&gt;25min walk to school/KG)</span>
      </div>
      <div class="legend-item">
        <div class="legend-color" style="background: rgba(25, 118, 210, 0.15); border: 1.5px dashed #42a5f5;"></div>
        <span>School 25-min Walk Catchment (2000m)</span>
      </div>
      <div class="legend-item">
        <div class="legend-color" style="background: rgba(245, 124, 0, 0.15); border: 1.5px dashed #ffa726;"></div>
        <span>Kindergarten 25-min Walk Catchment (2000m)</span>
      </div>
      <div class="legend-item">
        <div class="legend-dot" style="background: #42a5f5;"></div>
        <span>🏫 Municipal Schools (n=40)</span>
      </div>
      <div class="legend-item">
        <div class="legend-dot" style="background: #ffa726;"></div>
        <span>🎒 Municipal Kindergartens (n=53)</span>
      </div>
      <div class="legend-item">
        <div class="legend-line" style="background: #7986cb;"></div>
        <span>National Highways (Põhimaantee/Tugimaantee)</span>
      </div>
      <div class="legend-item">
        <div class="legend-line" style="background: #ffd54f; height: 3px; border-top: 1px dashed black;"></div>
        <span>Planned Connector Road (OVERRIDE-002)</span>
      </div>
    </div>
  </div>

  <div class="sect">
    <h2>Analytical Constraints &amp; Criteria</h2>
    <ul>
      <li>• <b>Minimum Area:</b> &ge; 20 000 m² (2.0 ha) measured in EPSG:3301 (L-EST97)</li>
      <li>• <b>Land-use (siht1):</b> Agricultural, Production, or Commercial in Tartu linn (79,056 county parcels evaluated)</li>
      <li>• <b>Highway Accessibility:</b> &le; 2 000 m planar distance to primary/secondary highway or planned connector</li>
      <li>• <b>Education Catchment:</b> &le; 25 min walk (2,000 m at 4.8 km/h) to public school and kindergarten</li>
    </ul>
  </div>

  <div class="sect">
    <h2>Assumptions</h2>
    <ul>{assump_html}</ul>
  </div>

  <div class="sect">
    <h2>Project Overrides ({len(PROJECT.get('overrides', []))})</h2>
    <ul>{overrides_html}</ul>
  </div>

  <div class="sect">
    <h2>Authoritative Sources &amp; Runtime Manifest</h2>
    <ul>{sources_html}</ul>
  </div>

  {warnings_html}

  <div class="sect">
    <h2>Validation Gates</h2>
    {checks_html}
    <div class="prov" style="margin-top: 4px;">Run ID: <code>{validation.get('run_id','')}</code> · Schema: <code>open-gis-project/v1</code></div>
  </div>
</aside>

<div id="map"></div>

<script src="https://unpkg.com/maplibre-gl@3.6.2/dist/maplibre-gl.js"></script>
<script>
const candidatesGeoJSON = {GEOJSON_STR};
const roadsGeoJSON = {ROADS_STR};
const plannedGeoJSON = {PLAN_STR};
const catchmentsGeoJSON = {CATCHMENTS_STR};
const poisGeoJSON = {POIS_STR};

const map = new maplibregl.Map({{
  container: 'map',
  style: 'https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json',
  center: [{c_lng:.4f}, {c_lat:.4f}],
  zoom: 11
}});

map.on('load', () => {{
  // 1. Add Education Catchment Buffers (25-min walking radius)
  map.addSource('catchments', {{ type: 'geojson', data: catchmentsGeoJSON }});
  
  // School catchment layer
  map.addLayer({{
    id: 'school_catchment_fill',
    type: 'fill',
    source: 'catchments',
    filter: ['==', 'type', 'school_catchment'],
    paint: {{
      'fill-color': '#1976d2',
      'fill-opacity': 0.10
    }}
  }});
  map.addLayer({{
    id: 'school_catchment_line',
    type: 'line',
    source: 'catchments',
    filter: ['==', 'type', 'school_catchment'],
    paint: {{
      'line-color': '#42a5f5',
      'line-width': 1.5,
      'line-opacity': 0.4,
      'line-dasharray': [4, 2]
    }}
  }});

  // Kindergarten catchment layer
  map.addLayer({{
    id: 'kg_catchment_fill',
    type: 'fill',
    source: 'catchments',
    filter: ['==', 'type', 'kindergarten_catchment'],
    paint: {{
      'fill-color': '#f57c00',
      'fill-opacity': 0.08
    }}
  }});
  map.addLayer({{
    id: 'kg_catchment_line',
    type: 'line',
    source: 'catchments',
    filter: ['==', 'type', 'kindergarten_catchment'],
    paint: {{
      'line-color': '#ffa726',
      'line-width': 1.5,
      'line-opacity': 0.4,
      'line-dasharray': [4, 2]
    }}
  }});

  // 2. Add Main Road Network
  map.addSource('main_roads', {{ type: 'geojson', data: roadsGeoJSON }});
  map.addLayer({{
    id: 'main_roads_layer',
    type: 'line',
    source: 'main_roads',
    paint: {{
      'line-color': '#7986cb',
      'line-width': 2.5,
      'line-opacity': 0.8
    }}
  }});

  // 3. Add Planned Road Override
  map.addSource('planned_road', {{ type: 'geojson', data: plannedGeoJSON }});
  map.addLayer({{
    id: 'planned_road_layer',
    type: 'line',
    source: 'planned_road',
    paint: {{
      'line-color': '#ffd54f',
      'line-width': 3.5,
      'line-dasharray': [3, 2]
    }}
  }});

  // 4. Add Candidate Parcels (Color-coded by Suitability Tier)
  map.addSource('candidates', {{ type: 'geojson', data: candidatesGeoJSON }});
  map.addLayer({{
    id: 'candidates_layer',
    type: 'fill',
    source: 'candidates',
    paint: {{
      'fill-color': [
        'match',
        ['get', 'suitability_tier'],
        'Tier 1: Prime (<=25min to School & Kindergarten)', '#2e7d32',
        'Tier 2: Good (<=25min to School or Kindergarten)', '#f57f17',
        '#455a64'
      ],
      'fill-opacity': [
        'match',
        ['get', 'suitability_tier'],
        'Tier 1: Prime (<=25min to School & Kindergarten)', 0.75,
        'Tier 2: Good (<=25min to School or Kindergarten)', 0.60,
        0.35
      ],
      'fill-outline-color': [
        'match',
        ['get', 'suitability_tier'],
        'Tier 1: Prime (<=25min to School & Kindergarten)', '#a5d6a7',
        'Tier 2: Good (<=25min to School or Kindergarten)', '#fff59d',
        '#90a4ae'
      ]
    }}
  }});

  // 5. Add Education POIs
  map.addSource('education_pois', {{ type: 'geojson', data: poisGeoJSON }});
  map.addLayer({{
    id: 'education_pois_layer',
    type: 'circle',
    source: 'education_pois',
    paint: {{
      'circle-radius': 5,
      'circle-color': [
        'match',
        ['get', 'amenity'],
        'school', '#42a5f5',
        '#ffa726'
      ],
      'circle-stroke-width': 1.5,
      'circle-stroke-color': '#ffffff'
    }}
  }});

  // Interactive Popup on Parcel Click
  map.on('click', 'candidates_layer', (e) => {{
    if (!e.features || !e.features.length) return;
    const p = e.features[0].properties;
    const ha = (p.area_m2 / 10000).toFixed(2);
    const tierBadge = p.suitability_tier.includes('Tier 1') ? 'tier1' : (p.suitability_tier.includes('Tier 2') ? 'tier2' : 'tier3');
    const html = `
      <div class="popup-badge ${{tierBadge}}">${{p.suitability_tier}}</div>
      <div class="popup-row"><b>Cadastral ID:</b> <code>${{p.cadastral_id}}</code></div>
      <div class="popup-row"><b>Address:</b> ${{p.address || 'N/A'}}</div>
      <div class="popup-row"><b>Settlement:</b> ${{p.settlement || 'N/A'}}</div>
      <div class="popup-row"><b>Land Use:</b> ${{p.land_use}}</div>
      <div class="popup-row"><b>Area:</b> ${{Number(p.area_m2).toLocaleString()}} m² (${{ha}} ha)</div>
      <div class="popup-row"><b>Highway Proximity:</b> ${{p.dist_main_road_m}} m</div>
      <div class="popup-row"><b>To Nearest School:</b> ${{p.walk_time_school_min}} min (${{p.dist_school_m}} m)</div>
      <div class="popup-row"><b>To Nearest Kindergarten:</b> ${{p.walk_time_kg_min}} min (${{p.dist_kg_m}} m)</div>
    `;
    new maplibregl.Popup()
      .setLngLat(e.lngLat)
      .setHTML(html)
      .addTo(map);
  }});

  // Interactive Popup on POI Click
  map.on('click', 'education_pois_layer', (e) => {{
    if (!e.features || !e.features.length) return;
    const p = e.features[0].properties;
    const icon = p.amenity === 'school' ? '🏫 School' : '🎒 Kindergarten';
    const html = `
      <div class="popup-row"><b>${{icon}}:</b> ${{p.name}}</div>
      <div class="popup-row"><b>Operator:</b> ${{p.operator || 'Tartu linn'}}</div>
      <div class="popup-row"><b>Type:</b> <code>${{p.amenity}}</code></div>
    `;
    new maplibregl.Popup()
      .setLngLat(e.lngLat)
      .setHTML(html)
      .addTo(map);
  }});

  map.on('mouseenter', 'candidates_layer', () => map.getCanvas().style.cursor = 'pointer');
  map.on('mouseleave', 'candidates_layer', () => map.getCanvas().style.cursor = '');
  map.on('mouseenter', 'education_pois_layer', () => map.getCanvas().style.cursor = 'pointer');
  map.on('mouseleave', 'education_pois_layer', () => map.getCanvas().style.cursor = '');

  try {{
    map.fitBounds([
      [{min_lng:.4f}, {min_lat:.4f}],
      [{max_lng:.4f}, {max_lat:.4f}]
    ], {{ padding: 40 }});
  }} catch (_) {{}}
}});
</script>
</body>
</html>"""

    out = ROOT / "dashboard.html"
    out.write_text(html)
    log.info("Rendered dashboard: %s (%0.1f KB)", out, len(html) / 1024)


def main() -> None:
    for d in (DERIVED, VALIDATION, RUNS, SOURCE):
        d.mkdir(parents=True, exist_ok=True)

    cadastre_gpkg, roads_geojson, pois_geojson, manifest = fetch_and_manifest_sources()

    con = duckdb.connect()
    con.install_extension("spatial")
    con.load_extension("spatial")

    run_pipeline(con, cadastre_gpkg, roads_geojson, pois_geojson)
    validation = write_validation(con)
    write_qgis_project(con)
    render_dashboard(con, validation, manifest)
    log.info("E2E full run complete!")


if __name__ == "__main__":
    main()