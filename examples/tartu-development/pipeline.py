# =============================================================================
# Deterministic pipeline for examples/tartu-development
# =============================================================================
# Reproduces the Tartu development suitability analysis using REAL datasets:
#   1. Maa- ja Ruumiamet Cadastral GeoPackage (Tartu maakond snapshot)
#      https://s3.pilw.io/rp-kemit-kataster/ANDMED/Tartu_maakond_KATASTER_GPKG.zip
#   2. ETAK National Road Network (main roads: Põhimaantee & Tugimaantee)
#      https://gsavalik.envir.ee/geoserver/etak/wfs
#   3. Educational Institutions POIs (Schools & Kindergartens)
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
# Requirements: duckdb (with spatial extension), pyproj
# Execution: python pipeline.py
# =============================================================================

import io
import json
import logging
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

import duckdb
import pyproj

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "data" / "source"
DERIVED = ROOT / "data" / "derived"
OVERRIDES = ROOT / "data" / "overrides"
VALIDATION = ROOT / "validation"
RUNS = ROOT / "runs"

ANALYSIS_CRS = "EPSG:3301"   # L-EST97 metric CRS for all distance/area calculations
STORAGE_CRS = "EPSG:4326"    # WGS84 for GeoJSON web rendering
WALK_SPEED_M_PER_MIN = 80.0  # 4.8 km/h standard pedestrian speed (25 min = 2000 m)

log = logging.getLogger("tartu-pipeline")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def ensure_sources() -> tuple[Path, Path, Path]:
    """Download and extract real source datasets if not already cached in data/source/."""
    SOURCE.mkdir(parents=True, exist_ok=True)

    # 1. Real Maa- ja Ruumiamet Cadastral GeoPackage for Tartu county
    cadastre_gpkg = SOURCE / "Tartu_maakond_KATASTER_GPKG.gpkg"
    cadastre_url = "https://s3.pilw.io/rp-kemit-kataster/ANDMED/Tartu_maakond_KATASTER_GPKG.zip"

    if not cadastre_gpkg.exists():
        log.info("Downloading official Tartu county Cadastral GeoPackage from Maa- ja Ruumiamet S3...")
        req = urllib.request.Request(cadastre_url, headers={"User-Agent": "open-gis-pipeline/1.0"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            content = resp.read()
        log.info("Extracting %0.1f MB archive to %s", len(content) / (1024 * 1024), SOURCE)
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            z.extractall(SOURCE)
    else:
        log.info("Using cached Cadastral GeoPackage: %s", cadastre_gpkg)

    # 2. Real ETAK Road Network via WFS (Tartu regional bbox in EPSG:3301)
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
        srv = "https://maps.mail.ru/osm/tools/overpass/api/interpreter"
        url = srv + "?data=" + urllib.parse.quote(query)
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

    return cadastre_gpkg, roads_geojson, pois_geojson


def main() -> None:
    for d in (DERIVED, VALIDATION, RUNS):
        d.mkdir(parents=True, exist_ok=True)

    cadastre_gpkg, roads_geojson, pois_geojson = ensure_sources()

    con = duckdb.connect()
    con.install_extension("spatial")
    con.load_extension("spatial")

    t_3301 = pyproj.Transformer.from_crs(4326, 3301, always_xy=True)
    t_4326 = pyproj.Transformer.from_crs(3301, 4326, always_xy=True)

    # STEP 1 — Load authoritative cadastral parcels from Maa- ja Ruumiamet GeoPackage
    log.info("Loading parcels from GeoPackage: %s", cadastre_gpkg)
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
    n_raw = con.execute("SELECT count(*) FROM parcels_raw").fetchone()[0]
    log.info("Loaded %d raw cadastral parcels across Tartu county", n_raw)

    # STEP 2 — Filter large parcels meeting development land-use criteria in Tartu linn
    con.execute("""
        CREATE OR REPLACE TABLE large_parcels AS
        SELECT *
        FROM parcels_raw
        WHERE area_m2 >= 20000
          AND land_use IN ('MAATULUNDUSMAA', 'TOOTMISMAA', 'ARIMAA')
          AND municipality = 'Tartu linn'
    """)
    n_large = con.execute("SELECT count(*) FROM large_parcels").fetchone()[0]
    log.info("Filtered %d large parcels (>= 2 ha) meeting land-use in Tartu linn", n_large)

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
    n_roads = con.execute("SELECT count(*) FROM main_roads").fetchone()[0]
    log.info("Loaded %d main road segments from ETAK", n_roads)

    # STEP 4 — Apply scenario overrides (OVERRIDE-002: planned connector road)
    planned_road_file = OVERRIDES / "planned-road.geojson"
    if planned_road_file.exists():
        log.info("Applying scenario override from %s", planned_road_file)
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

    n_schools = con.execute("SELECT count(*) FROM schools").fetchone()[0]
    n_kg = con.execute("SELECT count(*) FROM kindergartens").fetchone()[0]
    log.info("Loaded %d schools and %d kindergartens in Tartu area", n_schools, n_kg)

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

    n_total_candidates = con.execute("SELECT count(*) FROM candidate_parcels").fetchone()[0]
    n_tier1 = con.execute("SELECT count(*) FROM candidate_parcels WHERE dist_school_m <= 2000 AND dist_kg_m <= 2000").fetchone()[0]
    n_tier2 = con.execute("SELECT count(*) FROM candidate_parcels WHERE (dist_school_m <= 2000 OR dist_kg_m <= 2000) AND NOT (dist_school_m <= 2000 AND dist_kg_m <= 2000)").fetchone()[0]

    log.info("Total candidates meeting highway access: %d", n_total_candidates)
    log.info("  -> Tier 1 Prime (<=25 min walk to BOTH School & Kindergarten): %d parcels", n_tier1)
    log.info("  -> Tier 2 Good (<=25 min walk to School OR Kindergarten): %d parcels", n_tier2)

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
    # 1. GeoPackage & Parquet
    gpkg_out = DERIVED / "final-candidates.gpkg"
    con.execute(f"COPY candidate_parcels TO '{gpkg_out}' (FORMAT GDAL, DRIVER 'GPKG')")
    log.info("Exported GeoPackage (EPSG:3301): %s", gpkg_out)

    parquet_out = DERIVED / "final-candidates.parquet"
    con.execute(f"COPY candidate_parcels TO '{parquet_out}' (FORMAT PARQUET)")
    log.info("Exported Parquet: %s", parquet_out)

    # 2. GeoJSON Candidates (EPSG:4326)
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
    log.info("Exported Candidates GeoJSON (EPSG:4326): %d features", len(coll["features"]))

    # 3. GeoJSON Catchments (EPSG:4326)
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
    log.info("Exported Catchments GeoJSON: %s", DERIVED / "education_catchments.json")

    # 4. GeoJSON Education POIs (EPSG:4326)
    (DERIVED / "education_pois.json").write_text(pois_geojson.read_text())
    log.info("Exported Education POIs GeoJSON: %s", DERIVED / "education_pois.json")

    # STEP 9 — Validation Report
    report = run_validation(con, n_total_candidates, n_tier1)
    val_file = VALIDATION / "latest-report.json"
    val_file.write_text(json.dumps(report, indent=2, default=str))
    log.info("Validation report written to %s (status: %s)", val_file, report["status"])


def run_validation(con: duckdb.DuckDBPyConnection, n_candidates: int, n_tier1: int) -> dict:
    bad_geom = con.execute(
        "SELECT COUNT(*) FROM candidate_parcels WHERE NOT ST_IsValid(geometry)"
    ).fetchone()[0]
    dup_ids = con.execute(
        "SELECT COUNT(*) - COUNT(DISTINCT cadastral_id) FROM candidate_parcels"
    ).fetchone()[0]
    out_of_range = con.execute(
        "SELECT COUNT(*) FROM candidate_parcels WHERE area_m2 <= 0 OR area_m2 >= 100000000"
    ).fetchone()[0]

    checks = [
        {
            "id": "geometry_valid",
            "status": "passed" if bad_geom == 0 else "failed",
            "features_checked": n_candidates,
            "invalid_count": int(bad_geom),
        },
        {
            "id": "no_duplicate_cadastral_id",
            "status": "passed" if dup_ids == 0 else "failed",
            "duplicates": int(dup_ids),
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
            "out_of_range_count": int(out_of_range),
        },
        {
            "id": "poi_completeness",
            "status": "warning",
            "reason": "No authoritative completeness baseline available for OSM POIs",
        },
    ]
    status = "passed" if all(c["status"] in ("passed", "warning") for c in checks) else "failed"
    return {
        "run_id": "run-20260825-100000",
        "status": status,
        "checks": checks,
        "candidate_count": n_candidates,
        "prime_tier1_count": n_tier1,
    }


if __name__ == "__main__":
    main()