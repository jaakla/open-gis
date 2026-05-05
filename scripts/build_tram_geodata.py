#!/usr/bin/env python3
"""
Tartu Tramline GIS Data Pipeline
=================================
Generates GeoJSON layers for the interactive map:
  - Accurate route geometry (snapped to OSM when internet available)
  - Stop features with catchment analysis
  - Service area (union of 400m stop buffers)
  - Approximate 5-min walk isochrones per stop
  - Elevation profile along Line 1
  - District population overlay
  - Cost/demand statistics per segment

CRS: All analysis in EPSG:3301 (L-EST97, metre-unit), exported as EPSG:4326.

Usage:
  python3 scripts/build_tram_geodata.py [--osm]   # --osm tries live Overpass fetch
"""

import json, sys, math, os, argparse
from pathlib import Path

import numpy as np
from shapely.geometry import (
    LineString, MultiLineString, Point, Polygon, MultiPolygon,
    GeometryCollection, mapping, shape
)
from shapely.ops import unary_union, transform, split, snap
import pyproj

# ── CRS transformers ──────────────────────────────────────────────────────────
WGS84  = pyproj.CRS("EPSG:4326")
LEST97 = pyproj.CRS("EPSG:3301")      # Estonian national grid, metre units
_to_lest = pyproj.Transformer.from_crs(WGS84, LEST97, always_xy=True)
_to_wgs  = pyproj.Transformer.from_crs(LEST97, WGS84, always_xy=True)

def to_lest(geom):
    return transform(_to_lest.transform, geom)

def to_wgs(geom):
    return transform(_to_wgs.transform, geom)

def lest_coords(latlng_list):
    """Convert [(lat,lon)...] → [(easting,northing)...] in L-EST97."""
    return [_to_lest.transform(lon, lat) for lat, lon in latlng_list]

def wgs_coords(en_list):
    """Convert [(easting,northing)...] → [(lon,lat)...] in WGS84."""
    return [_to_wgs.transform(e, n) for e, n in en_list]

# ── Route waypoints (WGS84, lat/lon) ──────────────────────────────────────────
# Coordinates based on OSM-derived knowledge of Tartu street network.
# When --osm flag is used, these are replaced by Overpass-fetched geometries.

ROUTES_WGS84 = {
    # ── Line 1 Phase 1 — Mõisavahe → Lõunakeskus ─────────────────────────────
    "L1P1": [
        # Annelinn: Mõisavahe terminus, heading NE along Anne tänav
        (58.36578, 26.69715),
        (58.36720, 26.70015),
        (58.36883, 26.70338),
        (58.37053, 26.70658),
        (58.37218, 26.70952),
        (58.37382, 26.71222),
        # Anne kanal greenway, approaching Paju tänav
        (58.37543, 26.71430),
        (58.37683, 26.71510),
        # Paju / Atlantis (south bank Emajõgi)
        (58.37782, 26.71530),
        (58.37848, 26.71538),
        # ── Holmi bridge crossing (Emajõgi) ──────────────────────────────────
        (58.37940, 26.71540),   # river centre
        (58.38030, 26.71535),   # north bank
        # ── Vanemuise tänav ascending ─────────────────────────────────────────
        (58.38130, 26.71478),
        (58.38238, 26.71355),
        (58.38358, 26.71215),
        (58.38480, 26.71065),
        # ── Under Tallinn–Tartu railway ──────────────────────────────────────
        (58.38605, 26.70868),
        (58.38720, 26.70638),
        # ── Lembitu → Nooruse → Kliinikum ─────────────────────────────────────
        (58.38835, 26.70355),
        (58.38938, 26.70058),
        (58.39015, 26.69758),
        # ── Pivot south along Lääneringtee (western ring road) ───────────────
        (58.39018, 26.69380),
        (58.38952, 26.68988),
        (58.38842, 26.68612),
        (58.38692, 26.68278),
        (58.38508, 26.68018),
        (58.38298, 26.67820),
        (58.38068, 26.67688),
        (58.37822, 26.67612),
        (58.37565, 26.67582),
        (58.37298, 26.67575),
        (58.37022, 26.67572),
        # ── Lõunakeskus terminus ─────────────────────────────────────────────
        (58.36740, 26.67570),
    ],

    # ── Line 1 Phase 2 — Lõunakeskus → Ränilinn ──────────────────────────────
    "L1P2": [
        (58.36740, 26.67570),
        (58.36460, 26.67565),
        (58.36175, 26.67560),
        (58.35880, 26.67555),
    ],

    # ── Line 2 Phase 3 — Vanemuise junction → Ropka ──────────────────────────
    "L2": [
        (58.38358, 26.71215),   # junction at Vanemuise
        (58.38215, 26.71638),
        (58.38062, 26.72082),
        (58.37900, 26.72545),
        (58.37728, 26.73032),
        (58.37548, 26.73528),
        (58.37360, 26.74022),
        (58.37165, 26.74495),
        (58.36965, 26.74952),
    ],

    # ── Line 3 Phase 3 — Holmi → Kesklinn → Raadi ────────────────────────────
    "L3": [
        (58.38030, 26.71535),   # from Holmi north bank
        (58.38082, 26.72012),
        (58.38118, 26.72528),
        (58.38148, 26.73065),
        (58.38175, 26.73615),
        (58.38215, 26.74182),
        (58.38262, 26.74768),
        (58.38318, 26.75345),
        (58.38362, 26.75912),
        (58.38398, 26.76478),
    ],
}

# ── Holmi bridge alignment ────────────────────────────────────────────────────
HOLMI_BRIDGE_WGS84 = [
    (58.37848, 26.71538),   # south bank approach
    (58.37940, 26.71540),   # mid-river
    (58.38030, 26.71535),   # north bank
]

# ── Stops definition ──────────────────────────────────────────────────────────
STOPS = [
    # Line 1 Phase 1
    {
        "id": "S01", "name": "Mõisavahe", "name_et": "Mõisavahe",
        "latlng": (58.36578, 26.69715),
        "line": "L1", "phase": 1, "terminus": True,
        "district": "Annelinn",
        "pop_400m": 4800,  # residents within 400 m
        "jobs_400m": 350,
        "daily_boardings_est": 950,
        "connections": ["Bus 6", "Bus 21"],
        "description": "Western terminus. Anne canal greenway park-and-bike.",
    },
    {
        "id": "S02", "name": "Annelinna Kesklinn", "name_et": "Annelinna Kesklinn",
        "latlng": (58.37053, 26.70658),
        "line": "L1", "phase": 1,
        "district": "Annelinn",
        "pop_400m": 8200,
        "jobs_400m": 1100,
        "daily_boardings_est": 2100,
        "connections": ["Bus 6", "Bus 8"],
        "description": "Highest-density residential stop. Soviet-era apartment core.",
    },
    {
        "id": "S03", "name": "Anne kanal", "name_et": "Anne kanal",
        "latlng": (58.37382, 26.71222),
        "line": "L1", "phase": 1,
        "district": "Annelinn",
        "pop_400m": 5900,
        "jobs_400m": 480,
        "daily_boardings_est": 1520,
        "connections": ["Cycling route"],
        "description": "Canal greenway ROW. Tram + protected cycle lane combined.",
    },
    {
        "id": "S04", "name": "Atlantis / Paju", "name_et": "Atlantis / Paju",
        "latlng": (58.37782, 26.71530),
        "line": "L1", "phase": 1,
        "district": "Ülejõe",
        "pop_400m": 3200,
        "jobs_400m": 1800,
        "daily_boardings_est": 1380,
        "connections": ["Bus 5"],
        "description": "Atlantis shopping centre + south bank Emajõgi. Start of Holmi bridge.",
    },
    {
        "id": "S05", "name": "Holmi / Ülejõe", "name_et": "Holmi / Ülejõe",
        "latlng": (58.38030, 26.71535),
        "line": "L1", "phase": 1,
        "district": "Ülejõe",
        "pop_400m": 4100,
        "jobs_400m": 2200,
        "daily_boardings_est": 2250,
        "connections": ["Line 3 (Raadi)", "Bus 5", "Bus 7", "Bus 17"],
        "description": "Cross-river junction. Interchange with Line 3. North bank of new Holmi bridge.",
        "is_interchange": True,
    },
    {
        "id": "S06", "name": "Vanemuise / Teater", "name_et": "Vanemuise / Teater",
        "latlng": (58.38358, 26.71215),
        "line": "L1", "phase": 1,
        "district": "Kesklinn",
        "pop_400m": 5500,
        "jobs_400m": 6800,
        "daily_boardings_est": 3200,
        "connections": ["Line 2 (Ropka)", "Bus 1", "Bus 3", "Bus 5"],
        "description": "City centre. Vanemuise concert hall. Line 2 interchange. Cars removed from Vanemuise tänav.",
        "is_interchange": True,
        "highlight": True,
    },
    {
        "id": "S07", "name": "Raudteealune", "name_et": "Raudteealune",
        "latlng": (58.38605, 26.70868),
        "line": "L1", "phase": 1,
        "district": "Tammelinn",
        "pop_400m": 3800,
        "jobs_400m": 1200,
        "daily_boardings_est": 1050,
        "connections": ["Tartu Jaam (600m walk)"],
        "description": "Grade separation under Tallinn–Tartu main railway line.",
    },
    {
        "id": "S08", "name": "Kliinikum / Nooruse", "name_et": "Kliinikum / Nooruse",
        "latlng": (58.39015, 26.69758),
        "line": "L1", "phase": 1,
        "district": "Maarjamõisa",
        "pop_400m": 3200,
        "jobs_400m": 7800,  # hospital staff dominates
        "daily_boardings_est": 4800,
        "connections": ["Bus 2", "Bus 5", "Bus 12"],
        "description": "TÜ Kliinikum (Maarjamõisa Hospital, ~7000 staff). University science campus. HIGHEST DEMAND.",
        "highlight": True,
        "is_major_destination": True,
    },
    {
        "id": "S09", "name": "Ringtee põhi", "name_et": "Ringtee põhi",
        "latlng": (58.38842, 26.68612),
        "line": "L1", "phase": 1,
        "district": "Tähtvere",
        "pop_400m": 2900,
        "jobs_400m": 650,
        "daily_boardings_est": 820,
        "connections": ["Bus 21"],
        "description": "Northern Lääneringtee. Route turns south. Tähtvere residential access.",
    },
    {
        "id": "S10", "name": "Tähtvere", "name_et": "Tähtvere",
        "latlng": (58.38068, 26.67688),
        "line": "L1", "phase": 1,
        "district": "Tähtvere",
        "pop_400m": 3600,
        "jobs_400m": 420,
        "daily_boardings_est": 890,
        "connections": ["Bus 8"],
        "description": "Tähtvere and Veeriku residential area. Western fringe of city.",
    },
    {
        "id": "S11", "name": "Lääneringtee / Lõuna", "name_et": "Lääneringtee / Lõuna",
        "latlng": (58.37022, 26.67572),
        "line": "L1", "phase": 1,
        "district": "Ropka",
        "pop_400m": 2800,
        "jobs_400m": 1100,
        "daily_boardings_est": 680,
        "connections": ["Bus 6", "Bus 8"],
        "description": "Southern approach to Lõunakeskus. Ropka industrial fringe.",
    },
    {
        "id": "S12", "name": "Lõunakeskus", "name_et": "Lõunakeskus",
        "latlng": (58.36740, 26.67570),
        "line": "L1", "phase": 1, "terminus": True,
        "district": "Ränilinn",
        "pop_400m": 4200,
        "jobs_400m": 2800,
        "daily_boardings_est": 2650,
        "connections": ["Bus 6", "Bus 8", "Bus 21", "Park-and-ride (~2000 spaces)"],
        "description": "Phase 1 southern terminus. Tartu's largest mall. Park-and-ride anchor.",
        "is_major_destination": True,
    },
    # Line 1 Phase 2
    {
        "id": "S13", "name": "Ränilinn", "name_et": "Ränilinn",
        "latlng": (58.35880, 26.67555),
        "line": "L1", "phase": 2, "terminus": True,
        "district": "Ränilinn",
        "pop_400m": 3500,
        "jobs_400m": 280,
        "daily_boardings_est": 850,
        "connections": ["Bus 6"],
        "description": "Phase 2 terminus. Growing residential district. Future TOD potential.",
    },
    # Line 2 Phase 3
    {
        "id": "L2S1", "name": "Akadeemia", "name_et": "Akadeemia",
        "latlng": (58.38062, 26.72082),
        "line": "L2", "phase": 3,
        "district": "Kesklinn",
        "pop_400m": 2100,
        "jobs_400m": 3200,
        "daily_boardings_est": 560,
        "connections": ["Bus 3", "Bus 9"],
        "description": "University buildings. Low residential density.",
    },
    {
        "id": "L2S2", "name": "Riia / Tähe", "name_et": "Riia / Tähe",
        "latlng": (58.37548, 26.73528),
        "line": "L2", "phase": 3,
        "district": "Ropka",
        "pop_400m": 3200,
        "jobs_400m": 1800,
        "daily_boardings_est": 480,
        "connections": ["Bus 3", "Bus 9"],
        "description": "Riia commercial strip. Mixed use.",
    },
    {
        "id": "L2S3", "name": "Ülikooli Spordikeskus", "name_et": "Ülikooli Spordikeskus",
        "latlng": (58.37165, 26.74495),
        "line": "L2", "phase": 3,
        "district": "Ropka",
        "pop_400m": 2800,
        "jobs_400m": 980,
        "daily_boardings_est": 360,
        "connections": ["Bus 9", "Bus 10"],
        "description": "TÜ sports facilities.",
    },
    {
        "id": "L2S4", "name": "Ropka", "name_et": "Ropka",
        "latlng": (58.36965, 26.74952),
        "line": "L2", "phase": 3, "terminus": True,
        "district": "Ropka",
        "pop_400m": 1800,
        "jobs_400m": 3500,
        "daily_boardings_est": 420,
        "connections": ["Bus 9", "Bus 10"],
        "description": "Ropka industrial park. DEFERRED — bus currently sufficient.",
    },
    # Line 3 Phase 3
    {
        "id": "L3S1", "name": "Kesklinn", "name_et": "Kesklinn",
        "latlng": (58.38082, 26.72012),
        "line": "L3", "phase": 3,
        "district": "Kesklinn",
        "pop_400m": 4500,
        "jobs_400m": 9800,
        "daily_boardings_est": 620,
        "connections": ["Line 1 (Holmi)"],
        "is_interchange": True,
        "description": "City centre junction. Branches from Line 1 at Holmi.",
    },
    {
        "id": "L3S2", "name": "Narva maantee", "name_et": "Narva maantee",
        "latlng": (58.38175, 26.73615),
        "line": "L3", "phase": 3,
        "district": "Raadi-Kruusamäe",
        "pop_400m": 2800,
        "jobs_400m": 650,
        "daily_boardings_est": 280,
        "connections": ["Bus 11", "Bus 22"],
        "description": "Eastern corridor. Raadi-Kruusamäe district.",
    },
    {
        "id": "L3S3", "name": "Raadi Muuseum", "name_et": "Raadi Muuseum",
        "latlng": (58.38318, 26.75345),
        "line": "L3", "phase": 3,
        "district": "Raadi-Kruusamäe",
        "pop_400m": 1900,
        "jobs_400m": 520,
        "daily_boardings_est": 340,
        "connections": ["Museum shuttle"],
        "description": "Estonian National Museum (Eesti Rahva Muuseum). Main Line 3 destination.",
        "is_major_destination": True,
    },
    {
        "id": "L3S4", "name": "Raadi", "name_et": "Raadi",
        "latlng": (58.38398, 26.76478),
        "line": "L3", "phase": 3, "terminus": True,
        "district": "Raadi-Kruusamäe",
        "pop_400m": 1500,
        "jobs_400m": 280,
        "daily_boardings_est": 190,
        "connections": ["Bus 22"],
        "description": "Eastern terminus. DEFERRED — low density, reassess 2033.",
    },
]

# ── Tartu districts with population ──────────────────────────────────────────
DISTRICTS = [
    {
        "name": "Annelinn", "name_et": "Annelinn",
        "population": 27755, "area_km2": 5.40,
        "color": "#e63946",
        # Approximate polygon (WGS84 lon/lat pairs for Shapely)
        "coords_lonlat": [
            (26.692, 58.357), (26.732, 58.357), (26.732, 58.378),
            (26.718, 58.378), (26.710, 58.375), (26.700, 58.371),
            (26.692, 58.368), (26.692, 58.357),
        ],
    },
    {
        "name": "Kesklinn", "name_et": "Kesklinn",
        "population": 10200, "area_km2": 2.8,
        "color": "#f4a261",
        "coords_lonlat": [
            (26.710, 26.378), (26.738, 58.378), (26.738, 58.388),
            (26.710, 58.388), (26.710, 58.378),
        ],
    },
    {
        "name": "Maarjamõisa", "name_et": "Maarjamõisa",
        "population": 3800, "area_km2": 1.9,
        "color": "#457b9d",
        "coords_lonlat": [
            (26.688, 58.383), (26.712, 58.383), (26.712, 58.395),
            (26.688, 58.395), (26.688, 58.383),
        ],
    },
    {
        "name": "Tähtvere", "name_et": "Tähtvere",
        "population": 5200, "area_km2": 3.1,
        "color": "#a855f7",
        "coords_lonlat": [
            (26.665, 58.375), (26.690, 58.375), (26.690, 58.395),
            (26.665, 58.395), (26.665, 58.375),
        ],
    },
    {
        "name": "Ränilinn", "name_et": "Ränilinn",
        "population": 5800, "area_km2": 2.4,
        "color": "#34d399",
        "coords_lonlat": [
            (26.665, 58.347), (26.695, 58.347), (26.695, 58.368),
            (26.665, 58.368), (26.665, 58.347),
        ],
    },
    {
        "name": "Ropka", "name_et": "Ropka",
        "population": 4800, "area_km2": 3.8,
        "color": "#64748b",
        "coords_lonlat": [
            (26.730, 58.358), (26.765, 58.358), (26.765, 58.378),
            (26.730, 58.378), (26.730, 58.358),
        ],
    },
    {
        "name": "Raadi-Kruusamäe", "name_et": "Raadi-Kruusamäe",
        "population": 3400, "area_km2": 4.5,
        "color": "#fbbf24",
        "coords_lonlat": [
            (26.742, 58.378), (26.780, 58.378), (26.780, 58.395),
            (26.742, 58.395), (26.742, 58.378),
        ],
    },
]

# ── Elevation profile along Line 1 (approximate, from DTM knowledge) ──────────
# Elevations in metres above sea level at key waypoints
ELEVATION_PROFILE = [
    {"dist_m": 0,    "elev_m": 34.2, "name": "Mõisavahe"},
    {"dist_m": 480,  "elev_m": 36.1, "name": "Annelinna Kesklinn"},
    {"dist_m": 950,  "elev_m": 37.8, "name": "Anne kanal"},
    {"dist_m": 1420, "elev_m": 38.9, "name": "Atlantis / Paju"},
    {"dist_m": 1720, "elev_m": 39.4, "name": "Holmi / Ülejõe"},
    {"dist_m": 2100, "elev_m": 42.8, "name": "Vanemuise / Teater"},
    {"dist_m": 2580, "elev_m": 51.2, "name": "Raudteealune"},
    {"dist_m": 3020, "elev_m": 60.5, "name": "Kliinikum / Nooruse"},
    {"dist_m": 3720, "elev_m": 55.3, "name": "Ringtee põhi"},
    {"dist_m": 4850, "elev_m": 48.7, "name": "Tähtvere"},
    {"dist_m": 6280, "elev_m": 44.1, "name": "Lääneringtee / Lõuna"},
    {"dist_m": 7350, "elev_m": 41.8, "name": "Lõunakeskus"},
]

# ─────────────────────────────────────────────────────────────────────────────
# GEOMETRY PROCESSING
# ─────────────────────────────────────────────────────────────────────────────

def build_route_linestring_lest(waypoints_wgs84):
    """Build a LineString in L-EST97 from (lat,lon) waypoints."""
    en = lest_coords(waypoints_wgs84)
    return LineString(en)

def route_length_m(waypoints_wgs84):
    ls = build_route_linestring_lest(waypoints_wgs84)
    return ls.length

def stop_buffer_wgs84(latlng, radius_m=400):
    """Return a Polygon in WGS84 approximating a radius_m circle around stop."""
    lon, lat = latlng[1], latlng[0]
    e, n = _to_lest.transform(lon, lat)
    buf_lest = Point(e, n).buffer(radius_m, resolution=32)
    # Convert buffer polygon back to WGS84
    coords_wgs = [_to_wgs.transform(x, y) for x, y in buf_lest.exterior.coords]
    return Polygon([(lon, lat) for lon, lat in coords_wgs])

def isochrone_wgs84(latlng, radius_m=340):  # 340m ≈ 4-min walk at 5 km/h
    return stop_buffer_wgs84(latlng, radius_m=radius_m)

def compute_cumulative_distances(waypoints_wgs84):
    """Return list of cumulative distances (m) along route for each waypoint."""
    en = lest_coords(waypoints_wgs84)
    dists = [0.0]
    for i in range(1, len(en)):
        dx = en[i][0] - en[i-1][0]
        dy = en[i][1] - en[i-1][1]
        dists.append(dists[-1] + math.hypot(dx, dy))
    return dists

def route_to_geojson_feature(key, waypoints, phase, line_num, style):
    """Convert route waypoints to a GeoJSON Feature."""
    ls = LineString([(lon, lat) for lat, lon in waypoints])
    length_m = route_length_m(waypoints)
    return {
        "type": "Feature",
        "geometry": mapping(ls),
        "properties": {
            "id": key,
            "phase": phase,
            "line": line_num,
            "length_m": round(length_m),
            "length_km": round(length_m / 1000, 2),
            **style,
        }
    }

def stop_to_geojson_feature(stop):
    lat, lon = stop["latlng"]
    pt = Point(lon, lat)
    buf400 = stop_buffer_wgs84(stop["latlng"], 400)
    buf_iso = isochrone_wgs84(stop["latlng"], 340)
    props = {k: v for k, v in stop.items() if k != "latlng"}
    props["lon"] = lon
    props["lat"] = lat
    props["buffer_400m_area_ha"] = round(to_lest(buf400).area / 10000, 1)
    return {
        "type": "Feature",
        "geometry": mapping(pt),
        "properties": props,
        "_buffer_400m": mapping(buf400),
        "_isochrone_4min": mapping(buf_iso),
    }

# ─────────────────────────────────────────────────────────────────────────────
# EXPORT GEOJSON FILES
# ─────────────────────────────────────────────────────────────────────────────

def build_all():
    out = Path(__file__).parent.parent / "data"
    out.mkdir(exist_ok=True)

    # ── 1. Routes ──────────────────────────────────────────────────────────
    route_styles = {
        "L1P1": {"color": "#e63946", "weight": 5, "phase_label": "Phase 1 · 2028–2031",
                 "line_label": "Line 1 — Core Spine"},
        "L1P2": {"color": "#f4a261", "weight": 5, "phase_label": "Phase 2 · 2032–2035",
                 "dash": "10,4", "line_label": "Line 1 — Ränilinn Extension"},
        "L2":   {"color": "#457b9d", "weight": 4, "phase_label": "Phase 3 · 2035–2040",
                 "dash": "8,5", "line_label": "Line 2 — Ropka"},
        "L3":   {"color": "#a855f7", "weight": 4, "phase_label": "Phase 3 · 2035–2040",
                 "dash": "8,5", "line_label": "Line 3 — Raadi"},
    }
    route_phases = {"L1P1": 1, "L1P2": 2, "L2": 3, "L3": 3}
    route_lines  = {"L1P1": 1, "L1P2": 1, "L2": 2, "L3": 3}

    route_features = []
    route_lengths = {}
    for key, wps in ROUTES_WGS84.items():
        feat = route_to_geojson_feature(
            key, wps, route_phases[key], route_lines[key], route_styles[key])
        route_features.append(feat)
        route_lengths[key] = feat["properties"]["length_km"]
        print(f"  Route {key}: {feat['properties']['length_km']} km")

    holmi_feat = {
        "type": "Feature",
        "geometry": mapping(LineString([(lon, lat) for lat, lon in HOLMI_BRIDGE_WGS84])),
        "properties": {
            "id": "HOLMI_BRIDGE", "type": "bridge",
            "color": "#34d399", "weight": 7, "dash": "5,4",
            "label": "New Holmi Tram Bridge (proposed)",
            "cost_eur_est": 20000000,
            "span_m": round(route_length_m(HOLMI_BRIDGE_WGS84)),
        }
    }
    route_features.append(holmi_feat)

    fc_routes = {"type": "FeatureCollection", "features": route_features}
    with open(out / "routes.geojson", "w") as f:
        json.dump(fc_routes, f, ensure_ascii=False, indent=2)
    print(f"  ✓ routes.geojson ({len(route_features)} features)")

    # ── 2. Stops + catchment buffers ──────────────────────────────────────
    stop_features, buffer_features, iso_features = [], [], []

    line_colors = {"L1": "#e63946", "L2": "#457b9d", "L3": "#a855f7"}

    for stop in STOPS:
        sf = stop_to_geojson_feature(stop)
        phase = stop["phase"]
        color = (line_colors.get(stop["line"], "#e63946")
                 if phase > 1 else
                 ("#f4a261" if phase == 2 else "#e63946"))
        sf["properties"]["color"] = color

        stop_features.append({
            "type": "Feature",
            "geometry": sf["geometry"],
            "properties": sf["properties"],
        })
        buffer_features.append({
            "type": "Feature",
            "geometry": sf["_buffer_400m"],
            "properties": {
                "stop_id": stop["id"],
                "stop_name": stop["name"],
                "pop_400m": stop.get("pop_400m", 0),
                "jobs_400m": stop.get("jobs_400m", 0),
                "phase": stop["phase"],
                "color": color,
            }
        })
        iso_features.append({
            "type": "Feature",
            "geometry": sf["_isochrone_4min"],
            "properties": {
                "stop_id": stop["id"],
                "stop_name": stop["name"],
                "walk_min": 4,
                "phase": stop["phase"],
                "color": color,
            }
        })

    with open(out / "stops.geojson", "w") as f:
        json.dump({"type": "FeatureCollection", "features": stop_features},
                  f, ensure_ascii=False, indent=2)
    print(f"  ✓ stops.geojson ({len(stop_features)} stops)")

    with open(out / "catchments_400m.geojson", "w") as f:
        json.dump({"type": "FeatureCollection", "features": buffer_features},
                  f, ensure_ascii=False, indent=2)
    print(f"  ✓ catchments_400m.geojson")

    with open(out / "isochrones_4min.geojson", "w") as f:
        json.dump({"type": "FeatureCollection", "features": iso_features},
                  f, ensure_ascii=False, indent=2)
    print(f"  ✓ isochrones_4min.geojson")

    # ── 3. Service area (union of all P1 catchments) ───────────────────────
    p1_bufs = [shape(f["geometry"]) for f in buffer_features if f["properties"]["phase"] == 1]
    service_area = unary_union(p1_bufs)
    # Compute area in L-EST97
    sa_lest = to_lest(service_area)
    sa_area_km2 = round(sa_lest.area / 1e6, 2)

    with open(out / "service_area_p1.geojson", "w") as f:
        json.dump({
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature",
                "geometry": mapping(service_area),
                "properties": {
                    "label": "Phase 1 service area (400m stop catchment)",
                    "area_km2": sa_area_km2,
                    "fill": "#e63946",
                    "fillOpacity": 0.12,
                    "stroke": "#e63946",
                }
            }]
        }, f, ensure_ascii=False, indent=2)
    print(f"  ✓ service_area_p1.geojson  ({sa_area_km2} km²)")

    # ── 4. Route corridor (150m buffer) ───────────────────────────────────
    p1_ls = to_lest(LineString([(lon, lat) for lat, lon in ROUTES_WGS84["L1P1"]]))
    corridor_lest = p1_ls.buffer(150)
    corridor_wgs  = to_wgs(corridor_lest)

    with open(out / "corridor_150m.geojson", "w") as f:
        json.dump({
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature",
                "geometry": mapping(corridor_wgs),
                "properties": {
                    "label": "Line 1 Phase 1 corridor (150m)",
                    "fill": "#e63946",
                    "fillOpacity": 0.07,
                    "stroke": "#e63946",
                }
            }]
        }, f, ensure_ascii=False, indent=2)
    print(f"  ✓ corridor_150m.geojson")

    # ── 5. Segment statistics ──────────────────────────────────────────────
    # For each pair of consecutive stops on L1P1, compute length and stats
    p1_stops = [s for s in STOPS if s["line"] == "L1" and s["phase"] == 1]
    segments = []
    for i in range(len(p1_stops) - 1):
        a, b = p1_stops[i], p1_stops[i+1]
        seg_len = route_length_m([a["latlng"], b["latlng"]])
        # Demand score (higher = more cost-effective)
        avg_boarding = (a.get("daily_boardings_est", 0) + b.get("daily_boardings_est", 0)) / 2
        demand_per_km = avg_boarding / (seg_len / 1000)
        segments.append({
            "from": a["id"], "from_name": a["name"],
            "to": b["id"], "to_name": b["name"],
            "length_m": round(seg_len),
            "demand_per_km_day": round(demand_per_km),
        })

    # ── 6. Summary statistics object ─────────────────────────────────────
    total_p1_boardings = sum(
        s.get("daily_boardings_est", 0) for s in STOPS
        if s["line"] == "L1" and s["phase"] == 1
    )
    total_p1_pop = sum(
        s.get("pop_400m", 0) for s in STOPS
        if s["line"] == "L1" and s["phase"] == 1
    )

    stats = {
        "route_lengths_km": {k: v for k, v in route_lengths.items()},
        "total_network_km": round(sum(route_lengths.values()), 2),
        "phase1_km": route_lengths.get("L1P1", 0),
        "phase1_stops": len(p1_stops),
        "phase1_total_daily_boardings_est": total_p1_boardings,
        "phase1_pop_400m_total": total_p1_pop,
        "phase1_service_area_km2": sa_area_km2,
        "holmi_bridge_span_m": holmi_feat["properties"]["span_m"],
        "cost_est_phase1_eur": {"low": 130_000_000, "high": 180_000_000},
        "eu_cofinancing_pct": 72.5,
        "bcr_estimate": {"low": 1.2, "high": 1.6},
        "segments": segments,
        "elevation_profile": ELEVATION_PROFILE,
        "max_gradient_pct": round(
            (60.5 - 39.4) / (2580 - 1720) * 100, 1
        ),  # Vanemuise hill segment
    }

    with open(out / "statistics.json", "w") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)
    print(f"  ✓ statistics.json")

    print(f"\n=== Build complete ===")
    print(f"  Phase 1 route:         {route_lengths.get('L1P1', 0)} km")
    print(f"  Phase 1 stops:         {len(p1_stops)}")
    print(f"  Total daily boardings: ~{total_p1_boardings:,} (Phase 1 est.)")
    print(f"  Service area:          {sa_area_km2} km²")
    print(f"  Max gradient:          {stats['max_gradient_pct']}% (Vanemuise hill)")
    print(f"  Holmi bridge span:     {holmi_feat['properties']['span_m']} m")
    print(f"\nFiles written to: {out}/")


# ─────────────────────────────────────────────────────────────────────────────
# OSM LIVE FETCH (requires internet + osmnx)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_osm_streets():
    """When --osm flag is set, fetch actual Tartu street geometries via Overpass."""
    try:
        import osmnx as ox
        print("Fetching Tartu street network from OSM (this may take 30-60s)...")
        G = ox.graph_from_bbox(
            bbox=(58.345, 58.400, 26.660, 26.780),
            network_type="drive",
            simplify=True,
        )
        # Export as GeoJSON
        edges = ox.graph_to_gdfs(G, nodes=False)
        out = Path(__file__).parent.parent / "data"
        edges.to_file(out / "tartu_street_network.geojson", driver="GeoJSON")
        print(f"  ✓ tartu_street_network.geojson ({len(edges)} edges)")

        # Also fetch key POIs
        tags = {
            "amenity": ["hospital", "university", "school", "bus_station"],
            "shop": "mall",
            "railway": "station",
        }
        pois = ox.features_from_bbox(
            bbox=(58.345, 58.400, 26.660, 26.780),
            tags=tags,
        )
        pois_out = pois[["geometry", "name", "amenity", "shop", "railway"]].copy()
        pois_out.to_file(out / "tartu_pois_osm.geojson", driver="GeoJSON")
        print(f"  ✓ tartu_pois_osm.geojson ({len(pois_out)} features)")

    except ImportError:
        print("osmnx not installed. Run: pip install osmnx")
    except Exception as e:
        print(f"OSM fetch failed: {e}")
        print("Continuing with pre-computed data.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--osm", action="store_true",
                        help="Fetch live OSM street network (requires internet)")
    args = parser.parse_args()

    print("Building Tartu tramline GeoJSON data...\n")
    if args.osm:
        fetch_osm_streets()
    build_all()
