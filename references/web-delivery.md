# Web Delivery

Generating tiles, serving them, and rendering them. The 2026 default stack: PMTiles + Martin (or static hosting) + MapLibre. tippecanoe for vector tile generation, TiTiler for dynamic raster tiling.

## Map review rules

Rendered maps are validation surfaces. Do not claim visual insights until the map, screenshot, notebook view, or browser view has been rendered and inspected.

* Choose the layer for the question, not just the data shape: points for positions, H3/hexbin for normalized density, lines for paths, arcs for origin-destination flows, polygons for regions, and 3D only when height carries useful magnitude.
* Avoid redundant layers: do not stack heatmap and points for the same signal unless each answers a distinct question; do not color both fill and outline by the same metric.
* Use restrained defaults: point radius 2-4 px, dense point opacity around 0.6-0.8, network stroke 0.5-1.5 px, polygon borders 0.5-1 px, and lower opacity for overlapping flows.
* Use perceptual palettes: sequential for magnitude, diverging for signed values with a meaningful midpoint, qualitative for small category sets; avoid rainbow/jet.
* Lock the initial view to the scale where the insight is visible. A city-scale result should not open at world view.
* Inspect for blank layers, wrong CRS, swapped latitude/longitude, bbox-only rectangular overshoot, duplicate features, missing coverage, and broken attribution before delivery.

## Choosing the renderer

MapLibre is the default for delivered web maps. Choose an alternative renderer according to the job the view must do:

1. **Exploration / analyst first look → kepler.gl or lonboard.** Use kepler.gl for drag-and-drop exploration, interactive filters, time playback, H3/hexbin views, flows, and quick 3D. Use lonboard when the work is already in a Python notebook and the data is in GeoPandas, GeoArrow, GeoParquet, or DuckDB.
2. **Delivered web map / reproducible dashboard → MapLibre + PMTiles.** This preserves the default one-static-HTML-plus-one-PMTiles delivery shape, needs no application build step, and maps cleanly from the canonical `presentation` block to reviewable style layers and controls.
3. **Advanced visualization in a delivered map → MapLibre + deck.gl.** Add a deck.gl overlay for H3/hexbin density, origin-destination arcs, 3D magnitude, or roughly more than 500,000 simultaneously displayed raw features. The threshold is a trigger to profile, not a guarantee: geometry complexity, layer type, browser, and hardware still determine the limit.

kepler.gl does not replace MapLibre: it is an exploratory React/Redux application built on MapLibre and deck.gl. Its larger runtime, UI state, and generated configuration make it a poor default for a minimal static deployment or deterministic print/headless output. Keep `project.yaml` and its `presentation` block canonical regardless of renderer; renderer-specific state is a generated artifact, never the analytical source of truth.

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
# Note: FlatGeobuf is also an excellent intermediate format for piping to tippecanoe
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

> [!WARNING]
> While PMTiles rely on HTTP 206 Range Requests, many CDNs (including Cloudflare) **do not cache range requests by default**. You must configure specific page rules, cache rules, or workers to cache these requests, or every map pan will hit your origin bucket and inflate egress costs.

#### Local development: Python's `http.server` is not enough

Python's stdlib `http.server` (`python3 -m http.server`) **does not support HTTP Range requests** — it returns `200 OK` with the full body for any range query, which silently breaks the PMTiles protocol on every map pan. For local testing pick one of:

* `npx serve` — supports range requests out of the box.
* `caddy file-server` — single-binary, range-aware.
* `RangeHTTPServer` (PyPI) — `pip install rangehttpserver` and run as `python -m RangeHTTPServer 8080`. (Module activation can be finicky depending on Python version; verify a 206 response with `curl -I -H "Range: bytes=0-127" .../foo.pmtiles` before debugging anything else.)
* A 30-line `SimpleHTTPRequestHandler` subclass that handles `Range`. Sample skeleton:

```python
class RangeHandler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()

    def send_head(self):
        rng = self.headers.get("Range")
        if not rng or not rng.startswith("bytes="):
            return super().send_head()
        path = self.translate_path(self.path)
        size = os.path.getsize(path)
        s_str, e_str = rng[6:].split("-", 1)
        start = int(s_str) if s_str else 0
        end   = int(e_str) if e_str else size - 1
        f = open(path, "rb"); f.seek(start)
        self._range = (start, end, size)
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        return f
    # plus a copyfile() override that respects self._range
```

When a PMTiles map "loads but is blank" locally, this is by far the most common cause — confirm with DevTools that PMTiles requests are returning `206 Partial Content` before chasing style or schema issues.

Static hosting checklist:

* Enable HTTP range requests and configure CDN caching explicitly for 206 responses.
* Set CORS for the map origin, including `GET` and `HEAD`.
* Keep attribution visible in the UI for OSM, Overture, Sentinel, national datasets, and basemap providers.
* Record the PMTiles URL, data release, layer names, bounds, min/max zoom, and attribution in the manifest.

## tippecanoe — vector tile generation

The de facto industry standard. Handles zoom-level generalization, attribute filtering per zoom, layer merging.

Vector tile hygiene:

* Prune attributes before tiling; every property is repeated across tiles.
* Keep source-layer names stable and verify them with `pmtiles show` before writing MapLibre styles.
* Watch tile size warnings from tippecanoe. Prefer simplification, zoom-dependent filters, and attribute pruning over blindly dropping important classes.
* Set min/max zooms based on feature meaning, not just file size.

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

// MapLibre also natively supports 3D terrain via RGB DEM tiles
// map.addSource('terrain', { type: 'raster-dem', url: 'pmtiles://...dem.pmtiles' });
// map.setTerrain({ source: 'terrain', exaggeration: 1.5 });
</script>
```

## deck.gl overlay — advanced delivered visualization

Use deck.gl when the delivered view needs a visualization layer MapLibre does not express well on its own: aggregated hexagons, high-volume points, arcs, trips, or data-driven 3D. Keep MapLibre as the basemap and camera, and add deck.gl with [`MapboxOverlay`](https://deck.gl/docs/developer-guide/base-maps/using-with-maplibre). Interleaved mode lets deck.gl layers participate in the same WebGL2 scene so MapLibre labels and 3D objects can be ordered correctly.

This example adds an extruded density layer to an existing `map` created by MapLibre:

```javascript
import {MapboxOverlay} from '@deck.gl/mapbox';
import {HexagonLayer} from '@deck.gl/aggregation-layers';

const overlay = new MapboxOverlay({
  interleaved: true,
  layers: [
    new HexagonLayer({
      id: 'demand-density',
      data: points,
      getPosition: d => [d.longitude, d.latitude],
      getWeight: d => d.magnitude,
      radius: 250,
      extruded: true,
      elevationScale: 20,
      pickable: true,
    }),
  ],
});

map.addControl(overlay);
```

`points` should be a declared, deterministic project output in EPSG:4326. Pin the deck.gl version and all layer parameters, including aggregation radius, elevation scale, filters, layer order, and initial view. Browser code may visualize or re-filter pipeline measurements, but it must not become a second metric-analysis engine. Render and inspect the result, and assert that every expected overlay layer loaded; a successful bundle with a missing layer is a failed delivery.

## Exploration — kepler.gl and lonboard

### kepler.gl

[kepler.gl is actively maintained](https://github.com/keplergl/kepler.gl/commits/master/), with repository commits through July 2026. Its [Vector Tile layer](https://github.com/keplergl/kepler.gl/blob/master/docs/user-guides/c-types-of-layers/README.md#vector-tile-layer) accepts both MVT URL templates and remote vector PMTiles, so tile compatibility is no longer a reason to exclude it.

Use it for an analyst's first look when fast UI-driven iteration matters: drag in data, test filters, scrub time, compare aggregations, inspect origin-destination flows, and try 3D before deciding what the reproducible deliverable should contain. It is [built on MapLibre + deck.gl](https://github.com/keplergl/kepler.gl) and adds a React/Redux application and its own UI state; it is therefore an exploration environment, not a simpler rendering core.

Do not paste a saved kepler.gl session blob into `project.yaml`. If a project retains a kepler.gl view, generate its config from the canonical `presentation` semantics, pin the kepler.gl package/config version, and store the config as a derived output. In browser validation, assert that every expected dataset and layer ID exists after load, check filters and initial view, and inspect a rendered screenshot. Treat a silently dropped layer as a failed output even if the application itself did not raise an error.

### lonboard

Use lonboard for notebook-native exploration when the analysis already produces GeoPandas, GeoArrow, GeoParquet, or DuckDB results. Its [Arrow-backed path](https://developmentseed.org/lonboard/latest/how-it-works/) sends data to deck.gl without a GeoJSON text round trip and keeps exploratory visualization close to the Python analysis:

```python
import geopandas as gpd
from lonboard import viz

results = gpd.read_parquet("data/derived/results.parquet")
viz(results)
```

lonboard is an exploratory notebook view, not the canonical delivered dashboard. Any styling, filtering, or view choice accepted for delivery must be recorded in `project.yaml` and regenerated through the selected web renderer.

## Other web rendering tools

* **OpenLayers** — feature-rich, especially strong on OGC services (WMS/WFS/WMTS) and projections beyond Web Mercator.
* **Leaflet** — lightweight, simple, ubiquitous; doesn't render vector tiles natively (needs `leaflet.vectorgrid` plugin).

## Style and basemap sources (open)

For global production basemaps, first decide whether self-hosted PMTiles/vector tiles or a managed basemap service is more appropriate. See `services-and-scale.md`.

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

Smoke test before handing off:

* `pmtiles show data.pmtiles` reports expected bounds, zooms, and layer names.
* Browser devtools show `206 Partial Content` or equivalent ranged responses from the static origin.
* MapLibre style uses the exact `source-layer` from the tiles.
* Attribution, legend, empty-state handling, and mobile viewport behavior are visible.

## When NOT to pre-tile

* Data updates more often than every few hours → use Martin in front of PostGIS
* Per-user filtering or styling that can't be expressed as Mapbox style filters → server-side rendering or dynamic SQL via Martin functions
* Raster with on-demand band math / colormap → TiTiler
