---
name: open-gis
description: "Use for production GIS and geospatial work: source discovery and provenance, vector/raster/point-cloud pipelines, CRS and metric analysis, spatial SQL, routing and isochrones, QGIS projects, tile generation, and web maps. Applies to tools and formats such as OSM, Overture, STAC, Sentinel/Landsat, LiDAR, GeoPackage, GeoParquet, COG, PMTiles, WMS/WFS/OGC APIs, GDAL, GeoPandas, DuckDB Spatial, PostGIS, QGIS, MapLibre, and Estonian spatial data including ETAK and EPSG:3301. Open-first, with hosted services when scale or reliability requires them. Do not use for casual map references, simple place lookups, or ordinary travel directions without analytical GIS work."
---

# Open GIS Toolkit

Production-grade geospatial workflows with an open-first stack and pragmatic hosted/SaaS choices when global scale, latency, SLA, or data quality makes local processing a poor fit. Cloud-native by default: STAC for discovery, GeoParquet + COG + PMTiles for storage, DuckDB and PostGIS for compute, MapLibre and Martin for delivery.

## Reproducible project-first contract

> **Core principle: reasoning may be exploratory; the delivered analysis must be deterministic, inspectable, and reproducible.**

For any material multi-stage GIS analysis, do not optimize for reaching the final map, dashboard, or answer quickly. A one-off polished dashboard is **not** the deliverable — a reproducible technical GIS project is. First establish a reusable project artifact, then derive the map/dashboard/report from it.

Before treating an analysis as complete, you MUST compile (or maintain) a project like `examples/tartu-development`: a canonical `project.yaml` (`open-gis-project/v1`), `pipeline.py`, and `README.md`; pinned sources with timestamps, selections and licensing; explicit assumptions; every manual addition or correction stored as real geodata; deterministic ordered steps with explicit CRS; machine-readable validation rules plus the report from the run; and output definitions with semantic presentation intent and provenance surfaced in the rendered view. **`references/project-spec.md` is the full schema — read it before compiling a project.**

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

Hard rules for every material analysis — each is expanded in `references/project-spec.md`:

* **Real source data mandatory; never hallucinate coordinates.** Fabricating coordinates or synthesizing baseline geometry is forbidden without explicit, informed user consent. Hypothetical or planned features go in `data/overrides/` with provenance, rationale, and evidence.
* **Never mutate source data.** Immutable source + project override layer = effective input. Distinguish external facts, transformations, corrections, assumptions, and hypothetical data.
* **Overrides must be executable and verified.** Target a real source feature; for attribute changes the asserted prior value must match. Evidence must be non-placeholder. Validation reports each override `applied`, `rejected`, or `not_testable` — listing one is not applying it. Scenario features stay labeled hypothetical and visually distinct from authoritative layers.
* **Record, don't memoize on chat.** Encode every manual fix as data or pipeline logic. A fresh environment with the documented sources must reproduce the project; the transcript is not part of the dependency graph.
* **Prove semantic predicates from data.** Ownership, active status, public access, legal designation: the source must expose an authoritative field or documented mapping. Preserve unknown as unknown; never default a missing value to the desired class.
* **Bounded APIs must prove completeness.** Record `numberMatched`/equivalent and page until returned == matched. A response filled to the request limit is incomplete until proven otherwise.
* **Validation is a pipeline stage**, not prose advice, with machine-readable results. Every declared check appears exactly once in the report; `warning`/`not_testable` propagate to run and project status; run IDs and hashes resolve to a real `runs/*.json` record.
* **Run the project CLI when available.** Use `open-gis validate project.yaml` before delivery and `open-gis run project.yaml` for the canonical execution path. The CLI audits the manifest, provenance, graph, artifacts, report, and run record; it does not replace domain GIS checks performed by the pipeline.
* **The manifest must resolve.** Every step input is a source key or an earlier step's output, spelled as the producer declared it; every `generated_by` names a real step (`manifest_graph_resolves`).
* **One canonical implementation creates every declared output.** Convenience/E2E entrypoints may wrap `pipeline.py` but must not duplicate its processing, QGIS, or report logic.
* **Build a layer- and style-perfect QGIS project (`project.qgz`)** mirroring the web view: matching layer-tree groups, identical categorized styles, `./path.gpkg|layername=name` datasources, and a regional tiled basemap. **Success means valid layers, not exit code 0** — pin the runtime, and when PyQGIS is available require every layer `isValid()`; otherwise record `not_testable`, never an implicit pass.
* **Separate analysis semantics from rendering.** Declare semantic presentation roles; don't reinvent layout/colors/UX per run.
* **Ship a reconfigurable view, and never let it misrepresent the run.** Organise the sidebar into tabs of collapsible sections, give every layer group an on/off control, and expose the analysis parameters and scenario overrides as live controls. Each control opens at the value declared in `presentation.controls` and returning there must reproduce the published numbers; any other position labels itself exploratory and offers a reset. The browser re-applies published rules to values the pipeline measured — it never measures geometry, and a control that changes a shape switches between buffers the pipeline materialised.
* **Labels must match the operation.** A Euclidean buffer is a "2 km straight-line proxy", not a walking catchment, and column names must say so too. State the measurement basis (nearest edge vs centroid) as an assumption — it changes which features qualify.
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
| Tile generation (PMTiles, MVT), tile servers (Martin, TiTiler), delivered rendering (MapLibre, deck.gl), or exploration rendering (kepler.gl, lonboard) | `references/web-delivery.md` |
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

* Pin dataset versions (Overture release, STAC item IDs, OSM extract dates) — never "latest"
* Document CRS at every stage; never assume
* Use `conda-forge` envs or pinned container images (`ghcr.io/osgeo/gdal:alpine-small-latest` for GDAL, `qgis/qgis:<tag>` for PyQGIS); pip-only geospatial envs break frequently
* Validate outputs: `gpq` for GeoParquet, `rio-cogeo validate` for COG, `pmtiles show` for PMTiles, `is_valid` for geometries
* Preserve license metadata in column or sidecar JSON and carry required attribution into maps/APIs

Command-level detail for each of these lives in `references/validation-and-ops.md`.
