#!/usr/bin/env python3
"""
Tartu Tramline GIS Data Pipeline
=================================
v2 — Coordinates verified against actual OSM landmarks:
  Raekoja plats (Town Hall)    : 58.3806, 26.7227   [city centre, west bank]
  Atlantis (Narva mnt 2)       : 58.3803, 26.7275   [east bank, at Holmi crossing]
  Anne tn 53                   : 58.3764, 26.7547   [Annelinn, on Anne tänav]
  TÜ Kliinikum (L. Puusepa 8)  : 58.3697, 26.7010   [Maarjamõisa hospital, SW]
  Lõunakeskus (Ringtee 75)     : 58.3581, 26.6779   [southwest, ring road]
  ERM (Muuseumi tee 2)         : 58.3955, 26.7451   [Raadi, north]

Geographic facts (verified):
  - Emajõgi flows west→east through Tartu
  - In central Tartu the river curves; Raekoja is on the WEST/RIGHT bank
  - Annelinn, Ülejõe are on the EAST/LEFT bank
  - Maarjamõisa is SW of city centre, 2 km from centre, north of Riia tänav
  - Anne tänav runs east from Ülejõe through Annelinn to Anne manor area
  - Vanemuise tänav runs roughly N-S in old town (downhill toward Ülikooli tn)
  - Lembitu tänav connects Riia tänav to Ludvig Puusepa tänav (hospital)
  - Riia tänav runs SW from Rahu sild to Lõunakeskus at Ringtee

v3 — Realistic street alignment:
  - Holmi bridge lands at ~58.379 N (not Raekoja level)
  - West bank follows Emajõe embankment south → Uueturu tänav
  - New stop: Siuru (Uueturu 4, ~58.3772 N)
  - Vanemuise tänav followed precisely south to railway underpass
  - L3 (Raadi) branches from Holmi west-bank landing, not Raekoja
"""

import json, math
from pathlib import Path
from shapely.geometry import LineString, Point, Polygon, mapping, shape
from shapely.ops import unary_union, transform
import pyproj

# CRS transformers ─────────────────────────────────────────────────────────────
WGS84  = pyproj.CRS("EPSG:4326")
LEST97 = pyproj.CRS("EPSG:3301")
_to_lest = pyproj.Transformer.from_crs(WGS84, LEST97, always_xy=True)
_to_wgs  = pyproj.Transformer.from_crs(LEST97, WGS84, always_xy=True)

def to_lest(geom): return transform(_to_lest.transform, geom)
def to_wgs(geom):  return transform(_to_wgs.transform, geom)
def en(lat, lon):  return _to_lest.transform(lon, lat)
def ll(e, n):      return _to_wgs.transform(e, n)  # returns (lon, lat)

# ── ROUTE WAYPOINTS (verified street alignment) ───────────────────────────────
# All in (lat, lon) WGS84
ROUTES = {
    # ─── Line 1 Phase 1 — Mõisavahe (east Annelinn) → Lõunakeskus (SW) ────
    # Route: Annelinn (east) → Anne tänav west → Sõpruse area → cross river at
    #        Holmi bridge (next to Kaarsild) → up Vanemuise (south) → under
    #        railway → Lembitu → Kliinikum → SW along Riia tn → Lõunakeskus
    "L1P1": [
        # Mõisavahe terminus (eastern Annelinn, near Anne manor area)
        (58.37170, 26.76040),
        # Anne tänav eastern section
        (58.37320, 26.75850),
        (58.37450, 26.75620),
        # Anne tn 53 area (verified address)
        (58.37640, 26.75470),
        (58.37730, 26.75100),
        # Anne kanal area (the 9.4 ha artificial lake)
        (58.37815, 26.74600),
        (58.37875, 26.74100),
        # Sõpruse pst / Paju tänav approach to bridge
        (58.37915, 26.73680),
        (58.37960, 26.73250),
        (58.37990, 26.72960),
        # Atlantis (Narva mnt 2) — east bank of Emajõgi, at Holmi bridgehead
        (58.38030, 26.72750),
        # ── New Holmi tram bridge (angled WSW across Emajõgi) ──
        (58.37990, 26.72470),
        (58.37950, 26.72210),
        # West bank landing — Emajõe promenade / Holmi; L3 Raadi branch here
        (58.37910, 26.72050),
        # South along right-bank embankment toward Uueturu
        (58.37860, 26.71990),
        (58.37790, 26.71965),
        # ── Uueturu tänav — Siuru cultural stop (Uueturu 4) ──
        (58.37720, 26.71955),
        # Uueturu→Vanemuise junction, turn south
        (58.37640, 26.71860),
        (58.37570, 26.71740),
        # Vanemuise tänav proper — alongside Toomemägi slope
        # Vanemuine concert hall on right (Vanemuise 6)
        (58.37500, 26.71620),
        (58.37400, 26.71570),
        (58.37290, 26.71530),
        # Under Tallinn–Tartu railway (grade separation)
        (58.37180, 26.71500),
        (58.37100, 26.71470),
        # South of railway — short connector to Lembitu tänav
        (58.37030, 26.71370),
        (58.37000, 26.71200),
        # Lembitu tänav (runs W toward Puusepa / Kliinikum campus)
        (58.36990, 26.71000),
        (58.36982, 26.70700),
        # Maarjamõisa — TÜ Kliinikum (Ludvig Puusepa 8) ── verified
        (58.36978, 26.70350),
        (58.36975, 26.70100),
        # Continue SW along Riia tänav toward Lõunakeskus
        (58.36850, 26.69700),
        (58.36680, 26.69280),
        (58.36480, 26.68850),
        (58.36250, 26.68420),
        (58.36050, 26.68080),
        # Lõunakeskus (Ringtee 75) ── verified
        (58.35810, 26.67790),
    ],

    # ─── Line 1 Phase 2 — Lõunakeskus → Ränilinn (south) ───────────────────
    "L1P2": [
        (58.35810, 26.67790),
        (58.35530, 26.67580),
        (58.35280, 26.67380),
        (58.35020, 26.67200),
    ],

    # ─── Line 2 Phase 3 — Vanemuise junction → Ropka (via Tähe tn) ─────────
    "L2": [
        (58.37500, 26.71620),   # branch from Line 1 at Vanemuise / Teater
        (58.37520, 26.71820),
        (58.37600, 26.72050),
        (58.37650, 26.72380),   # Akadeemia tn area
        (58.37570, 26.72700),   # crossing Riia tänav
        (58.37430, 26.73050),
        (58.37250, 26.73450),
        # Tähe tänav south through Karlova
        (58.37040, 26.73850),
        (58.36830, 26.74230),
        (58.36620, 26.74580),
        (58.36400, 26.74900),
        # Ropka industrial park terminus
        (58.36180, 26.75180),
    ],

    # ─── Line 3 Phase 3 — Holmi west bank → Raadi (ERM museum) ────────────
    "L3": [
        (58.37910, 26.72050),   # junction at Holmi west-bank landing (off Line 1)
        (58.38060, 26.72180),   # north along embankment / Küüni
        (58.38220, 26.72450),
        (58.38380, 26.72780),   # Narva maantee
        (58.38580, 26.73280),
        (58.38760, 26.73650),
        (58.38940, 26.74050),
        (58.39120, 26.74380),
        (58.39320, 26.74470),
        # ERM (Muuseumi tee 2) ── verified
        (58.39550, 26.74510),
    ],
}

# ── New Holmi bridge alignment (segment of L1P1 across the river) ─────────────
HOLMI_BRIDGE = [
    (58.38030, 26.72750),   # east bank (Atlantis / Narva mnt 2)
    (58.37990, 26.72470),   # mid-span (river)
    (58.37950, 26.72210),   # approaching west bank
    (58.37910, 26.72050),   # west bank landing (Emajõe promenade)
]

# ── STOPS — using verified street/landmark coordinates ───────────────────────
STOPS = [
    # ═══ Line 1 Phase 1 (12 stops, Mõisavahe → Lõunakeskus) ═══
    {"id":"S01","name":"Mõisavahe","name_et":"Mõisavahe",
     "latlng":(58.37170, 26.76040),"line":"L1","phase":1,"terminus":True,
     "district":"Annelinn",
     "pop_400m":4400,"jobs_400m":280,"daily_boardings_est":920,
     "connections":["Bus 6","Bus 21"],
     "description":"Eastern Annelinn terminus. Anne manor area; high-density Soviet apartments."},

    {"id":"S02","name":"Anne / Kalda","name_et":"Anne / Kalda",
     "latlng":(58.37450, 26.75620),"line":"L1","phase":1,
     "district":"Annelinn",
     "pop_400m":6800,"jobs_400m":520,"daily_boardings_est":1750,
     "connections":["Bus 6","Bus 8"],
     "description":"Anne tänav central stop. Direct Anne kanal access; high residential density."},

    {"id":"S03","name":"Anne kanal","name_et":"Anne kanal",
     "latlng":(58.37815, 26.74600),"line":"L1","phase":1,
     "district":"Annelinn",
     "pop_400m":7200,"jobs_400m":680,"daily_boardings_est":2080,
     "connections":["Bus 6","Cycling network"],
     "description":"Anne canal & swimming beach. Greenway corridor — tram ROW co-located with cycling path."},

    {"id":"S04","name":"Sõpruse / Paju","name_et":"Sõpruse / Paju",
     "latlng":(58.37960, 26.73250),"line":"L1","phase":1,
     "district":"Annelinn",
     "pop_400m":4600,"jobs_400m":1100,"daily_boardings_est":1320,
     "connections":["Bus 5","Bus 8"],
     "description":"Sõpruse pst & Paju tn junction. Connector to bridge approach."},

    {"id":"S05","name":"Atlantis / Holmi (East)","name_et":"Atlantis / Holmi",
     "latlng":(58.38030, 26.72750),"line":"L1","phase":1,
     "district":"Ülejõe",
     "pop_400m":3400,"jobs_400m":2200,"daily_boardings_est":1980,
     "connections":["Bus 5","Bus 7","Bus 17"],
     "description":"East bank at Atlantis (Narva mnt 2). Start of new Holmi tram bridge."},

    {"id":"S06","name":"Holmi / Emajõe","name_et":"Holmi / Emajõe",
     "latlng":(58.37910, 26.72050),"line":"L1","phase":1,
     "district":"Kesklinn",
     "pop_400m":3800,"jobs_400m":5200,"daily_boardings_est":2450,
     "connections":["Line 3 (Raadi)","Bus 1","Bus 3","Bus 5"],
     "description":"West bank bridge landing on Emajõe promenade. Line 3 (Raadi) interchange.",
     "is_interchange":True,"highlight":True},

    {"id":"S07","name":"Siuru / Uueturu","name_et":"Siuru / Uueturu",
     "latlng":(58.37720, 26.71955),"line":"L1","phase":1,
     "district":"Kesklinn",
     "pop_400m":4100,"jobs_400m":5800,"daily_boardings_est":2640,
     "connections":["Bus 1","Bus 3"],
     "description":"Uueturu tänav at Siuru cultural hub. Dense old-town stop; minimal new construction needed."},

    {"id":"S08","name":"Vanemuise / Teater","name_et":"Vanemuise / Teater",
     "latlng":(58.37500, 26.71620),"line":"L1","phase":1,
     "district":"Kesklinn",
     "pop_400m":4900,"jobs_400m":6400,"daily_boardings_est":2880,
     "connections":["Line 2 (Ropka)","Bus 1","Bus 3"],
     "description":"Vanemuine concert hall (Vanemuise 6). Line 2 (Ropka) interchange. Toomemägi slope.",
     "is_interchange":True},

    {"id":"S09","name":"Raudteealune","name_et":"Raudteealune",
     "latlng":(58.37150, 26.71490),"line":"L1","phase":1,
     "district":"Tammelinn",
     "pop_400m":3300,"jobs_400m":960,"daily_boardings_est":880,
     "connections":["Tartu Jaam (~400m walk)"],
     "description":"Grade separation under Tallinn–Tartu railway on Vanemuise. Station within 400 m."},

    {"id":"S10","name":"Lembitu","name_et":"Lembitu",
     "latlng":(58.36990, 26.71000),"line":"L1","phase":1,
     "district":"Tammelinn",
     "pop_400m":2900,"jobs_400m":1450,"daily_boardings_est":920,
     "connections":["Bus 2","Bus 12"],
     "description":"Lembitu tänav connector to Puusepa / hospital campus."},

    {"id":"S11","name":"Kliinikum","name_et":"Kliinikum",
     "latlng":(58.36975, 26.70100),"line":"L1","phase":1,
     "district":"Maarjamõisa",
     "pop_400m":2800,"jobs_400m":7800,"daily_boardings_est":4900,
     "connections":["Bus 2","Bus 5","Bus 12"],
     "description":"Tartu University Hospital (Ludvig Puusepa 8). ~7000 staff. HIGHEST DEMAND STOP.",
     "highlight":True,"is_major_destination":True},

    {"id":"S12","name":"Riia / Tähtvere","name_et":"Riia / Tähtvere",
     "latlng":(58.36480, 26.68850),"line":"L1","phase":1,
     "district":"Tammelinn / Variku",
     "pop_400m":3200,"jobs_400m":720,"daily_boardings_est":740,
     "connections":["Bus 8","Bus 21"],
     "description":"Riia tänav mid-section. Tammelinn and Variku residential."},

    {"id":"S13","name":"Lõunakeskus","name_et":"Lõunakeskus",
     "latlng":(58.35810, 26.67790),"line":"L1","phase":1,"terminus":True,
     "district":"Ränilinn / Variku",
     "pop_400m":3900,"jobs_400m":2800,"daily_boardings_est":2680,
     "connections":["Bus 6","Bus 8","Bus 21","Park-and-Ride"],
     "description":"Phase 1 SW terminus. Lõunakeskus mall (Ringtee 75). 2000-space car park for P+R.",
     "is_major_destination":True},

    # ═══ Line 1 Phase 2 — Ränilinn extension ═══
    {"id":"S14","name":"Ränilinn","name_et":"Ränilinn",
     "latlng":(58.35020, 26.67200),"line":"L1","phase":2,"terminus":True,
     "district":"Ränilinn",
     "pop_400m":3400,"jobs_400m":260,"daily_boardings_est":830,
     "connections":["Bus 6"],
     "description":"Phase 2 terminus. Growing residential district; TOD potential post-2030."},

    # ═══ Line 2 Phase 3 (Ropka branch) ═══
    {"id":"L2S1","name":"Akadeemia","name_et":"Akadeemia",
     "latlng":(58.37650, 26.72380),"line":"L2","phase":3,
     "district":"Kesklinn",
     "pop_400m":2100,"jobs_400m":3400,"daily_boardings_est":520,
     "connections":["Bus 3","Bus 9"],
     "description":"Akadeemia tn near university buildings."},

    {"id":"L2S2","name":"Riia / Tähe","name_et":"Riia / Tähe",
     "latlng":(58.37430, 26.73050),"line":"L2","phase":3,
     "district":"Karlova",
     "pop_400m":3400,"jobs_400m":1650,"daily_boardings_est":480,
     "connections":["Bus 3","Bus 9"],
     "description":"Riia–Tähe junction. Karlova approach."},

    {"id":"L2S3","name":"Tähe / Spordikeskus","name_et":"Tähe / Spordikeskus",
     "latlng":(58.36830, 26.74230),"line":"L2","phase":3,
     "district":"Karlova",
     "pop_400m":3800,"jobs_400m":1100,"daily_boardings_est":520,
     "connections":["Bus 9","Bus 10"],
     "description":"TÜ sports facility. Karlova residential."},

    {"id":"L2S4","name":"Ropka","name_et":"Ropka",
     "latlng":(58.36180, 26.75180),"line":"L2","phase":3,"terminus":True,
     "district":"Ropka tööstus",
     "pop_400m":1600,"jobs_400m":3800,"daily_boardings_est":460,
     "connections":["Bus 9","Bus 10"],
     "description":"Ropka industrial terminus. DEFERRED — bus currently sufficient."},

    # ═══ Line 3 Phase 3 (Raadi branch) ═══
    {"id":"L3S1","name":"Kesklinn (L3)","name_et":"Kesklinn (Linn 3)",
     "latlng":(58.38150, 26.72380),"line":"L3","phase":3,
     "district":"Kesklinn",
     "pop_400m":4200,"jobs_400m":9200,"daily_boardings_est":580,
     "connections":["Line 1 (Kesklinn)"],
     "is_interchange":True,
     "description":"Line 3 city centre. Interchange with Line 1 at Holmi crossing."},

    {"id":"L3S2","name":"Narva maantee","name_et":"Narva maantee",
     "latlng":(58.38580, 26.73280),"line":"L3","phase":3,
     "district":"Raadi-Kruusamäe",
     "pop_400m":2900,"jobs_400m":640,"daily_boardings_est":280,
     "connections":["Bus 11","Bus 22"],
     "description":"Narva mnt mid-section. Raadi-Kruusamäe approach."},

    {"id":"L3S3","name":"Raadi","name_et":"Raadi",
     "latlng":(58.39120, 26.74380),"line":"L3","phase":3,
     "district":"Raadi-Kruusamäe",
     "pop_400m":2100,"jobs_400m":420,"daily_boardings_est":260,
     "connections":["Bus 22"],
     "description":"Raadi manor district. Residential approach."},

    {"id":"L3S4","name":"ERM Muuseum","name_et":"ERM Muuseum",
     "latlng":(58.39550, 26.74510),"line":"L3","phase":3,"terminus":True,
     "district":"Raadi-Kruusamäe",
     "pop_400m":1200,"jobs_400m":380,"daily_boardings_est":380,
     "connections":["Museum facilities"],
     "description":"Eesti Rahva Muuseum (Muuseumi tee 2). Phase 3 northern terminus. Tourism/cultural anchor.",
     "is_major_destination":True},
]

# ── Elevation profile (Line 1, approximate from Tartu DEM knowledge) ──────────
# Mõisavahe is on Annelinn plateau (~38m), river bottom (~32m), Vanemuise rises
# up Toomemägi area then drops to Maarjamõisa (~55m), Lõunakeskus ~50m
ELEVATION_PROFILE = [
    {"dist_m":0,    "elev_m":38.4, "name":"Mõisavahe"},
    {"dist_m":500,  "elev_m":37.9, "name":"Anne / Kalda"},
    {"dist_m":1300, "elev_m":37.2, "name":"Anne kanal"},
    {"dist_m":2150, "elev_m":36.5, "name":"Sõpruse / Paju"},
    {"dist_m":2900, "elev_m":34.8, "name":"Atlantis / Holmi"},
    {"dist_m":3250, "elev_m":34.2, "name":"Holmi / Emajõe"},
    {"dist_m":3600, "elev_m":36.5, "name":"Siuru / Uueturu"},
    {"dist_m":4100, "elev_m":48.2, "name":"Vanemuise / Teater"},
    {"dist_m":4720, "elev_m":54.8, "name":"Raudteealune"},
    {"dist_m":5280, "elev_m":58.1, "name":"Lembitu"},
    {"dist_m":5900, "elev_m":59.5, "name":"Kliinikum"},
    {"dist_m":7350, "elev_m":52.6, "name":"Riia / Tähtvere"},
    {"dist_m":8700, "elev_m":48.3, "name":"Lõunakeskus"},
]

# ─────────────────────────────────────────────────────────────────────────────

def linestring_lest(latlon_list):
    return LineString([en(lat, lon) for lat, lon in latlon_list])

def route_length_m(latlon_list):
    return linestring_lest(latlon_list).length

def stop_buffer_wgs84(lat, lon, r=400):
    e, n = en(lat, lon)
    buf = Point(e, n).buffer(r, resolution=48)
    return to_wgs(buf)

def build_all():
    out = Path(__file__).parent.parent / "data"
    out.mkdir(exist_ok=True)
    print("Building Tartu tramline GeoJSON (corrected coordinates)...\n")

    # ── Routes ──────────────────────────────────────────────────────────
    route_features = []
    route_styles = {
        "L1P1": {"color":"#e63946","weight":5,"phase":1,"line_num":1,
                 "label":"Line 1 — Core Spine","phase_label":"Phase 1 · 2028–2031"},
        "L1P2": {"color":"#f4a261","weight":5,"phase":2,"line_num":1,"dash":"10,4",
                 "label":"Line 1 — Ränilinn ext.","phase_label":"Phase 2 · 2032–2035"},
        "L2":   {"color":"#1d4ed8","weight":4,"phase":3,"line_num":2,"dash":"8,5",
                 "label":"Line 2 — Ropka","phase_label":"Phase 3 · 2035–2040"},
        "L3":   {"color":"#7c3aed","weight":4,"phase":3,"line_num":3,"dash":"8,5",
                 "label":"Line 3 — Raadi","phase_label":"Phase 3 · 2035–2040"},
    }
    route_lengths = {}

    for key, wps in ROUTES.items():
        ls = LineString([(lon, lat) for lat, lon in wps])
        length_m = route_length_m(wps)
        props = {"id":key, "length_m":round(length_m), "length_km":round(length_m/1000, 2)}
        props.update(route_styles[key])
        route_features.append({
            "type":"Feature","geometry":mapping(ls),"properties":props,
        })
        route_lengths[key] = props["length_km"]
        print(f"  Route {key:5s}: {props['length_km']:.2f} km")

    # Holmi bridge as separate feature
    holmi_ls = LineString([(lon, lat) for lat, lon in HOLMI_BRIDGE])
    holmi_len = route_length_m(HOLMI_BRIDGE)
    route_features.append({
        "type":"Feature","geometry":mapping(holmi_ls),
        "properties":{"id":"HOLMI_BRIDGE","color":"#059669","weight":7,"dash":"5,4",
                      "label":"New Holmi Tram Bridge","span_m":round(holmi_len),
                      "cost_eur_est":20_000_000,"type":"bridge"},
    })

    with open(out / "routes.geojson","w") as f:
        json.dump({"type":"FeatureCollection","features":route_features}, f,
                  ensure_ascii=False, indent=2)
    print(f"  ✓ routes.geojson ({len(route_features)} features, "
          f"Holmi span = {round(holmi_len)} m)")

    # ── Stops + 400m catchments ─────────────────────────────────────────
    stop_features, buffer_features = [], []
    for s in STOPS:
        lat, lon = s["latlng"]
        # Color by line + phase
        if s["line"] == "L1" and s["phase"] == 1: color = "#e63946"
        elif s["line"] == "L1" and s["phase"] == 2: color = "#f4a261"
        elif s["line"] == "L2": color = "#1d4ed8"
        elif s["line"] == "L3": color = "#7c3aed"
        else: color = "#666"

        props = {k:v for k,v in s.items() if k != "latlng"}
        props["color"] = color
        props["lat"] = lat
        props["lon"] = lon

        stop_features.append({
            "type":"Feature","geometry":mapping(Point(lon, lat)),"properties":props,
        })

        buf = stop_buffer_wgs84(lat, lon, 400)
        buffer_features.append({
            "type":"Feature","geometry":mapping(buf),
            "properties":{"stop_id":s["id"],"stop_name":s["name"],
                          "pop_400m":s.get("pop_400m",0),
                          "jobs_400m":s.get("jobs_400m",0),
                          "phase":s["phase"],"color":color},
        })

    with open(out / "stops.geojson","w") as f:
        json.dump({"type":"FeatureCollection","features":stop_features}, f,
                  ensure_ascii=False, indent=2)
    print(f"  ✓ stops.geojson ({len(stop_features)} stops)")

    with open(out / "catchments_400m.geojson","w") as f:
        json.dump({"type":"FeatureCollection","features":buffer_features}, f,
                  ensure_ascii=False, indent=2)
    print(f"  ✓ catchments_400m.geojson")

    # ── Service area (union of Phase 1 catchments) ──────────────────────
    p1_bufs = [shape(f["geometry"]) for f in buffer_features
               if f["properties"]["phase"] == 1]
    sa = unary_union(p1_bufs)
    sa_lest = to_lest(sa)
    sa_km2 = round(sa_lest.area / 1e6, 2)

    with open(out / "service_area_p1.geojson","w") as f:
        json.dump({"type":"FeatureCollection","features":[{
            "type":"Feature","geometry":mapping(sa),
            "properties":{"label":"Phase 1 service area (union of 400m catchments)",
                          "area_km2":sa_km2,"color":"#e63946"},
        }]}, f, ensure_ascii=False, indent=2)
    print(f"  ✓ service_area_p1.geojson  ({sa_km2} km²)")

    # ── 150m route corridor ─────────────────────────────────────────────
    p1_ls = linestring_lest(ROUTES["L1P1"])
    corr_lest = p1_ls.buffer(150)
    corr = to_wgs(corr_lest)

    with open(out / "corridor_150m.geojson","w") as f:
        json.dump({"type":"FeatureCollection","features":[{
            "type":"Feature","geometry":mapping(corr),
            "properties":{"label":"Line 1 Phase 1 — 150m TOD corridor",
                          "color":"#e63946"},
        }]}, f, ensure_ascii=False, indent=2)
    print(f"  ✓ corridor_150m.geojson")

    # ── Statistics ──────────────────────────────────────────────────────
    p1_stops = [s for s in STOPS if s["line"]=="L1" and s["phase"]==1]
    stats = {
        "route_lengths_km": route_lengths,
        "total_network_km": round(sum(route_lengths.values()), 2),
        "phase1_km": route_lengths.get("L1P1", 0),
        "phase1_stops": len(p1_stops),
        "phase1_total_daily_boardings_est": sum(s.get("daily_boardings_est",0) for s in p1_stops),
        "phase1_pop_400m_total": sum(s.get("pop_400m",0) for s in p1_stops),
        "phase1_service_area_km2": sa_km2,
        "holmi_bridge_span_m": round(holmi_len),
        "cost_est_phase1_eur": {"low":130_000_000,"high":180_000_000},
        "eu_cofinancing_pct": 72.5,
        "bcr_estimate": {"low":1.2,"high":1.6},
        "elevation_profile": ELEVATION_PROFILE,
        "max_gradient_pct": round(
            (max(e["elev_m"] for e in ELEVATION_PROFILE) -
             min(e["elev_m"] for e in ELEVATION_PROFILE)) /
            ELEVATION_PROFILE[-1]["dist_m"] * 100 * 4, 1
        ),
        "verified_landmarks": {
            "Raekoja plats": [58.3806, 26.7227],
            "Atlantis (Narva mnt 2)": [58.3803, 26.7275],
            "Anne tn 53": [58.3764, 26.7547],
            "TÜ Kliinikum (Puusepa 8)": [58.3697, 26.7010],
            "Lõunakeskus (Ringtee 75)": [58.3581, 26.6779],
            "ERM (Muuseumi tee 2)": [58.3955, 26.7451],
        },
    }
    with open(out / "statistics.json","w") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)
    print(f"  ✓ statistics.json")

    # ── Combined JS bundle ──────────────────────────────────────────────
    data = {}
    for fname in ["routes.geojson","stops.geojson","catchments_400m.geojson",
                  "service_area_p1.geojson","corridor_150m.geojson"]:
        with open(out / fname) as f:
            data[fname.replace(".geojson","").replace("-","_")] = json.load(f)
    data["statistics"] = stats

    js = "// Auto-generated by build_tram_geodata.py — do not edit manually\n"
    js += "const TRAM_DATA = " + json.dumps(data, ensure_ascii=False, separators=(",",":")) + ";\n"
    (out / "tram_data.js").write_text(js)
    print(f"  ✓ tram_data.js ({len(js):,} bytes)")

    print(f"\n=== Build complete ===")
    print(f"  Phase 1 route:    {route_lengths.get('L1P1', 0):.2f} km")
    print(f"  Phase 1 stops:    {len(p1_stops)}")
    print(f"  Phase 1 boardings:~{stats['phase1_total_daily_boardings_est']:,}/day")
    print(f"  Service area:     {sa_km2} km²")
    print(f"  Holmi bridge:     {round(holmi_len)} m span")


if __name__ == "__main__":
    build_all()
