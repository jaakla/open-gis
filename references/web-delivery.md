# Web Delivery

Generating tiles, serving them, and rendering them. The 2026 default stack: PMTiles + Martin (or static hosting) + MapLibre. tippecanoe for vector tile generation, TiTiler for dynamic raster tiling.

## Decision tree

1. **Static deployment, no server budget?** → Generate PMTiles, host on S3 / R2 / any static origin, fetch with MapLibre via the PMTiles protocol plugin.
2. **Dynamic data in PostGIS, multi-user?** → Martin in front of PostGIS, MVT generation in-database with `ST_AsMVT`.
3. **On-the-fly raster tiling from COGs?** → TiTiler (FastAPI-based) with on-demand resampling/styling.
4. **Mixed vector + raster, batch generation, very large dataset?** → Planetiler for OSM-derived vector at planet scale; tippecanoe for everything else.

## PMTiles — the modern default

Single file containing all zoom levels, served from any HTTP origin via range requests. No tile server needed for reads. Both vector (MVT) and raster.

### Why PMTiles

* No tile server to operate at read time
* CDN-friendly (each range request is a normal HTTP GET)
* Trivial to deploy: upload one file
* Open spec, multiple compatible writers and readers

### Generating PMTiles

From GeoJSON or GeoParquet via `tippecanoe` (now `felt/tippecanoe`):

```bash
# Basic generation
tippecanoe -o output.pmtiles \
  --maximum-zoom=14 --minimum-zoom=4 \
  -l layer_name \
  input.geojson

# Smarter: drop densest features at low zooms automatically
tippecanoe -o output.pmtiles \
  -zg --drop-densest-as-needed \
  --extend-zooms-if-still-dropping \
  -l buildings \
  buildings.geojson

# From GeoParquet via ogr2ogr → ndjson pipe
ogr2ogr -f GeoJSONSeq /vsistdout/ buildings.parquet | \
  tippecanoe -o buildings.pmtiles -l buildings -zg --drop-densest-as-needed
```

Key tippecanoe flags:

| Flag | Effect |
|---|---|
| `-zg` | Choose max zoom automatically based on feature density |
| `-Z N -z M` | Min and max zoom levels |
| `--drop-densest-as-needed` | At lower zooms, randomly drop densest features to fit tile size |
| `--coalesce-densest-as-needed` | Merge overlapping features to reduce density |
| `--simplification=N` | Simplify geometries (default 4) |
| `--detect-shared-borders` | Polygon topology preservation |
| `--extend-zooms-if-still-dropping` | Auto-add zoom levels until feature density is acceptable |

### Inspecting PMTiles

```bash
pmtiles show output.pmtiles
pmtiles extract output.pmtiles subset.pmtiles --bbox=24.5,59.3,25.0,59.5
```

### Serving PMTiles statically (the simplest deployment)

Upload to S3 / R2 / Backblaze with public read. Configure CORS to allow the rendering origin. That's the entire backend.

## tippecanoe — vector tile generation

The de facto industry standard. Handles zoom-level generalization, attribute filtering per zoom, layer merging.

### Generalization patterns

```bash
# Zoom-dependent feature filtering with a JSON filter
tippecanoe -o out.pmtiles \
  --feature-filter='{ "*": [ "any", [">=", "$zoom", 10], [">", "population", 10000] ] }' \
  cities.geojson

# Different layers from different sources
tippecanoe -o combined.pmtiles \
  -L'{"file":"buildings.geojson", "layer":"buildings", "minzoom":12}' \
  -L'{"file":"roads.geojson", "layer":"roads", "minzoom":4}'
```

## Planetiler — when tippecanoe is too slow

For OSM-derived vector tiles at country or planet scale, **Planetiler** (Java) is dramatically faster than tippecanoe — it can generate planet-scale OpenMapTiles in a few hours on a single machine.

```bash
java -jar planetiler.jar --download \
  --osm-path=planet.osm.pbf \
  --output=planet.pmtiles
```

Use Planetiler's profiles (OpenMapTiles, Shortbread, custom). Use tippecanoe for everything that isn't a global OSM-style basemap.

## Tile servers

When data is dynamic or in a database, a tile server beats pre-generation.

| Server | Backend | Strengths |
|---|---|---|
| **Martin** | PostGIS, MBTiles, PMTiles, COG | Rust, very fast, modern, MapLibre-built |
| **pg_tileserv** | PostGIS only | Minimal, single binary, CrunchyData |
| **TiPg** | PostGIS | OGC API Features-aligned |
| **TiTiler** | COG, STAC, MosaicJSON | Dynamic raster tiling, on-the-fly band math, FastAPI |
| **tegola** | PostGIS | Older Go option |
| **tileserver-gl** | MBTiles + raster | Style-aware, includes static rendering |

### Martin in front of PostGIS

```bash
# Auto-discover all tables with geometry columns
docker run -d -p 3000:3000 \
  -e DATABASE_URL=postgres://user:pass@host/db \
  ghcr.io/maplibre/martin

# Tiles available at /<schema>.<table>/{z}/{x}/{y}
# Style URL: http://localhost:3000/buildings
```

Martin auto-generates MVT from any geometry column. For complex tiles, write a SQL function returning `bytea` (an MVT) and register it.

### TiTiler for COG / STAC

```bash
docker run -d -p 8000:8000 ghcr.io/developmentseed/titiler:latest

# Serve a COG
http://localhost:8000/cog/tiles/{z}/{x}/{y}.png?url=https://example.com/data.tif

# With band math
http://localhost:8000/cog/tiles/{z}/{x}/{y}.png?url=...&expression=(b8-b4)/(b8+b4)
```

TiTiler supports STAC items directly — useful for serving satellite imagery without pre-tiling.

## MapLibre GL — the rendering layer

The open fork of Mapbox GL JS (and native equivalents). Default rendering choice.

### Loading PMTiles

```html
<script src="https://unpkg.com/maplibre-gl@4/dist/maplibre-gl.js"></script>
<script src="https://unpkg.com/pmtiles@3/dist/pmtiles.js"></script>
<link rel="stylesheet" href="https://unpkg.com/maplibre-gl@4/dist/maplibre-gl.css"/>

<div id="map" style="height: 500px"></div>
<script>
const protocol = new pmtiles.Protocol();
maplibregl.addProtocol("pmtiles", protocol.tile);

const map = new maplibregl.Map({
  container: 'map',
  style: {
    version: 8,
    sources: {
      buildings: {
        type: 'vector',
        url: 'pmtiles://https://your-cdn.example.com/buildings.pmtiles',
      }
    },
    layers: [
      { id: 'bg', type: 'background', paint: { 'background-color': '#fff' } },
      {
        id: 'buildings-fill',
        type: 'fill',
        source: 'buildings',
        'source-layer': 'buildings',
        paint: { 'fill-color': '#888', 'fill-opacity': 0.6 }
      }
    ]
  },
  center: [24.754, 59.437],
  zoom: 12,
});
</script>
```

## Other rendering / overlay tools

* **deck.gl** — for large-scale data visualizations (millions of points, hex bins, arc layers). Pairs well with MapLibre as a base.
* **OpenLayers** — feature-rich, especially strong on OGC services (WMS/WFS/WMTS) and projections beyond Web Mercator.
* **Leaflet** — lightweight, simple, ubiquitous; doesn't render vector tiles natively (needs `leaflet.vectorgrid` plugin).

## Style and basemap sources (open)

* **Protomaps** — open basemap styles + global PMTiles. Free for non-commercial; modest fee for commercial.
* **OpenMapTiles** — open style/schema, generate your own tiles via Planetiler
* **MapTiler** — has a free tier; styles are open even when hosted tiles are paid
* **MapLibre demo styles** — minimal starter styles in the MapLibre repo
* **OpenStreetMap raster tiles** (`tile.openstreetmap.org`) — usable for low-volume only; check tile usage policy

## OGC API services

When standards-compliant interoperability matters more than performance:

* **GeoServer** — mature, comprehensive, Java
* **pygeoapi** — modern, OGC API-first, Python
* **MapServer** — long-established C-based stack
* **QGIS Server** — serve QGIS projects directly as WMS/WFS/WMTS

OGC API Tiles is increasingly served by Martin, pg_tileserv, and pygeoapi alongside the legacy WMTS spec.

## Static cartography output

Not all maps go on the web. For PNG/PDF print output:

* **QGIS print composer** — the production cartographic tool (see `qgis.md`)
* **matplotlib + contextily** — scientific figures with basemaps
* **PrettyMaps** — opinionated generative cartographic Python library
* **MapLibre Native** + headless rendering — for repeatable web-style output as raster

## Common end-to-end pipeline

"From GeoParquet to web map":

```
1. Source data (Overture, OSM, in-house) → GeoParquet
2. ogr2ogr -f GeoJSONSeq | tippecanoe -o data.pmtiles -zg
3. Upload data.pmtiles to S3 with public read + CORS
4. MapLibre style.json with pmtiles:// source
5. Static HTML page hosted anywhere
```

Cost: storage of one file. No tile server. No database. Scales to millions of tile requests via CDN.

## When NOT to pre-tile

* Data updates more often than every few hours → use Martin in front of PostGIS
* Per-user filtering or styling that can't be expressed as Mapbox style filters → server-side rendering or dynamic SQL via Martin functions
* Raster with on-demand band math / colormap → TiTiler
