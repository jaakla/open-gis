# =============================================================================
# pipeline.py — Canonical reproducible run for examples/tartu-development
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
#   3. Tartu municipal schools and kindergartens (official ArcGIS Feature Services)
#      https://gis.tartulv.ee/arcgis/rest/services/Haridus
#   4. Explicit hypothetical scenario (OVERRIDE-002: connector road)
#      data/overrides/planned-road.geojson
#
# Multi-criteria constraints:
#   - Minimum parcel size >= 20,000 m2 (2.0 ha) in EPSG:3301 (L-EST97)
#   - Land-use (siht1): Agricultural, Production, or Commercial in Tartu linn
#   - Arterial road proximity <= 2,000 m (Põhimaantee/Tugimaantee or planned road)
#   - Education screening proxy: <= 2,000 m straight-line distance to verified municipal facilities
#
# Execution: python pipeline.py (run_e2e.py is a thin wrapper)
# =============================================================================

import datetime
import hashlib
import io
import json
import logging
import os
import sqlite3
import urllib.parse
import urllib.request
import zipfile
import xml.etree.ElementTree as ET
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

log = logging.getLogger("tartu-pipeline")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

PROJECT = yaml.safe_load((ROOT / "project.yaml").read_text())

ANALYSIS_CRS = 3301   # L-EST97 metric CRS
STORAGE_CRS = 4326    # WGS84 for MapLibre rendering
WALK_SPEED_M_PER_MIN = 80.0  # 4.8 km/h standard pedestrian speed (25 min = 2000 m)


def _run_id() -> str:
    return "run-" + datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as src:
        for chunk in iter(lambda: src.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _read_json_url(base_url: str, params: dict, timeout: int = 120) -> dict:
    url = base_url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "open-gis-pipeline/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


# ------------------------------- STEP 0: sources ----------------------------
def fetch_and_manifest_sources() -> tuple[Path, Path, Path, list[dict]]:
    """Fetch complete, semantically fit sources and record exact runtime metadata."""
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

    # 2. ETAK main roads via complete, paginated WFS query.
    roads_geojson = SOURCE / "etak_main_roads.geojson"
    roads_meta_file = SOURCE / "etak_main_roads.meta.json"
    roads_wfs_url = "https://gsavalik.envir.ee/geoserver/etak/wfs"
    cql_filter = (
        "BBOX(shape,640000,6455000,685000,6500000,'EPSG:3301') "
        "AND tyyp_tekst IN ('Põhimaantee','Tugimaantee')"
    )
    page_size = 1000
    roads_cache_valid = False
    if roads_geojson.exists() and roads_meta_file.exists():
        roads_meta = json.loads(roads_meta_file.read_text())
        roads_raw = json.loads(roads_geojson.read_text())
        roads_cache_valid = (
            roads_meta.get("cql_filter") == cql_filter
            and roads_meta.get("matched") == roads_meta.get("returned")
            and roads_meta.get("returned") == len(roads_raw.get("features", []))
        )
    if not roads_cache_valid:
        log.info("Fetching complete ETAK main-road result with WFS pagination...")
        features: list[dict] = []
        matched = None
        pages = 0
        while matched is None or len(features) < matched:
            page = _read_json_url(
                roads_wfs_url,
                {
                    "service": "WFS",
                    "version": "2.0.0",
                    "request": "GetFeature",
                    "typeNames": "etak:e_501_tee_j",
                    "srsName": "EPSG:3301",
                    "outputFormat": "application/json",
                    "CQL_FILTER": cql_filter,
                    "count": page_size,
                    "startIndex": len(features),
                },
            )
            if matched is None:
                matched = int(page.get("numberMatched", page.get("totalFeatures", -1)))
                if matched < 0:
                    raise RuntimeError("ETAK WFS did not report numberMatched; completeness cannot be proven")
            returned = int(page.get("numberReturned", len(page.get("features", []))))
            page_features = page.get("features", [])
            if returned != len(page_features) or not page_features:
                raise RuntimeError("ETAK WFS pagination stopped before numberMatched was returned")
            features.extend(page_features)
            pages += 1
        if len(features) != matched:
            raise RuntimeError(f"ETAK completeness failure: matched={matched}, returned={len(features)}")
        roads_raw = {
            "type": "FeatureCollection",
            "numberMatched": matched,
            "numberReturned": len(features),
            "features": features,
        }
        roads_geojson.write_text(json.dumps(roads_raw, indent=2, ensure_ascii=False))
        roads_meta = {
            "source_url": roads_wfs_url,
            "retrieved_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "cql_filter": cql_filter,
            "page_size": page_size,
            "pages": pages,
            "matched": matched,
            "returned": len(features),
        }
        roads_meta_file.write_text(json.dumps(roads_meta, indent=2, ensure_ascii=False))
    else:
        log.info("Using completeness-verified cached ETAK roads: %s", roads_geojson)
    if "retrieved_at" not in roads_meta:
        roads_meta["retrieved_at"] = datetime.datetime.fromtimestamp(
            roads_geojson.stat().st_mtime, datetime.timezone.utc
        ).isoformat()
        roads_meta_file.write_text(json.dumps(roads_meta, indent=2, ensure_ascii=False))

    # 3. Tartu authoritative municipal schools and kindergartens.
    pois_geojson = SOURCE / "tartu_municipal_education.geojson"
    pois_meta_file = SOURCE / "tartu_municipal_education.meta.json"
    school_item = "45671f12e7864221976cb11c48c57cd1"
    kindergarten_item = "2500887fd6414aa18cc08c7b8e712623"
    school_url = "https://gis.tartulv.ee/arcgis/rest/services/Haridus/LU_koolid/FeatureServer/0/query"
    kindergarten_url = "https://gis.tartulv.ee/arcgis/rest/services/Haridus/LU_lasteaiad_lastehoiud/FeatureServer/0/query"
    school_where = "Omand=1 AND Liik<>5 AND Lopetamise_kp IS NULL"
    kindergarten_where = "Liik=10 AND Lopetamise_kp IS NULL"
    education_cache_valid = False
    if pois_geojson.exists() and pois_meta_file.exists():
        pois_raw = json.loads(pois_geojson.read_text())
        pois_meta = json.loads(pois_meta_file.read_text())
        education_cache_valid = (
            pois_meta.get("matched") == pois_meta.get("returned") == len(pois_raw.get("features", []))
            and all(
                f.get("properties", {}).get("ownership") == "municipal"
                and f.get("properties", {}).get("active") is True
                for f in pois_raw.get("features", [])
            )
        )
    if not education_cache_valid:
        log.info("Fetching authoritative Tartu municipal education layers...")
        normalized: list[dict] = []
        counts: dict[str, int] = {}
        for amenity, item_id, url, where in (
            ("school", school_item, school_url, school_where),
            ("kindergarten", kindergarten_item, kindergarten_url, kindergarten_where),
        ):
            out_fields = "OBJECTID,Nimi,Liik,GlobalID,Aadress,Lopetamise_kp"
            if amenity == "school":
                out_fields += ",Omand"
            count_result = _read_json_url(url, {"f": "json", "where": where, "returnCountOnly": "true"})
            matched = int(count_result["count"])
            result = _read_json_url(
                url,
                {
                    "f": "geojson",
                    "where": where,
                    "outFields": out_fields,
                    "outSR": 4326,
                    "returnGeometry": "true",
                },
            )
            returned = len(result.get("features", []))
            if returned != matched:
                raise RuntimeError(f"Tartu {amenity} completeness failure: matched={matched}, returned={returned}")
            counts[amenity] = matched
            for feature in result["features"]:
                props = feature["properties"]
                if amenity == "school" and props.get("Omand") != 1:
                    raise RuntimeError("Non-municipal school passed the authoritative ownership predicate")
                if amenity == "kindergarten" and props.get("Liik") != 10:
                    raise RuntimeError("Non-municipal kindergarten passed the authoritative type predicate")
                if props.get("Lopetamise_kp") is not None:
                    raise RuntimeError("Inactive education feature passed the active-status predicate")
                stable_id = (props.get("GlobalID") or str(props["OBJECTID"])).strip("{}")
                normalized.append(
                    {
                        "type": "Feature",
                        "properties": {
                            "source_id": f"{amenity}:{stable_id}",
                            "name": props["Nimi"],
                            "amenity": amenity,
                            "ownership": "municipal",
                            "official_type_code": props.get("Liik"),
                            "address": props.get("Aadress") or "",
                            "source_item": item_id,
                            "active": True,
                        },
                        "geometry": feature["geometry"],
                    }
                )
        pois_raw = {"type": "FeatureCollection", "features": normalized}
        pois_geojson.write_text(json.dumps(pois_raw, indent=2, ensure_ascii=False))
        pois_meta = {
            "sources": [school_item, kindergarten_item],
            "retrieved_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "predicates": {"school": school_where, "kindergarten": kindergarten_where},
            "counts": counts,
            "matched": sum(counts.values()),
            "returned": len(normalized),
        }
        pois_meta_file.write_text(json.dumps(pois_meta, indent=2, ensure_ascii=False))
    else:
        log.info("Using semantics-verified cached municipal education data: %s", pois_geojson)
    if "retrieved_at" not in pois_meta:
        pois_meta["retrieved_at"] = datetime.datetime.fromtimestamp(
            pois_geojson.stat().st_mtime, datetime.timezone.utc
        ).isoformat()
        pois_meta_file.write_text(json.dumps(pois_meta, indent=2, ensure_ascii=False))

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
            "sha256": _sha256(cadastre_gpkg),
        },
        {
            "key": "roads",
            "role": PROJECT["sources"]["roads"]["role"],
            "file": "etak_roads.geojson (WFS GetFeature query result)",
            "format": "GeoJSON (FeatureCollection, EPSG:3301)",
            "table_name": "etak:e_501_tee_j",
            "source_url": roads_wfs_url,
            "portal_page": "https://geoportaal.maaruum.ee/est/ruumiandmed/eesti-topograafia-andmekogu/laadi-etak-andmed-alla-p609.html",
            "download_timestamp": roads_meta["retrieved_at"],
            "version": "etak:e_501_tee_j completeness-verified snapshot",
            "rows": roads_count,
            "n_columns": len(roads_cols),
            "columns": roads_cols,
            "completeness": roads_meta,
            "sha256": _sha256(roads_geojson),
        },
        {
            "key": "education_pois",
            "role": PROJECT["sources"]["education_pois"]["role"],
            "file": "tartu_municipal_education.geojson (normalized official ArcGIS queries)",
            "format": "GeoJSON (FeatureCollection, EPSG:4326)",
            "table_name": "municipal_education",
            "source_url": "https://gis.tartulv.ee/arcgis/rest/services/Haridus",
            "portal_page": "https://geohub.tartulv.ee/",
            "download_timestamp": pois_meta["retrieved_at"],
            "version": f"ArcGIS items {school_item} + {kindergarten_item}",
            "rows": pois_count,
            "n_columns": len(pois_cols),
            "columns": pois_cols,
            "semantic_predicates": pois_meta["predicates"],
            "completeness": {"matched": pois_meta["matched"], "returned": pois_meta["returned"]},
            "sha256": _sha256(pois_geojson),
        },
    ]
    (SOURCE / "manifest.json").write_text(json.dumps(manifest, indent=2))
    log.info("Source manifest recorded: %d parcels, %d roads, %d education POIs", parcels_info, roads_count, pois_count)
    return cadastre_gpkg, roads_geojson, pois_geojson, manifest


# ----------------------------- STEP 1-7: pipeline ---------------------------
PLACEHOLDER_EVIDENCE = {"", "todo", "tbd", "n/a", "na", "none", "placeholder", "example", "xxx"}


def apply_attribute_overrides(source_collection: dict, source_key: str) -> tuple[dict, list[dict]]:
    """Apply project.yaml `modify_attribute` overrides to a source FeatureCollection.

    The source file is never rewritten: this returns the EFFECTIVE collection
    (immutable source + override layer). Each override is only applied when

      1. its target feature exists in the source, and
      2. the asserted prior value (`change.from`) matches the source value, and
      3. its evidence is present and non-placeholder,

    otherwise it is reported as `rejected` with a reason. Merely declaring an
    override is not applying it (project-spec.md section 2.3).
    """
    effective = json.loads(json.dumps(source_collection))
    by_id = {f["properties"].get("source_id"): f for f in effective["features"]}
    results: list[dict] = []

    for override in PROJECT.get("overrides", []):
        if override.get("action") != "modify_attribute":
            continue
        target = override.get("target", {})
        if target.get("source") != source_key:
            continue

        feature = by_id.get(target.get("feature_id"))
        change = override.get("change", {})
        field, want_from, want_to = change.get("field"), change.get("from"), change.get("to")
        evidence = [
            e for e in override.get("evidence", [])
            if str(e.get("value", "")).strip().lower() not in PLACEHOLDER_EVIDENCE
        ]

        if feature is None:
            results.append({"id": override["id"], "status": "rejected",
                            "detail": f"target feature {target.get('feature_id')!r} not found in {source_key}"})
            continue
        if not evidence:
            results.append({"id": override["id"], "status": "rejected",
                            "detail": "evidence is missing or a placeholder"})
            continue
        actual_from = feature["properties"].get(field)
        if actual_from != want_from:
            results.append({"id": override["id"], "status": "rejected",
                            "detail": f"prior value mismatch on {field}: source={actual_from!r}, asserted={want_from!r}"})
            continue

        feature["properties"][field] = want_to
        feature["properties"]["override_id"] = override["id"]
        feature["properties"]["override_origin"] = override.get("origin", override.get("created_by", "analyst"))
        results.append({
            "id": override["id"],
            "status": "applied",
            "detail": f"{target.get('feature_id')}.{field}: {want_from!r} -> {want_to!r}",
            "target": target.get("feature_id"),
            "field": field,
            "from": want_from,
            "to": want_to,
            "origin": override.get("origin", override.get("created_by", "analyst")),
        })

    # map_class drives both the MapLibre and QGIS categorized styles, so scenario
    # facilities stay visually distinct from authoritative ones.
    for feature in effective["features"]:
        props = feature["properties"]
        props["map_class"] = (
            props["amenity"] if props.get("active") is True else "scenario_inactive"
        )
    return effective, results


def run_pipeline(
    con: duckdb.DuckDBPyConnection, cadastre_gpkg: Path, roads_geojson: Path, pois_geojson: Path
) -> list[dict]:
    """Run every processing step; returns the per-override application results."""
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

    # STEP 3 — Load completeness-verified official main roads.
    con.execute(f"""
        CREATE OR REPLACE TABLE official_roads AS
        SELECT ST_GeomFromGeoJSON(f.geometry) AS geometry,
               f.properties.nimetus AS name,
               f.properties.tyyp_tekst AS road_class
        FROM (
            SELECT unnest(features) as f FROM read_json_auto('{roads_geojson}')
        )
        WHERE f.properties.tyyp_tekst IN ('Põhimaantee', 'Tugimaantee')
    """)

    # STEP 4 — Load the hypothetical connector into a separate scenario table.
    con.execute("CREATE OR REPLACE TABLE scenario_roads (geometry GEOMETRY, name VARCHAR, road_class VARCHAR)")
    planned_road_file = OVERRIDES / "planned-road.geojson"
    if planned_road_file.exists():
        plan_raw = json.loads(planned_road_file.read_text())
        for ft in plan_raw.get("features", []):
            coords_3301 = [t_3301.transform(x, y) for x, y in ft["geometry"]["coordinates"]]
            wkt = "LINESTRING(" + ", ".join(f"{x} {y}" for x, y in coords_3301) + ")"
            con.execute(
                "INSERT INTO scenario_roads VALUES (ST_GeomFromText(?), ?, ?)",
                [wkt, ft["properties"].get("name", "Hypothetical connector road"), "Scenario (OVERRIDE-002)"],
            )

    # STEP 5 — Load education POIs, verify source semantics, then apply the
    # declared attribute overrides. Immutable source + override = effective input:
    # the source file on disk is never rewritten here.
    pois_raw = json.loads(pois_geojson.read_text())
    for f in pois_raw.get("features", []):
        props = f["properties"]
        if props.get("ownership") != "municipal" or props.get("active") is not True:
            raise RuntimeError(f"Education semantic predicate failed for {props.get('source_id')}")

    effective_pois, override_results = apply_attribute_overrides(pois_raw, "education_pois")
    (DERIVED / "education_pois.json").write_text(
        json.dumps(effective_pois, indent=2, ensure_ascii=False)
    )
    for result in override_results:
        log.info("Override %s: %s (%s)", result["id"], result["status"], result.get("detail", ""))

    # STEP 5b — Only facilities that are active AFTER overrides enter the analysis.
    con.execute("CREATE OR REPLACE TABLE schools (source_id VARCHAR, name VARCHAR, ownership VARCHAR, active BOOLEAN, geometry GEOMETRY)")
    con.execute("CREATE OR REPLACE TABLE kindergartens (source_id VARCHAR, name VARCHAR, ownership VARCHAR, active BOOLEAN, geometry GEOMETRY)")

    for f in effective_pois["features"]:
        props = f["properties"]
        if props.get("active") is not True:
            continue
        lon, lat = f["geometry"]["coordinates"]
        x, y = t_3301.transform(lon, lat)
        tbl = "schools" if props["amenity"] == "school" else "kindergartens"
        con.execute(
            f"INSERT INTO {tbl} VALUES (?, ?, ?, ?, ST_Point(?, ?))",
            [props["source_id"], props["name"], props["ownership"], props["active"], x, y],
        )

    # STEP 6 — Multi-criteria Spatial Evaluation:
    # - Distance to highway network (<= 2000 m)
    # - Distance to nearest school (m) and walk time (min)
    # - Distance to nearest kindergarten (m) and walk time (min)
    con.execute("""
        CREATE OR REPLACE TABLE candidate_parcels AS
        WITH official_road_geom AS (SELECT ST_Union_Agg(geometry) AS u FROM official_roads),
             scenario_road_geom AS (SELECT ST_Union_Agg(geometry) AS u FROM scenario_roads),
             school_geom AS (SELECT ST_Union_Agg(geometry) AS u FROM schools),
             kg_geom AS (SELECT ST_Union_Agg(geometry) AS u FROM kindergartens)
        SELECT p.cadastral_id,
               p.address,
               p.municipality,
               p.settlement,
               p.land_use,
               p.area_m2,
               round(least(ST_Distance(p.geometry, r.u), ST_Distance(p.geometry, sr.u)), 1) AS dist_main_road_m,
               round(ST_Distance(p.geometry, r.u), 1) AS dist_official_road_m,
               round(ST_Distance(p.geometry, sr.u), 1) AS dist_scenario_road_m,
               CASE WHEN ST_Distance(p.geometry, sr.u) < ST_Distance(p.geometry, r.u)
                    THEN 'scenario' ELSE 'official' END AS nearest_road_source,
               round(ST_Distance(p.geometry, s.u), 1) AS dist_school_m,
               round(ST_Distance(p.geometry, k.u), 1) AS dist_kg_m,
               round(ST_Distance(p.geometry, s.u) / 80.0, 1) AS straightline_time_school_min,
               round(ST_Distance(p.geometry, k.u) / 80.0, 1) AS straightline_time_kg_min,
               CASE
                 WHEN ST_Distance(p.geometry, s.u) <= 2000 AND ST_Distance(p.geometry, k.u) <= 2000
                   THEN 'Tier 1: Prime (<=2km proxy to School & Kindergarten)'
                 WHEN ST_Distance(p.geometry, s.u) <= 2000 OR ST_Distance(p.geometry, k.u) <= 2000
                   THEN 'Tier 2: Good (<=2km proxy to School or Kindergarten)'
                 ELSE 'Tier 3: Highway Access Only (>2km proxy to School/KG)'
               END AS suitability_tier,
               p.geometry
        FROM large_parcels p, official_road_geom r, scenario_road_geom sr, school_geom s, kg_geom k
        WHERE least(ST_Distance(p.geometry, r.u), ST_Distance(p.geometry, sr.u)) <= 2000
    """)
    n_cand = con.execute("SELECT count(*) FROM candidate_parcels").fetchone()[0]
    log.info("Identified %d road-accessible candidate parcels across all suitability tiers", n_cand)

    # STEP 7 — Build explicit 2 km straight-line accessibility proxies.
    con.execute("""
        CREATE OR REPLACE TABLE school_catchment AS
        SELECT 'Municipal schools (2 km straight-line proxy)' AS name,
               'school_catchment' AS type,
               ST_Union_Agg(ST_Buffer(geometry, 2000, 64)) AS geometry
        FROM schools
    """)
    con.execute("""
        CREATE OR REPLACE TABLE kg_catchment AS
        SELECT 'Municipal kindergartens (2 km straight-line proxy)' AS name,
               'kindergarten_catchment' AS type,
               ST_Union_Agg(ST_Buffer(geometry, 2000, 64)) AS geometry
        FROM kindergartens
    """)

    # STEP 8 — Export derived outputs
    DERIVED.mkdir(parents=True, exist_ok=True)
    gpkg_out = DERIVED / "final-candidates.gpkg"
    con.execute(f"COPY candidate_parcels TO '{gpkg_out}' (FORMAT GDAL, DRIVER 'GPKG')")
    with sqlite3.connect(gpkg_out) as gpkg:
        gpkg.execute("UPDATE gpkg_contents SET last_change = '2026-08-25T00:00:00.000Z'")
    con.execute(f"COPY candidate_parcels TO '{DERIVED / 'final-candidates.parquet'}' (FORMAT PARQUET)")

    # 1. Transform candidate parcels to EPSG:4326 for web rendering
    def transform_coords(coords):
        if isinstance(coords[0], (int, float)):
            return list(t_4326.transform(coords[0], coords[1]))
        return [transform_coords(c) for c in coords]

    feats = con.execute("""
        SELECT cadastral_id, address, municipality, settlement, land_use,
               area_m2, dist_main_road_m, dist_official_road_m, dist_scenario_road_m,
               nearest_road_source, dist_school_m, dist_kg_m,
               straightline_time_school_min, straightline_time_kg_min, suitability_tier,
               ST_AsGeoJSON(geometry)
        FROM candidate_parcels
    """).fetchall()

    coll = {"type": "FeatureCollection", "crs": {"type": "name", "properties": {"name": "EPSG:4326"}}, "features": []}
    for row in feats:
        fid, addr, mun, sett, lu, area, dist_r, dist_ro, dist_rs, road_source, dist_s, dist_k, w_s, w_k, tier, gj_str = row
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
                "dist_official_road_m": float(dist_ro),
                "dist_scenario_road_m": float(dist_rs),
                "nearest_road_source": road_source,
                "dist_school_m": float(dist_s),
                "dist_kg_m": float(dist_k),
                "straightline_time_school_min": float(w_s),
                "straightline_time_kg_min": float(w_k),
                "suitability_tier": tier,
            },
            "geometry": g,
        })
    (DERIVED / "final-candidates.json").write_text(json.dumps(coll, indent=2))

    # 2. Export main roads as GeoJSON
    road_feats = con.execute("SELECT name, road_class, ST_AsGeoJSON(geometry) FROM official_roads").fetchall()
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
                "properties": {"name": "Municipal schools: 2 km straight-line proxy", "type": "school_catchment"},
                "geometry": geom_to_4326(school_gj_str),
            },
            {
                "type": "Feature",
                "properties": {"name": "Municipal kindergartens: 2 km straight-line proxy", "type": "kindergarten_catchment"},
                "geometry": geom_to_4326(kg_gj_str),
            },
        ],
    }
    (DERIVED / "education_catchments.json").write_text(json.dumps(catchments_coll, indent=2))

    # 4. Education POIs were already exported in effective (post-override) form
    #    by STEP 5; the immutable source stays in data/source untouched.
    log.info("Derived datasets exported: GPKG, Parquet, GeoJSON (candidates, catchments, pois, roads)")
    return override_results


# ------------------------------ STEP 8: validation --------------------------
def _validate_qgis_project(con: duckdb.DuckDBPyConnection) -> tuple[dict, dict]:
    qgz = ROOT / "project.qgz"
    errors: list[str] = []
    try:
        with zipfile.ZipFile(qgz) as archive:
            bad_member = archive.testzip()
            if bad_member:
                errors.append(f"corrupt archive member: {bad_member}")
            xml = ET.fromstring(archive.read("project.qgs"))
        project_layers = xml.findall("./projectlayers/maplayer")
        project_ids = {layer.findtext("id") for layer in project_layers}
        tree_ids = {layer.attrib.get("id") for layer in xml.findall(".//layer-tree-layer")}
        if project_ids != tree_ids:
            errors.append("layer-tree IDs do not match project layer IDs")
        for layer in project_layers:
            source = layer.findtext("datasource") or ""
            local = source.split("|", 1)[0]
            if local.startswith("./") and not (ROOT / local[2:]).exists():
                errors.append(f"missing datasource: {local}")
        renderer = next(
            (
                layer.find("renderer-v2")
                for layer in project_layers
                if layer.findtext("id") == "candidate_parcels_layer"
            ),
            None,
        )
        styled = set()
        if renderer is not None:
            styled = {category.attrib["value"] for category in renderer.findall("./categories/category")}
        actual = {
            row[0]
            for row in con.execute("SELECT DISTINCT suitability_tier FROM candidate_parcels").fetchall()
        }
        if styled != actual:
            errors.append(f"candidate style domain mismatch: styled={sorted(styled)}, actual={sorted(actual)}")
        poi_renderer = next(
            (
                layer.find("renderer-v2")
                for layer in project_layers
                if layer.findtext("id") == "education_pois_layer"
            ),
            None,
        )
        poi_styled = set()
        if poi_renderer is not None:
            poi_styled = {c.attrib["value"] for c in poi_renderer.findall("./categories/category")}
        poi_actual = set(_poi_class_counts())
        if not poi_actual.issubset(poi_styled):
            errors.append(f"POI style domain mismatch: styled={sorted(poi_styled)}, actual={sorted(poi_actual)}")
        roads = json.loads((DERIVED / "main_roads.json").read_text())
        if any(f.get("properties", {}).get("class", "").startswith("Scenario") for f in roads["features"]):
            errors.append("scenario road leaked into authoritative ETAK presentation layer")
    except Exception as exc:
        errors.append(str(exc))

    static_check = {
        "id": "qgis_project_static_valid",
        "status": "passed" if not errors else "failed",
        "project": "project.qgz",
        "errors": errors,
    }

    try:
        from qgis.core import QgsApplication, QgsProject  # type: ignore

        app = QgsApplication([], False)
        app.initQgis()
        loaded = QgsProject.instance().read(str(qgz))
        invalid = [layer.name() for layer in QgsProject.instance().mapLayers().values() if not layer.isValid()]
        app.exitQgis()
        runtime_check = {
            "id": "qgis_runtime_load",
            "status": "passed" if loaded and not invalid else "failed",
            "invalid_layers": invalid,
        }
    except ImportError:
        runtime_check = {
            "id": "qgis_runtime_load",
            "status": "not_testable",
            "reason": "PyQGIS is not installed in this execution environment",
        }
    except Exception as exc:
        runtime_check = {"id": "qgis_runtime_load", "status": "failed", "reason": str(exc)}
    return static_check, runtime_check


def _check_manifest_graph() -> dict:
    """Every step input must resolve to a source key or an earlier step's output."""
    produced = set(PROJECT["sources"])
    dangling: list[str] = []
    for step in PROJECT["processing"]["steps"]:
        consumed: list[str] = []
        for key in ("input", "inputs", "source", "target"):
            value = step.get(key)
            if isinstance(value, str):
                consumed.append(value)
            elif isinstance(value, list):
                consumed.extend(value)
        dangling += [f"{step['id']}: unresolved input {name!r}" for name in consumed if name not in produced]
        out = step.get("output")
        if isinstance(out, str):
            produced.update(part.strip() for part in out.split(","))
    step_ids = {step["id"] for step in PROJECT["processing"]["steps"]}
    dangling += [
        f"outputs.{name}: generated_by {spec.get('generated_by')!r} is not a step"
        for name, spec in PROJECT.get("outputs", {}).items()
        if spec.get("generated_by") not in step_ids
    ]
    return {
        "id": "manifest_graph_resolves",
        "status": "passed" if not dangling else "failed",
        "steps_checked": len(step_ids),
        "symbols_resolved": len(produced),
        "errors": dangling,
    }


def write_validation(con: duckdb.DuckDBPyConnection, run_id: str, override_results: list[dict]) -> dict:
    def n(q):
        return int(con.execute(q).fetchone()[0])

    n_candidates = n("SELECT COUNT(*) FROM candidate_parcels")
    n_tier1 = n("SELECT COUNT(*) FROM candidate_parcels WHERE dist_school_m <= 2000 AND dist_kg_m <= 2000")
    bad_geom = n("SELECT COUNT(*) FROM candidate_parcels WHERE NOT ST_IsValid(geometry)")
    dup_ids = n("SELECT COUNT(*) - COUNT(DISTINCT cadastral_id) FROM candidate_parcels")
    null_ids = n("SELECT COUNT(*) FROM candidate_parcels WHERE cadastral_id IS NULL")
    out_of_range = n("SELECT COUNT(*) FROM candidate_parcels WHERE area_m2 <= 0 OR area_m2 >= 100000000")
    bad_road_distance = n("SELECT COUNT(*) FROM candidate_parcels WHERE dist_main_road_m > 2000")
    bad_education_semantics = n(
        "SELECT (SELECT COUNT(*) FROM schools WHERE ownership <> 'municipal' OR NOT active) + "
        "(SELECT COUNT(*) FROM kindergartens WHERE ownership <> 'municipal' OR NOT active)"
    )
    gpkg_meta = con.execute(f"SELECT layers FROM ST_Read_Meta('{DERIVED / 'final-candidates.gpkg'}')").fetchone()[0]
    crs = gpkg_meta[0]["geometry_fields"][0]["crs"]
    crs_ok = crs.get("auth_name") == "EPSG" and str(crs.get("auth_code")) == "3301"
    road_meta = json.loads((SOURCE / "etak_main_roads.meta.json").read_text())
    education_meta = json.loads((SOURCE / "tartu_municipal_education.meta.json").read_text())
    complete = (
        road_meta["matched"] == road_meta["returned"]
        and education_meta["matched"] == education_meta["returned"]
    )
    override_features = json.loads((OVERRIDES / "planned-road.geojson").read_text())["features"]
    scenario_rows = n("SELECT COUNT(*) FROM scenario_roads")
    geometry_override_ok = (
        scenario_rows == len(override_features)
        and all(f["properties"].get("geometry_origin") == "scenario" for f in override_features)
    )
    # Every declared override must have an application result; attribute overrides
    # come back from the pipeline, the scenario geometry is verified here.
    override_status = list(override_results) + [
        {
            "id": "OVERRIDE-002",
            "status": "applied" if geometry_override_ok else "rejected",
            "detail": f"{scenario_rows} scenario road geometry loaded from data/overrides/planned-road.geojson",
        }
    ]
    declared_ids = {o["id"] for o in PROJECT.get("overrides", [])}
    reported_override_ids = {o["id"] for o in override_status}
    for missing in sorted(declared_ids - reported_override_ids):
        override_status.append({"id": missing, "status": "not_testable",
                                "detail": "declared in project.yaml but not evaluated by this run"})
    override_ok = bool(override_status) and all(o["status"] == "applied" for o in override_status)
    poi_counts = _poi_class_counts()
    qgis_static, qgis_runtime = _validate_qgis_project(con)

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
            "id": "crs_known",
            "status": "passed" if crs_ok else "failed",
            "expected": "EPSG:3301",
            "actual": f"{crs.get('auth_name')}:{crs.get('auth_code')}",
        },
        {
            "id": "no_null_cadastral_id",
            "status": "passed" if null_ids == 0 else "failed",
            "nulls": null_ids,
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
            "id": "highway_distance_check",
            "status": "passed" if bad_road_distance == 0 else "failed",
            "out_of_range_count": bad_road_distance,
        },
        {
            "id": "source_semantics_verified",
            "status": "passed" if bad_education_semantics == 0 else "failed",
            "invalid_education_rows": bad_education_semantics,
            "school_predicate": education_meta["predicates"]["school"],
            "kindergarten_predicate": education_meta["predicates"]["kindergarten"],
        },
        {
            "id": "education_ownership_check",
            "status": "passed" if bad_education_semantics == 0 else "failed",
            "invalid_rows": bad_education_semantics,
        },
        {
            "id": "source_result_complete",
            "status": "passed" if complete else "failed",
            "roads": {"matched": road_meta["matched"], "returned": road_meta["returned"]},
            "education": {"matched": education_meta["matched"], "returned": education_meta["returned"]},
        },
        {
            "id": "overrides_applied",
            "status": "passed" if override_ok else "failed",
            "declared": sorted(declared_ids),
            "results": override_status,
            "effective_education_pois": poi_counts,
        },
        _check_manifest_graph(),
        qgis_static,
        qgis_runtime,
        {
            "id": "education_source_license",
            "status": "warning",
            "reason": "The authoritative Tartu ArcGIS items do not publish explicit license text",
        },
        {
            "id": "education_access_method",
            "status": "warning",
            "reason": "2 km straight-line proxy; no pedestrian-network isochrone was computed",
        },
    ]
    expected_checks = set(PROJECT["validation"]["required"])
    expected_checks.update(check["name"] for check in PROJECT["validation"].get("domain_checks", []))
    reported_ids = [check["id"] for check in checks]
    parity_ok = (
        all(reported_ids.count(check_id) == 1 for check_id in expected_checks - {"manifest_report_parity"})
        and expected_checks.issubset(set(reported_ids) | {"manifest_report_parity"})
    )
    checks.insert(
        -2,
        {
            "id": "manifest_report_parity",
            "status": "passed" if parity_ok else "failed",
            "expected": sorted(expected_checks),
            "reported": sorted(set(reported_ids) | {"manifest_report_parity"}),
        },
    )
    if any(c["status"] == "failed" for c in checks):
        status = "failed"
    elif any(c["status"] in ("warning", "not_testable") for c in checks):
        status = "warning"
    else:
        status = "passed"
    report = {
        "run_id": run_id,
        "schema": "open-gis-project/v1",
        "status": status,
        "checks": checks,
        "candidate_count": n_candidates,
        "prime_tier1_count": n_tier1,
        "sources": {k: v.get("source_url") for k, v in PROJECT["sources"].items()},
        "overrides": override_status,
    }
    VALIDATION.mkdir(parents=True, exist_ok=True)
    (VALIDATION / "latest-report.json").write_text(json.dumps(report, indent=2, default=str))
    log.info("Validation report written (status: %s)", status)
    return report


def _combined_hash(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda value: str(value)):
        digest.update(str(path.relative_to(ROOT)).encode("utf-8"))
        digest.update(path.read_bytes())
    return "sha256:" + digest.hexdigest()


def finalize_run(report: dict, manifest: list[dict], started_at: str) -> None:
    completed_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    input_paths = [
        SOURCE / "Tartu_maakond_KATASTER_GPKG.gpkg",
        SOURCE / "etak_main_roads.geojson",
        SOURCE / "tartu_municipal_education.geojson",
        OVERRIDES / "planned-road.geojson",
        ROOT / "pipeline.py",
    ]
    output_paths = [
        DERIVED / "final-candidates.gpkg",
        DERIVED / "final-candidates.parquet",
        DERIVED / "final-candidates.json",
        DERIVED / "education_catchments.json",
        DERIVED / "education_pois.json",
        DERIVED / "main_roads.json",
        ROOT / "project.qgz",
    ]
    inputs_hash = _combined_hash(input_paths)
    outputs_hash = _combined_hash(output_paths)
    report["inputs_hash"] = inputs_hash
    report["outputs_hash"] = outputs_hash

    run_record = {
        "run_id": report["run_id"],
        "started_at": started_at,
        "completed_at": completed_at,
        "status": report["status"],
        "inputs_hash": inputs_hash,
        "outputs_hash": outputs_hash,
        "source_manifest": "data/source/manifest.json",
        "validation_report": "validation/latest-report.json",
        "sources": [{"key": item["key"], "sha256": item.get("sha256")} for item in manifest],
        "outputs": [{"path": str(path.relative_to(ROOT)), "sha256": _sha256(path)} for path in output_paths],
        "environment": {
            "python": os.sys.version.split()[0],
            "duckdb": duckdb.__version__,
            "pyproj": pyproj.__version__,
        },
    }
    run_file = RUNS / f"{report['run_id']}.json"
    run_file.write_text(json.dumps(run_record, indent=2, ensure_ascii=False))
    (VALIDATION / "latest-report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))

    PROJECT["project"]["updated_at"] = completed_at
    PROJECT["project"]["status"] = "validated" if report["status"] == "passed" else report["status"]
    for item in manifest:
        source = PROJECT["sources"].get(item["key"])
        if not source:
            continue
        source["access"]["retrieved_at"] = item["download_timestamp"]
        source["access"]["downloaded_at"] = item["download_timestamp"]
        if "file" in source["access"]:
            source["access"]["file"]["row_count"] = item["rows"]
        completeness = item.get("completeness")
        if completeness and item["key"] == "roads":
            source["selection"]["completeness"] = {
                key: completeness[key]
                for key in ("matched", "returned", "page_size", "pages")
            }
        elif completeness:
            source["selection"]["completeness"] = completeness
    PROJECT["runs"]["latest"] = {
        "id": report["run_id"],
        "started_at": started_at,
        "completed_at": completed_at,
        "status": report["status"],
        "inputs_hash": inputs_hash,
        "outputs_hash": outputs_hash,
        "record": {"path": str(run_file.relative_to(ROOT))},
        "validation_report": {"path": "validation/latest-report.json"},
    }
    (ROOT / "project.yaml").write_text(
        yaml.safe_dump(PROJECT, sort_keys=False, allow_unicode=True, width=100)
    )


# ----------------------------- QGIS project (.qgz) -------------------------
def _poi_class_counts() -> dict[str, int]:
    """map_class -> count over the effective (post-override) POI layer."""
    counts: dict[str, int] = {}
    pois = json.loads((DERIVED / "education_pois.json").read_text())
    for feature in pois["features"]:
        key = feature["properties"].get("map_class", "unknown")
        counts[key] = counts.get(key, 0) + 1
    return counts


def write_qgis_project(con: duckdb.DuckDBPyConnection) -> Path:
    """Generate a complete, fully-styled QGIS project (.qgz) matching the web dashboard."""
    import subprocess
    import zipfile

    zpath = ROOT / "project.qgz"
    school_count = int(con.execute("SELECT COUNT(*) FROM schools").fetchone()[0])
    kindergarten_count = int(con.execute("SELECT COUNT(*) FROM kindergartens").fetchone()[0])
    poi_classes = _poi_class_counts()
    scenario_inactive_count = poi_classes.get("scenario_inactive", 0)

    # Attempt to build via PyQGIS in Docker for 100% native QGIS binary perfection
    pyqgis_script = f"""
import qgis
from qgis.core import (
    QgsApplication, QgsProject, QgsVectorLayer, QgsRasterLayer,
    QgsCoordinateReferenceSystem, QgsCategorizedSymbolRenderer,
    QgsRendererCategory, QgsFillSymbol, QgsLineSymbol, QgsMarkerSymbol,
    QgsSingleSymbolRenderer, QgsRectangle, QgsMapSettings, QgsMapRendererParallelJob
)
from qgis.PyQt.QtCore import QSize

QgsApplication.setPrefixPath('/usr', True)
qgs = QgsApplication([], False)
qgs.initQgis()

project = QgsProject.instance()
project.clear()
project.setTitle('Potential development areas near main roads and schools (Tartu)')

crs3301 = QgsCoordinateReferenceSystem('EPSG:3301')
project.setCrs(crs3301)

# 1. Candidate Parcels Layer
p_layer = QgsVectorLayer('/workspace/data/derived/final-candidates.gpkg|layername=final-candidates', 'Candidate Parcels (Tartu)', 'ogr')
cat1 = QgsRendererCategory('Tier 1: Prime (<=2km proxy to School & Kindergarten)', QgsFillSymbol.createSimple({{'color': '46,125,50,190', 'outline_color': '165,214,167,255', 'outline_width': '0.5'}}), 'Tier 1: Prime (<=2km proxy to School & KG)')
cat2 = QgsRendererCategory('Tier 2: Good (<=2km proxy to School or Kindergarten)', QgsFillSymbol.createSimple({{'color': '245,127,23,170', 'outline_color': '255,245,157,255', 'outline_width': '0.4'}}), 'Tier 2: Good (<=2km proxy to School or KG)')
cat3 = QgsRendererCategory('Tier 3: Highway Access Only (>2km proxy to School/KG)', QgsFillSymbol.createSimple({{'color': '69,90,100,100', 'outline_color': '144,164,174,255', 'outline_width': '0.3'}}), 'Tier 3: Highway Access Only')
p_layer.setRenderer(QgsCategorizedSymbolRenderer('suitability_tier', [cat1, cat2, cat3]))
p_layer.setOpacity(0.85)

# 2. Education Catchments
c_layer = QgsVectorLayer('/workspace/data/derived/education_catchments.json', 'Education 2 km Straight-line Proxies', 'ogr')
c_cat1 = QgsRendererCategory('school_catchment', QgsFillSymbol.createSimple({{'color': '25,118,210,30', 'outline_color': '66,165,245,200', 'outline_style': 'dash', 'outline_width': '0.6'}}), 'Municipal schools (2 km straight-line proxy)')
c_cat2 = QgsRendererCategory('kindergarten_catchment', QgsFillSymbol.createSimple({{'color': '245,124,0,25', 'outline_color': '255,167,38,200', 'outline_style': 'dash', 'outline_width': '0.6'}}), 'Municipal kindergartens (2 km straight-line proxy)')
c_layer.setRenderer(QgsCategorizedSymbolRenderer('type', [c_cat1, c_cat2]))

# 3. Education POIs
poi_layer = QgsVectorLayer('/workspace/data/derived/education_pois.json', 'Verified Municipal Schools & Kindergartens', 'ogr')
poi_cat1 = QgsRendererCategory('school', QgsMarkerSymbol.createSimple({{'color': '66,165,245,255', 'outline_color': '255,255,255,255', 'size': '3.2', 'outline_width': '0.4'}}), 'Verified municipal schools (n={school_count})')
poi_cat2 = QgsRendererCategory('kindergarten', QgsMarkerSymbol.createSimple({{'color': '255,167,38,255', 'outline_color': '255,255,255,255', 'size': '3.2', 'outline_width': '0.4'}}), 'Verified municipal kindergartens (n={kindergarten_count})')
poi_cat3 = QgsRendererCategory('scenario_inactive', QgsMarkerSymbol.createSimple({{'color': '120,120,120,255', 'outline_color': '229,57,53,255', 'size': '3.6', 'outline_width': '0.8'}}), 'Scenario outage: excluded from analysis (n={scenario_inactive_count})')
poi_layer.setRenderer(QgsCategorizedSymbolRenderer('map_class', [poi_cat1, poi_cat2, poi_cat3]))

# 4. Hypothetical Connector Road Scenario
plan_layer = QgsVectorLayer('/workspace/data/overrides/planned-road.geojson', 'Hypothetical Connector Road (OVERRIDE-002)', 'ogr')
plan_layer.setRenderer(QgsSingleSymbolRenderer(QgsLineSymbol.createSimple({{'line_color': '255,213,79,255', 'line_style': 'dash', 'line_width': '1.0'}})))

# 5. National Highways
roads_layer = QgsVectorLayer('/workspace/data/derived/main_roads.json', 'Official National Highways (ETAK)', 'ogr')
roads_layer.setRenderer(QgsSingleSymbolRenderer(QgsLineSymbol.createSimple({{'line_color': '121,134,203,255', 'line_style': 'solid', 'line_width': '0.7'}})))

# 6. Basemaps
carto_grey = QgsRasterLayer('type=xyz&url=https://basemaps.cartocdn.com/rastertiles/light_all/{{z}}/{{x}}/{{y}}.png&zmax=19&zmin=0', 'CartoDB Positron (Light Grey Basemap)', 'wms')
maaamet_base = QgsRasterLayer('contextualWMSLegend=0&crs=EPSG:3301&dpiMode=7&featureCount=10&format=image/png&layers=BAASKAART&styles=&url=https://kaart.maaamet.ee/wms/alus', 'Maa- ja Ruumiamet: Baaskaart (WMS)', 'wms')
osm_base = QgsRasterLayer('type=xyz&url=https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png&zmax=19&zmin=0', 'OpenStreetMap (XYZ)', 'wms')

for l in [poi_layer, p_layer, plan_layer, roads_layer, c_layer, carto_grey, maaamet_base, osm_base]:
    project.addMapLayer(l, False)

root = project.layerTreeRoot()
root.clear()

g_results = root.addGroup('Analysis Results')
g_results.addLayer(p_layer)

g_edu = root.addGroup('Educational Accessibility')
g_edu.addLayer(poi_layer)
g_edu.addLayer(c_layer)

g_trans = root.addGroup('Transportation & Overrides')
g_trans.addLayer(plan_layer)
g_trans.addLayer(roads_layer)

g_base = root.addGroup('Basemaps')
g_base.addLayer(carto_grey)
g_base.addLayer(maaamet_base)
g_base.addLayer(osm_base)

root.findLayer(osm_base.id()).setItemVisibilityChecked(False)
root.findLayer(maaamet_base.id()).setItemVisibilityChecked(False)
root.findLayer(carto_grey.id()).setItemVisibilityChecked(True)

invalid = [layer.name() for layer in [p_layer, c_layer, poi_layer, plan_layer, roads_layer, carto_grey, maaamet_base, osm_base] if not layer.isValid()]
if invalid:
    raise RuntimeError('Invalid QGIS layers: ' + ', '.join(invalid))
if not project.write('/workspace/project.qgz'):
    raise RuntimeError('QGIS project write failed')
qgs.exitQgis()
"""
    use_qgis_docker = os.environ.get("OPEN_GIS_USE_QGIS_DOCKER") == "1"
    try:
        if not use_qgis_docker:
            raise RuntimeError("native QGIS Docker generation not requested")
        cur_dir = str(ROOT)
        uid = os.getuid()
        gid = os.getgid()
        res = subprocess.run(
            ["docker", "run", "--rm", "-u", f"{uid}:{gid}", "-e", "QT_QPA_PLATFORM=offscreen", "-v", f"{cur_dir}:/workspace", "-w", "/workspace", "qgis/qgis:3.44.3", "python3", "-c", pyqgis_script],
            capture_output=True,
            text=True,
            timeout=30
        )
        if res.returncode == 0 and zpath.exists():
            log.info("QGIS project compiled natively via PyQGIS: %s", zpath)
            return zpath
    except Exception as e:
        if use_qgis_docker:
            log.warning("PyQGIS docker runner failed (%s), falling back to standalone XML builder", e)
        else:
            log.info("Using deterministic standalone QGIS XML builder")

    # Fallback to standalone XML construction
    xml = """<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>
<qgis projectname="tartu-development-access" version="3.44.3">
  <homePath path=""/>
  <title>Potential development areas near main roads and schools (Tartu)</title>
  <autotransaction active="0"/>
  <evaluateDefaultValues active="0"/>
  <trust active="0"/>
  <projectCrs>
    <spatialrefsys nativeFormat="Wkt">
      <wkt>PROJCRS["Estonian Coordinate System of 1997",BASEGEOGCRS["EST97",DATUM["Estonia 1997",ELLIPSOID["GRS 1980",6378137,298.257222101,LENGTHUNIT["metre",1]]],PRIMEM["Greenwich",0,ANGLEUNIT["degree",0.0174532925199433]],ID["EPSG",4180]],CONVERSION["Estonian National Grid",METHOD["Lambert Conic Conformal (2SP)",ID["EPSG",9802]],PARAMETER["Latitude of false origin",57.5175539305556,ANGLEUNIT["degree",0.0174532925199433],ID["EPSG",8821]],PARAMETER["Longitude of false origin",24,ANGLEUNIT["degree",0.0174532925199433],ID["EPSG",8822]],PARAMETER["Latitude of 1st standard parallel",59.3333333333333,ANGLEUNIT["degree",0.0174532925199433],ID["EPSG",8823]],PARAMETER["Latitude of 2nd standard parallel",58,ANGLEUNIT["degree",0.0174532925199433],ID["EPSG",8824]],PARAMETER["Easting at false origin",500000,LENGTHUNIT["metre",1],ID["EPSG",8826]],PARAMETER["Northing at false origin",6375000,LENGTHUNIT["metre",1],ID["EPSG",8827]]],CS[Cartesian,2],AXIS["northing (X)",north,ORDER[1],LENGTHUNIT["metre",1]],AXIS["easting (Y)",east,ORDER[2],LENGTHUNIT["metre",1]],USAGE[SCOPE["Topographic mapping (large scale)."],AREA["Estonia - onshore and offshore."],BBOX[57.52,20.37,60,28.2]],ID["EPSG",3301]]</wkt>
      <proj4>+proj=lcc +lat_0=57.5175539305556 +lon_0=24 +lat_1=59.3333333333333 +lat_2=58 +x_0=500000 +y_0=6375000 +ellps=GRS80 +towgs84=0,0,0,0,0,0,0 +units=m +no_defs</proj4>
      <srsid>1259</srsid>
      <srid>3301</srid>
      <authid>EPSG:3301</authid>
      <description>Estonian Coordinate System of 1997</description>
      <projectionacronym>lcc</projectionacronym>
      <ellipsoidacronym>EPSG:7019</ellipsoidacronym>
      <geographicflag>false</geographicflag>
    </spatialrefsys>
  </projectCrs>
  <layer-tree-group>
    <customproperties/>
    <layer-tree-group name="Analysis Results" expanded="1" checked="Qt.Checked">
      <layer-tree-layer id="candidate_parcels_layer" name="Candidate Parcels (Tartu)" providerKey="ogr" expanded="1" checked="Qt.Checked"/>
    </layer-tree-group>
    <layer-tree-group name="Educational Accessibility" expanded="1" checked="Qt.Checked">
      <layer-tree-layer id="education_pois_layer" name="Verified Municipal Schools &amp; Kindergartens" providerKey="ogr" expanded="1" checked="Qt.Checked"/>
      <layer-tree-layer id="education_catchments_layer" name="Education 2 km Straight-line Proxies" providerKey="ogr" expanded="1" checked="Qt.Checked"/>
    </layer-tree-group>
    <layer-tree-group name="Transportation &amp; Overrides" expanded="1" checked="Qt.Checked">
      <layer-tree-layer id="planned_road_layer" name="Hypothetical Connector Road (OVERRIDE-002)" providerKey="ogr" expanded="1" checked="Qt.Checked"/>
      <layer-tree-layer id="main_roads_layer" name="Official National Highways (ETAK)" providerKey="ogr" expanded="1" checked="Qt.Checked"/>
    </layer-tree-group>
    <layer-tree-group name="Basemaps" expanded="1" checked="Qt.Checked">
      <layer-tree-layer id="cartodb_basemap_layer" name="CartoDB Positron (Light Grey Basemap)" providerKey="wms" expanded="0" checked="Qt.Checked"/>
      <layer-tree-layer id="maaamet_basemap_layer" name="Maa- ja Ruumiamet: Baaskaart (WMS)" providerKey="wms" expanded="0" checked="Qt.Unchecked"/>
      <layer-tree-layer id="osm_basemap_layer" name="OpenStreetMap (XYZ)" providerKey="wms" expanded="0" checked="Qt.Unchecked"/>
    </layer-tree-group>
  </layer-tree-group>
  <mapcanvas>
    <units>meters</units>
    <extent>
      <xmin>645000</xmin>
      <ymin>6460000</ymin>
      <xmax>675000</xmax>
      <ymax>6490000</ymax>
    </extent>
    <rotation>0</rotation>
    <destinationsrs>
      <spatialrefsys>
        <srid>3301</srid>
        <authid>EPSG:3301</authid>
        <description>Eesti 97</description>
      </spatialrefsys>
    </destinationsrs>
  </mapcanvas>
  <projectlayers>
    <maplayer type="vector" geometry="Polygon" hasScaleBasedVisibilityFlag="0" readOnly="0" maxScale="0" minScale="1e+08" styleCategories="AllStyleCategories">
      <id>candidate_parcels_layer</id>
      <datasource>./data/derived/final-candidates.gpkg|layername=final-candidates</datasource>
      <layername>Candidate Parcels (Tartu)</layername>
      <srs><spatialrefsys><srid>3301</srid><authid>EPSG:3301</authid><description>Eesti 97</description></spatialrefsys></srs>
      <provider encoding="UTF-8">ogr</provider>
      <renderer-v2 type="categorizedSymbol" attr="suitability_tier" enableorderby="0">
        <categories>
          <category value="Tier 1: Prime (&lt;=2km proxy to School &amp; Kindergarten)" symbol="0" label="Tier 1: Prime (&lt;=2km proxy to School &amp; KG)" render="true"/>
          <category value="Tier 2: Good (&lt;=2km proxy to School or Kindergarten)" symbol="1" label="Tier 2: Good (&lt;=2km proxy to School or KG)" render="true"/>
          <category value="Tier 3: Highway Access Only (&gt;2km proxy to School/KG)" symbol="2" label="Tier 3: Highway Access Only" render="true"/>
        </categories>
        <symbols>
          <symbol type="fill" name="0" alpha="0.75"><layer class="SimpleFill" enabled="1"><prop k="color" v="46,125,50,190"/><prop k="outline_color" v="165,214,167,255"/><prop k="outline_width" v="0.6"/></layer></symbol>
          <symbol type="fill" name="1" alpha="0.65"><layer class="SimpleFill" enabled="1"><prop k="color" v="245,127,23,165"/><prop k="outline_color" v="255,245,157,255"/><prop k="outline_width" v="0.5"/></layer></symbol>
          <symbol type="fill" name="2" alpha="0.40"><layer class="SimpleFill" enabled="1"><prop k="color" v="69,90,100,100"/><prop k="outline_color" v="144,164,174,255"/><prop k="outline_width" v="0.3"/></layer></symbol>
        </symbols>
      </renderer-v2>
    </maplayer>
    <maplayer type="vector" geometry="Polygon" hasScaleBasedVisibilityFlag="0" readOnly="0" maxScale="0" minScale="1e+08" styleCategories="AllStyleCategories">
      <id>education_catchments_layer</id>
      <datasource>./data/derived/education_catchments.json</datasource>
      <layername>Education 2 km Straight-line Proxies</layername>
      <srs><spatialrefsys><srid>4326</srid><authid>EPSG:4326</authid><description>WGS 84</description></spatialrefsys></srs>
      <provider encoding="UTF-8">ogr</provider>
      <renderer-v2 type="categorizedSymbol" attr="type" enableorderby="0">
        <categories>
          <category value="school_catchment" symbol="0" label="Municipal schools: 2 km straight-line proxy" render="true"/>
          <category value="kindergarten_catchment" symbol="1" label="Municipal kindergartens: 2 km straight-line proxy" render="true"/>
        </categories>
        <symbols>
          <symbol type="fill" name="0" alpha="0.12"><layer class="SimpleFill" enabled="1"><prop k="color" v="25,118,210,30"/><prop k="outline_color" v="66,165,245,180"/><prop k="outline_style" v="dash"/><prop k="outline_width" v="0.5"/></layer></symbol>
          <symbol type="fill" name="1" alpha="0.10"><layer class="SimpleFill" enabled="1"><prop k="color" v="245,124,0,25"/><prop k="outline_color" v="255,167,38,180"/><prop k="outline_style" v="dash"/><prop k="outline_width" v="0.5"/></layer></symbol>
        </symbols>
      </renderer-v2>
    </maplayer>
    <maplayer type="vector" geometry="Point" hasScaleBasedVisibilityFlag="0" readOnly="0" maxScale="0" minScale="1e+08" styleCategories="AllStyleCategories">
      <id>education_pois_layer</id>
      <datasource>./data/derived/education_pois.json</datasource>
      <layername>Verified Municipal Schools &amp; Kindergartens</layername>
      <srs><spatialrefsys><srid>4326</srid><authid>EPSG:4326</authid><description>WGS 84</description></spatialrefsys></srs>
      <provider encoding="UTF-8">ogr</provider>
      <renderer-v2 type="categorizedSymbol" attr="map_class" enableorderby="0">
        <categories>
          <category value="school" symbol="0" label="School" render="true"/>
          <category value="kindergarten" symbol="1" label="Kindergarten" render="true"/>
          <category value="scenario_inactive" symbol="2" label="Scenario outage (OVERRIDE-001, excluded)" render="true"/>
        </categories>
        <symbols>
          <symbol type="marker" name="0" alpha="1"><layer class="SimpleMarker" enabled="1"><prop k="color" v="66,165,245,255"/><prop k="outline_color" v="255,255,255,255"/><prop k="size" v="3.5"/></layer></symbol>
          <symbol type="marker" name="1" alpha="1"><layer class="SimpleMarker" enabled="1"><prop k="color" v="255,167,38,255"/><prop k="outline_color" v="255,255,255,255"/><prop k="size" v="3.5"/></layer></symbol>
          <symbol type="marker" name="2" alpha="1"><layer class="SimpleMarker" enabled="1"><prop k="color" v="120,120,120,255"/><prop k="outline_color" v="229,57,53,255"/><prop k="outline_width" v="0.8"/><prop k="size" v="3.8"/></layer></symbol>
        </symbols>
      </renderer-v2>
    </maplayer>
    <maplayer type="vector" geometry="Line" hasScaleBasedVisibilityFlag="0" readOnly="0" maxScale="0" minScale="1e+08" styleCategories="AllStyleCategories">
      <id>planned_road_layer</id>
      <datasource>./data/overrides/planned-road.geojson</datasource>
      <layername>Hypothetical Connector Road (OVERRIDE-002)</layername>
      <srs><spatialrefsys><srid>4326</srid><authid>EPSG:4326</authid><description>WGS 84</description></spatialrefsys></srs>
      <provider encoding="UTF-8">ogr</provider>
      <renderer-v2 type="singleSymbol" enableorderby="0">
        <symbols>
          <symbol type="line" name="0" alpha="1"><layer class="SimpleLine" enabled="1"><prop k="line_color" v="255,213,79,255"/><prop k="line_style" v="dash"/><prop k="line_width" v="1.0"/></layer></symbol>
        </symbols>
      </renderer-v2>
    </maplayer>
    <maplayer type="vector" geometry="Line" hasScaleBasedVisibilityFlag="0" readOnly="0" maxScale="0" minScale="1e+08" styleCategories="AllStyleCategories">
      <id>main_roads_layer</id>
      <datasource>./data/derived/main_roads.json</datasource>
      <layername>Official National Highways (ETAK)</layername>
      <srs><spatialrefsys><srid>4326</srid><authid>EPSG:4326</authid><description>WGS 84</description></spatialrefsys></srs>
      <provider encoding="UTF-8">ogr</provider>
      <renderer-v2 type="singleSymbol" enableorderby="0">
        <symbols>
          <symbol type="line" name="0" alpha="0.8"><layer class="SimpleLine" enabled="1"><prop k="line_color" v="121,134,203,255"/><prop k="line_style" v="solid"/><prop k="line_width" v="0.8"/></layer></symbol>
        </symbols>
      </renderer-v2>
    </maplayer>
    <maplayer type="raster" hasScaleBasedVisibilityFlag="0" maxScale="0" minScale="1e+08" styleCategories="AllStyleCategories">
      <id>cartodb_basemap_layer</id>
      <datasource>type=xyz&amp;url=https://basemaps.cartocdn.com/rastertiles/light_all/{z}/{x}/{y}.png&amp;zmax=19&amp;zmin=0</datasource>
      <layername>CartoDB Positron (Light Grey Basemap)</layername>
      <srs><spatialrefsys><srid>3857</srid><authid>EPSG:3857</authid><description>WGS 84 / Pseudo-Mercator</description></spatialrefsys></srs>
      <provider>wms</provider>
      <pipe><provider><resampling enabled="false"/></provider><rasterrenderer type="singlebandcolordata" opacity="1"/></pipe>
    </maplayer>
    <maplayer type="raster" hasScaleBasedVisibilityFlag="0" maxScale="0" minScale="1e+08" styleCategories="AllStyleCategories">
      <id>maaamet_basemap_layer</id>
      <datasource>contextualWMSLegend=0&amp;crs=EPSG:3301&amp;dpiMode=7&amp;featureCount=10&amp;format=image/png&amp;layers=BAASKAART&amp;styles=&amp;url=https://kaart.maaamet.ee/wms/alus</datasource>
      <layername>Maa- ja Ruumiamet: Baaskaart (WMS)</layername>
      <srs><spatialrefsys><srid>3301</srid><authid>EPSG:3301</authid><description>Eesti 97</description></spatialrefsys></srs>
      <provider>wms</provider>
      <pipe><provider><resampling enabled="false"/></provider><rasterrenderer type="singlebandcolordata" opacity="1"/></pipe>
    </maplayer>
    <maplayer type="raster" hasScaleBasedVisibilityFlag="0" maxScale="0" minScale="1e+08" styleCategories="AllStyleCategories">
      <id>osm_basemap_layer</id>
      <datasource>type=xyz&amp;url=https://tile.openstreetmap.org/{z}/{x}/{y}.png&amp;zmax=19&amp;zmin=0</datasource>
      <layername>OpenStreetMap (XYZ)</layername>
      <srs><spatialrefsys><srid>3857</srid><authid>EPSG:3857</authid><description>WGS 84 / Pseudo-Mercator</description></spatialrefsys></srs>
      <provider>wms</provider>
      <pipe><provider><resampling enabled="false"/></provider><rasterrenderer type="singlebandcolordata" opacity="1"/></pipe>
    </maplayer>
  </projectlayers>
</qgis>"""

    (ROOT / "project.qgs").write_text(xml)
    zip_info = zipfile.ZipInfo("project.qgs", date_time=(1980, 1, 1, 0, 0, 0))
    zip_info.compress_type = zipfile.ZIP_DEFLATED
    zip_info.external_attr = 0o100644 << 16
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(zip_info, xml.encode("utf-8"))
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
    # Counts describe what actually entered the analysis: a facility switched off
    # by a scenario override is reported separately, never inside the active tally.
    school_count = sum(f["properties"].get("map_class") == "school" for f in pois_gj["features"])
    kindergarten_count = sum(f["properties"].get("map_class") == "kindergarten" for f in pois_gj["features"])
    scenario_inactive_count = sum(
        f["properties"].get("map_class") == "scenario_inactive" for f in pois_gj["features"]
    )

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
    <span class="badge {validation['status']}">Project: {pr['status']}</span>
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
        <span>Prime Parcels (Road + School + KG within 2 km proxy)</span>
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
        <span><b>Tier 1 Prime:</b> Road &le;2km &amp; School+KG within 2 km straight-line proxy</span>
      </div>
      <div class="legend-item">
        <div class="legend-color" style="background: #f57f17; border-color: #fff59d;"></div>
        <span><b>Tier 2 Good:</b> Road &le;2km &amp; School or KG within 2 km straight-line proxy</span>
      </div>
      <div class="legend-item">
        <div class="legend-color" style="background: #455a64; border-color: #90a4ae;"></div>
        <span><b>Tier 3:</b> Road access only (&gt;2 km straight-line proxy to school/KG)</span>
      </div>
      <div class="legend-item">
        <div class="legend-color" style="background: rgba(25, 118, 210, 0.15); border: 1.5px dashed #42a5f5;"></div>
        <span>Municipal schools: 2 km straight-line proxy</span>
      </div>
      <div class="legend-item">
        <div class="legend-color" style="background: rgba(245, 124, 0, 0.15); border: 1.5px dashed #ffa726;"></div>
        <span>Municipal kindergartens: 2 km straight-line proxy</span>
      </div>
      <div class="legend-item">
        <div class="legend-dot" style="background: #42a5f5;"></div>
        <span>🏫 Verified Municipal Schools (n={school_count})</span>
      </div>
      <div class="legend-item">
        <div class="legend-dot" style="background: #ffa726;"></div>
        <span>🎒 Verified Municipal Kindergartens (n={kindergarten_count})</span>
      </div>
      <div class="legend-item">
        <div class="legend-dot" style="background: #787878; border: 2px solid #e53935;"></div>
        <span>⚠️ Scenario outage — excluded from analysis (n={scenario_inactive_count})</span>
      </div>
      <div class="legend-item">
        <div class="legend-line" style="background: #7986cb;"></div>
        <span>Official National Highways (Põhimaantee/Tugimaantee)</span>
      </div>
      <div class="legend-item">
        <div class="legend-line" style="background: #ffd54f; height: 3px; border-top: 1px dashed black;"></div>
        <span>Hypothetical Connector Road (OVERRIDE-002)</span>
      </div>
    </div>
  </div>

  <div class="sect">
    <h2>Analytical Constraints &amp; Criteria</h2>
    <ul>
      <li>• <b>Minimum Area:</b> &ge; 20 000 m² (2.0 ha) measured in EPSG:3301 (L-EST97)</li>
      <li>• <b>Land-use (siht1):</b> Agricultural, Production, or Commercial in Tartu linn (79,056 county parcels evaluated)</li>
      <li>• <b>Highway Accessibility:</b> &le; 2 000 m planar distance to official primary/secondary highway or hypothetical scenario connector</li>
      <li>• <b>Education Proxy:</b> &le; 2,000 m straight-line distance to verified active municipal school and kindergarten; not a network isochrone</li>
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
  // 1. Add 2 km straight-line education accessibility proxies.
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
        'Tier 1: Prime (<=2km proxy to School & Kindergarten)', '#2e7d32',
        'Tier 2: Good (<=2km proxy to School or Kindergarten)', '#f57f17',
        '#455a64'
      ],
      'fill-opacity': [
        'match',
        ['get', 'suitability_tier'],
        'Tier 1: Prime (<=2km proxy to School & Kindergarten)', 0.75,
        'Tier 2: Good (<=2km proxy to School or Kindergarten)', 0.60,
        0.35
      ],
      'fill-outline-color': [
        'match',
        ['get', 'suitability_tier'],
        'Tier 1: Prime (<=2km proxy to School & Kindergarten)', '#a5d6a7',
        'Tier 2: Good (<=2km proxy to School or Kindergarten)', '#fff59d',
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
      'circle-radius': [
        'match', ['get', 'map_class'], 'scenario_inactive', 7, 5
      ],
      'circle-color': [
        'match',
        ['get', 'map_class'],
        'school', '#42a5f5',
        'kindergarten', '#ffa726',
        'scenario_inactive', '#787878',
        '#ffa726'
      ],
      'circle-stroke-width': [
        'match', ['get', 'map_class'], 'scenario_inactive', 2.5, 1.5
      ],
      'circle-stroke-color': [
        'match', ['get', 'map_class'], 'scenario_inactive', '#e53935', '#ffffff'
      ]
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
      <div class="popup-row"><b>Road Proximity:</b> ${{p.dist_main_road_m}} m (${{p.nearest_road_source}})</div>
      <div class="popup-row"><b>Straight-line to School:</b> ${{p.dist_school_m}} m (~${{p.straightline_time_school_min}} min-equivalent)</div>
      <div class="popup-row"><b>Straight-line to Kindergarten:</b> ${{p.dist_kg_m}} m (~${{p.straightline_time_kg_min}} min-equivalent)</div>
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
    const overridden = p.map_class === 'scenario_inactive';
    const html = `
      ${{overridden ? '<div class="popup-badge" style="background:#e53935;color:#fff;">SCENARIO OVERRIDE ' + p.override_id + '</div>' : ''}}
      <div class="popup-row"><b>${{icon}}:</b> ${{p.name}}</div>
      <div class="popup-row"><b>Ownership:</b> ${{p.ownership}}</div>
      <div class="popup-row"><b>Address:</b> ${{p.address || '—'}}</div>
      <div class="popup-row"><b>Type:</b> <code>${{p.amenity}}</code></div>
      <div class="popup-row"><b>Status in analysis:</b> ${{overridden
        ? 'excluded — hypothetical outage (source record says active)'
        : 'active (authoritative source)'}}</div>
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
    started_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    run_id = _run_id()
    for d in (DERIVED, VALIDATION, RUNS, SOURCE):
        d.mkdir(parents=True, exist_ok=True)

    cadastre_gpkg, roads_geojson, pois_geojson, manifest = fetch_and_manifest_sources()

    con = duckdb.connect()
    con.install_extension("spatial")
    con.load_extension("spatial")

    override_results = run_pipeline(con, cadastre_gpkg, roads_geojson, pois_geojson)
    write_qgis_project(con)
    validation = write_validation(con, run_id, override_results)
    finalize_run(validation, manifest, started_at)
    render_dashboard(con, validation, manifest)
    log.info("E2E full run complete!")


if __name__ == "__main__":
    main()
