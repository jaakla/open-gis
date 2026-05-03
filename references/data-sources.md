# Data Sources

Strategies and concrete commands for discovering and acquiring open geospatial data. Discovery comes before download; STAC is the modern catalog protocol for raster, and Overture's STAC + GeoParquet pattern increasingly applies to vector basemaps too.

## Discovery hierarchy — try in this order

1. **Existing STAC catalogs** — for any raster, satellite, or EO data
2. **Overture Maps** — for global building, place, transportation, address basemap
3. **OpenStreetMap (via Overpass or extracts)** — for detailed local features Overture doesn't cover
4. **National / regional portals** — for authoritative or jurisdiction-specific data
5. **Specialist datasets** — building footprints (Microsoft, Google), elevation (Copernicus DEM), point clouds (OpenTopography)

Only fall back to ad-hoc downloads when the above don't cover the need.

## STAC — SpatioTemporal Asset Catalog

The default protocol for raster discovery. Every major satellite imagery provider now exposes a STAC API.

### Primary STAC endpoints

| Catalog | URL | Coverage |
|---|---|---|
| Microsoft Planetary Computer | `https://planetarycomputer.microsoft.com/api/stac/v1` | Sentinel, Landsat, MODIS, NAIP, climate, DEMs — broadest |
| Element 84 Earth Search | `https://earth-search.aws.element84.com/v1` | Sentinel-2 L2A on AWS COG |
| Copernicus Data Space | `https://catalogue.dataspace.copernicus.eu/stac` | Official Sentinel access |
| USGS LandsatLook | `https://landsatlook.usgs.gov/stac-server` | Landsat archive |
| Overture Maps | `https://labs.overturemaps.org/stac/catalog.json` | Vector basemap themes (addresses, buildings, places, transportation, divisions) |

### Search pattern (Python)

```python
from pystac_client import Client
import planetary_computer  # for MS PC, signs asset URLs

client = Client.open(
    "https://planetarycomputer.microsoft.com/api/stac/v1",
    modifier=planetary_computer.sign_inplace,
)
items = client.search(
    collections=["sentinel-2-l2a"],
    bbox=[24.5, 59.3, 25.0, 59.5],   # Tallinn area
    datetime="2025-06-01/2025-08-31",
    query={"eo:cloud_cover": {"lt": 20}},
).item_collection()
```

### Lazy load to xarray (preferred over manual download)

```python
import odc.stac
ds = odc.stac.load(
    items,
    bands=["red", "green", "blue", "nir"],
    chunks={"x": 1024, "y": 1024},  # dask-backed
    resolution=10,
    crs="EPSG:3301",  # reproject during load
)
# Now ds is lazy — only computes when .compute() or .to_zarr() is called
```

`stackstac` is an alternative; `odc-stac` is generally more featureful.

### Cost-aware planning

Use `estimate_data_size` (available via STAC MCP) or compute the bbox-clipped pixel count yourself before pulling. Sentinel-2 L2A at 10m resolution over a 1° bbox is roughly 10GB per scene — plan accordingly.

## Overture Maps — modern open vector basemap

Conflated open data (OSM + Meta + Esri + Microsoft + Google) under permissive licensing. Released monthly. Distributed as GeoParquet + PMTiles via STAC catalog.

Before writing a direct S3/Azure path, check the release calendar and use a release still present in the public buckets. Overture keeps only recent public releases for GDPR/right-to-be-forgotten reasons. For long-lived pipelines, pin the release and mirror the raw inputs or build an internal archive.

Themes:

* **addresses** — global address points
* **base** — water, land cover, infrastructure
* **buildings** — global building footprints with heights where available
* **divisions** — administrative boundaries
* **places** — POIs (categories in transition; use `basic_category` going forward)
* **transportation** — roads, segments, connectors

### Download (CLI)

```bash
# Install once
pip install overturemaps

# Bbox download for one theme
overturemaps download \
  --bbox=24.5,59.3,25.0,59.5 \
  --type=building \
  -f geoparquet \
  -o tallinn_buildings.parquet
```

### Query directly on S3 (preferred for ad-hoc analysis)

DuckDB reads Overture's GeoParquet over HTTP without downloading:

```sql
INSTALL httpfs; LOAD httpfs;
INSTALL spatial; LOAD spatial;

-- Replace OVERTURE_RELEASE with a currently retained release from:
-- https://docs.overturemaps.org/release-calendar/
-- Pin the chosen release in your manifest; do not commit "latest".
SELECT id, names.primary AS name, height, geometry
FROM read_parquet(
  's3://overturemaps-us-west-2/release/OVERTURE_RELEASE/theme=buildings/type=building/*.parquet',
  filename = true, hive_partitioning = 1
)
WHERE bbox.xmin > 24.5 AND bbox.xmax < 25.0
  AND bbox.ymin > 59.3 AND bbox.ymax < 59.5;
```

The `bbox` column is a struct (`xmin`, `ymin`, `xmax`, `ymax`) that Overture emits specifically to enable predicate pushdown. Filtering on it skips most of the parquet files entirely.

### License note

Overture data is mostly CDLA-Permissive 2.0, but Foursquare-sourced places are Apache 2.0, and OSM-derived data inherits ODbL obligations (share-alike + attribution). The `sources` array on each feature records provenance — preserve it.

## OpenStreetMap

Use when Overture doesn't have the feature class needed (footpaths, fine-grained POI tags, niche infrastructure), for very recent edits, or for tag-level richness Overture filters out.

### Choosing the access method

| Need | Tool |
|---|---|
| Small bbox, ad-hoc query, < 25k features | Overpass API |
| Country / region extract | Geofabrik, BBBike, NextGIS |
| Whole planet | planet.osm.pbf (~80GB compressed) |
| Iterative refinement, custom filters | `osmium` on a local extract |
| Routable graph for one shot | `osmnx` |

### Overpass via Python

```python
import overpy
api = overpy.Overpass()
result = api.query("""
[out:json][timeout:60];
area["ISO3166-1"="EE"]->.searchArea;
node(area.searchArea)["amenity"="cafe"];
out body;
""")
```

### Local extract + osmium (for anything serious)

```bash
# Pull Estonia from Geofabrik
wget https://download.geofabrik.de/europe/estonia-latest.osm.pbf

# Filter to POIs
osmium tags-filter estonia-latest.osm.pbf \
  n/amenity=cafe,restaurant,bar \
  -o estonia-food.osm.pbf

# Convert to GeoParquet via ogr2ogr
ogr2ogr -f Parquet estonia-food.parquet estonia-food.osm.pbf points
```

### Load OSM directly to PostGIS

```bash
osm2pgsql -d gisdb --slim -G --hstore -C 4000 \
  -S /usr/share/osm2pgsql/default.style \
  estonia-latest.osm.pbf
```

`--slim` keeps update-able tables; `-G` produces multipolygons; `-C 4000` is RAM cache in MB.

## Specialist data sources

### Building footprints

* **Microsoft Global Building Footprints** — global, public domain (ODbL where derived from OSM). Released as country-wise GeoJSON or GeoPackage on GitHub.
* **Google Open Buildings** — Africa, South Asia, SE Asia, LATAM. CSV + Parquet.
* **Overture Buildings** — conflates the above with OSM and is usually the simplest entry point now.

### Elevation

* **Copernicus DEM (GLO-30)** — 30m global, the modern default. Available via STAC on Microsoft Planetary Computer.
* **SRTM** — older but proven, 30m or 90m
* **National LiDAR-derived DTMs** — for any country with open LiDAR (Estonia: Maa-amet ~1m DTM under CC-BY)

### Point clouds

* **USGS 3DEP** — US LiDAR
* **OpenTopography** — research repository, global
* **National open LiDAR** — many EU countries, including Estonia (Maa-amet)
* Distributed as LAZ; cloud-native form is COPC

### Administrative, population, land cover, and mobility

* **Natural Earth** — small-scale countries, admin boundaries, populated places; public domain and ideal for global overview maps.
* **geoBoundaries** — research-grade administrative boundaries with explicit licensing; useful when national portals are inconsistent.
* **Overture divisions / OSM boundaries** — practical defaults for admin joins when official boundaries are not required.
* **GHSL / WorldPop** — population grids for exposure and accessibility analysis; record vintage, resolution, and license.
* **ESA WorldCover / Copernicus Land Monitoring** — open land-cover layers; note class schema and year.
* **GTFS feeds** — transit schedules for accessibility and routing; license varies by operator, so record feed URL, download date, and terms.

### OGC services and portals

For WMS/WFS/WMTS/OGC API endpoints, start with `GetCapabilities` or the landing page before guessing layer names. Record service URL, layer ID, CRS, time dimension, paging limit, and terms of use in the manifest.

Common traps:

* WMS 1.3.0 with `EPSG:4326` may use latitude/longitude bbox order; `CRS:84` uses longitude/latitude.
* WFS often needs `count`/`startIndex` paging and an explicit `outputFormat` such as GeoJSON or GML.
* WMTS tile matrix sets may not be Web Mercator; read the matrix set before constructing tile URLs.

## Estonia-specific sources (regional context)

* **Maa-amet (Estonian Land Board)** — geoportaal.maaamet.ee. WMS / WFS / WMTS endpoints. Topographic data, orthophotos, LiDAR DTMs, cadastre. Most data is open under CC-BY 4.0 with attribution to Maa-amet.
* **ETAK (Estonian topographic database)** — vector base data downloadable as Shapefile or GPKG.
* **X-tee** — government data exchange layer; some geospatial services exposed.
* **Default CRS for Estonia: EPSG:3301 (L-EST97 / Estonian Coordinate System of 1997)**. Convert from WGS84 with `pyproj` or `gdalwarp -t_srs EPSG:3301`.
* **Maa-amet WMS example:**
  ```
  https://kaart.maaamet.ee/wms/alus?
    SERVICE=WMS&VERSION=1.3.0&REQUEST=GetCapabilities
  ```

## MCP servers for catalog-driven discovery

For LLM-orchestrated workflows, these MCP servers replace manual catalog browsing:

| Server | Repo | What it does |
|---|---|---|
| **STAC MCP** | `BnJam/stac-mcp` | Search any STAC catalog (Microsoft PC by default), with `estimate_data_size` for lazy planning. Federated multi-catalog search. |
| **OSM MCP (Python)** | `jagan-shanmugam/open-streetmap-mcp` | Geocoding, POI search, routing primitives. Broad tool surface. |
| **OSM MCP (Go)** | `NERVsystems/osmmcp` | Performance-focused, OSRM + Nominatim under the hood. |
| **gis-mcp** | `mahdin75/gis-mcp` | Geometry ops + STAC-backed Sentinel/Landsat band downloads + map generation. |

A useful baseline configuration: STAC MCP + one OSM MCP + gis-mcp, plus a custom Overture or PostGIS MCP for organization-specific data.

### When discovery via MCP beats discovery via CLI

* Iterative refinement — "narrow this further", "what about this time window instead"
* Cross-catalog comparison
* Cost / size estimation before commit
* Mixing vector and raster discovery in one conversation

When the bbox and time window are already known and the task is purely batch ingestion, plain `pystac-client` is leaner.

## Reproducibility — pin everything

* Overture: pin release version, not `latest`; public buckets retain only recent releases, so mirror anything needed long term.
* STAC: pin item IDs in the manifest you save with the pipeline, not just (collection, bbox, time).
* OSM extracts: record the Geofabrik file timestamp.
* National data: record download date + portal version.

A `data-manifest.json` next to outputs is enough; full DVC / lakeFS is overkill for most pipelines but useful for production.
