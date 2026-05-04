# Data Sources

Strategies and concrete commands for discovering and acquiring open geospatial data. Discovery comes before download; STAC is the modern catalog protocol for raster, and Overture's STAC + GeoParquet pattern increasingly applies to vector basemaps too.

## Discovery hierarchy — try in this order

1. **Existing STAC catalogs** — for any raster, satellite, or EO data
2. **Overture Maps** — for global building, place, transportation, address basemap
3. **OpenStreetMap (via Overpass or extracts)** — for detailed local features Overture doesn't cover
4. **National / regional portals** — for authoritative or jurisdiction-specific data
5. **Specialist datasets** — building footprints (Microsoft, Google), elevation (Copernicus DEM), point clouds (OpenTopography), weather/climate (ECMWF, NOAA)

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
* **buildings** — global building footprints with heights. Note that `theme=buildings` contains both `type=building` and `type=building_part` (useful for 3D mapping).
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

> [!WARNING]
> Public Overpass API instances (`overpass-api.de`, etc.) are heavily rate-limited. For large areas (e.g., country-wide), always download a Geofabrik extract instead of slamming the public API.

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

### Weather and Climate

* **Copernicus Climate Data Store (CDS)** — ERA5 reanalysis, seasonal forecasts. Usually distributed as NetCDF/GRIB.
* **ECMWF Open Data** — forecast models, real-time data.
* **NOAA AWS Registry** — GFS, HRRR, NEXRAD radar (often available as Zarr or NetCDF).

### Administrative, population, land cover, and mobility

* **Natural Earth** — small-scale countries, admin boundaries, populated places; public domain and ideal for global overview maps.
* **geoBoundaries** — research-grade administrative boundaries with explicit licensing; useful when national portals are inconsistent.
* **Overture divisions / OSM boundaries** — practical defaults for admin joins when official boundaries are not required.
* **GHSL / WorldPop** — population grids for exposure and accessibility analysis; record vintage, resolution, and license.
* **ESA WorldCover / Copernicus Land Monitoring** — open land-cover layers; note class schema and year.
* **GTFS feeds** — transit schedules for accessibility and routing; license varies by operator, so record feed URL, download date, and terms.

### Place identifiers and global addressing

Prefer stable identifiers over name-only joins. Store the namespace with the ID (`unlocode`, `geonames`, `wikidata`, `osm`, `wof`, provider-specific place ID) so IDs from different systems cannot be confused.

* **UN/LOCODE** — United Nations code for trade and transport locations; use for ports, airports, terminals, logistics hubs, and shipping/trade analytics.
* **GeoNames ID** — broad global gazetteer identifier for populated places and physical features; useful for coarse global joins and fallback search.
* **Wikidata QID** — cross-domain entity identifier; useful for linking places to external facts, but geometry and admin hierarchy quality varies.
* **OpenStreetMap IDs** — useful for OSM-derived workflows; store element type (`node`, `way`, `relation`) because numeric IDs are not globally unique across types.
* **Who's On First IDs** — useful for place hierarchies and historical/admin boundary context.
* **Provider place IDs** — Google Place IDs, HERE IDs, Mapbox IDs, etc. are service-specific; check storage and reuse terms before persisting them.
* **Open Location Code / Plus Codes** — open, offline-computable global addressing code for places without reliable street addresses. A full code identifies an area, not a parcel or legal address; short codes need a reference locality.
* **Postcodes / ZIP codes** — postal geography is operational, not always polygonal or stable. Use national authoritative datasets where possible; for global coverage consider GeoNames postal codes, OpenAddresses, provider APIs, or paid postal-code datasets.

### OGC services and portals

For WMS/WFS/WMTS/OGC API endpoints, start with `GetCapabilities` or the landing page before guessing layer names. Record service URL, layer ID, CRS, time dimension, paging limit, and terms of use in the manifest.

Common traps:

* WMS 1.3.0 with `EPSG:4326` may use latitude/longitude bbox order; `CRS:84` uses longitude/latitude.
* WFS often needs `count`/`startIndex` paging and an explicit `outputFormat` such as GeoJSON or GML.
* WMTS tile matrix sets may not be Web Mercator; read the matrix set before constructing tile URLs.

## Estonia-specific sources (regional context)

* **Maa- ja Ruumiamet (Estonian Land and Spatial Development Board, formerly Maa-amet)** — geoportaal.maaamet.ee. WMS / WFS / WMTS endpoints. Topographic data, orthophotos, LiDAR DTMs, cadastre. Most data is open under CC-BY 4.0 with attribution to Maa-amet.
* **ETAK (Estonian Topographic Database)** — vector base data, downloadable as Shapefile / GPKG and also served via WFS. Layers cover 39 themes (kõlvikud / teed / veekogud / ehitised / pinnavormid).
* **X-tee** — government data exchange layer; some geospatial services exposed.
* **Default CRS for Estonia: EPSG:3301 (L-EST97 / Estonian Coordinate System of 1997)**. Convert from WGS84 with `pyproj` or `gdalwarp -t_srs EPSG:3301`.

### ETAK WFS — programmatic access

The legacy `https://teenus.maaamet.ee/ows/wfs_etak` endpoint that older docs reference is dead. The live WFS lives on the Environment Agency's GeoServer at:

```
https://gsavalik.envir.ee/geoserver/etak/wfs
```

Layer naming is `etak:e_<code>_<name>_<geom>`, where `<geom>` is `j` (joon / line), `p` (punkt / point), `a` (ala / area / polygon), or `ka` (kinnine ala / closed-polygon area). The most useful layers:

| Layer | Theme |
|---|---|
| `etak:e_401_hoone_ka` | Buildings (hoone) — polygons |
| `etak:e_404_maaalune_hoone_ka` | Underground buildings |
| `etak:e_402_korgrajatis_p` | Tall structures (towers, masts) |
| `etak:e_501_tee_j` / `_a` | Roads — line / area |
| `etak:e_502_roobastee_j` | Railway lines |
| `etak:e_201_meri_a` / `e_202_seisuveekogu_a` / `e_203_vooluveekogu_a` | Sea / lakes / rivers |
| `etak:e_303_haritav_maa_a` / `e_305_puittaimestik_a` | Cropland / forest |

**Building filter (residential):** the `e_401_hoone_ka` schema carries `tyyp` (Estonian use-type code). Filter `tyyp = 10` (Elu- või ühiskondlik hoone — residential or public) and require `ehr_gid IS NOT NULL` to drop foundation-only outlines (`tyyp = 30`). Use `ads_lahiaadress` for the postal address. Curl-ready example for a Tartu bbox in EPSG:3301:

```bash
curl "https://gsavalik.envir.ee/geoserver/etak/wfs?service=WFS&version=2.0.0&request=GetFeature\
&typeNames=etak:e_401_hoone_ka\
&srsName=EPSG:4326\
&outputFormat=application/json\
&bbox=657000,6471000,670000,6480000,EPSG:3301\
&count=5000"
```

GeoServer pages 5000 features at a time by default; loop with `startIndex` until a page returns fewer than the page size.

### Estonian OSM admin levels

Estonia uses different `admin_level` values from the OSM defaults most generic docs assume. If you copy a query that says `admin_level=8` for a city, you'll get nothing for any Estonian municipality.

| `admin_level` | Estonia meaning |
|---|---|
| 4 | Country (Eesti) |
| 6 | Maakond (county) |
| 7 | Linn / vald (municipality) — the right level for "city of Tartu", "city of Tallinn" |
| 8 | Asustusüksus (settlement unit, optional) |
| 9 | Sub-area, neighbourhood (rare) |

Concrete relation IDs: Tartu linn = `351439` (al=7), Tartu maakond = `351246` (al=6), Tallinn = `2618383` (al=7). Stable OSM relation IDs are far easier to query than name + `admin_level` filters. Overpass example:

```
[out:json][timeout:60];
relation(351439);                 -- Tartu linn (admin_level=7)
out geom;
```

> [!NOTE]
> **Post-2017 administrative reform:** the 2017 reform consolidated 213 municipalities to 79; many city polygons absorbed surrounding rural land. Modern *Tartu linn* is ~154 km², not the historic ~38 km² urban core. Always check the polygon area before assuming "the city" matches the historic centre — building counts and POI density estimates that assume the small polygon will be wildly off.

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
