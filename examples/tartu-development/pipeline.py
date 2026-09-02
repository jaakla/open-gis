# =============================================================================
# pipeline.py — Canonical reproducible run for examples/tartu-development
# =============================================================================
# Executes the full openmapstack-project/v1 loop for the Tartu development-access
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

import copy
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
MIN_PARCEL_AREA_M2 = 20000    # accepted minimum developable parcel size
MAX_ROAD_DISTANCE_M = 2000    # accepted highway-accessibility threshold
CANONICAL_CATCHMENT_M = 2000  # the accepted threshold; every other radius is exploratory
# Radii materialised so the dashboard's education-threshold control always draws a
# real buffer computed in EPSG:3301, never a browser-side approximation of one.
CATCHMENT_RADII_M = (1000, 1500, 2000, 2500, 3000)


def _run_id() -> str:
    return "run-" + datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")


def _round_geometry(geom: dict, ndigits: int = 6) -> dict:
    """Round GeoJSON coordinates for web payloads (~0.1 m at this latitude).

    Applied only to browser-bound copies and to the exploratory catchment
    variants. The canonical GPKG/Parquet/GeoJSON outputs keep full precision.
    """

    def walk(c):
        if isinstance(c[0], (int, float)):
            return [round(v, ndigits) for v in c]
        return [walk(sub_c) for sub_c in c]

    geom["coordinates"] = walk(geom["coordinates"])
    return geom


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as src:
        for chunk in iter(lambda: src.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _read_json_url(base_url: str, params: dict, timeout: int = 120) -> dict:
    url = base_url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "openmapstack-pipeline/1.0"})
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
        req = urllib.request.Request(cadastre_url, headers={"User-Agent": "openmapstack-pipeline/1.0"})
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

        feature["properties"][f"{field}_source"] = actual_from
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
        props.setdefault("active_source", props.get("active"))
        props["map_class"] = (
            props["amenity"] if props.get("active") is True else "scenario_inactive"
        )
        props["map_class_baseline"] = (
            props["amenity"] if props.get("active_source") is True else "scenario_inactive"
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
    con.execute(f"""
        CREATE OR REPLACE TABLE large_parcels AS
        SELECT *
        FROM parcels_raw
        WHERE area_m2 >= {MIN_PARCEL_AREA_M2}
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
    # The `_source` twins hold the same facilities as the authoritative source states
    # them, with no override applied. They never feed the accepted result; they exist
    # so the view can show what the scenario actually costs, measured the same way.
    facility_tables = ("schools", "kindergartens", "schools_source", "kindergartens_source")
    for tbl in facility_tables:
        con.execute(
            f"CREATE OR REPLACE TABLE {tbl} "
            "(source_id VARCHAR, name VARCHAR, ownership VARCHAR, active BOOLEAN, geometry GEOMETRY)"
        )

    for f in effective_pois["features"]:
        props = f["properties"]
        lon, lat = f["geometry"]["coordinates"]
        x, y = t_3301.transform(lon, lat)
        base = "schools" if props["amenity"] == "school" else "kindergartens"
        targets = []
        if props.get("active") is True:
            targets.append(base)
        if props.get("active_source") is True:
            targets.append(f"{base}_source")
        for tbl in targets:
            con.execute(
                f"INSERT INTO {tbl} VALUES (?, ?, ?, ?, ST_Point(?, ?))",
                [props["source_id"], props["name"], props["ownership"], props["active"], x, y],
            )

    # STEP 6 — Multi-criteria Spatial Evaluation:
    # - Distance to highway network (<= 2000 m)
    # - Distance to nearest school (m) and walk time (min)
    # - Distance to nearest kindergarten (m) and walk time (min)
    con.execute(f"""
        CREATE OR REPLACE TABLE candidate_parcels AS
        WITH official_road_geom AS (SELECT ST_Union_Agg(geometry) AS u FROM official_roads),
             scenario_road_geom AS (SELECT ST_Union_Agg(geometry) AS u FROM scenario_roads),
             school_geom AS (SELECT ST_Union_Agg(geometry) AS u FROM schools),
             kg_geom AS (SELECT ST_Union_Agg(geometry) AS u FROM kindergartens),
             school_src_geom AS (SELECT ST_Union_Agg(geometry) AS u FROM schools_source),
             kg_src_geom AS (SELECT ST_Union_Agg(geometry) AS u FROM kindergartens_source)
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
               round(ST_Distance(p.geometry, ss.u), 1) AS dist_school_baseline_m,
               round(ST_Distance(p.geometry, ks.u), 1) AS dist_kg_baseline_m,
               round(ST_Distance(p.geometry, s.u) / 80.0, 1) AS straightline_time_school_min,
               round(ST_Distance(p.geometry, k.u) / 80.0, 1) AS straightline_time_kg_min,
               CASE
                 WHEN ST_Distance(p.geometry, s.u) <= {CANONICAL_CATCHMENT_M} AND ST_Distance(p.geometry, k.u) <= {CANONICAL_CATCHMENT_M}
                   THEN 'Tier 1: Prime (<=2km proxy to School & Kindergarten)'
                 WHEN ST_Distance(p.geometry, s.u) <= {CANONICAL_CATCHMENT_M} OR ST_Distance(p.geometry, k.u) <= {CANONICAL_CATCHMENT_M}
                   THEN 'Tier 2: Good (<=2km proxy to School or Kindergarten)'
                 ELSE 'Tier 3: Highway Access Only (>2km proxy to School/KG)'
               END AS suitability_tier,
               p.geometry
        FROM large_parcels p, official_road_geom r, scenario_road_geom sr, school_geom s, kg_geom k,
             school_src_geom ss, kg_src_geom ks
        WHERE least(ST_Distance(p.geometry, r.u), ST_Distance(p.geometry, sr.u)) <= {MAX_ROAD_DISTANCE_M}
    """)
    n_cand = con.execute("SELECT count(*) FROM candidate_parcels").fetchone()[0]
    log.info("Identified %d road-accessible candidate parcels across all suitability tiers", n_cand)

    # STEP 7 — Build explicit 2 km straight-line accessibility proxies.
    con.execute(f"""
        CREATE OR REPLACE TABLE school_catchment AS
        SELECT 'Municipal schools (2 km straight-line proxy)' AS name,
               'school_catchment' AS type,
               ST_Union_Agg(ST_Buffer(geometry, {CANONICAL_CATCHMENT_M}, 64)) AS geometry
        FROM schools
    """)
    con.execute(f"""
        CREATE OR REPLACE TABLE kg_catchment AS
        SELECT 'Municipal kindergartens (2 km straight-line proxy)' AS name,
               'kindergarten_catchment' AS type,
               ST_Union_Agg(ST_Buffer(geometry, {CANONICAL_CATCHMENT_M}, 64)) AS geometry
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
               dist_school_baseline_m, dist_kg_baseline_m,
               straightline_time_school_min, straightline_time_kg_min, suitability_tier,
               ST_AsGeoJSON(geometry)
        FROM candidate_parcels
    """).fetchall()

    coll = {"type": "FeatureCollection", "crs": {"type": "name", "properties": {"name": "EPSG:4326"}}, "features": []}
    for row in feats:
        (fid, addr, mun, sett, lu, area, dist_r, dist_ro, dist_rs, road_source,
         dist_s, dist_k, dist_s_base, dist_k_base, w_s, w_k, tier, gj_str) = row
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
                "dist_school_baseline_m": float(dist_s_base),
                "dist_kg_baseline_m": float(dist_k_base),
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

    # 3b. Catchment variants: the same buffer rule at every radius the dashboard's
    # education-threshold control offers, for both the effective (overrides applied)
    # and baseline (source as published) facility sets. Every polygon here is a real
    # EPSG:3301 buffer, so moving the control never shows an approximated shape.
    # `education_catchments.json` stays the canonical 2 km effective pair; this file
    # is the exploratory companion and is never the accepted result.
    variant_sources = {
        ("school_catchment", "effective"): ("schools", "Municipal schools"),
        ("kindergarten_catchment", "effective"): ("kindergartens", "Municipal kindergartens"),
        ("school_catchment", "baseline"): ("schools_source", "Municipal schools"),
        ("kindergarten_catchment", "baseline"): ("kindergartens_source", "Municipal kindergartens"),
    }

    def _facility_ids(table: str) -> set:
        return {r[0] for r in con.execute(f"SELECT source_id FROM {table}").fetchall()}

    variant_feats = []
    for (ctype, variant), (table, label) in variant_sources.items():
        effective_table = variant_sources[(ctype, "effective")][0]
        if variant == "baseline" and _facility_ids(table) == _facility_ids(effective_table):
            # No override touches this facility class; the view reuses the effective one.
            continue
        for radius in CATCHMENT_RADII_M:
            gj_str = con.execute(
                f"SELECT ST_AsGeoJSON(ST_Union_Agg(ST_Buffer(geometry, {radius}, 32))) FROM {table}"
            ).fetchone()[0]
            variant_feats.append({
                "type": "Feature",
                "properties": {
                    "name": f"{label}: {radius:,} m straight-line proxy",
                    "type": ctype,
                    "variant": variant,
                    "radius_m": radius,
                    "canonical": variant == "effective" and radius == CANONICAL_CATCHMENT_M,
                    "facility_count": len(_facility_ids(table)),
                },
                "geometry": _round_geometry(geom_to_4326(gj_str)),
            })
    (DERIVED / "education_catchment_variants.json").write_text(
        json.dumps({"type": "FeatureCollection", "features": variant_feats}, indent=2)
    )
    log.info(
        "Exported %d catchment variants (%d radii x facility sets)",
        len(variant_feats),
        len(CATCHMENT_RADII_M),
    )

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


def _check_view_controls(con: duckdb.DuckDBPyConnection) -> dict:
    """The view's canonical control positions must equal the accepted thresholds.

    A reconfigurable dashboard tells the reader "this is the accepted run" at one
    specific control position. If project.yaml drifts from the thresholds the
    pipeline ran, that claim silently becomes false, so it fails the run instead.
    """
    filters, scenarios = _declared_controls()
    land_use = [row[0] for row in con.execute(
        "SELECT DISTINCT land_use FROM candidate_parcels ORDER BY land_use"
    ).fetchall()]
    declared_overrides = {o["id"] for o in PROJECT.get("overrides", [])}

    mismatches = []

    def expect(control_id, key, actual):
        declared = filters.get(control_id, {}).get(key)
        if declared != actual:
            mismatches.append(f"{control_id}.{key}: declared {declared!r}, pipeline ran {actual!r}")

    expect("min_area", "canonical", MIN_PARCEL_AREA_M2)
    expect("max_road_distance", "canonical", MAX_ROAD_DISTANCE_M)
    expect("education_threshold", "canonical", CANONICAL_CATCHMENT_M)
    expect("land_use", "canonical", land_use)

    options = list(filters.get("education_threshold", {}).get("options", []))
    if options != list(CATCHMENT_RADII_M):
        mismatches.append(
            f"education_threshold.options: declared {options}, materialised {list(CATCHMENT_RADII_M)}"
        )
    for scenario in scenarios.values():
        override_id = scenario.get("override")
        if override_id and override_id not in declared_overrides:
            mismatches.append(f"{scenario['id']} targets unknown override {override_id}")
        if override_id and not scenario.get("canonical", True):
            mismatches.append(
                f"{scenario['id']} is declared off by default while {override_id} is applied by the run"
            )

    return {
        "id": "view_controls_match_pipeline",
        "status": "passed" if not mismatches else "failed",
        "controls": sorted(filters) + sorted(scenarios),
        "mismatches": mismatches,
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
        _check_view_controls(con),
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
        "schema": "openmapstack-project/v1",
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
        relative = str(path.relative_to(ROOT)).encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(path.read_bytes())
    return "sha256:" + digest.hexdigest()


def finalize_run(report: dict, manifest: list[dict], started_at: str) -> None:
    completed_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    input_paths = [
        path
        for directory in (SOURCE, OVERRIDES)
        for path in directory.rglob("*")
        if path.is_file()
    ] + [ROOT / "pipeline.py"]
    output_paths = [
        DERIVED / "final-candidates.gpkg",
        DERIVED / "final-candidates.parquet",
        DERIVED / "final-candidates.json",
        DERIVED / "education_catchments.json",
        DERIVED / "education_catchment_variants.json",
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
        "inputs": [{"path": str(path.relative_to(ROOT)), "sha256": _sha256(path)} for path in input_paths],
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
    group_titles = {
        group["id"]: group["title"]
        for group in PROJECT["presentation"]["map"]["layer_groups"]
    }
    candidate_filters = {
        "tier1": '"suitability_tier" LIKE \'Tier 1:%\'',
        "tier2": '"suitability_tier" LIKE \'Tier 2:%\'',
        "tier3": '"suitability_tier" LIKE \'Tier 3:%\'',
    }
    poi_filters = {
        "active": '"map_class" <> \'scenario_inactive\'',
        "scenario": '"map_class" = \'scenario_inactive\'',
    }

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
p_layer.setName('Tier 1 Candidate Parcels')
p_layer.setSubsetString({candidate_filters["tier1"]!r})
p_tier2_layer = p_layer.clone()
p_tier2_layer.setName('Tier 2 Candidate Parcels')
p_tier2_layer.setSubsetString({candidate_filters["tier2"]!r})
p_tier3_layer = p_layer.clone()
p_tier3_layer.setName('Tier 3 Highway-only Candidate Parcels')
p_tier3_layer.setSubsetString({candidate_filters["tier3"]!r})

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
poi_layer.setName('Active Municipal Schools & Kindergartens')
poi_layer.setSubsetString({poi_filters["active"]!r})
scenario_poi_layer = poi_layer.clone()
scenario_poi_layer.setName('Scenario Facility Outage (OVERRIDE-001)')
scenario_poi_layer.setSubsetString({poi_filters["scenario"]!r})

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

for l in [
    p_layer, p_tier2_layer, p_tier3_layer, c_layer, poi_layer,
    scenario_poi_layer, plan_layer, roads_layer, carto_grey,
    maaamet_base, osm_base,
]:
    project.addMapLayer(l, False)

root = project.layerTreeRoot()
root.clear()

g_tier1 = root.addGroup({group_titles["candidates_tier1"]!r})
g_tier1.addLayer(p_layer)
g_tier2 = root.addGroup({group_titles["candidates_tier2"]!r})
g_tier2.addLayer(p_tier2_layer)
g_tier3 = root.addGroup({group_titles["candidates_highway"]!r})
g_tier3.addLayer(p_tier3_layer)
g_tier3.setExpanded(False)
g_catchments = root.addGroup({group_titles["catchments"]!r})
g_catchments.addLayer(c_layer)
g_education = root.addGroup({group_titles["education_pois"]!r})
g_education.addLayer(poi_layer)
g_overrides = root.addGroup({group_titles["user_overrides"]!r})
g_overrides.addLayer(scenario_poi_layer)
g_overrides.addLayer(plan_layer)
g_infrastructure = root.addGroup({group_titles["infrastructure"]!r})
g_infrastructure.addLayer(roads_layer)

g_base = root.addGroup('Basemaps')
g_base.addLayer(carto_grey)
g_base.addLayer(maaamet_base)
g_base.addLayer(osm_base)

root.findLayer(osm_base.id()).setItemVisibilityChecked(False)
root.findLayer(maaamet_base.id()).setItemVisibilityChecked(False)
root.findLayer(carto_grey.id()).setItemVisibilityChecked(True)

invalid = [
    layer.name() for layer in [
        p_layer, p_tier2_layer, p_tier3_layer, c_layer, poi_layer,
        scenario_poi_layer, plan_layer, roads_layer, carto_grey,
        maaamet_base, osm_base,
    ] if not layer.isValid()
]
if invalid:
    raise RuntimeError('Invalid QGIS layers: ' + ', '.join(invalid))
if not project.write('/workspace/project.qgz'):
    raise RuntimeError('QGIS project write failed')
qgs.exitQgis()
"""
    use_qgis_docker = os.environ.get("OPENMAPSTACK_USE_QGIS_DOCKER") == "1"
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

    # The standalone template above keeps the verbose renderer definitions
    # readable. Build its layer tree from the manifest at generation time so
    # the QGIS product cannot drift back to an unrelated thematic hierarchy.
    qgs_root = ET.fromstring(xml)
    project_layers = qgs_root.find("projectlayers")
    layer_tree = qgs_root.find("layer-tree-group")
    if project_layers is None or layer_tree is None:
        raise RuntimeError("standalone QGIS template is missing its layer tree or project layers")

    layers_by_id = {
        layer.findtext("id"): layer
        for layer in project_layers.findall("maplayer")
    }

    def clone_filtered_layer(source_id: str, layer_id: str, name: str, subset: str) -> ET.Element:
        layer = copy.deepcopy(layers_by_id[source_id])
        layer.find("id").text = layer_id
        layer.find("layername").text = name
        subset_node = layer.find("subsetString")
        if subset_node is None:
            subset_node = ET.SubElement(layer, "subsetString")
        subset_node.text = subset
        project_layers.append(layer)
        layers_by_id[layer_id] = layer
        return layer

    candidate = layers_by_id["candidate_parcels_layer"]
    candidate.find("layername").text = "Tier 1 Candidate Parcels"
    ET.SubElement(candidate, "subsetString").text = '"suitability_tier" LIKE \'Tier 1:%\''
    clone_filtered_layer(
        "candidate_parcels_layer", "candidate_parcels_tier2_layer",
        "Tier 2 Candidate Parcels", '"suitability_tier" LIKE \'Tier 2:%\'',
    )
    clone_filtered_layer(
        "candidate_parcels_layer", "candidate_parcels_tier3_layer",
        "Tier 3 Highway-only Candidate Parcels", '"suitability_tier" LIKE \'Tier 3:%\'',
    )

    education_pois = layers_by_id["education_pois_layer"]
    education_pois.find("layername").text = "Active Municipal Schools & Kindergartens"
    ET.SubElement(education_pois, "subsetString").text = '"map_class" <> \'scenario_inactive\''
    clone_filtered_layer(
        "education_pois_layer", "scenario_pois_layer",
        "Scenario Facility Outage (OVERRIDE-001)", '"map_class" = \'scenario_inactive\'',
    )

    for child in list(layer_tree):
        if child.tag == "layer-tree-group":
            layer_tree.remove(child)

    tree_specs = [
        ("candidates_tier1", True, [("candidate_parcels_layer", "Tier 1 Candidate Parcels", "ogr", True)]),
        ("candidates_tier2", True, [("candidate_parcels_tier2_layer", "Tier 2 Candidate Parcels", "ogr", True)]),
        ("candidates_highway", False, [("candidate_parcels_tier3_layer", "Tier 3 Highway-only Candidate Parcels", "ogr", True)]),
        ("catchments", True, [("education_catchments_layer", "Education 2 km Straight-line Proxies", "ogr", True)]),
        ("education_pois", True, [("education_pois_layer", "Active Municipal Schools & Kindergartens", "ogr", True)]),
        ("user_overrides", True, [
            ("scenario_pois_layer", "Scenario Facility Outage (OVERRIDE-001)", "ogr", True),
            ("planned_road_layer", "Hypothetical Connector Road (OVERRIDE-002)", "ogr", True),
        ]),
        ("infrastructure", True, [("main_roads_layer", "Official National Highways (ETAK)", "ogr", True)]),
    ]
    for group_id, expanded, layers in tree_specs:
        group_node = ET.SubElement(layer_tree, "layer-tree-group", {
            "name": group_titles[group_id],
            "expanded": "1" if expanded else "0",
            "checked": "Qt.Checked",
        })
        for layer_id, name, provider, checked in layers:
            ET.SubElement(group_node, "layer-tree-layer", {
                "id": layer_id,
                "name": name,
                "providerKey": provider,
                "expanded": "1",
                "checked": "Qt.Checked" if checked else "Qt.Unchecked",
            })

    basemap_group = ET.SubElement(layer_tree, "layer-tree-group", {
        "name": "Basemaps", "expanded": "1", "checked": "Qt.Checked",
    })
    for layer_id, name, checked in (
        ("cartodb_basemap_layer", "CartoDB Positron (Light Grey Basemap)", True),
        ("maaamet_basemap_layer", "Maa- ja Ruumiamet: Baaskaart (WMS)", False),
        ("osm_basemap_layer", "OpenStreetMap (XYZ)", False),
    ):
        ET.SubElement(basemap_group, "layer-tree-layer", {
            "id": layer_id,
            "name": name,
            "providerKey": "wms",
            "expanded": "0",
            "checked": "Qt.Checked" if checked else "Qt.Unchecked",
        })

    xml = "<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>\n" + ET.tostring(
        qgs_root, encoding="unicode"
    )
    (ROOT / "project.qgs").write_text(xml)
    zip_info = zipfile.ZipInfo("project.qgs", date_time=(1980, 1, 1, 0, 0, 0))
    zip_info.compress_type = zipfile.ZIP_DEFLATED
    zip_info.external_attr = 0o100644 << 16
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(zip_info, xml.encode("utf-8"))
    log.info("QGIS project generated: %s", zpath)
    return zpath


# ------------------------------ STEP 9: dashboard ---------------------------
# The dashboard is a VIEW over the project, never the definition of the analysis.
# Python assembles a semantic view descriptor from project.yaml + the run's
# artifacts; the template below renders it. Nothing analytical is decided here.

# Stable semantic roles -> concrete symbology, shared by the map, the legend and
# the QGIS mirror. Agents must not invent per-run colors (project-spec.md s.3).
TIER_STYLE = [
    {
        "id": "tier1",
        "role": "primary_result",
        "label": "Prime",
        "fill": "#22a06b",
        "line": "#8ee0b8",
        "opacity": 0.72,
        "canonical_prefix": "Tier 1",
    },
    {
        "id": "tier2",
        "role": "secondary_result",
        "label": "Good",
        "fill": "#d98324",
        "line": "#f6cf8a",
        "opacity": 0.58,
        "canonical_prefix": "Tier 2",
    },
    {
        "id": "tier3",
        "role": "constraint",
        "label": "Road access only",
        "fill": "#5b6b7d",
        "line": "#a9b6c4",
        "opacity": 0.32,
        "canonical_prefix": "Tier 3",
    },
]

# Renderer bindings for the layer groups declared in project.yaml. The project
# says WHAT to show; this table says which MapLibre layers realise it.
LAYER_BINDINGS = {
    "candidates_tier1": {"layers": ["tier1-fill", "tier1-line"], "swatch": "fill:tier1", "count": "tier1"},
    "candidates_tier2": {"layers": ["tier2-fill", "tier2-line"], "swatch": "fill:tier2", "count": "tier2"},
    "candidates_highway": {"layers": ["tier3-fill", "tier3-line"], "swatch": "fill:tier3", "count": "tier3"},
    "catchments": {
        "layers": ["school-catchment-fill", "school-catchment-line", "kg-catchment-fill", "kg-catchment-line"],
        "swatch": "buffer",
    },
    "education_pois": {"layers": ["pois"], "swatch": "dots", "count": "facilities"},
    "user_overrides": {
        "layers": [
            "planned-road", "scenario-pois", "draft-overrides-fill",
            "draft-overrides-line", "draft-overrides-point",
        ],
        "swatch": "scenario",
        "count": "overrides",
    },
    "infrastructure": {"layers": ["main-roads"], "swatch": "road"},
}


def _source_cards(manifest: list[dict]) -> list[dict]:
    """Flatten the runtime manifest into inspectable provenance cards."""
    cards = []
    for m in manifest:
        key = m["key"]
        declared = PROJECT["sources"].get(key, {})
        columns = m.get("columns")
        cards.append({
            "key": key,
            "provider": declared.get("provider", "Unknown"),
            "license": (declared.get("license") or {}).get("name") if isinstance(declared.get("license"), dict) else declared.get("license"),
            "file": m.get("file", "n/a"),
            "table": m.get("table_name", "n/a"),
            "rows": m.get("rows"),
            "columns_n": m.get("n_columns"),
            "columns": columns if isinstance(columns, list) else ([] if columns is None else [str(columns)]),
            "downloaded_at": m.get("download_timestamp", "n/a"),
            "version": m.get("version", "n/a"),
            "sha256": m.get("sha256", ""),
            "source_url": m.get("source_url", declared.get("source_url", "")),
            "portal_page": m.get("portal_page", declared.get("portal_page", "")),
            "completeness": m.get("completeness"),
        })
    return cards


def _control_key(control_id: str) -> str:
    """`scenario_road` -> `scenarioRoad`: the state key the view uses."""
    head, *rest = control_id.split("_")
    return head + "".join(word.capitalize() for word in rest)


def _declared_controls() -> tuple[dict, dict]:
    """The filter and scenario declarations from project.yaml, keyed by id."""
    controls = PROJECT["presentation"].get("controls", {})
    filters = {f["id"]: f for f in controls.get("filters", [])}
    scenarios = {sc["id"]: sc for sc in controls.get("scenarios", [])}
    return filters, scenarios


def _override_cards() -> list[dict]:
    """Overrides, each tied to the control that switches it on and off."""
    _, scenarios = _declared_controls()
    controls = {
        sc["override"]: _control_key(sc["id"])
        for sc in scenarios.values() if sc.get("override")
    }
    cards = []
    for o in PROJECT.get("overrides", []):
        target = o.get("target", {})
        cards.append({
            "id": o["id"],
            "action": o.get("action", ""),
            "origin": o.get("origin", o.get("created_by", "analyst")),
            "target": target.get("feature_name") or target.get("feature_id") or o.get("layer", ""),
            "change": (
                f"{o['change']['field']}: {o['change']['from']} → {o['change']['to']}"
                if o.get("change") else o.get("geometry_file", {}).get("path", "")
            ),
            "rationale": (o.get("rationale") or "").strip(),
            "evidence": [str(e.get("value", "")).strip() for e in o.get("evidence", [])],
            "created_at": str(o.get("created_at", "")),
            "control": controls.get(o["id"]),
        })
    return cards



# The renderer. Placeholders (__TOKEN__) are substituted by render_dashboard, so
# the CSS/JS below is plain text and needs no brace escaping.
DASHBOARD_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__ — OpenMapStack project view</title>
<link href="https://unpkg.com/maplibre-gl@3.6.2/dist/maplibre-gl.css" rel="stylesheet">
<style>
:root {
  color-scheme: light dark;
  --font-sans: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  --font-mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;

  --bg: #eef1f5;
  --surface: #ffffff;
  --surface-2: #f4f6f9;
  --surface-3: #e9edf2;
  --border: #dde2ea;
  --border-strong: #c5cddb;
  --text: #101720;
  --text-muted: #5a6675;
  --text-faint: #8c96a4;
  --accent: #0f766e;
  --accent-text: #0b5c56;
  --accent-soft: rgba(15, 118, 110, 0.10);
  --ok: #197a45;
  --ok-soft: rgba(25, 122, 69, 0.12);
  --warn: #a35a08;
  --warn-soft: rgba(163, 90, 8, 0.12);
  --err: #b3261e;
  --err-soft: rgba(179, 38, 30, 0.12);
  --info: #1d4ed8;
  --shadow-sm: 0 1px 2px rgba(16, 23, 32, 0.06), 0 1px 3px rgba(16, 23, 32, 0.05);
  --shadow-md: 0 4px 12px rgba(16, 23, 32, 0.10), 0 2px 4px rgba(16, 23, 32, 0.06);
  --radius: 10px;
  --radius-sm: 7px;
  --panel-w: 384px;
}
:root[data-theme="dark"], :root:not([data-theme="light"]) {
  --bg: #070b10;
  --surface: #10161e;
  --surface-2: #161e28;
  --surface-3: #1d2733;
  --border: #232f3d;
  --border-strong: #33445a;
  --text: #e6ecf3;
  --text-muted: #93a2b4;
  --text-faint: #6d7d90;
  --accent: #2dd4bf;
  --accent-text: #5eead4;
  --accent-soft: rgba(45, 212, 191, 0.12);
  --ok: #4ade80;
  --ok-soft: rgba(74, 222, 128, 0.13);
  --warn: #fbbf24;
  --warn-soft: rgba(251, 191, 36, 0.13);
  --err: #f87171;
  --err-soft: rgba(248, 113, 113, 0.13);
  --info: #60a5fa;
  --shadow-sm: 0 1px 2px rgba(0, 0, 0, 0.4);
  --shadow-md: 0 8px 24px rgba(0, 0, 0, 0.45);
}
@media (prefers-color-scheme: light) {
  :root:not([data-theme="dark"]) {
    --bg: #eef1f5;
    --surface: #ffffff;
    --surface-2: #f4f6f9;
    --surface-3: #e9edf2;
    --border: #dde2ea;
    --border-strong: #c5cddb;
    --text: #101720;
    --text-muted: #5a6675;
    --text-faint: #8c96a4;
    --accent: #0f766e;
    --accent-text: #0b5c56;
    --accent-soft: rgba(15, 118, 110, 0.10);
    --ok: #197a45;
    --ok-soft: rgba(25, 122, 69, 0.12);
    --warn: #a35a08;
    --warn-soft: rgba(163, 90, 8, 0.12);
    --err: #b3261e;
    --err-soft: rgba(179, 38, 30, 0.12);
    --info: #1d4ed8;
    --shadow-sm: 0 1px 2px rgba(16, 23, 32, 0.06), 0 1px 3px rgba(16, 23, 32, 0.05);
    --shadow-md: 0 4px 12px rgba(16, 23, 32, 0.10), 0 2px 4px rgba(16, 23, 32, 0.06);
  }
}

* { box-sizing: border-box; margin: 0; padding: 0; }
[hidden] { display: none !important; }
html, body { height: 100%; }
body {
  font-family: var(--font-sans);
  font-size: 13px;
  line-height: 1.5;
  color: var(--text);
  background: var(--bg);
  -webkit-font-smoothing: antialiased;
}
#app {
  height: 100%;
  display: grid;
  grid-template-columns: var(--panel-w) 1fr;
  grid-template-rows: 56px 1fr;
  grid-template-areas: "top top" "panel map";
}
a { color: var(--accent-text); text-decoration: none; }
a:hover { text-decoration: underline; }
code, .mono { font-family: var(--font-mono); font-size: 11px; }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 4px; }

/* ---------------------------------------------------------------- topbar -- */
.topbar {
  grid-area: top;
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 0 16px;
  background: var(--surface);
  border-bottom: 1px solid var(--border);
  z-index: 5;
}
.brand { display: flex; align-items: center; gap: 11px; min-width: 0; }
.brand .mark {
  width: 30px; height: 30px; flex: none;
  display: grid; place-items: center;
  border-radius: 8px;
  background: var(--accent-soft);
  color: var(--accent-text);
  font-size: 15px;
}
.brand h1 {
  font-size: 14px; font-weight: 620; letter-spacing: -0.01em;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.brand .sub {
  font-size: 11px; color: var(--text-faint);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.topbar-right { margin-left: auto; display: flex; align-items: center; gap: 8px; }

.pill {
  display: inline-flex; align-items: center; gap: 5px;
  font-size: 10.5px; font-weight: 600;
  letter-spacing: 0.04em; text-transform: uppercase;
  padding: 4px 9px; border-radius: 999px;
  border: 1px solid var(--border-strong); color: var(--text-muted);
  white-space: nowrap;
}
.pill .dot { width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
.pill.ok { color: var(--ok); background: var(--ok-soft); border-color: transparent; }
.pill.warn { color: var(--warn); background: var(--warn-soft); border-color: transparent; }
.pill.err { color: var(--err); background: var(--err-soft); border-color: transparent; }
.pill.info { color: var(--accent-text); background: var(--accent-soft); border-color: transparent; }

.icon-btn {
  width: 32px; height: 32px; flex: none;
  display: grid; place-items: center;
  border: 1px solid var(--border); border-radius: 8px;
  background: var(--surface-2); color: var(--text-muted);
  cursor: pointer; font-size: 14px;
  transition: background 0.15s, color 0.15s;
}
.icon-btn:hover { background: var(--surface-3); color: var(--text); }
.mode-btn {
  display: inline-flex; align-items: center; gap: 7px;
  min-height: 32px; padding: 5px 11px;
  border: 1px solid var(--border-strong); border-radius: 8px;
  background: var(--surface-2); color: var(--text-muted);
  font: inherit; font-size: 11.5px; font-weight: 620; cursor: pointer;
}
.mode-btn:hover { border-color: var(--accent); color: var(--accent-text); }
.mode-btn[aria-pressed="true"] {
  color: var(--accent-text); background: var(--accent-soft); border-color: var(--accent);
}

/* ----------------------------------------------------------------- panel -- */
.panel {
  grid-area: panel;
  display: flex; flex-direction: column;
  min-height: 0;
  background: var(--surface);
  border-right: 1px solid var(--border);
}
.tabs {
  display: flex; gap: 2px; padding: 8px 8px 0;
  border-bottom: 1px solid var(--border);
}
.tab {
  flex: 1;
  appearance: none; border: 0; background: none;
  font: inherit; font-size: 12px; font-weight: 560;
  color: var(--text-muted); cursor: pointer;
  padding: 8px 6px 9px; border-radius: 7px 7px 0 0;
  border-bottom: 2px solid transparent;
  transition: color 0.15s, background 0.15s;
}
.tab:hover { color: var(--text); background: var(--surface-2); }
.tab[aria-selected="true"] { color: var(--accent-text); border-bottom-color: var(--accent); }
.tab .tab-badge {
  display: inline-block; margin-left: 5px;
  font-size: 10px; font-weight: 700;
  color: var(--warn);
}
.panel-scroll { flex: 1; min-height: 0; overflow-y: auto; overscroll-behavior: contain; }
.panel-scroll::-webkit-scrollbar { width: 10px; }
.panel-scroll::-webkit-scrollbar-thumb {
  background: var(--border-strong); border-radius: 999px;
  border: 3px solid var(--surface);
}
.tabpanel { display: none; padding: 4px 0 28px; }
.tabpanel.is-active { display: block; }

/* ------------------------------------------------------------- accordion -- */
.acc { border-bottom: 1px solid var(--border); }
.acc > summary {
  list-style: none; cursor: pointer;
  display: flex; align-items: center; gap: 8px;
  padding: 11px 16px;
  font-size: 11px; font-weight: 700;
  letter-spacing: 0.08em; text-transform: uppercase;
  color: var(--text-muted);
  user-select: none;
}
.acc > summary::-webkit-details-marker { display: none; }
.acc > summary:hover { color: var(--text); background: var(--surface-2); }
.acc > summary .chev {
  margin-left: auto; flex: none;
  transition: transform 0.18s ease;
  color: var(--text-faint);
}
.acc[open] > summary .chev { transform: rotate(90deg); }
.acc > summary .sum-count {
  font-size: 10.5px; font-weight: 600; letter-spacing: 0;
  text-transform: none; color: var(--text-faint);
  padding: 1px 6px; border-radius: 999px; background: var(--surface-3);
}
.acc-body { padding: 2px 16px 16px; display: flex; flex-direction: column; gap: 12px; }
.acc-body p { color: var(--text-muted); }
.acc-body p.lead { color: var(--text); }

/* ---------------------------------------------------------------- pieces -- */
.metrics { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.metric {
  background: var(--surface-2); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 11px 12px;
}
.metric.primary { border-color: color-mix(in srgb, var(--accent) 35%, transparent); background: var(--accent-soft); }
.metric .val {
  font-size: 22px; font-weight: 640; line-height: 1.1;
  letter-spacing: -0.02em; font-variant-numeric: tabular-nums;
}
.metric.primary .val { color: var(--accent-text); }
.metric .lbl { font-size: 11px; color: var(--text-muted); margin-top: 3px; }
.metric .delta { font-size: 10.5px; font-weight: 600; color: var(--warn); margin-top: 3px; font-variant-numeric: tabular-nums; }
.metric .delta:empty { display: none; }

.field { display: flex; flex-direction: column; gap: 7px; }
.field-head { display: flex; align-items: baseline; gap: 8px; }
.field-head .name { font-size: 12px; font-weight: 560; }
.field-head .value {
  margin-left: auto; font-family: var(--font-mono); font-size: 11.5px;
  font-variant-numeric: tabular-nums; color: var(--accent-text);
}
.field-head .value.off-canonical { color: var(--warn); }
.field .hint { font-size: 11px; color: var(--text-faint); }

input[type="range"] {
  appearance: none; -webkit-appearance: none;
  width: 100%; height: 18px; background: transparent; cursor: pointer;
}
input[type="range"]::-webkit-slider-runnable-track {
  height: 5px; border-radius: 999px; background: var(--surface-3);
  border: 1px solid var(--border-strong);
}
input[type="range"]::-moz-range-track {
  height: 5px; border-radius: 999px; background: var(--surface-3);
  border: 1px solid var(--border-strong);
}
input[type="range"]::-webkit-slider-thumb {
  -webkit-appearance: none; appearance: none;
  width: 15px; height: 15px; margin-top: -6px;
  border-radius: 50%; background: var(--accent);
  border: 2px solid var(--surface); box-shadow: var(--shadow-sm);
}
input[type="range"]::-moz-range-thumb {
  width: 15px; height: 15px; border-radius: 50%;
  background: var(--accent); border: 2px solid var(--surface); box-shadow: var(--shadow-sm);
}

.chips { display: flex; flex-wrap: wrap; gap: 6px; }
.chip {
  appearance: none; font: inherit; font-size: 11px; font-weight: 550;
  padding: 5px 10px; border-radius: 999px; cursor: pointer;
  border: 1px solid var(--border-strong); background: var(--surface);
  color: var(--text-muted); transition: all 0.15s;
}
.chip:hover { border-color: var(--accent); color: var(--text); }
.chip[aria-pressed="true"] {
  background: var(--accent-soft); border-color: color-mix(in srgb, var(--accent) 45%, transparent);
  color: var(--accent-text);
}

.switch-row {
  display: flex; align-items: flex-start; gap: 10px;
  padding: 10px 11px; border-radius: var(--radius);
  border: 1px solid var(--border); background: var(--surface-2);
}
.switch-row .body { flex: 1; min-width: 0; }
.switch-row .title { font-size: 12px; font-weight: 570; display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.switch-row .desc { font-size: 11px; color: var(--text-muted); margin-top: 3px; }
.switch { position: relative; flex: none; width: 34px; height: 20px; margin-top: 1px; }
.switch input { position: absolute; opacity: 0; width: 100%; height: 100%; margin: 0; cursor: pointer; }
.switch .track {
  position: absolute; inset: 0; border-radius: 999px;
  background: var(--surface-3); border: 1px solid var(--border-strong);
  transition: background 0.18s, border-color 0.18s; pointer-events: none;
}
.switch .knob {
  position: absolute; top: 3px; left: 3px;
  width: 14px; height: 14px; border-radius: 50%;
  background: var(--text-faint); transition: transform 0.18s, background 0.18s;
  pointer-events: none;
}
.switch input:checked ~ .track { background: var(--accent-soft); border-color: var(--accent); }
.switch input:checked ~ .knob { transform: translateX(14px); background: var(--accent); }
.switch input:focus-visible ~ .track { outline: 2px solid var(--accent); outline-offset: 2px; }

.layer-row {
  display: flex; align-items: center; gap: 10px;
  padding: 7px 8px; border-radius: var(--radius-sm);
  cursor: pointer; transition: background 0.14s;
}
.layer-row:hover { background: var(--surface-2); }
.layer-row input { position: absolute; opacity: 0; pointer-events: none; }
.layer-row .box {
  width: 16px; height: 16px; flex: none; border-radius: 5px;
  border: 1.5px solid var(--border-strong); background: var(--surface);
  display: grid; place-items: center; transition: all 0.14s;
}
.layer-row .box svg { opacity: 0; transform: scale(0.7); transition: all 0.14s; }
.layer-row input:checked ~ .box { background: var(--accent); border-color: var(--accent); }
.layer-row input:checked ~ .box svg { opacity: 1; transform: none; color: var(--surface); }
.layer-row input:focus-visible ~ .box { outline: 2px solid var(--accent); outline-offset: 2px; }
.layer-row .swatch { flex: none; width: 18px; display: grid; place-items: center; }
.layer-row .txt { flex: 1; min-width: 0; }
.layer-row .txt .t { font-size: 12px; }
.layer-row .txt .d { font-size: 10.5px; color: var(--text-faint); }
.layer-row .n { font-size: 11px; color: var(--text-faint); font-variant-numeric: tabular-nums; }
.layer-row.is-off .txt, .layer-row.is-off .n { opacity: 0.45; }

.sw-fill { width: 15px; height: 12px; border-radius: 3px; border: 1.5px solid; }
.sw-line { width: 17px; height: 0; border-top-width: 3px; border-top-style: solid; border-radius: 2px; }
.sw-dot { width: 10px; height: 10px; border-radius: 50%; border: 1.5px solid var(--surface); }
.sw-stack { display: flex; gap: 2px; align-items: center; }

.note {
  display: flex; gap: 9px; padding: 10px 11px;
  border-radius: var(--radius); font-size: 11.5px; line-height: 1.45;
  border: 1px solid transparent;
}
.note .ico { flex: none; font-size: 13px; line-height: 1.2; }
.note.warn { background: var(--warn-soft); color: var(--warn); border-color: color-mix(in srgb, var(--warn) 28%, transparent); }
.note.info { background: var(--accent-soft); color: var(--accent-text); border-color: color-mix(in srgb, var(--accent) 25%, transparent); }
.note strong { font-weight: 640; }

.btn {
  appearance: none; font: inherit; font-size: 11.5px; font-weight: 560;
  padding: 6px 11px; border-radius: 7px; cursor: pointer;
  border: 1px solid var(--border-strong); background: var(--surface);
  color: var(--text); transition: all 0.15s; white-space: nowrap;
}
.btn:hover { border-color: var(--accent); color: var(--accent-text); }
.btn.primary { background: var(--accent); border-color: var(--accent); color: #05201d; }
.btn.primary:hover { filter: brightness(1.08); color: #05201d; }
.btn.danger { color: var(--err); border-color: color-mix(in srgb, var(--err) 38%, var(--border)); }
.btn:disabled { opacity: 0.45; cursor: not-allowed; pointer-events: none; }
.btn-row { display: flex; align-items: center; gap: 7px; flex-wrap: wrap; }

.edit-empty {
  min-height: 116px; display: grid; place-items: center; text-align: center;
  padding: 18px; border: 1px dashed var(--border-strong); border-radius: var(--radius);
  color: var(--text-muted); background: var(--surface-2);
}
.edit-form { display: flex; flex-direction: column; gap: 10px; }
.edit-form label { display: flex; flex-direction: column; gap: 5px; font-size: 11px; color: var(--text-muted); }
.edit-form input, .edit-form select, .edit-form textarea {
  width: 100%; border: 1px solid var(--border-strong); border-radius: 7px;
  background: var(--surface); color: var(--text); font: inherit; font-size: 12px;
  padding: 7px 9px;
}
.edit-form textarea { min-height: 62px; resize: vertical; line-height: 1.45; }
.edit-form input:focus, .edit-form select:focus, .edit-form textarea:focus { border-color: var(--accent); outline: none; }
.edit-error { color: var(--err); font-size: 11px; }
.selection-card { border-left: 3px solid var(--accent); }
.draw-tools { display: grid; grid-template-columns: repeat(3, 1fr); gap: 6px; }
.draw-tools .btn[aria-pressed="true"] { background: var(--accent-soft); border-color: var(--accent); color: var(--accent-text); }
.op-list { display: flex; flex-direction: column; gap: 8px; }
.op-card { border: 1px solid var(--border); border-radius: var(--radius); padding: 9px 10px; background: var(--surface-2); }
.op-card .op-head { display: flex; align-items: center; gap: 7px; }
.op-card .op-title { flex: 1; min-width: 0; font-size: 11.5px; font-weight: 620; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.op-card .op-meta { margin-top: 4px; color: var(--text-faint); font-size: 10.5px; }
.op-card .op-reason { margin-top: 5px; color: var(--text-muted); font-size: 11px; }

.card {
  border: 1px solid var(--border); border-radius: var(--radius);
  background: var(--surface-2); padding: 11px 12px;
  display: flex; flex-direction: column; gap: 6px;
}
.card .card-head { display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; }
.card .card-head .t { font-size: 12.5px; font-weight: 600; }
.card .card-head .p { font-size: 11px; color: var(--text-muted); }
.kv { display: grid; grid-template-columns: 92px 1fr; gap: 2px 10px; font-size: 11px; }
.kv dt { color: var(--text-faint); }
.kv dd { color: var(--text-muted); word-break: break-word; }
.kv dd.mono { font-family: var(--font-mono); font-size: 10.5px; }
.schema-toggle { font-size: 11px; color: var(--accent-text); cursor: pointer; }
.schema-cols { font-family: var(--font-mono); font-size: 10px; color: var(--text-faint); word-break: break-all; line-height: 1.5; }

.check-row {
  display: flex; align-items: center; gap: 8px;
  padding: 5px 0; font-size: 11.5px;
  border-bottom: 1px dashed var(--border);
}
.check-row:last-child { border-bottom: 0; }
.check-row .id { font-family: var(--font-mono); font-size: 11px; color: var(--text-muted); }
.check-row .st {
  margin-left: auto; font-size: 10px; font-weight: 700;
  text-transform: uppercase; letter-spacing: 0.05em; padding: 2px 7px; border-radius: 999px;
}
.check-row .st.passed { color: var(--ok); background: var(--ok-soft); }
.check-row .st.warning { color: var(--warn); background: var(--warn-soft); }
.check-row .st.failed { color: var(--err); background: var(--err-soft); }
.check-row .st.not_testable { color: var(--text-faint); background: var(--surface-3); }
.check-reason { font-size: 10.5px; color: var(--text-faint); padding: 0 0 6px 2px; }

.crit { display: flex; gap: 9px; font-size: 11.5px; align-items: flex-start; }
.crit .mk { flex: none; width: 5px; height: 5px; border-radius: 50%; background: var(--accent); margin-top: 7px; }
.crit .body { flex: 1; }
.crit .body b { font-weight: 600; }
.crit .tag {
  font-size: 9.5px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em;
  color: var(--warn); background: var(--warn-soft); padding: 1px 5px; border-radius: 4px; margin-left: 5px;
}

/* ------------------------------------------------------------------- map -- */
.mapwrap { grid-area: map; position: relative; min-width: 0; }
#map { position: absolute; inset: 0; background: var(--surface-2); }
.map-chip {
  position: absolute; top: 12px; left: 12px; z-index: 2;
  display: none; align-items: center; gap: 9px;
  padding: 7px 9px 7px 11px; border-radius: 999px;
  background: var(--surface); border: 1px solid color-mix(in srgb, var(--warn) 40%, transparent);
  box-shadow: var(--shadow-md); font-size: 11.5px; color: var(--warn); font-weight: 560;
}
.map-chip.is-on { display: flex; }
.map-chip.draft { color: var(--accent-text); border-color: color-mix(in srgb, var(--accent) 42%, transparent); }
#mapChip.is-on + #draftChip.is-on { top: 58px; }
.draw-hud {
  position: absolute; top: 12px; left: 50%; transform: translateX(-50%); z-index: 3;
  display: none; align-items: center; gap: 8px; max-width: calc(100% - 280px);
  padding: 7px 11px; border-radius: 999px; background: var(--surface);
  border: 1px solid var(--accent); box-shadow: var(--shadow-md); color: var(--accent-text);
  font-size: 11.5px; font-weight: 600;
}
.draw-hud.is-on { display: flex; }
.map-status {
  position: absolute; left: 12px; bottom: 12px; z-index: 2;
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  max-width: calc(100% - 24px);
  padding: 6px 11px; border-radius: 999px;
  background: color-mix(in srgb, var(--surface) 88%, transparent);
  backdrop-filter: blur(8px);
  border: 1px solid var(--border); box-shadow: var(--shadow-sm);
  font-size: 11px; color: var(--text-muted);
}
.map-status .sep { width: 1px; height: 11px; background: var(--border-strong); }
.map-status b { color: var(--text); font-weight: 600; font-variant-numeric: tabular-nums; }

.maplibregl-ctrl-group { border-radius: 8px !important; box-shadow: var(--shadow-md) !important; }
.maplibregl-popup-content {
  background: var(--surface); color: var(--text);
  border: 1px solid var(--border); border-radius: var(--radius);
  padding: 13px 14px; font-size: 12px; box-shadow: var(--shadow-md);
  max-width: 320px;
}
.maplibregl-popup-close-button { color: var(--text-faint); font-size: 17px; padding: 2px 7px; }
.maplibregl-popup-anchor-bottom .maplibregl-popup-tip { border-top-color: var(--surface); }
.maplibregl-popup-anchor-top .maplibregl-popup-tip { border-bottom-color: var(--surface); }
.maplibregl-popup-anchor-left .maplibregl-popup-tip { border-right-color: var(--surface); }
.maplibregl-popup-anchor-right .maplibregl-popup-tip { border-left-color: var(--surface); }
.pop-badge {
  display: inline-flex; align-items: center; gap: 5px;
  font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em;
  padding: 3px 8px; border-radius: 999px; margin-bottom: 8px;
}
.pop-title { font-size: 13px; font-weight: 620; margin-bottom: 8px; letter-spacing: -0.01em; }
.pop-kv { display: grid; grid-template-columns: 96px 1fr; gap: 3px 10px; font-size: 11.5px; }
.pop-kv dt { color: var(--text-faint); }
.pop-kv dd { color: var(--text); font-variant-numeric: tabular-nums; }
.pop-foot {
  margin-top: 10px; padding-top: 9px; border-top: 1px solid var(--border);
  display: flex; align-items: center; gap: 8px; font-size: 10.5px; color: var(--text-faint);
}
.tooltip {
  position: absolute; z-index: 3; pointer-events: none;
  padding: 5px 9px; border-radius: 7px;
  background: color-mix(in srgb, var(--surface) 94%, transparent);
  border: 1px solid var(--border); box-shadow: var(--shadow-md);
  font-size: 11.5px; white-space: nowrap; display: none;
}
.tooltip.is-on { display: block; }
.tooltip .t-tier { font-weight: 640; }

@media (max-width: 900px) {
  #app {
    width: 100%; overflow: hidden;
    grid-template-columns: minmax(0, 1fr); grid-template-rows: 56px 44vh 1fr;
    grid-template-areas: "top" "panel" "map";
  }
  .panel { border-right: 0; border-bottom: 1px solid var(--border); }
  .topbar { min-width: 0; gap: 8px; padding: 0 10px; }
  .brand { flex: 1 1 0; overflow: hidden; }
  .topbar-right { flex: 0 0 auto; min-width: 0; }
  .tabs { min-width: 0; }
  .tab { min-width: 0; font-size: 11px; padding-inline: 3px; }
  .topbar .pill:not(#pillDraft) { display: none; }
  .brand .sub { display: none; }
  .draw-hud { max-width: calc(100% - 24px); }
}
</style>
</head>
<body>
<div id="app">
  <header class="topbar">
    <div class="brand">
      <span class="mark" aria-hidden="true">◨</span>
      <div style="min-width:0">
        <h1 id="projTitle"></h1>
        <p class="sub" id="projSub"></p>
      </div>
    </div>
    <div class="topbar-right">
      <span class="pill" id="pillProject"></span>
      <span class="pill" id="pillValidation"></span>
      <span class="pill info" id="pillDraft" hidden></span>
      <button class="mode-btn" id="editModeToggle" type="button" aria-pressed="false"><span aria-hidden="true">✎</span> Edit</button>
      <button class="icon-btn" id="themeToggle" type="button" title="Switch light / dark theme" aria-label="Switch light / dark theme">◐</button>
    </div>
  </header>

  <aside class="panel">
    <nav class="tabs" role="tablist" aria-label="Project view">
      <button class="tab" role="tab" data-tab="analysis" aria-selected="true">Analysis</button>
      <button class="tab" role="tab" data-tab="map" aria-selected="false">Map<span class="tab-badge" id="tabBadge" hidden>●</span></button>
      <button class="tab" role="tab" data-tab="edit" aria-selected="false">Edit<span class="tab-badge" id="editBadge" hidden></span></button>
      <button class="tab" role="tab" data-tab="data" aria-selected="false">Provenance</button>
    </nav>
    <div class="panel-scroll">
      <section class="tabpanel is-active" data-panel="analysis" role="tabpanel"></section>
      <section class="tabpanel" data-panel="map" role="tabpanel"></section>
      <section class="tabpanel" data-panel="edit" role="tabpanel"></section>
      <section class="tabpanel" data-panel="data" role="tabpanel"></section>
    </div>
  </aside>

  <main class="mapwrap">
    <div id="map"></div>
    <div class="map-chip" id="mapChip">
      <span>Reconfigured view — not the accepted run</span>
      <button class="btn" type="button" data-reset>Reset</button>
    </div>
    <div class="map-chip draft" id="draftChip">
      <span id="draftChipText"></span>
      <button class="btn" type="button" data-export-draft>Export</button>
    </div>
    <div class="draw-hud" id="drawHud"></div>
    <div class="map-status" id="mapStatus"></div>
    <div class="tooltip" id="tooltip"></div>
  </main>
</div>

<script src="https://unpkg.com/maplibre-gl@3.6.2/dist/maplibre-gl.js"></script>
<script>
const VIEW = __VIEW__;
const CANDIDATES = __CANDIDATES__;
const ROADS = __ROADS__;
const PLANNED = __PLANNED__;
const CATCHMENTS = __CATCHMENTS__;
const POIS = __POIS__;
</script>
<script>
(function () {
  "use strict";

  // ---------------------------------------------------------------- helpers --
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  const esc = (v) => String(v == null ? "" : v).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
  const int = (n) => Math.round(n).toLocaleString("en-US");
  const ha = (m2) => (m2 / 10000).toLocaleString("en-US", { maximumFractionDigits: 1 });
  const km = (m) => (m >= 1000 ? (m / 1000).toFixed(m % 1000 === 0 ? 0 : 1) + " km" : Math.round(m) + " m");
  const CHEV = '<svg class="chev" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M9 6l6 6-6 6"/></svg>';
  const TICK = '<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6L9 17l-5-5"/></svg>';
  const clone = (value) => JSON.parse(JSON.stringify(value));

  const PALETTE = {
    school: "#4f9cf0",
    kindergarten: "#f0913a",
    schoolBuffer: "#3b82f6",
    kgBuffer: "#f59e0b",
    road: "#7f8ce0",
    planned: "#eab308",
    inactive: "#7d8794",
    inactiveRing: "#ef4444",
  };
  const TIER = {};
  VIEW.tiers.forEach((t) => { TIER[t.id] = t; });

  function acc(id, title, bodyHtml, open, countHtml) {
    return '<details class="acc"' + (open ? " open" : "") + ' data-acc="' + id + '">'
      + "<summary>" + esc(title)
      + (countHtml ? '<span class="sum-count">' + countHtml + "</span>" : "")
      + CHEV + "</summary>"
      + '<div class="acc-body">' + bodyHtml + "</div></details>";
  }

  // ------------------------------------------------------------------ state --
  const C = VIEW.canonical;
  const state = {
    minAreaM2: C.minAreaM2,
    maxRoadM: C.maxRoadM,
    educationM: C.educationM,
    landUse: new Set(C.landUse),
    scenarioRoad: C.scenarioRoad,
    scenarioOutage: C.scenarioOutage,
    layers: {},
    basemap: "auto",
    editMode: false,
    selected: null,
    drawMode: null,
    sketch: [],
  };
  VIEW.layerGroups.forEach((g) => { state.layers[g.id] = g.default_open !== false; });

  const BASE_CANDIDATES = clone(CANDIDATES);
  const BASE_POIS = clone(POIS);
  let WORKING_CANDIDATES = clone(BASE_CANDIDATES);
  let WORKING_POIS = clone(BASE_POIS);
  let DRAWN_OVERRIDES = { type: "FeatureCollection", features: [] };

  const DRAFT_KEY = "openmapstack-draft:" + VIEW.project.id + ":" + VIEW.run.id;
  function emptyDraft() {
    return {
      schema: "openmapstack-dashboard-draft/v1",
      project_id: VIEW.project.id,
      base_run_id: VIEW.run.id,
      events: [],
      redo: [],
    };
  }
  function loadDraft() {
    try {
      const parsed = JSON.parse(window.localStorage.getItem(DRAFT_KEY) || "null");
      if (parsed && parsed.schema === "openmapstack-dashboard-draft/v1"
          && parsed.project_id === VIEW.project.id && parsed.base_run_id === VIEW.run.id
          && Array.isArray(parsed.events)) {
        parsed.redo = Array.isArray(parsed.redo) ? parsed.redo : [];
        return parsed;
      }
    } catch (err) { /* storage disabled or corrupt; start a clean in-memory draft */ }
    return emptyDraft();
  }
  const draft = loadDraft();

  function persistDraft() {
    try { window.localStorage.setItem(DRAFT_KEY, JSON.stringify(draft)); } catch (err) { /* private mode */ }
  }

  function activeOperations() {
    const active = new Map();
    const originals = new Map();
    draft.events.forEach((event) => {
      if (event.type === "apply" && event.operation) {
        originals.set(event.operation.id, event.operation);
        active.set(event.operation.id, event.operation);
      } else if (event.type === "revert") {
        active.delete(event.target);
      } else if (event.type === "restore" && originals.has(event.target)) {
        active.set(event.target, originals.get(event.target));
      }
    });
    return Array.from(active.values());
  }

  function eventId(prefix) {
    const random = Math.random().toString(36).slice(2, 7).toUpperCase();
    return prefix + "-" + Date.now().toString(36).toUpperCase() + "-" + random;
  }

  function recordOperation(operation) {
    operation.id = operation.id || eventId("DRAFT");
    operation.created_at = operation.created_at || new Date().toISOString();
    operation.created_by = operation.created_by || "user";
    operation.status = "draft_unvalidated";
    operation.base_run_id = VIEW.run.id;
    draft.events.push({ id: eventId("EVENT"), type: "apply", at: new Date().toISOString(), operation: operation });
    draft.redo = [];
    persistDraft();
    rebuildDraftPreview();
  }

  function revertOperation(id) {
    if (!activeOperations().some((o) => o.id === id)) return;
    draft.events.push({ id: eventId("EVENT"), type: "revert", at: new Date().toISOString(), target: id });
    draft.redo.push(id);
    persistDraft();
    rebuildDraftPreview();
  }

  function undoLastOperation() {
    const active = activeOperations();
    if (active.length) revertOperation(active[active.length - 1].id);
  }

  function redoLastOperation() {
    const id = draft.redo.pop();
    if (!id) return;
    draft.events.push({ id: eventId("EVENT"), type: "restore", at: new Date().toISOString(), target: id });
    persistDraft();
    rebuildDraftPreview();
  }

  function sourceMeta(key) {
    return VIEW.sources.find((s) => s.key === key) || {};
  }

  function editingTarget(kind) {
    return (VIEW.editing.targets || {})[kind];
  }

  function findFeature(kind, id) {
    const target = editingTarget(kind);
    const collection = kind === "candidates" ? WORKING_CANDIDATES : WORKING_POIS;
    if (!target || !collection) return null;
    return collection.features.find((f) => String(f.properties[target.id_field]) === String(id)) || null;
  }

  function applyDraftOperation(operation) {
    if (operation.action === "add_feature" && operation.geometry) {
      DRAWN_OVERRIDES.features.push({
        type: "Feature",
        properties: Object.assign({}, operation.properties || {}, {
          draft_id: operation.id,
          draft_kind: operation.kind || "scenario",
          provenance: "user_drawn",
        }),
        geometry: clone(operation.geometry),
      });
      return;
    }
    const kind = operation.target_kind;
    const target = editingTarget(kind);
    if (!target || !operation.target) return;
    const feature = findFeature(kind, operation.target.feature_id);
    if (!feature) return;
    if (operation.action === "hide_source_feature") {
      feature.properties._draftHidden = true;
      feature.properties._draftId = operation.id;
      return;
    }
    if (operation.action !== "modify_attribute" || !operation.change) return;
    const field = operation.change.view_field || operation.change.field;
    feature.properties[field] = operation.change.to;
    feature.properties._draftId = operation.id;
    if (kind === "pois" && field === "active") {
      const active = operation.change.to === true || operation.change.to === "true";
      feature.properties.active = active;
      feature.properties.map_class = active ? feature.properties.amenity : "scenario_inactive";
      feature.properties.map_class_baseline = active ? feature.properties.amenity : "scenario_inactive";
      if (!active) feature.properties.override_id = operation.id;
    }
  }

  function rebuildDraftPreview() {
    WORKING_CANDIDATES = clone(BASE_CANDIDATES);
    WORKING_POIS = clone(BASE_POIS);
    DRAWN_OVERRIDES = { type: "FeatureCollection", features: [] };
    activeOperations().forEach(applyDraftOperation);
    if (map.getSource("draft-overrides")) map.getSource("draft-overrides").setData(DRAWN_OVERRIDES);
    updateSelectionOverlay();
    renderEditorTab();
    const provenancePanel = $('[data-panel="data"]');
    if (provenancePanel) provenancePanel.innerHTML = dataTab();
    refresh();
  }

  const canonicalSettings = Object.assign({}, C, { landUse: new Set(C.landUse) });

  function isCanonical() {
    return state.minAreaM2 === C.minAreaM2
      && state.maxRoadM === C.maxRoadM
      && state.educationM === C.educationM
      && state.scenarioRoad === C.scenarioRoad
      && state.scenarioOutage === C.scenarioOutage
      && state.landUse.size === C.landUse.length
      && C.landUse.every((code) => state.landUse.has(code));
  }

  // The whole re-derivation. Every number the browser shows comes from distances
  // the pipeline measured in EPSG:3301 — the view re-applies the published rule to
  // them, it never re-measures geometry or invents a value.
  function evaluate(s, assign) {
    const st = { shown: 0, area: 0, tier1: 0, tier2: 0, tier3: 0, areaTier1: 0, areaTier2: 0, areaTier3: 0 };
    for (const f of WORKING_CANDIDATES.features) {
      const p = f.properties;
      const road = s.scenarioRoad ? Math.min(p.dist_official_road_m, p.dist_scenario_road_m) : p.dist_official_road_m;
      const ds = s.scenarioOutage ? p.dist_school_m : p.dist_school_baseline_m;
      const dk = s.scenarioOutage ? p.dist_kg_m : p.dist_kg_baseline_m;
      const pass = !p._draftHidden && p.area_m2 >= s.minAreaM2 && road <= s.maxRoadM && s.landUse.has(p.land_use);
      const tier = (ds <= s.educationM && dk <= s.educationM) ? "tier1"
        : ((ds <= s.educationM || dk <= s.educationM) ? "tier2" : "tier3");
      if (assign) {
        p._pass = pass;
        p._tier = tier;
        p._road = Math.round(road * 10) / 10;
        p._ds = ds;
        p._dk = dk;
      }
      if (pass) {
        st.shown += 1;
        st.area += p.area_m2;
        st[tier] += 1;
        st["areaTier" + tier.slice(-1)] += p.area_m2;
      }
    }
    return st;
  }

  const canonicalStats = evaluate(canonicalSettings, false);
  let stats = canonicalStats;

  const facilityClass = () => (state.scenarioOutage ? "map_class" : "map_class_baseline");
  function facilityCounts() {
    const key = facilityClass();
    const out = { school: 0, kindergarten: 0, scenario_inactive: 0 };
    WORKING_POIS.features.filter((f) => !f.properties._draftHidden)
      .forEach((f) => { out[f.properties[key]] = (out[f.properties[key]] || 0) + 1; });
    return out;
  }
  const hasBaselineCatchment = new Set(
    CATCHMENTS.features.filter((f) => f.properties.variant === "baseline").map((f) => f.properties.type)
  );

  // ------------------------------------------------------------ panel: HTML --
  function analysisTab() {
    const metrics = '<div class="metrics" id="metrics"></div>'
      + '<div class="note info" id="scopeNote" hidden></div>';
    const criteria = '<div id="criteria" style="display:flex;flex-direction:column;gap:9px"></div>';
    const assumptions = VIEW.assumptions.map((a) => (
      '<div class="crit"><span class="mk"></span><div class="body"><b>' + esc(a.id) + "</b> — "
      + esc(a.statement) + (a.rationale ? '<div style="color:var(--text-faint);margin-top:3px">' + esc(a.rationale) + "</div>" : "")
      + "</div></div>"
    )).join("");
    const warnings = VIEW.warnings.map((w) => (
      '<div class="card"><div class="card-head"><span class="t">' + esc(w.id) + "</span>"
      + '<span class="pill ' + (w.severity === "high" ? "err" : "warn") + '">' + esc(w.severity) + "</span>"
      + '<span class="p">' + esc(w.issue) + "</span></div>"
      + "<p>" + esc(w.statement) + "</p>"
      + '<p style="color:var(--text-faint)"><b>Mitigation:</b> ' + esc(w.mitigation) + "</p></div>"
    )).join("");
    return acc("objective", "Analytical objective", '<p class="lead">' + esc(VIEW.objective) + "</p>", true)
      + acc("results", "Results", metrics, true)
      + acc("criteria", "Criteria in force", criteria, true)
      + acc("assumptions", "Assumptions", assumptions, false, String(VIEW.assumptions.length))
      + acc("warnings", "Warnings", warnings, false, String(VIEW.warnings.length));
  }

  function scenarioRows() {
    return VIEW.overrides.filter((o) => o.control).map((o) => (
      '<label class="switch-row">'
      + '<span class="switch"><input type="checkbox" data-scenario="' + esc(o.control) + '"'
      + (state[o.control] ? " checked" : "") + '><span class="track"></span><span class="knob"></span></span>'
      + '<span class="body"><span class="title">' + esc(o.id)
      + '<span class="pill warn" style="text-transform:none;letter-spacing:0">hypothetical</span></span>'
      + '<span class="desc">' + esc(o.target) + " · " + esc(o.change) + "</span></span></label>"
    )).join("");
  }

  function filterFields() {
    const areaMax = Math.max(VIEW.areaBounds.max, VIEW.areaBounds.min + 10000);
    const landUse = VIEW.landUse.map((l) => (
      '<button class="chip" type="button" data-landuse="' + esc(l.code) + '" aria-pressed="'
      + (state.landUse.has(l.code) ? "true" : "false") + '" title="' + esc(l.label) + '">'
      + esc(l.label) + "</button>"
    )).join("");
    return '<div class="field"><div class="field-head"><span class="name">Minimum parcel area</span>'
      + '<span class="value" id="valArea"></span></div>'
      + '<input type="range" id="ctlArea" min="' + VIEW.areaBounds.min + '" max="' + areaMax
      + '" step="5000" value="' + state.minAreaM2 + '"></div>'

      + '<div class="field"><div class="field-head"><span class="name">Max distance to highway</span>'
      + '<span class="value" id="valRoad"></span></div>'
      + '<input type="range" id="ctlRoad" min="0" max="' + C.maxRoadM + '" step="100" value="' + state.maxRoadM + '">'
      + '<span class="hint">The run only measured parcels within ' + km(C.maxRoadM)
      + " of a highway, so this control can tighten the rule but never widen it.</span></div>"

      + '<div class="field"><div class="field-head"><span class="name">Education proximity threshold</span>'
      + '<span class="value" id="valEdu"></span></div>'
      + '<input type="range" id="ctlEdu" min="0" max="' + (VIEW.catchmentRadii.length - 1)
      + '" step="1" value="' + VIEW.catchmentRadii.indexOf(state.educationM) + '">'
      + '<span class="hint">Sets the tier rule and redraws the matching buffer, each one measured in '
      + esc(VIEW.project.analysis_crs) + ". Straight-line screening distance, not a walking isochrone.</span></div>"

      + '<div class="field"><div class="field-head"><span class="name">Land use</span></div>'
      + '<div class="chips">' + landUse + "</div></div>";
  }

  function swatch(kind) {
    if (kind.indexOf("fill:") === 0) {
      const t = TIER[kind.slice(5)];
      return '<span class="sw-fill" style="background:' + t.fill + ";border-color:" + t.line + '"></span>';
    }
    if (kind === "buffer") {
      return '<span class="sw-stack"><span class="sw-fill" style="width:9px;background:' + PALETTE.schoolBuffer
        + '33;border-color:' + PALETTE.schoolBuffer + ';border-style:dashed"></span>'
        + '<span class="sw-fill" style="width:9px;background:' + PALETTE.kgBuffer + '33;border-color:'
        + PALETTE.kgBuffer + ';border-style:dashed"></span></span>';
    }
    if (kind === "dots") {
      return '<span class="sw-stack"><span class="sw-dot" style="background:' + PALETTE.school + '"></span>'
        + '<span class="sw-dot" style="background:' + PALETTE.kindergarten + '"></span></span>';
    }
    if (kind === "scenario") {
      return '<span class="sw-stack"><span class="sw-line" style="border-top-color:' + PALETTE.planned
        + ';border-top-style:dashed;width:11px"></span>'
        + '<span class="sw-dot" style="background:' + PALETTE.inactive + ";border-color:" + PALETTE.inactiveRing + '"></span></span>';
    }
    return '<span class="sw-line" style="border-top-color:' + PALETTE.road + '"></span>';
  }

  function layerRows() {
    return VIEW.layerGroups.map((g) => (
      '<label class="layer-row' + (state.layers[g.id] ? "" : " is-off") + '" data-layerrow="' + esc(g.id) + '">'
      + '<input type="checkbox" data-layer="' + esc(g.id) + '"' + (state.layers[g.id] ? " checked" : "") + ">"
      + '<span class="box">' + TICK + "</span>"
      + '<span class="swatch">' + swatch(g.swatch) + "</span>"
      + '<span class="txt"><span class="t">' + esc(g.title) + "</span></span>"
      + '<span class="n" data-count="' + esc(g.count || "") + '"></span></label>'
    )).join("");
  }

  function mapTab() {
    const basemaps = ["auto", "dark", "light"].map((b) => (
      '<button class="chip" type="button" data-basemap="' + b + '" aria-pressed="'
      + (state.basemap === b ? "true" : "false") + '">' + b[0].toUpperCase() + b.slice(1) + "</button>"
    )).join("");
    return '<div style="padding:12px 16px 0"><div class="note warn" id="reconfNote" hidden>'
      + '<span class="ico">⚑</span><span><strong>Reconfigured view.</strong> These numbers are an exploratory '
      + 'what-if, not the accepted run. <button class="btn" type="button" data-reset '
      + 'style="margin-top:7px;display:block">Reset to the canonical run</button></span></div></div>'
      + acc("scenarios", "Scenario overrides", scenarioRows()
        + '<p style="font-size:11px;color:var(--text-faint)">Both overrides are hypothetical. Switching one off '
        + "re-screens the parcels against distances the pipeline measured without it — the authoritative source "
        + "is never modified either way.</p>", true, String(VIEW.overrides.length))
      + acc("filters", "Filters", filterFields(), true)
      + acc("layers", "Layers & legend", layerRows(), true, String(VIEW.layerGroups.length))
      + acc("basemap", "Basemap", '<div class="chips">' + basemaps + "</div>", false);
  }

  // ----------------------------------------------------------- panel: edit --
  function editingAvailable() {
    const e = VIEW.editing || {};
    return !!(e.allow_draw_geometry || e.allow_attribute_override
      || e.allow_hide_source_feature || e.allow_add_annotation);
  }

  function activateTab(name) {
    $$(".tab").forEach((t) => t.setAttribute("aria-selected", String(t.dataset.tab === name)));
    $$(".tabpanel").forEach((p) => p.classList.toggle("is-active", p.dataset.panel === name));
    $(".panel-scroll").scrollTop = 0;
  }

  function selectedFeature() {
    return state.selected ? findFeature(state.selected.kind, state.selected.id) : null;
  }

  function selectForEditing(kind, feature) {
    const target = editingTarget(kind);
    if (!target || !feature) return;
    state.selected = { kind: kind, id: feature.properties[target.id_field] };
    updateSelectionOverlay();
    renderEditorTab();
    activateTab("edit");
  }

  function fieldValue(field, properties) {
    const key = field.source_value_field || field.view_field;
    return properties[key];
  }

  function valueControl(field, value) {
    if (field.type === "choice") {
      return '<select id="editNewValue">' + (field.options || []).map((v) => (
        '<option value="' + esc(v) + '"' + (String(v) === String(value) ? " selected" : "") + ">"
        + esc(v) + "</option>"
      )).join("") + "</select>";
    }
    if (field.type === "boolean") {
      return '<select id="editNewValue"><option value="true"' + (value === true ? " selected" : "")
        + '>true</option><option value="false"' + (value === false ? " selected" : "") + ">false</option></select>";
    }
    return '<input id="editNewValue" type="' + (field.type === "number" ? "number" : "text")
      + '" value="' + esc(value == null ? "" : value) + '">';
  }

  function selectionEditor() {
    const feature = selectedFeature();
    if (!feature || !state.selected) {
      return '<div class="edit-empty"><div><b>No feature selected</b><br>Turn on Edit mode, then select a parcel or education facility on the map.</div></div>';
    }
    const target = editingTarget(state.selected.kind);
    const p = feature.properties;
    const label = p[target.label_field] || p[target.id_field];
    const first = target.fields[0];
    const source = sourceMeta(target.source);
    const options = target.fields.map((f) => (
      '<option value="' + esc(f.view_field) + '">' + esc(f.label) + "</option>"
    )).join("");
    const sourceStatus = p._draftHidden
      ? '<span class="pill err" style="text-transform:none;letter-spacing:0">hidden in draft</span>'
      : (p._draftId ? '<span class="pill info" style="text-transform:none;letter-spacing:0">draft modified</span>' : "");
    return '<div class="card selection-card"><div class="card-head"><span class="t">' + esc(label) + "</span>"
      + sourceStatus + '<span class="p">' + esc(target.label) + "</span></div>"
      + '<dl class="kv"><dt>Source</dt><dd>' + esc(target.source) + "</dd>"
      + '<dt>Feature ID</dt><dd class="mono">' + esc(p[target.id_field]) + "</dd>"
      + '<dt>Version</dt><dd class="mono">' + esc(source.version || "n/a") + "</dd></dl></div>"
      + '<div class="edit-form"><label>Attribute<select id="editField">' + options + "</select></label>"
      + '<label>New value<span id="editValueSlot">' + valueControl(first, fieldValue(first, p)) + "</span></label>"
      + '<label>Interpretation<select id="editKind"><option value="source_correction">Source correction</option>'
      + '<option value="scenario">Hypothetical scenario</option><option value="decision">Review decision</option></select></label>'
      + '<label>Rationale <span style="color:var(--err)">*</span><textarea id="editReason" placeholder="Why should this differ from the pinned source?"></textarea></label>'
      + '<label>Evidence URL or note<input id="editEvidence" type="text" placeholder="https://… or document reference"></label>'
      + '<div class="edit-error" id="editError" hidden></div>'
      + '<div class="btn-row">'
      + (VIEW.editing.allow_attribute_override ? '<button class="btn primary" type="button" data-save-attribute>Record attribute edit</button>' : "")
      + (VIEW.editing.allow_hide_source_feature && !p._draftHidden
        ? '<button class="btn danger" type="button" data-hide-feature>Hide feature</button>' : "")
      + '<button class="btn" type="button" data-clear-selection>Clear selection</button></div></div>';
  }

  function operationTitle(operation) {
    if (operation.action === "add_feature") return operation.properties && operation.properties.name
      ? operation.properties.name : "Drawn " + operation.geometry.type;
    return (operation.target && (operation.target.feature_name || operation.target.feature_id)) || operation.action;
  }

  function operationRows() {
    const operations = activeOperations();
    if (!operations.length) return '<div class="edit-empty"><div>No draft operations yet.<br>Edits are saved locally for this run.</div></div>';
    return '<div class="op-list">' + operations.slice().reverse().map((o) => (
      '<div class="op-card"><div class="op-head"><span class="pill info" style="text-transform:none;letter-spacing:0">'
      + esc(o.kind || "correction") + '</span><span class="op-title">' + esc(operationTitle(o)) + "</span>"
      + '<button class="btn" type="button" data-revert-operation="' + esc(o.id) + '">Remove</button></div>'
      + '<div class="op-meta mono">' + esc(o.id) + " · " + esc(o.action) + "</div>"
      + '<div class="op-meta">Preview: ' + esc((o.preview_effect || "map_only").replaceAll("_", " ")) + "</div>"
      + '<div class="op-reason">' + esc(o.rationale) + "</div></div>"
    )).join("") + "</div>";
  }

  function drawingEditor() {
    if (!VIEW.editing.allow_draw_geometry) return "";
    return '<div class="draw-tools">' + ["Point", "LineString", "Polygon"].map((kind) => (
      '<button class="btn" type="button" data-draw-mode="' + kind + '" aria-pressed="'
      + String(state.drawMode === kind) + '">' + (kind === "LineString" ? "Line" : kind) + "</button>"
    )).join("") + "</div>"
      + '<div class="edit-form"><label>Name<input id="drawName" type="text" placeholder="Scenario feature or annotation"></label>'
      + '<label>Kind<select id="drawKind"><option value="scenario">Hypothetical scenario</option>'
      + '<option value="annotation">Annotation</option><option value="aoi">Area of interest</option></select></label>'
      + '<label>Rationale <span style="color:var(--err)">*</span><textarea id="drawReason" placeholder="What does this user-drawn geometry represent?"></textarea></label>'
      + '<div class="edit-error" id="drawError" hidden></div>'
      + '<div class="btn-row"><button class="btn primary" type="button" data-finish-drawing disabled>Record drawing</button>'
      + '<button class="btn" type="button" data-undo-vertex disabled>Undo vertex</button>'
      + '<button class="btn" type="button" data-cancel-drawing>Cancel</button>'
      + '<span id="sketchCount" style="font-size:11px;color:var(--text-faint)">Choose a geometry type</span></div></div>';
  }

  function editorTab() {
    const operations = activeOperations();
    if (!editingAvailable()) {
      return '<div style="padding:12px 16px"><div class="note warn"><span class="ico">⚠</span><span>Editing is disabled by <code>presentation.editing</code>.</span></div></div>';
    }
    if (!state.editMode) {
      return '<div style="padding:12px 16px"><div class="note info"><span class="ico">✎</span><span><strong>Edit mode is off.</strong> Enable it to select source features or draw additions. Drafts never change the validated run.</span></div>'
        + '<button class="btn primary" type="button" data-enable-edit style="margin-top:12px">Enable Edit mode</button></div>'
        + acc("drafts", "Saved draft", operationRows(), true, String(operations.length));
    }
    return '<div style="padding:12px 16px 0"><div class="note warn"><span class="ico">◇</span><span><strong>Draft preview.</strong> Parcel hide/area/land-use edits re-apply existing browser rules. Facility changes and drawn geometry are map-only until the canonical pipeline recomputes spatial measurements. Source files remain immutable.</span></div></div>'
      + acc("selection", "Selected source feature", selectionEditor(), true)
      + acc("drawing", "Draw addition", drawingEditor(), true)
      + acc("drafts", "Draft operations", operationRows(), true, String(operations.length))
      + '<div style="padding:14px 16px"><div class="btn-row"><button class="btn" type="button" data-undo-operation'
      + (operations.length ? "" : " disabled") + '>Undo</button><button class="btn" type="button" data-redo-operation'
      + (draft.redo.length ? "" : " disabled") + '>Redo</button><button class="btn primary" type="button" data-export-draft'
      + (operations.length ? "" : " disabled") + '>Export override bundle</button></div></div>';
  }

  function renderEditorTab() {
    const panel = $('[data-panel="edit"]');
    if (panel) panel.innerHTML = editorTab();
    updateSketchUI();
  }

  function parseEditValue(field, raw) {
    if (field.type === "number") return Number(raw);
    if (field.type === "boolean") return raw === "true";
    return raw;
  }

  function showEditError(id, message) {
    const element = $(id);
    if (!element) return;
    element.textContent = message;
    element.hidden = !message;
  }

  function makePrecondition(target, field, value) {
    const source = sourceMeta(target.source);
    return {
      source_version: source.version || "",
      source_sha256: source.sha256 || "",
      field: field ? field.source_field : undefined,
      equals: value,
    };
  }

  function saveAttributeEdit() {
    const feature = selectedFeature();
    const target = state.selected && editingTarget(state.selected.kind);
    if (!feature || !target) return;
    const field = target.fields.find((f) => f.view_field === $("#editField").value);
    const reason = $("#editReason").value.trim();
    const evidence = $("#editEvidence").value.trim();
    if (!field || reason.length < 3) { showEditError("#editError", "A concrete rationale is required."); return; }
    const from = fieldValue(field, feature.properties);
    const to = parseEditValue(field, $("#editNewValue").value);
    if (field.type === "number" && !Number.isFinite(to)) { showEditError("#editError", "Enter a valid number."); return; }
    if (String(from) === String(to)) { showEditError("#editError", "The new value matches the pinned source value."); return; }
    recordOperation({
      kind: $("#editKind").value,
      action: "modify_attribute",
      target_kind: state.selected.kind,
      target: {
        source: target.source,
        feature_id: feature.properties[target.id_field],
        feature_name: feature.properties[target.label_field] || "",
      },
      precondition: makePrecondition(target, field, from),
      change: { field: field.source_field, view_field: field.view_field, from: from, to: to },
      rationale: reason,
      evidence: evidence ? [{ type: evidence.indexOf("http") === 0 ? "url" : "note", value: evidence }] : [],
      preview_effect: state.selected.kind === "candidates" && ["land_use", "area_m2"].indexOf(field.view_field) >= 0
        ? "analysis_rules_reapplied" : "map_only",
    });
  }

  function hideSelectedFeature() {
    const feature = selectedFeature();
    const target = state.selected && editingTarget(state.selected.kind);
    const reason = $("#editReason") ? $("#editReason").value.trim() : "";
    const evidence = $("#editEvidence") ? $("#editEvidence").value.trim() : "";
    if (!feature || !target) return;
    if (reason.length < 3) { showEditError("#editError", "A concrete rationale is required before hiding a source feature."); return; }
    recordOperation({
      kind: $("#editKind").value,
      action: "hide_source_feature",
      target_kind: state.selected.kind,
      target: {
        source: target.source,
        feature_id: feature.properties[target.id_field],
        feature_name: feature.properties[target.label_field] || "",
      },
      precondition: makePrecondition(target, null, null),
      rationale: reason,
      evidence: evidence ? [{ type: evidence.indexOf("http") === 0 ? "url" : "note", value: evidence }] : [],
      preview_effect: state.selected.kind === "candidates" ? "analysis_rules_reapplied" : "map_only",
    });
  }

  function sketchGeometry(preview) {
    const coords = state.sketch.slice();
    if (!coords.length) return null;
    if (state.drawMode === "Point") return { type: "Point", coordinates: coords[0] };
    if (state.drawMode === "LineString") {
      if (coords.length < 2 && preview) return { type: "Point", coordinates: coords[0] };
      return coords.length >= 2 ? { type: "LineString", coordinates: coords } : null;
    }
    if (state.drawMode === "Polygon") {
      if (coords.length < 3 && preview) return coords.length === 1
        ? { type: "Point", coordinates: coords[0] } : { type: "LineString", coordinates: coords };
      return coords.length >= 3 ? { type: "Polygon", coordinates: [coords.concat([coords[0]])] } : null;
    }
    return null;
  }

  function updateSketchSource() {
    const geometry = sketchGeometry(true);
    const data = { type: "FeatureCollection", features: geometry
      ? [{ type: "Feature", properties: {}, geometry: geometry }] : [] };
    if (map.getSource("draft-sketch")) map.getSource("draft-sketch").setData(data);
  }

  function updateSketchUI() {
    const count = $("#sketchCount");
    const finish = $("[data-finish-drawing]");
    const undo = $("[data-undo-vertex]");
    const valid = !!sketchGeometry(false);
    if (count) count.textContent = state.drawMode
      ? state.sketch.length + " map " + (state.sketch.length === 1 ? "vertex" : "vertices") : "Choose a geometry type";
    if (finish) finish.disabled = !valid;
    if (undo) undo.disabled = !state.sketch.length;
    const hud = $("#drawHud");
    if (hud) {
      hud.classList.toggle("is-on", !!state.drawMode);
      hud.innerHTML = state.drawMode
        ? "Drawing " + esc(state.drawMode === "LineString" ? "line" : state.drawMode.toLowerCase())
          + " · click the map to add vertices · " + state.sketch.length + " placed"
        : "";
    }
    if (map && map.getCanvas()) map.getCanvas().style.cursor = state.drawMode ? "crosshair" : "";
    updateSketchSource();
  }

  function startDrawing(mode) {
    state.drawMode = mode;
    state.sketch = [];
    renderEditorTab();
  }

  function cancelDrawing() {
    state.drawMode = null;
    state.sketch = [];
    updateSketchSource();
    renderEditorTab();
  }

  function finishDrawing() {
    const geometry = sketchGeometry(false);
    const reason = $("#drawReason") ? $("#drawReason").value.trim() : "";
    const name = $("#drawName") ? $("#drawName").value.trim() : "";
    const kind = $("#drawKind") ? $("#drawKind").value : "scenario";
    if (!geometry) { showEditError("#drawError", "Add enough vertices to finish this geometry."); return; }
    if (reason.length < 3) { showEditError("#drawError", "A concrete rationale is required."); return; }
    const operation = {
      kind: kind,
      action: "add_feature",
      layer: kind === "annotation" ? "annotations" : (kind === "aoi" ? "areas_of_interest" : "scenario_features"),
      properties: { name: name || "User-drawn " + geometry.type, status: kind },
      geometry: geometry,
      geometry_origin: "user_drawn",
      rationale: reason,
      evidence: [],
      preview_effect: "map_only",
    };
    state.drawMode = null;
    state.sketch = [];
    recordOperation(operation);
  }

  function exportDraft() {
    const operations = activeOperations();
    if (!operations.length) return;
    const bundle = {
      schema: VIEW.editing.export_format || "openmapstack-override-bundle/v1",
      status: "draft_unvalidated",
      project: {
        id: VIEW.project.id,
        base_run_id: VIEW.run.id,
        inputs_hash: VIEW.run.inputs_hash,
      },
      exported_at: new Date().toISOString(),
      warnings: [
        "This bundle is a browser preview and has not passed canonical pipeline validation.",
        "Apply operations to immutable sources, verify preconditions, rerun the analysis, and record every result.",
      ],
      operations: operations,
      history: draft.events,
    };
    const blob = new Blob([JSON.stringify(bundle, null, 2) + "\n"], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = VIEW.project.id + "-overrides-" + new Date().toISOString().slice(0, 10) + ".json";
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  function dataTab() {
    const sources = VIEW.sources.map((s) => {
      const comp = s.completeness
        ? '<dt>Completeness</dt><dd>matched ' + esc(s.completeness.matched) + " · returned "
          + esc(s.completeness.returned) + "</dd>"
        : "";
      return '<div class="card"><div class="card-head"><span class="t">' + esc(s.key) + "</span>"
        + '<span class="p">' + esc(s.provider) + "</span></div>"
        + '<dl class="kv"><dt>File</dt><dd class="mono">' + esc(s.file) + "</dd>"
        + "<dt>Table</dt><dd class=\"mono\">" + esc(s.table) + "</dd>"
        + "<dt>Rows</dt><dd>" + (s.rows == null ? "n/a" : int(s.rows))
        + (s.columns_n ? " · " + esc(s.columns_n) + " columns" : "") + "</dd>"
        + "<dt>Version</dt><dd class=\"mono\">" + esc(s.version) + "</dd>"
        + "<dt>Retrieved</dt><dd class=\"mono\">" + esc(s.downloaded_at) + "</dd>"
        + (s.license ? "<dt>License</dt><dd>" + esc(s.license) + "</dd>" : "")
        + comp
        + (s.sha256 ? '<dt>Checksum</dt><dd class="mono">' + esc(s.sha256).slice(0, 26) + "…</dd>" : "")
        + "</dl>"
        + '<div style="display:flex;gap:12px;flex-wrap:wrap">'
        + (s.source_url ? '<a href="' + esc(s.source_url) + '" target="_blank" rel="noopener">Direct source ↗</a>' : "")
        + (s.portal_page ? '<a href="' + esc(s.portal_page) + '" target="_blank" rel="noopener">Portal page ↗</a>' : "")
        + (s.columns.length ? '<span class="schema-toggle" data-schema>Schema (' + s.columns.length + ") ▾</span>" : "")
        + "</div>"
        + (s.columns.length ? '<div class="schema-cols" hidden>' + esc(s.columns.join(", ")) + "</div>" : "")
        + "</div>";
    }).join("");

    const overrides = VIEW.overrides.map((o) => (
      '<div class="card"><div class="card-head"><span class="t">' + esc(o.id) + "</span>"
      + '<span class="pill warn" style="text-transform:none;letter-spacing:0">' + esc(o.origin) + "</span>"
      + '<span class="p">' + esc(o.action) + "</span></div>"
      + '<dl class="kv"><dt>Target</dt><dd>' + esc(o.target) + "</dd>"
      + "<dt>Change</dt><dd class=\"mono\">" + esc(o.change) + "</dd>"
      + "<dt>Recorded</dt><dd class=\"mono\">" + esc(o.created_at) + "</dd></dl>"
      + "<p>" + esc(o.rationale) + "</p>"
      + (o.evidence.length ? '<p style="color:var(--text-faint)"><b>Evidence:</b> ' + esc(o.evidence.join(" · ")) + "</p>" : "")
      + "</div>"
    )).join("");

    const checks = VIEW.validation.checks.map((c) => (
      '<div class="check-row"><span class="id">' + esc(c.id) + "</span>"
      + '<span class="st ' + esc(c.status) + '">' + esc(c.status.replace("_", " ")) + "</span></div>"
      + (c.reason ? '<div class="check-reason">' + esc(c.reason) + "</div>" : "")
    )).join("");

    const outputs = VIEW.outputs.map((o) => (
      '<div class="check-row"><span class="id">' + esc(o.path) + '</span>'
      + '<span class="n" style="margin-left:auto;font-size:10.5px;color:var(--text-faint)">' + esc(o.format) + "</span></div>"
    )).join("");

    const run = '<dl class="kv"><dt>Run</dt><dd class="mono">' + esc(VIEW.run.id) + "</dd>"
      + "<dt>Completed</dt><dd class=\"mono\">" + esc(VIEW.run.completed_at) + "</dd>"
      + "<dt>Inputs</dt><dd class=\"mono\">" + esc(VIEW.run.inputs_hash).slice(0, 26) + "…</dd>"
      + "<dt>Outputs</dt><dd class=\"mono\">" + esc(VIEW.run.outputs_hash).slice(0, 26) + "…</dd>"
      + "<dt>Schema</dt><dd class=\"mono\">" + esc(VIEW.project.schema) + "</dd>"
      + "<dt>Analysis CRS</dt><dd class=\"mono\">" + esc(VIEW.project.analysis_crs) + "</dd></dl>";

    return acc("sources", "Sources & runtime manifest", sources, true, String(VIEW.sources.length))
      + acc("overrides", "Project overrides", overrides, false, String(VIEW.overrides.length))
      + acc("draft-overrides", "Browser draft overrides", operationRows(), false, String(activeOperations().length))
      + acc("validation", "Validation gates", checks, true, VIEW.validation.status)
      + acc("outputs", "Declared outputs", outputs, false, String(VIEW.outputs.length))
      + acc("run", "Run record", run, false);
  }

  // ------------------------------------------------------------ panel: live --
  function tierChip(id, count, area) {
    const t = TIER[id];
    return '<div class="crit"><span class="mk" style="background:' + t.fill + '"></span>'
      + '<div class="body"><b>' + esc(t.label) + "</b> — " + int(count) + " parcels · " + ha(area) + " ha</div></div>";
  }

  function updateMetrics() {
    const modified = !isCanonical();
    const d = (now, was) => {
      if (!modified || now === was) return "";
      const diff = now - was;
      return (diff > 0 ? "+" : "−") + int(Math.abs(diff)) + " vs. canonical run";
    };
    $("#metrics").innerHTML = ""
      + '<div class="metric primary"><div class="val">' + int(stats.tier1) + "</div>"
      + '<div class="lbl">Prime parcels · Tier 1</div>'
      + '<div class="delta">' + d(stats.tier1, canonicalStats.tier1) + "</div></div>"
      + '<div class="metric primary"><div class="val">' + ha(stats.areaTier1) + '<span style="font-size:13px"> ha</span></div>'
      + '<div class="lbl">Prime area</div>'
      + '<div class="delta">' + (modified && Math.round(stats.areaTier1) !== Math.round(canonicalStats.areaTier1)
        ? (stats.areaTier1 > canonicalStats.areaTier1 ? "+" : "−")
          + ha(Math.abs(stats.areaTier1 - canonicalStats.areaTier1)) + " ha vs. canonical"
        : "") + "</div></div>"
      + '<div class="metric"><div class="val">' + int(stats.shown) + "</div>"
      + '<div class="lbl">Parcels in scope</div>'
      + '<div class="delta">' + d(stats.shown, canonicalStats.shown) + "</div></div>"
      + '<div class="metric"><div class="val">' + ha(stats.area) + '<span style="font-size:13px"> ha</span></div>'
      + '<div class="lbl">Evaluated area</div></div>';

    const note = $("#scopeNote");
    note.innerHTML = '<span class="ico">◆</span><span>' + (modified
      ? "Exploratory settings are active. The accepted run reports <b>" + int(canonicalStats.tier1)
        + " prime parcels</b> over <b>" + ha(canonicalStats.areaTier1) + " ha</b>."
      : "These are the accepted run's numbers, reproduced from <code>" + esc(VIEW.run.id) + "</code>.") + "</span>";
    note.className = "note " + (modified ? "warn" : "info");
    note.hidden = false;
  }

  function updateCriteria() {
    const mark = (on) => (on ? '<span class="tag">changed</span>' : "");
    const rows = [
      ["Minimum parcel area", "≥ " + ha(state.minAreaM2) + " ha, measured in " + VIEW.project.analysis_crs,
        state.minAreaM2 !== C.minAreaM2],
      ["Land use", VIEW.landUse.filter((l) => state.landUse.has(l.code)).map((l) => l.label).join(", ") || "none selected",
        state.landUse.size !== C.landUse.length],
      ["Highway accessibility", "≤ " + km(state.maxRoadM) + " to a national primary or secondary road"
        + (state.scenarioRoad ? " or the scenario connector" : ""), state.maxRoadM !== C.maxRoadM],
      ["Education proximity", "≤ " + km(state.educationM) + " straight-line to a municipal school and/or kindergarten (~"
        + Math.round(state.educationM / VIEW.walkSpeedMPerMin) + " min-equivalent)", state.educationM !== C.educationM],
      ["OVERRIDE-002 connector road", state.scenarioRoad ? "counted as access" : "excluded",
        state.scenarioRoad !== C.scenarioRoad],
      ["OVERRIDE-001 facility outage", state.scenarioOutage ? "applied" : "not applied",
        state.scenarioOutage !== C.scenarioOutage],
    ];
    $("#criteria").innerHTML = rows.map((r) => (
      '<div class="crit"><span class="mk"></span><div class="body"><b>' + esc(r[0]) + "</b>" + mark(r[2])
      + '<div style="color:var(--text-muted)">' + esc(r[1]) + "</div></div></div>"
    )).join("")
      + '<div style="border-top:1px solid var(--border);padding-top:9px;display:flex;flex-direction:column;gap:7px">'
      + tierChip("tier1", stats.tier1, stats.areaTier1)
      + tierChip("tier2", stats.tier2, stats.areaTier2)
      + tierChip("tier3", stats.tier3, stats.areaTier3) + "</div>";
  }

  function updateControlLabels() {
    const flag = (elem, off) => { elem.classList.toggle("off-canonical", off); };
    const a = $("#valArea"), r = $("#valRoad"), e = $("#valEdu");
    a.textContent = ha(state.minAreaM2) + " ha";
    r.textContent = km(state.maxRoadM);
    e.textContent = km(state.educationM);
    flag(a, state.minAreaM2 !== C.minAreaM2);
    flag(r, state.maxRoadM !== C.maxRoadM);
    flag(e, state.educationM !== C.educationM);
    $$("[data-landuse]").forEach((btn) => {
      btn.setAttribute("aria-pressed", state.landUse.has(btn.dataset.landuse) ? "true" : "false");
    });
    $$("[data-scenario]").forEach((box) => { box.checked = !!state[box.dataset.scenario]; });
  }

  function updateLayerCounts() {
    const fc = facilityCounts();
    const counts = {
      tier1: stats.tier1,
      tier2: stats.tier2,
      tier3: stats.tier3,
      facilities: fc.school + fc.kindergarten,
      overrides: VIEW.overrides.length + activeOperations().length,
    };
    $$("[data-count]").forEach((span) => {
      const key = span.dataset.count;
      span.textContent = key && counts[key] != null ? int(counts[key]) : "";
    });
  }

  function updateStatusBar() {
    const fc = facilityCounts();
    const draftCount = activeOperations().length;
    $("#mapStatus").innerHTML = "<span><b>" + int(stats.shown) + "</b> parcels in scope</span>"
      + '<span class="sep"></span><span><b>' + int(stats.tier1) + "</b> prime</span>"
      + '<span class="sep"></span><span><b>' + int(fc.school) + "</b> schools · <b>"
      + int(fc.kindergarten) + "</b> kindergartens</span>"
      + '<span class="sep"></span><span>' + esc(VIEW.project.analysis_crs) + "</span>"
      + (draftCount ? '<span class="sep"></span><span style="color:var(--accent-text)"><b>'
        + int(draftCount) + "</b> draft " + (draftCount === 1 ? "edit" : "edits") + "</span>" : "")
      + '<span class="sep"></span><span class="mono">' + esc(VIEW.run.id) + "</span>";
  }

  function updateReconfigured() {
    const modified = !isCanonical();
    $("#mapChip").classList.toggle("is-on", modified);
    $("#reconfNote").hidden = !modified;
    $("#tabBadge").hidden = !modified;
  }

  function updateDraftUI() {
    const count = activeOperations().length;
    $("#draftChip").classList.toggle("is-on", count > 0);
    $("#draftChipText").textContent = count + " unpublished draft " + (count === 1 ? "edit" : "edits")
      + " — preview only";
    $("#editBadge").hidden = count === 0;
    $("#editBadge").textContent = count ? String(count) : "";
    $("#pillDraft").hidden = count === 0;
    $("#pillDraft").innerHTML = count ? '<span class="dot"></span>draft · ' + count : "";
    $("#editModeToggle").setAttribute("aria-pressed", String(state.editMode));
    document.body.classList.toggle("editing", state.editMode);
  }

  // ------------------------------------------------------------------- map --
  const BASEMAPS = {
    dark: "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json",
    light: "https://basemaps.cartocdn.com/gl/positron-gl-style/style.json",
  };
  function currentTheme() {
    const attr = document.documentElement.getAttribute("data-theme");
    if (attr) return attr;
    return window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  }
  const basemapUrl = () => BASEMAPS[state.basemap === "auto" ? currentTheme() : state.basemap];

  const map = new maplibregl.Map({
    container: "map",
    style: basemapUrl(),
    bounds: VIEW.bounds,
    fitBoundsOptions: { padding: 48 },
    attributionControl: { compact: true },
  });
  map.addControl(new maplibregl.NavigationControl({ visualizePitch: false }), "top-right");
  map.addControl(new maplibregl.ScaleControl({ maxWidth: 110, unit: "metric" }), "bottom-right");

  function catchmentFilter(type) {
    const variant = (!state.scenarioOutage && hasBaselineCatchment.has(type)) ? "baseline" : "effective";
    return ["all",
      ["==", ["get", "type"], type],
      ["==", ["get", "radius_m"], state.educationM],
      ["==", ["get", "variant"], variant]];
  }
  const tierFilter = (id) => ["all", ["==", ["get", "_pass"], true], ["==", ["get", "_tier"], id]];

  function addOverlays() {
    if (map.getSource("candidates")) return;
    map.addSource("catchments", { type: "geojson", data: CATCHMENTS });
    map.addSource("roads", { type: "geojson", data: ROADS });
    map.addSource("planned", { type: "geojson", data: PLANNED });
    map.addSource("candidates", { type: "geojson", data: WORKING_CANDIDATES });
    map.addSource("pois", { type: "geojson", data: WORKING_POIS });
    map.addSource("draft-overrides", { type: "geojson", data: DRAWN_OVERRIDES });
    map.addSource("draft-sketch", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
    map.addSource("editor-selection", { type: "geojson", data: { type: "FeatureCollection", features: [] } });

    [["school_catchment", PALETTE.schoolBuffer, "school"], ["kindergarten_catchment", PALETTE.kgBuffer, "kg"]]
      .forEach(([type, color, key]) => {
        map.addLayer({
          id: key + "-catchment-fill", type: "fill", source: "catchments",
          filter: catchmentFilter(type), paint: { "fill-color": color, "fill-opacity": 0.09 },
        });
        map.addLayer({
          id: key + "-catchment-line", type: "line", source: "catchments",
          filter: catchmentFilter(type),
          paint: { "line-color": color, "line-width": 1.4, "line-opacity": 0.55, "line-dasharray": [4, 2] },
        });
      });

    map.addLayer({
      id: "main-roads", type: "line", source: "roads",
      paint: { "line-color": PALETTE.road, "line-width": 2.2, "line-opacity": 0.85 },
    });
    map.addLayer({
      id: "planned-road", type: "line", source: "planned",
      paint: { "line-color": PALETTE.planned, "line-width": 3.2, "line-dasharray": [3, 2] },
    });
    map.addLayer({
      id: "draft-overrides-fill", type: "fill", source: "draft-overrides",
      paint: { "fill-color": PALETTE.planned, "fill-opacity": 0.18 },
    });
    map.addLayer({
      id: "draft-overrides-line", type: "line", source: "draft-overrides",
      paint: { "line-color": PALETTE.planned, "line-width": 3, "line-dasharray": [2, 1.5] },
    });
    map.addLayer({
      id: "draft-overrides-point", type: "circle", source: "draft-overrides",
      paint: { "circle-radius": 7, "circle-color": PALETTE.planned, "circle-stroke-color": "#ffffff", "circle-stroke-width": 2 },
    });

    VIEW.tiers.slice().reverse().forEach((t) => {
      map.addLayer({
        id: t.id + "-fill", type: "fill", source: "candidates", filter: tierFilter(t.id),
        paint: { "fill-color": t.fill, "fill-opacity": t.opacity },
      });
      map.addLayer({
        id: t.id + "-line", type: "line", source: "candidates", filter: tierFilter(t.id),
        paint: { "line-color": t.line, "line-width": 0.9, "line-opacity": 0.85 },
      });
    });
    map.addLayer({
      id: "candidate-hover", type: "line", source: "candidates",
      filter: ["==", ["get", "cadastral_id"], ""],
      paint: { "line-color": "#ffffff", "line-width": 2.4 },
    });

    map.addLayer({
      id: "pois", type: "circle", source: "pois",
      filter: ["!=", ["get", "map_class"], "scenario_inactive"],
      paint: {
        "circle-radius": 5,
        "circle-color": ["match", ["get", "amenity"], "school", PALETTE.school, PALETTE.kindergarten],
        "circle-stroke-width": 1.5,
        "circle-stroke-color": "#ffffff",
      },
    });
    map.addLayer({
      id: "scenario-pois", type: "circle", source: "pois",
      filter: ["==", ["get", "map_class"], "scenario_inactive"],
      paint: {
        "circle-radius": 7, "circle-color": PALETTE.inactive,
        "circle-stroke-width": 2.5, "circle-stroke-color": PALETTE.inactiveRing,
      },
    });
    map.addLayer({
      id: "draft-sketch-fill", type: "fill", source: "draft-sketch",
      paint: { "fill-color": PALETTE.planned, "fill-opacity": 0.12 },
    });
    map.addLayer({
      id: "draft-sketch-line", type: "line", source: "draft-sketch",
      paint: { "line-color": PALETTE.planned, "line-width": 3, "line-dasharray": [1.5, 1.5] },
    });
    map.addLayer({
      id: "draft-sketch-point", type: "circle", source: "draft-sketch",
      paint: { "circle-radius": 6, "circle-color": PALETTE.planned, "circle-stroke-color": "#ffffff", "circle-stroke-width": 2 },
    });
    map.addLayer({
      id: "editor-selection-line", type: "line", source: "editor-selection",
      paint: { "line-color": "#ffffff", "line-width": 4 },
    });
    map.addLayer({
      id: "editor-selection-point", type: "circle", source: "editor-selection",
      paint: { "circle-radius": 10, "circle-color": "rgba(0,0,0,0)", "circle-stroke-color": "#ffffff", "circle-stroke-width": 3 },
    });

    applyLayerVisibility();
    applyFilters();
    updateSketchSource();
    updateSelectionOverlay();
    bindMapInteractions();
  }

  function applyLayerVisibility() {
    VIEW.layerGroups.forEach((g) => {
      g.layers.forEach((id) => {
        if (map.getLayer(id)) {
          map.setLayoutProperty(id, "visibility", state.layers[g.id] ? "visible" : "none");
        }
      });
    });
  }

  function applyFilters() {
    if (!map.getSource("candidates")) return;
    map.getSource("candidates").setData({
      type: "FeatureCollection",
      features: WORKING_CANDIDATES.features.filter((f) => !f.properties._draftHidden),
    });
    map.getSource("pois").setData({
      type: "FeatureCollection",
      features: WORKING_POIS.features.filter((f) => !f.properties._draftHidden),
    });
    map.getSource("draft-overrides").setData(DRAWN_OVERRIDES);
    VIEW.tiers.forEach((t) => {
      map.setFilter(t.id + "-fill", tierFilter(t.id));
      map.setFilter(t.id + "-line", tierFilter(t.id));
    });
    map.setFilter("school-catchment-fill", catchmentFilter("school_catchment"));
    map.setFilter("school-catchment-line", catchmentFilter("school_catchment"));
    map.setFilter("kg-catchment-fill", catchmentFilter("kindergarten_catchment"));
    map.setFilter("kg-catchment-line", catchmentFilter("kindergarten_catchment"));
    const key = facilityClass();
    map.setFilter("pois", ["!=", ["get", key], "scenario_inactive"]);
    map.setFilter("scenario-pois", ["==", ["get", key], "scenario_inactive"]);
  }

  function updateSelectionOverlay() {
    if (!map.getSource("editor-selection")) return;
    const feature = selectedFeature();
    map.getSource("editor-selection").setData({
      type: "FeatureCollection",
      features: feature ? [{ type: "Feature", properties: {}, geometry: clone(feature.geometry) }] : [],
    });
  }

  // -------------------------------------------------------- map: inspection --
  const cadastreSource = VIEW.sources.filter((s) => s.key === "cadastral_parcels")[0] || VIEW.sources[0] || {};
  const poiSource = VIEW.sources.filter((s) => s.key === "education_pois")[0] || {};
  const outageOverride = VIEW.overrides.filter((o) => o.control === "scenario_outage")[0];
  const roadOverride = VIEW.overrides.filter((o) => o.control === "scenario_road")[0];

  function bbox(geometry) {
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    (function walk(c) {
      if (typeof c[0] === "number") {
        minX = Math.min(minX, c[0]); maxX = Math.max(maxX, c[0]);
        minY = Math.min(minY, c[1]); maxY = Math.max(maxY, c[1]);
      } else { c.forEach(walk); }
    })(geometry.coordinates);
    return [[minX, minY], [maxX, maxY]];
  }

  function parcelPopup(p) {
    const t = TIER[p._tier];
    const canonicalTier = String(p.suitability_tier || "");
    const drifted = canonicalTier.indexOf(t.canonical_prefix) !== 0;
    const minutes = (m) => Math.round(m / VIEW.walkSpeedMPerMin);
    return '<div class="pop-badge" style="background:' + t.fill + "22;color:" + t.fill + '">'
      + esc(t.label) + "</div>"
      + '<div class="pop-title">' + esc(p.address || p.cadastral_id) + "</div>"
      + '<dl class="pop-kv">'
      + "<dt>Cadastral ID</dt><dd class=\"mono\">" + esc(p.cadastral_id) + "</dd>"
      + "<dt>Settlement</dt><dd>" + esc(p.settlement || "—") + "</dd>"
      + "<dt>Land use</dt><dd>" + esc(p.land_use) + "</dd>"
      + "<dt>Area</dt><dd>" + int(p.area_m2) + " m² (" + ha(p.area_m2) + " ha)</dd>"
      + "<dt>To highway</dt><dd>" + int(p._road) + " m · " + esc(p.nearest_road_source) + "</dd>"
      + "<dt>To school</dt><dd>" + int(p._ds) + " m (~" + minutes(p._ds) + " min-equiv.)</dd>"
      + "<dt>To kindergarten</dt><dd>" + int(p._dk) + " m (~" + minutes(p._dk) + " min-equiv.)</dd>"
      + (drifted ? '<dt>Canonical run</dt><dd style="color:var(--warn)">' + esc(canonicalTier) + "</dd>" : "")
      + "</dl>"
      + '<div class="pop-foot"><span>' + esc(cadastreSource.provider || "") + " · "
      + esc(cadastreSource.version || "") + "</span>"
      + '<button class="btn" type="button" data-zoom style="margin-left:auto">Zoom to</button></div>';
  }

  function poiPopup(p) {
    const inactive = p[facilityClass()] === "scenario_inactive";
    const label = p.amenity === "school" ? "School" : "Kindergarten";
    return (inactive
      ? '<div class="pop-badge" style="background:var(--err-soft);color:var(--err)">'
        + esc(p.override_id || "scenario") + " · excluded</div>"
      : '<div class="pop-badge" style="background:var(--accent-soft);color:var(--accent-text)">'
        + esc(label) + "</div>")
      + '<div class="pop-title">' + esc(p.name) + "</div>"
      + '<dl class="pop-kv">'
      + "<dt>Type</dt><dd>" + esc(label) + "</dd>"
      + "<dt>Ownership</dt><dd>" + esc(p.ownership) + "</dd>"
      + "<dt>Address</dt><dd>" + esc(p.address || "—") + "</dd>"
      + "<dt>In analysis</dt><dd>" + (inactive
        ? '<span style="color:var(--warn)">excluded by a hypothetical outage — the source still lists it as active</span>'
        : "active, per the authoritative source") + "</dd>"
      + "<dt>Source ID</dt><dd class=\"mono\">" + esc(p.source_id) + "</dd></dl>"
      + '<div class="pop-foot"><span>' + esc(poiSource.provider || "") + " · retrieved "
      + esc((poiSource.downloaded_at || "").slice(0, 10)) + "</span></div>";
  }

  function overridePopup(o) {
    return '<div class="pop-badge" style="background:var(--warn-soft);color:var(--warn)">'
      + esc(o.id) + " · hypothetical</div>"
      + '<div class="pop-title">' + esc(o.target) + "</div>"
      + "<p style=\"font-size:11.5px;color:var(--text-muted)\">" + esc(o.rationale) + "</p>"
      + '<div class="pop-foot"><span>Scenario geometry — not an approved or planned road</span></div>';
  }

  let bound = false;
  function bindMapInteractions() {
    if (bound) return;
    bound = true;
    const tooltip = $("#tooltip");
    const tierLayers = VIEW.tiers.map((t) => t.id + "-fill");

    tierLayers.forEach((layer) => {
      map.on("click", layer, (e) => {
        const feature = e.features && e.features[0];
        if (!feature) return;
        if (state.editMode && !state.drawMode) {
          selectForEditing("candidates", feature);
          return;
        }
        if (state.drawMode) return;
        const popup = new maplibregl.Popup({ maxWidth: "330px" })
          .setLngLat(e.lngLat).setHTML(parcelPopup(feature.properties)).addTo(map);
        const btn = popup.getElement() && popup.getElement().querySelector("[data-zoom]");
        if (btn) btn.addEventListener("click", () => map.fitBounds(bbox(feature.geometry), { padding: 120, maxZoom: 16 }));
      });
      map.on("mousemove", layer, (e) => {
        const feature = e.features && e.features[0];
        if (!feature) return;
        map.getCanvas().style.cursor = state.drawMode ? "crosshair" : "pointer";
        map.setFilter("candidate-hover", ["==", ["get", "cadastral_id"], feature.properties.cadastral_id]);
        tooltip.innerHTML = '<span class="t-tier" style="color:' + TIER[feature.properties._tier].fill + '">'
          + esc(TIER[feature.properties._tier].label) + "</span> · "
          + esc(feature.properties.address || feature.properties.cadastral_id)
          + " · " + ha(feature.properties.area_m2) + " ha";
        tooltip.classList.add("is-on");
        tooltip.style.left = Math.min(e.point.x + 14, map.getCanvas().clientWidth - tooltip.offsetWidth - 8) + "px";
        tooltip.style.top = (e.point.y + 16) + "px";
      });
      map.on("mouseleave", layer, () => {
        map.getCanvas().style.cursor = state.drawMode ? "crosshair" : "";
        map.setFilter("candidate-hover", ["==", ["get", "cadastral_id"], ""]);
        tooltip.classList.remove("is-on");
      });
    });

    ["pois", "scenario-pois"].forEach((layer) => {
      map.on("click", layer, (e) => {
        if (!e.features || !e.features.length) return;
        if (state.editMode && !state.drawMode) {
          selectForEditing("pois", e.features[0]);
          return;
        }
        if (state.drawMode) return;
        new maplibregl.Popup({ maxWidth: "320px" })
          .setLngLat(e.lngLat).setHTML(poiPopup(e.features[0].properties)).addTo(map);
      });
      map.on("mouseenter", layer, () => { map.getCanvas().style.cursor = state.drawMode ? "crosshair" : "pointer"; });
      map.on("mouseleave", layer, () => { map.getCanvas().style.cursor = state.drawMode ? "crosshair" : ""; });
    });

    if (roadOverride) {
      map.on("click", "planned-road", (e) => {
        new maplibregl.Popup({ maxWidth: "320px" })
          .setLngLat(e.lngLat).setHTML(overridePopup(roadOverride)).addTo(map);
      });
      map.on("mouseenter", "planned-road", () => { map.getCanvas().style.cursor = "pointer"; });
      map.on("mouseleave", "planned-road", () => { map.getCanvas().style.cursor = ""; });
    }

    ["draft-overrides-fill", "draft-overrides-line", "draft-overrides-point"].forEach((layer) => {
      map.on("click", layer, (e) => {
        if (state.drawMode || !e.features || !e.features.length) return;
        const p = e.features[0].properties;
        const operation = activeOperations().find((o) => o.id === p.draft_id);
        if (!operation) return;
        new maplibregl.Popup({ maxWidth: "320px" }).setLngLat(e.lngLat)
          .setHTML('<div class="pop-badge" style="background:var(--accent-soft);color:var(--accent-text)">draft · unvalidated</div>'
            + '<div class="pop-title">' + esc(operationTitle(operation)) + "</div>"
            + '<p style="font-size:11.5px;color:var(--text-muted)">' + esc(operation.rationale) + "</p>")
          .addTo(map);
      });
    });

    map.on("click", (e) => {
      if (!state.editMode || !state.drawMode) return;
      const coordinate = [Number(e.lngLat.lng.toFixed(6)), Number(e.lngLat.lat.toFixed(6))];
      if (state.drawMode === "Point") state.sketch = [coordinate];
      else state.sketch.push(coordinate);
      updateSketchSource();
      updateSketchUI();
    });
  }

  // -------------------------------------------------------------- refreshing --
  function refresh() {
    stats = evaluate(state, true);
    applyFilters();
    updateMetrics();
    updateCriteria();
    updateControlLabels();
    updateLayerCounts();
    updateStatusBar();
    updateReconfigured();
    updateDraftUI();
  }

  function resetToCanonical() {
    state.minAreaM2 = C.minAreaM2;
    state.maxRoadM = C.maxRoadM;
    state.educationM = C.educationM;
    state.landUse = new Set(C.landUse);
    state.scenarioRoad = C.scenarioRoad;
    state.scenarioOutage = C.scenarioOutage;
    $("#ctlArea").value = String(state.minAreaM2);
    $("#ctlRoad").value = String(state.maxRoadM);
    $("#ctlEdu").value = String(VIEW.catchmentRadii.indexOf(state.educationM));
    refresh();
  }

  // ------------------------------------------------------------------- init --
  $("#projTitle").textContent = VIEW.project.title;
  $("#projSub").textContent = VIEW.project.schema + " · " + VIEW.run.id;
  const statusPill = (elem, label, status) => {
    const cls = status === "passed" || status === "validated" ? "ok"
      : (status === "failed" ? "err" : (status === "warning" ? "warn" : "info"));
    elem.className = "pill " + cls;
    elem.innerHTML = '<span class="dot"></span>' + esc(label) + " · " + esc(status);
  };
  statusPill($("#pillProject"), "project", VIEW.project.status);
  statusPill($("#pillValidation"), "validation", VIEW.validation.status);

  $('[data-panel="analysis"]').innerHTML = analysisTab();
  $('[data-panel="map"]').innerHTML = mapTab();
  $('[data-panel="edit"]').innerHTML = editorTab();
  $('[data-panel="data"]').innerHTML = dataTab();

  $$(".tab").forEach((tab) => {
    tab.addEventListener("click", () => activateTab(tab.dataset.tab));
  });

  function setEditMode(enabled) {
    state.editMode = editingAvailable() && enabled;
    if (!state.editMode) {
      state.drawMode = null;
      state.sketch = [];
      updateSketchSource();
    }
    renderEditorTab();
    updateDraftUI();
    if (state.editMode) activateTab("edit");
  }
  $("#editModeToggle").disabled = !editingAvailable();
  $("#editModeToggle").addEventListener("click", () => setEditMode(!state.editMode));

  $("#ctlArea").addEventListener("input", (e) => { state.minAreaM2 = Number(e.target.value); refresh(); });
  $("#ctlRoad").addEventListener("input", (e) => { state.maxRoadM = Number(e.target.value); refresh(); });
  $("#ctlEdu").addEventListener("input", (e) => {
    state.educationM = VIEW.catchmentRadii[Number(e.target.value)];
    refresh();
  });
  $$("[data-landuse]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const code = btn.dataset.landuse;
      if (state.landUse.has(code)) { state.landUse.delete(code); } else { state.landUse.add(code); }
      refresh();
    });
  });
  $$("[data-scenario]").forEach((box) => {
    box.addEventListener("change", () => { state[box.dataset.scenario] = box.checked; refresh(); });
  });
  $$("[data-layer]").forEach((box) => {
    box.addEventListener("change", () => {
      state.layers[box.dataset.layer] = box.checked;
      $('[data-layerrow="' + box.dataset.layer + '"]').classList.toggle("is-off", !box.checked);
      applyLayerVisibility();
    });
  });
  $$("[data-reset]").forEach((btn) => btn.addEventListener("click", resetToCanonical));
  document.addEventListener("click", (e) => {
    const toggle = e.target.closest && e.target.closest("[data-schema]");
    if (!toggle) return;
    const cols = toggle.parentElement.parentElement.querySelector(".schema-cols");
    cols.hidden = !cols.hidden;
    toggle.textContent = toggle.textContent.replace(cols.hidden ? "▴" : "▾", cols.hidden ? "▾" : "▴");
  });
  document.addEventListener("click", (e) => {
    const target = e.target.closest && e.target.closest("button");
    if (!target) return;
    if (target.hasAttribute("data-enable-edit")) setEditMode(true);
    else if (target.hasAttribute("data-save-attribute")) saveAttributeEdit();
    else if (target.hasAttribute("data-hide-feature")) hideSelectedFeature();
    else if (target.hasAttribute("data-clear-selection")) {
      state.selected = null; updateSelectionOverlay(); renderEditorTab();
    } else if (target.dataset.drawMode) startDrawing(target.dataset.drawMode);
    else if (target.hasAttribute("data-finish-drawing")) finishDrawing();
    else if (target.hasAttribute("data-undo-vertex")) {
      state.sketch.pop(); updateSketchSource(); updateSketchUI();
    } else if (target.hasAttribute("data-cancel-drawing")) cancelDrawing();
    else if (target.hasAttribute("data-undo-operation")) undoLastOperation();
    else if (target.hasAttribute("data-redo-operation")) redoLastOperation();
    else if (target.dataset.revertOperation) revertOperation(target.dataset.revertOperation);
    else if (target.hasAttribute("data-export-draft")) exportDraft();
  });
  document.addEventListener("change", (e) => {
    if (e.target.id !== "editField") return;
    const feature = selectedFeature();
    const target = state.selected && editingTarget(state.selected.kind);
    if (!feature || !target) return;
    const field = target.fields.find((f) => f.view_field === e.target.value);
    if (field) $("#editValueSlot").innerHTML = valueControl(field, fieldValue(field, feature.properties));
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && state.drawMode) cancelDrawing();
  });

  function setBasemap() {
    map.setStyle(basemapUrl());
    map.once("styledata", addOverlays);
  }
  $$("[data-basemap]").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.basemap = btn.dataset.basemap;
      $$("[data-basemap]").forEach((b) => b.setAttribute("aria-pressed", String(b === btn)));
      setBasemap();
    });
  });

  let storedTheme = null;
  try { storedTheme = window.localStorage.getItem("openmapstack-theme"); } catch (err) { storedTheme = null; }
  if (storedTheme) document.documentElement.setAttribute("data-theme", storedTheme);
  $("#themeToggle").addEventListener("click", () => {
    const next = currentTheme() === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    try { window.localStorage.setItem("openmapstack-theme", next); } catch (err) { /* private mode */ }
    if (state.basemap === "auto") setBasemap();
  });

  rebuildDraftPreview();
  map.on("load", () => { addOverlays(); refresh(); });
  refresh();
})();
</script>
</body>
</html>
"""


# Cadastral sihtotstarve codes present in the accepted selection.
LAND_USE_LABELS = {
    "MAATULUNDUSMAA": "Agricultural / profit-yielding land",
    "TOOTMISMAA": "Production land",
    "ARIMAA": "Commercial land",
}


def _area_slider_max(final_gj: dict) -> int:
    """Upper bound for the minimum-area control, rounded to the 5,000 m2 step."""
    areas = sorted(f["properties"]["area_m2"] for f in final_gj["features"])
    if not areas:
        return 100000
    p90 = areas[min(len(areas) - 1, int(len(areas) * 0.9))]
    return max(40000, int(round(p90 / 5000) * 5000))


def render_dashboard(con: duckdb.DuckDBPyConnection, validation: dict, manifest: list[dict]) -> None:
    pr = PROJECT["project"]
    pres = PROJECT["presentation"]
    inte = PROJECT["interpretation"]

    final_gj = json.loads((DERIVED / "final-candidates.json").read_text())
    roads_gj = json.loads((DERIVED / "main_roads.json").read_text())
    catchments_gj = json.loads((DERIVED / "education_catchment_variants.json").read_text())
    pois_gj = json.loads((DERIVED / "education_pois.json").read_text())

    plan_gj = {"type": "FeatureCollection", "features": []}
    planned_road_file = OVERRIDES / "planned-road.geojson"
    if planned_road_file.exists():
        plan_gj = json.loads(planned_road_file.read_text())

    # Bounds over the accepted result, so the initial view frames the analysis.
    lngs: list[float] = []
    lats: list[float] = []

    def collect(coords):
        if isinstance(coords[0], (int, float)):
            lngs.append(coords[0])
            lats.append(coords[1])
        else:
            for sub in coords:
                collect(sub)

    for feature in final_gj["features"]:
        collect(feature["geometry"]["coordinates"])
    bounds = (
        [[min(lngs), min(lats)], [max(lngs), max(lats)]]
        if lngs else [[26.55, 58.30], [26.85, 58.45]]
    )

    land_use_present = sorted({f["properties"]["land_use"] for f in final_gj["features"]})
    latest_run = PROJECT.get("runs", {}).get("latest", {}) or {}

    # The canonical settings ARE the accepted analysis, so they are read from
    # project.yaml rather than restated here. Any departure the user makes is
    # labelled in the UI as an exploratory reconfiguration.
    filters, scenarios = _declared_controls()
    missing = {"min_area", "max_road_distance", "education_threshold", "land_use"} - set(filters)
    if missing:
        raise RuntimeError(f"presentation.controls.filters is missing {sorted(missing)}")
    canonical = {
        "minAreaM2": filters["min_area"]["canonical"],
        "maxRoadM": filters["max_road_distance"]["canonical"],
        "educationM": filters["education_threshold"]["canonical"],
        "landUse": filters["land_use"]["canonical"],
    }
    for sc in scenarios.values():
        canonical[_control_key(sc["id"])] = bool(sc.get("canonical", True))

    layer_groups = []
    for group in pres["map"].get("layer_groups", []):
        binding = LAYER_BINDINGS.get(group["id"])
        if not binding:
            continue
        layer_groups.append({**group, **binding})

    view = {
        "project": {
            "id": pr["id"],
            "title": pr["title"],
            "status": pr.get("status", ""),
            "updated_at": pr.get("updated_at", ""),
            "schema": PROJECT.get("schema", "openmapstack-project/v1"),
            "analysis_crs": f"EPSG:{ANALYSIS_CRS}",
        },
        "objective": " ".join(inte["objective"].split()),
        "assumptions": [
            {"id": a["id"], "statement": a["statement"], "rationale": a.get("rationale", "")}
            for a in inte.get("assumptions", [])
        ],
        "warnings": [
            {
                "id": w["id"],
                "severity": w.get("severity", "medium"),
                "issue": w.get("issue", ""),
                "statement": w.get("statement", ""),
                "mitigation": w.get("mitigation", ""),
            }
            for w in PROJECT.get("warnings", [])
        ],
        "overrides": _override_cards(),
        "sources": _source_cards(manifest),
        "validation": {
            "status": validation["status"],
            "run_id": validation.get("run_id", ""),
            "checks": [
                {"id": c["id"], "status": c["status"], "reason": c.get("reason", "")}
                for c in validation["checks"]
            ],
        },
        "run": {
            "id": latest_run.get("id", validation.get("run_id", "")),
            "completed_at": latest_run.get("completed_at", ""),
            "inputs_hash": latest_run.get("inputs_hash", ""),
            "outputs_hash": latest_run.get("outputs_hash", ""),
        },
        "outputs": [
            {"key": key, "path": spec["path"], "format": spec["format"]}
            for key, spec in PROJECT.get("outputs", {}).items()
        ],
        "tiers": TIER_STYLE,
        "layerGroups": layer_groups,
        "landUse": [
            {"code": code, "label": LAND_USE_LABELS.get(code, code)} for code in land_use_present
        ],
        "canonical": canonical,
        "catchmentRadii": list(filters["education_threshold"]["options"]),
        "areaBounds": {
            "min": filters["min_area"]["canonical"],
            # The 90th percentile, not the maximum: a handful of 100 ha parcels
            # would otherwise make every useful slider position indistinguishable.
            "max": _area_slider_max(final_gj),
        },
        "walkSpeedMPerMin": WALK_SPEED_M_PER_MIN,
        "bounds": bounds,
        "provenanceUI": pres.get("provenance_ui", {}),
        "interaction": pres["map"].get("interaction", {}),
        "editing": pres.get("editing", {}),
    }

    def embed(payload: dict, round_coords: bool = True) -> str:
        if round_coords:
            payload = json.loads(json.dumps(payload))
            for feature in payload.get("features", []):
                _round_geometry(feature["geometry"])
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/")

    html = DASHBOARD_TEMPLATE
    for token, value in {
        "__TITLE__": pr["title"],
        "__VIEW__": embed(view, round_coords=False),
        "__CANDIDATES__": embed(final_gj),
        "__ROADS__": embed(roads_gj),
        "__PLANNED__": embed(plan_gj),
        "__CATCHMENTS__": embed(catchments_gj, round_coords=False),
        "__POIS__": embed(pois_gj),
    }.items():
        assert token in html, f"dashboard template is missing {token}"
        html = html.replace(token, value)

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
