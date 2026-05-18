# Tartu Tramline GIS Deliverables — How to Use

## What's in this folder

```
open-gis/
├── tartu-tramline-analysis.md     ← Full written analysis (route, costs, BCR)
├── tartu-tramline-map.html        ← Interactive web map (open in any browser)
├── tartu-tramline.qgs             ← QGIS 3.x project — open this in QGIS
│
├── data/
│   ├── tartu_tramline.gpkg        ← All layers in a single GeoPackage
│   ├── tram_data.js               ← JS bundle consumed by the HTML map
│   ├── routes.geojson             ← 5 route features (Line 1 P1/P2, L2, L3, bridge)
│   ├── stops.geojson              ← 21 stop points with passenger / job stats
│   ├── catchments_400m.geojson    ← 400 m walking-distance buffers per stop
│   ├── service_area_p1.geojson    ← Phase 1 union service area (~4.6 km²)
│   ├── corridor_150m.geojson      ← 150 m TOD influence zone
│   ├── statistics.json            ← Network stats, elevation profile, costs
│   └── styles/                    ← QGIS .qml style files (one per layer)
│
└── scripts/
    ├── build_tram_geodata.py      ← Regenerate all GeoJSON + GeoPackage
    └── build_qgis_project.py      ← Regenerate the .qgs project file
```

---

## Option A — Open the interactive web map

**Just double-click `tartu-tramline-map.html`** — it opens in any browser.

The map fetches map tiles, Leaflet, Chart.js, Turf.js, and the Maa-amet WMS
direct from CDNs/Estonia, so you need an internet connection but no server.

Features in the browser map:
- 6 toggleable basemaps: CARTO Light (default), Voyager, Dark, OSM, plus
  **Maa-amet WMS orthophoto** and **Maa-amet topographic** (Estonian Land Board)
- 4 tram lines (toggleable per phase / line)
- Numbered stop markers, demand circles (∝ daily boardings)
- Toggleable 400 m catchments, service area, 150 m corridor
- "Fetch OSM Streets" button — pulls live Overpass API street geometry
- Click anywhere → Turf.js highlights nearest stop
- Sidebar shows live network statistics
- Bottom panel: elevation profile chart (Mõisavahe → Lõunakeskus)

---

## Option B — Open the QGIS project

**File → Open → `tartu-tramline.qgs`** in QGIS 3.x.

Everything loads automatically:
- Project CRS: **EPSG:3301** (L-EST97 / Estonian Coordinate System)
- 5 vector layers (styled, ready to view) from `data/tartu_tramline.gpkg`
- **Maa-amet WMS** orthophoto basemap (enabled by default)
- **Maa-amet WMS** topographic basemap (toggleable)
- Initial extent: central Tartu including the entire Phase 1 route

Layer order in the Layers panel (top to bottom):
1. **Tram Stops** — Points, styled by phase/terminus/highlight, labelled
2. **Tram Routes** — Lines styled by category (L1P1 red, L1P2 orange, L2 blue, L3 purple, bridge green)
3. **150 m Route Corridor** — off by default, toggle for TOD analysis
4. **Stop Catchments 400 m** — off by default, toggle for walk-distance analysis
5. **Phase 1 Service Area** — off by default, the union polygon
6. **Maa-amet Topographic** — WMS, off by default
7. **Maa-amet Orthophoto** — WMS, on by default

### Quick QGIS workflows

**Calculate population served along the route:**
1. Enable "Stop Catchments 400 m" layer
2. Open Field Calculator → expression `sum("pop_400m")` over the layer
3. Result: ~46,000 residents within walking distance of Phase 1

**Measure route length in EPSG:3301:**
1. Open Tram Routes attribute table
2. Field `length_km` shows precomputed lengths

**Add Tartu Statistics Estonia population grid:**
1. Browser → XYZ Tiles → Add new connection
2. Or: `Layer → Add Layer → Add WMS/WMTS Layer → use Maa-amet endpoints`
   from <https://geoportaal.maaamet.ee/>

---

## Option C — Add layers manually (any GIS tool)

The **GeoPackage** (`data/tartu_tramline.gpkg`) is a single file containing
all 5 layers (routes, stops, catchments_400m, service_area_p1, corridor_150m).
It works with QGIS, ArcGIS, R `sf`, Python `geopandas`, OGR, etc.

```python
import geopandas as gpd
stops    = gpd.read_file("data/tartu_tramline.gpkg", layer="stops")
routes   = gpd.read_file("data/tartu_tramline.gpkg", layer="routes")
catch    = gpd.read_file("data/tartu_tramline.gpkg", layer="catchments_400m")
service  = gpd.read_file("data/tartu_tramline.gpkg", layer="service_area_p1")
corridor = gpd.read_file("data/tartu_tramline.gpkg", layer="corridor_150m")
```

Individual GeoJSON files in `data/` work standalone too.

To apply the bundled styling in QGIS:
- Right-click layer → Properties → Symbology
- Click **Style → Load Style…** (bottom of dialog)
- Pick the matching `.qml` from `data/styles/`

---

## Option D — Regenerate everything

If you edit coordinates, demand estimates or add stops:

```bash
# 1. Regenerate all GeoJSON + GeoPackage
python3 scripts/build_tram_geodata.py

# 2. Optionally fetch live OSM street network (requires internet + osmnx)
python3 scripts/build_tram_geodata.py --osm

# 3. Regenerate QGIS project file
python3 scripts/build_qgis_project.py
```

Required Python packages: `geopandas shapely pyproj pyogrio`
(install via `pip install geopandas shapely pyproj pyogrio`).

---

## Data sources

- 2020 feasibility study: [Tartu linna kergrööbastranspordi teostatavus- ja tasuvusanalüüs](https://www.tartu.ee/sites/default/files/research_import/2020-03/Tartu%20linna%20kergr%C3%B6%C3%B6bastranspordi%20teede%20m%C3%A4%C3%A4ramine%20ning%20teostatavus-%20ja%20tasuvusanal%C3%BC%C3%BCs.pdf) (Civitta/Artes Terrae/Stratum)
- General plan: [Tartu Üldplaneering 2040+](https://tartu.ee/et/uldplaneering2040)
- Basemap: [Maa-amet (Estonian Land Board) WMS](https://kaart.maaamet.ee/wms/alus)
- All coordinates verified against actual street addresses (Raekoja plats,
  Atlantis Narva mnt 2, TÜ Kliinikum L. Puusepa 8, Lõunakeskus Ringtee 75,
  ERM Muuseumi tee 2).

CRS notes: All exports in **EPSG:4326** (WGS84); all area/distance analysis
performed in **EPSG:3301** (Estonian L-EST97, metre units).
