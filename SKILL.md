---
name: open-gis
description: "Use this skill for any geospatial or GIS work using free and open source tools and data — building spatial data pipelines; analyzing or processing satellite imagery, LiDAR, vector data, or raster data; working with formats like GeoParquet, Cloud Optimized GeoTIFF (COG), PMTiles, Shapefile, GeoPackage, GeoTIFF, LAZ, or COPC; generating vector or raster tiles or web maps; performing CRS transformations; routing or isochrone analysis; terrain, hydrology, or point cloud analysis; or orchestrating workflows across GDAL/OGR, GeoPandas, Shapely, xarray/rioxarray, DuckDB Spatial, PostGIS, QGIS, OSRM/Valhalla/GraphHopper, tippecanoe, Martin, MapLibre, or STAC catalogs. Trigger this skill whenever the user mentions GIS, geospatial, OpenStreetMap (OSM), Overture Maps, Sentinel, Landsat, satellite imagery, LiDAR, GeoTIFF, shapefile, GeoPackage, raster, vector tiles, geocoding, isochrones, spatial joins, coordinate reference systems, EPSG codes, projections, basemaps, or any specific tool from the open geospatial stack — even when they don't use the word 'GIS' explicitly. Also trigger for tasks involving Estonia-specific data (Maa-amet, ETAK, EPSG:3301 / L-EST97), regional data portals, or INSPIRE datasets. Do NOT trigger for simple location lookups ('what city is this in?'), turn-by-turn directions for travel planning, or casual map references that don't involve analytical or production GIS work."
---

# Open GIS Toolkit

Production-grade geospatial workflows using only free and open source tools and open data. Cloud-native by default: STAC for discovery, GeoParquet + COG + PMTiles for storage, DuckDB and PostGIS for compute, MapLibre and Martin for delivery.

## Modules — read the relevant reference(s) before starting work

| If the task involves... | Read |
|---|---|
| Finding or sourcing data (OSM, Overture, Sentinel, Landsat, building footprints, regional portals, STAC catalogs, MCP-based discovery) | `references/data-sources.md` |
| Choosing a format, converting between formats, or any CRS / projection / EPSG question | `references/formats-and-crs.md` |
| Running GDAL/OGR, GeoPandas, xarray, DuckDB, PostGIS, or PDAL — the actual processing | `references/processing.md` |
| Vector analytics, raster analytics, terrain/hydrology, network analysis, point cloud workflows | `references/analytics.md` |
| Tile generation (PMTiles, MVT), tile servers (Martin, TiTiler), web map rendering (MapLibre, deck.gl) | `references/web-delivery.md` |
| QGIS desktop, QGIS plugin ecosystem, QGIS MCP, PyQGIS scripting, Processing toolbox | `references/qgis.md` |

For simple one-shot questions (single CRS conversion, one `ogr2ogr` invocation), the relevant reference alone is usually enough. For multi-stage pipelines, read `data-sources.md` and `processing.md` together; for end-to-end "from raw data to web map" tasks, also read `web-delivery.md`.

## Global defaults — apply unless the user specifies otherwise

* **Storage formats:** GeoParquet (vector analytics), COG (raster), PMTiles (tile delivery), GeoPackage (desktop interchange). Never produce Shapefile as new output.
* **CRS:** WGS84 (EPSG:4326) for storage; Web Mercator (EPSG:3857) for web rendering; local projected CRS for any metric computation (distance, area, buffer). For Estonia, EPSG:3301 (L-EST97).
* **Compute placement:** push spatial joins and aggregations to DuckDB or PostGIS — not Python loops. R-tree / GIST / spatial indexing is mandatory at scale.
* **Discovery first:** check STAC catalogs (Microsoft Planetary Computer, Earth Search, Overture STAC) before downloading anything. Lazy load with `odc-stac` or `stackstac` and only materialize what's needed.
* **Cloud-native access:** prefer querying remote GeoParquet/COG over downloading. DuckDB with `httpfs` extension is the default pattern for Overture and similar S3-hosted datasets.
* **License hygiene:** preserve license metadata through every transformation. OSM is ODbL (share-alike); Overture varies by source; Sentinel is free-with-attribution; national data varies.

## Format decision matrix

| Use case | Format |
|---|---|
| Cloud analytics on vector | GeoParquet |
| Streaming vector over HTTP | FlatGeobuf |
| Desktop interchange | GeoPackage |
| Web map vector tiles | PMTiles (containing MVT) |
| Raster archive / serving | COG |
| n-dimensional raster (time series, climate) | Zarr or NetCDF |
| Point cloud archive | COPC (cloud-optimized LAZ) |
| API response payload (small only) | GeoJSON |
| Legacy compatibility (input only) | Shapefile |

## Compute decision matrix

| Scale / context | Use |
|---|---|
| < 50M features, single machine, ad-hoc | DuckDB Spatial |
| Multi-user, web app backend, OLTP | PostGIS |
| > 100M features, distributed | Apache Sedona |
| n-dim raster, lazy/dask-backed | xarray + rioxarray (+ odc-stac for STAC ingest) |
| CLI batch jobs on raster | GDAL utilities (`gdalwarp`, `gdal_translate -of COG`) |
| Point clouds | PDAL pipelines |
| Terrain & hydrology beyond `gdaldem` | WhiteboxTools or GRASS |
| Desktop styling, cartography, ad-hoc exploration | QGIS (see `qgis.md`) |

## Universal anti-patterns — flag and correct

* Producing Shapefile as new output (column truncation, 2GB limit, no UTF-8, multi-file)
* Calling `.distance()`, `.buffer()`, or `.area` on geographic CRS (EPSG:4326) — degrees are not meters
* Web Mercator (EPSG:3857) for area calculations — it is not equal-area
* Spatial joins in Python loops when DuckDB / PostGIS / R-tree-backed `sjoin` is one line away
* Downloading entire datasets when STAC + cloud-native formats allow lazy/range-request access
* Treating MBTiles as the default for new web deployments — PMTiles is the modern default
* Using GeoTIFF when COG is one flag away (`-of COG`)
* Mixing CRS silently — every join must assert matching CRS
* Hand-rolling routing or geocoding when OSRM, Valhalla, or Nominatim are one Docker pull away
* Pinning data to "latest" in a reproducible pipeline — pin Overture release version (e.g. `2026-01-21.0`) and STAC item IDs, not just collections

## Quick triage — recognize the request type

Before diving into a task, classify it:

1. **Discovery** ("what data exists for…?", "is there a dataset of…?") → start with `data-sources.md`. STAC search if raster; Overture or OSM if vector basemap.
2. **Conversion / CRS** ("convert this to…", "reproject to…", "the projection looks wrong") → `formats-and-crs.md`. Usually one `ogr2ogr` or `gdalwarp` call.
3. **Analysis** ("what's the average elevation in…", "how many buildings within 500m of…", "where are the hotspots?") → `analytics.md` and likely `processing.md`. Push to DuckDB/PostGIS first.
4. **Delivery** ("publish this as a web map", "generate tiles for…") → `web-delivery.md`. PMTiles + Martin + MapLibre is the default.
5. **Desktop / cartography** ("style this in QGIS", "make a print map", "automate this in QGIS") → `qgis.md`. Consider QGIS MCP for agentic workflows.

Most real tasks span 2–3 of these — read the relevant references in order.

## Reproducibility checklist for any pipeline you produce

* Pin dataset versions (Overture release, STAC item IDs, OSM extract dates)
* Document CRS at every stage; never assume
* Use `conda-forge` envs or container images (`osgeo/gdal` is a sensible base) — pip-only geospatial envs break frequently
* Validate outputs: `gpq` for GeoParquet, `rio-cogeo validate` for COG, `is_valid` for geometries
* Preserve license metadata in column or sidecar JSON
