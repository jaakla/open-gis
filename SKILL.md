---
name: open-gis
description: "Use this skill for production GIS/geospatial work, open-first but pragmatic about hosted/SaaS services when scale or data quality requires them: spatial data pipelines; vector/raster/point-cloud processing; satellite/EO imagery; LiDAR; CRS/projection/EPSG troubleshooting; spatial joins, buffers, distance/area analysis; spatial SQL / GeoSQL; routing, isochrones, geocoding; terrain/hydrology; tile generation; and web maps. Trigger when the user mentions GIS, geospatial, OpenStreetMap/OSM, Overture Maps, Sentinel, Landsat, STAC, LiDAR, GeoTIFF/COG, GeoParquet, Shapefile, GeoPackage, PMTiles, vector tiles, raster, CRS, EPSG, projections, WMS/WFS/WMTS/OGC API, QGIS, GDAL/OGR, GeoPandas, Shapely, xarray/rioxarray, DuckDB Spatial, PostGIS, BigQuery GIS, Snowflake geospatial, Sedona, PDAL, OSRM, Valhalla, GraphHopper, tippecanoe, Martin, MapLibre, Estonia data (Maa- ja Ruumiamet, ETAK, EPSG:3301/L-EST97), INSPIRE, or regional data portals. Do not trigger for simple location lookups, travel directions, or casual map references without analytical or production GIS work."
---

# Open GIS Toolkit

Production-grade geospatial workflows with an open-first stack and pragmatic hosted/SaaS choices when global scale, latency, SLA, or data quality makes local processing a poor fit. Cloud-native by default: STAC for discovery, GeoParquet + COG + PMTiles for storage, DuckDB and PostGIS for compute, MapLibre and Martin for delivery.

## Reproducible project-first contract

> **Core principle: reasoning may be exploratory; the delivered analysis must be deterministic, inspectable, and reproducible.**

For any material multi-stage GIS analysis, do not optimize for reaching the final map, dashboard, or answer quickly. A one-off polished dashboard is **not** the deliverable — a reproducible technical GIS project is. First establish a reusable project artifact, then derive the map/dashboard/report from it.

Before treating an analysis as complete, you MUST compile (or maintain) a project like `examples/tartu-development` containing:

* a canonical `project.yaml` (`open-gis-project/v1`), `pipeline.py`, and `README.md`
* exact source datasets — URLs, versions, retrieval/version timestamps, selections, licensing
* explicit assumptions and data-selection rationales
* every manual data addition or correction stored as real geodata (not chat text)
* deterministic ordered processing steps with CRS and parameters made explicit
* machine-readable validation rules and a validation report from the run
* output definitions, semantic presentation intent, and provenance surfaced in the rendered view

Workflow (agent may retry/experiment internally, but the accepted analysis is recompiled deterministically):

```
USER QUESTION
    ↓
interpretation / exploration        (internal, may be ad-hoc)
    ↓
COMPILE GIS PROJECT                 project.yaml + pipeline + manifest + overrides + validation
    ↓
EXECUTE PROJECT
    ↓
validated derived datasets
    ↓
QGIS project / standardized web view
    ↓
FINAL ANALYSIS / DASHBOARD / ANSWER
```

The polished map/dashboard is a **view over the project**, not the canonical definition of the analysis.

Hard rules for every material analysis:

* **Real source data mandatory; never hallucinate coordinates.** Hallucination or synthesis of fake coordinates/geometries is strictly forbidden without explicit, informed user consent. Always discover, download, and analyze real, verified datasets from official/authoritative sources (e.g. national cadastre, ETAK road network, OSM/Overpass, STAC). If a hypothetical or planned scenario feature is needed, record it explicitly as a user/project override layer (`data/overrides/`) with documented provenance, rationale, and evidence — never by quietly inventing baseline data.
* **Build a layer- and style-perfect QGIS project (`project.qgz`).** Every multi-stage analysis must deliver a companion QGIS project that is a faithful, layer- and style-perfect mirror of the web map/dashboard. It must organize layers into matching layer tree groups, apply identical categorized/rule-based visual styles (colors, opacities, outlines, marker sizes, stroke widths), bind GeoPackages with correct OGR syntax (`./path.gpkg|layername=name`), and include standard tiled basemaps (e.g. Maa- ja Ruumiamet grey WMS `pohi_mvr2` for Estonia or OpenStreetMap/CartoDB XYZ).
* **Record, don't memoize on chat.** If you make a manual fix while solving the task, encode it as data or pipeline logic. Never leave an important correction only in chat context or transient code.
* **Represent facts as data.** If a fact cannot be represented by available geodata (planned road, corrected POI, custom AOI, assumed development area), create an explicit project override/scenario layer with provenance and rationale instead of silently approximating it.
* **Never mutate source data.** Immutable source + project override layer = effective input. Distinguish external facts, transformations, corrections, assumptions, and hypothetical data.
* **Runs are rerunnable without the chat.** A fresh environment with the documented sources must reproduce the project. The conversation transcript is not part of the analytical dependency graph.
* **Validation is a pipeline stage**, not prose advice, with machine-readable results.
* **Separate analysis semantics from rendering.** Define semantic presentation roles; never reinvent layout/colors/UX arbitrarily on each run.
* Cheat-sheet: `references/project-spec.md` defines the full schema; `templates/` gives ready scaffolds; `examples/tartu-development` is a worked reference project matching the acceptance scenario.

## Modules — read the relevant reference(s) before starting work

| If the task involves... | Read |
|---|---|
| Finding or sourcing data (OSM, Overture, Sentinel, Landsat, building footprints, regional portals, STAC catalogs, MCP-based discovery) | `references/data-sources.md` |
| Choosing local processing vs online/hosted/SaaS services for global or continental scale; basemaps, elevation, routing, geocoding, place search, postcode lookup APIs | `references/services-and-scale.md` |
| Choosing a format, converting between formats, or any CRS / projection / EPSG question | `references/formats-and-crs.md` |
| Compiling a reproducible GIS project artifact (`project.yaml`, pipeline, overrides, validation, presentation) | `references/project-spec.md` + `templates/` |
| Running GDAL/OGR, GeoPandas, xarray, DuckDB, PostGIS, or PDAL — the actual processing | `references/processing.md` |
| Writing or reviewing spatial SQL / GeoSQL in DuckDB Spatial, PostGIS, BigQuery GIS, Snowflake, or Sedona | `references/spatial-sql.md` |
| Vector analytics, raster analytics, terrain/hydrology, network analysis, point cloud workflows | `references/analytics.md` |
| Tile generation (PMTiles, MVT), tile servers (Martin, TiTiler), web map rendering (MapLibre, deck.gl) | `references/web-delivery.md` |
| QGIS desktop, QGIS plugin ecosystem, QGIS MCP, PyQGIS scripting, Processing toolbox | `references/qgis.md` |
| Reproducibility, validation, license attribution, tile smoke tests, deployment checks | `references/validation-and-ops.md` |

For simple one-shot questions (single CRS conversion, one `ogr2ogr` invocation), the relevant reference alone is sufficient — a full project artifact is not needed. For multi-stage pipelines, read `data-sources.md` and `processing.md` together, and see `project-spec.md` + `templates/` to compile analysis into a rerunnable project. For end-to-end "from raw data to web map" tasks, also read `web-delivery.md`.

## Global defaults — apply unless the user specifies otherwise

* **Storage formats:** GeoParquet (vector analytics), COG (raster), PMTiles (tile delivery), GeoPackage (desktop interchange). Never produce Shapefile as new output.
* **CRS:** WGS84 (EPSG:4326) for storage; Web Mercator (EPSG:3857) for web rendering; local projected CRS for any metric computation (distance, area, buffer). For Estonia, EPSG:3301 (L-EST97).
* **Compute placement:** push spatial joins and aggregations to DuckDB or PostGIS — not Python loops. R-tree / GIST / spatial indexing is mandatory at scale.
* **Discovery first:** check STAC catalogs (Microsoft Planetary Computer, Earth Search, Overture STAC) before downloading anything. Lazy load with `odc-stac` or `stackstac` and only materialize what's needed.
* **Cloud-native access:** prefer querying remote GeoParquet/COG over downloading. DuckDB with `httpfs` extension is the default pattern for Overture and similar S3-hosted datasets.
* **Scale first:** local tools are fine for city/state work; at continental/global scale prefer cloud-native partitioned datasets, precomputed tiles, hosted APIs, or SaaS when they are more reliable than local batch processing.
* **License hygiene:** preserve license metadata through every transformation. OSM is ODbL (share-alike); Overture varies by source; Sentinel is free-with-attribution; national data varies.
* **Runtime hygiene:** prefer `conda-forge` environments or containers for GDAL/PROJ/GEOS/QGIS stacks. Avoid pip-only geospatial environments unless the project already proves they work.

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
| Continental/global lookup/search/routing/elevation | Hosted API or SaaS where coverage, SLA, terms, and price fit |
| Planet-scale basemap delivery | Prebuilt PMTiles/vector tiles or managed basemap service |
| n-dim raster, lazy/dask-backed | xarray + rioxarray (+ odc-stac for STAC ingest) |
| CLI batch jobs on raster | GDAL utilities (`gdalwarp`, `gdal_translate -of COG`) |
| Point clouds | PDAL pipelines |
| Terrain & hydrology beyond `gdaldem` | WhiteboxTools or GRASS |
| Desktop styling, cartography, ad-hoc exploration | QGIS (see `qgis.md`) |

## Universal anti-patterns — flag and correct

* Hallucinating or fabricating mock coordinates and geometries instead of retrieving real source data (unless the user gave explicit, informed consent for a synthetic mock test)
* Generating a QGIS project that lacks the web dashboard's layers, omits basemaps, or uses broken OGR datasource syntax (`path.gpkg|layer` without `layername=`), causing layers to load as non-spatial attribute tables
* Producing Shapefile as new output (column truncation, 2GB limit, no UTF-8, multi-file)
* Calling `.distance()`, `.buffer()`, or `.area` on geographic CRS (EPSG:4326) — degrees are not meters; unless specific tool explicitly supports wgs84 based geodesic calculations
* Web Mercator (EPSG:3857) for area or distance calculations — it is not equal-area, and the units are not in meters except at the equator
* Spatial joins in Python loops when DuckDB / PostGIS / R-tree-backed `sjoin` is one line away
* Using bbox containment for area queries when features can cross the boundary — use bbox overlap as the scan gate, then an exact spatial predicate
* Downloading entire datasets when STAC + cloud-native formats allow lazy/range-request access
* Running planet-scale local processing for lookup/search problems when reliable hosted services or precomputed global products already exist
* Treating MBTiles as the default for new web deployments — PMTiles is the modern default
* Using GeoTIFF when COG is one flag away (`-of COG`)
* Mixing CRS silently — every join must assert matching CRS
* Hand-rolling routing or geocoding when OSRM, Valhalla, or Nominatim are one Docker pull away
* Pinning data to "latest" in a reproducible pipeline — pin Overture release version and STAC item IDs, not just collections. For Overture, verify the pinned release is still available or mirror it.

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
* Use `conda-forge` envs or container images (`ghcr.io/osgeo/gdal:alpine-small-latest` is a sensible base — note the registry; the legacy Docker Hub path `osgeo/gdal` no longer publishes new images) — pip-only geospatial envs break frequently
* Validate outputs: `gpq` for GeoParquet, `rio-cogeo validate` for COG, `pmtiles show` for PMTiles, `is_valid` for geometries
* Preserve license metadata in column or sidecar JSON and carry required attribution into maps/APIs
