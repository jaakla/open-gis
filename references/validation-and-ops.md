# Validation and Operations

Cross-cutting checks for geospatial pipelines. Read this before delivering production outputs, publishing tiles, or handing a workflow to another team.

## Reproducible GIS project validation (preferred over prose checklists)

For any material multi-stage analysis, validate **the project artifact**, not just the output files. Prefer machine-readable project validation (`openmapstack validate project.yaml` / the `validation/*.yaml` + `validation/latest-report.json` pair) over a prose checklist.

**Statuses are explicit. Never turn "not tested" into an implicit pass:**

| Status | Meaning |
|---|---|
| `passed` | Actually checked, green |
| `failed` | Actually checked, red → blocks `project.status: validated` |
| `warning` | Known limit / soft miss, surfaced in UX |
| `not_testable` | Could not run — record it explicitly |

A run emits a machine-readable report (e.g. `validation/latest-report.json`):

```json
{
  "run_id": "run-20260825-081503",
  "status": "passed",
  "checks": [
    {"id": "geometry_valid", "status": "passed", "features_checked": 12458},
    {"id": "duplicate_parcel_ids", "status": "passed", "duplicates": 0},
    {"id": "poi_completeness", "status": "warning",
     "reason": "No authoritative completeness baseline available"}
  ]
}
```

Project-level checks to run before declaring an analysis complete:

* **Schema** — `project.yaml` parses and matches `openmapstack-project/v1`.
* **Source provenance** — every source has `source_url`, `retrieved_at`, a pinned version, a license, and a selection.
* **Source semantic fitness** — every decision-critical predicate (ownership, active status, access, classification) is backed by an authoritative field/domain; missing values remain unknown rather than being coerced to a passing value.
* **API completeness** — bounded/paged APIs record matched and returned counts; equality with a page limit is treated as suspicious until pagination or a hits/count request proves completeness.
* **CRS** — `analysis_crs` is projected/metric, `storage_crs` is documented; no metric ops on EPSG:4326.
* **Referenced files** — every `data/overrides/*`, output, and `pipeline.py` path exists.
* **Required validations** — the `validation.required` list all pass.
* **Undocumented overrides** — every analyst correction appears in `project.yaml overrides` with rationale + evidence (nothing siloed in chat).
* **Applied overrides** — every override target exists, asserted prior values match, evidence is non-placeholder, and the report records whether the override was applied. Scenario additions remain distinct from authoritative source layers.
* **Licensing** — each source has a license; attribution chain is preserved.
* **Reproducibility** — a fresh environment can rerun it without the chat transcript.
* **Manifest/report parity** — every required and domain check is present exactly once; warnings or `not_testable` checks propagate to overall status; run IDs and hashes resolve to an actual run record.
* **QGIS validity** — all tree IDs resolve to project layers, local sources exist, categorized styles cover the data domain, and PyQGIS loads every layer as valid when that runtime is available. Otherwise record runtime validation as `not_testable`.

See `references/project-spec.md` for the full schema and `templates/validation.yaml` for a starter.

### CLI workflow

```bash
# Before running: validate declarations and required input files.
openmapstack validate project.yaml --preflight

# Execute runtime.implementation.pipeline/command, then audit the artifact.
openmapstack run project.yaml

# Re-audit without execution; use strict mode in release gates.
openmapstack validate project.yaml --strict

# Emit machine-readable results for CI or another agent.
openmapstack validate project.yaml --json --output validation/cli-report.json
openmapstack inspect project.yaml --json
```

Warnings and `not_testable` conditions produce overall `warning` but return zero
unless `--strict` is used. Structural/artifact failures return one. The CLI
validates that pipeline-produced domain checks exist and propagate correctly;
it does not turn a missing spatial test into its own synthetic pass.

## Spatial SQL validation gates

Before presenting a spatial SQL result, run the relevant validation in SQL:

* **Cost / dry run:** For BigQuery, dry run with a bytes cap before execution. For Snowflake and other cloud warehouses, keep previews bounded and use available explain/cost tools where relevant. If a query is over budget, tighten bbox, filters, dates, selected columns, or aggregation resolution before executing.
* **Row count:** Run `COUNT(*)` on the final filters. If it returns zero, debug before presenting: verify target-area lookup, bbox overlap direction, CRS/SRID, exact predicate, attribute filters, and column names.
* **Geometry validity:** Check `ST_IsValid`, `ST_IsValidReason`, `GEOS_VALIDITY`, or engine equivalents after import, reprojection, overlay, dissolve, or simplification.
* **Area / length sanity:** For polygon outputs, compute total area. For line outputs, compute total length. Confirm units: geography functions usually return meters, while planar geometry returns CRS units.
* **Query plan:** Use `EXPLAIN` / `EXPLAIN ANALYZE` on recurring, slow, or production queries. Confirm spatial indexes, bbox pruning, partition pruning, or file pruning are actually used.
* **Preview rows:** Inspect a small sample for geometry presence, nulls, IDs/names, unexpected categories, and duplicated features before exporting or mapping.

## Data manifest

Every reproducible pipeline should write a small manifest next to outputs:

```json
{
  "sources": [
    {
      "name": "Overture buildings",
      "release": "YYYY-MM-DD.0",
      "url": "s3://overturemaps-us-west-2/release/YYYY-MM-DD.0/...",
      "license": "recorded from source metadata"
    }
  ],
  "crs": {
    "storage": "EPSG:4326",
    "analysis": "EPSG:3301"
  },
  "environment": {
    "gdal": "from gdalinfo --version",
    "duckdb": "from SELECT version()",
    "python": "from runtime"
  },
  "outputs": [
    {
      "path": "buildings.pmtiles",
      "format": "PMTiles",
      "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
      "validation": ["pmtiles show"],
      "attribution": "required map/API text"
    }
  ]
}
```

Pin STAC item IDs, Overture release versions, OSM extract timestamps, portal download dates, and GTFS feed dates. For Overture, mirror releases needed beyond the public retention window.

## Validation commands

| Output | Checks |
|---|---|
| GeoParquet | `gpq validate output.parquet`; confirm geometry column, CRS metadata, bbox metadata, row count |
| STAC | `stac-validator item.json`; validate items and catalogs against the schema |
| GeoPackage | `ogrinfo -al -so output.gpkg`; confirm layer names, geometry type, CRS, feature count |
| COG | `rio cogeo validate output.tif`; `gdalinfo output.tif`; confirm internal tiling, overviews, compression, NoData |
| Raster analysis | Confirm scale/offset, dtype, NoData, band order, resolution, CRS, transform, and bounds |
| PMTiles | `pmtiles show output.pmtiles`; confirm bounds, min/max zoom, vector layer names, metadata |
| PostGIS | Check SRID, GIST indexes, row counts, `ST_IsValid`, and `EXPLAIN ANALYZE` on expected queries |
| Web map | Load the style in a browser, check network requests, source-layer names, attribution, legend, and mobile viewport |

## CRS and geometry gates

Before metric operations:

1. Assert input CRS.
2. Reproject to a metric local CRS for distance, area, buffer, clustering radius, and density.
3. Run geometry validity checks (`ST_IsValid`, `GEOS_VALIDITY`) after import, reprojection, overlay, dissolve, or simplification. Topological errors break `tippecanoe` and PostGIS workflows downstream.
4. Reproject back to EPSG:4326 only for storage/interchange or to EPSG:3857 for web rendering.

In SQL, avoid meter distances against lon/lat geometry. Use projected geometry columns or PostGIS geography where appropriate.

## License and attribution

Preserve attribution in both data and UI:

* Keep source/provenance columns when present, especially Overture `sources`.
* Add a `LICENSES.json` or manifest field for every source.
* Carry OSM ODbL, Overture, Sentinel/Copernicus, national portal, basemap, and GTFS attribution into public maps and APIs.
* If derivative data is redistributed, check share-alike obligations before changing license terms.

## Deployment checks

For static PMTiles/COG hosting:

* Confirm HTTP range requests work through the CDN.
* Set CORS for `GET` and `HEAD` from the map origin.
* Use long cache TTLs for immutable versioned URLs; avoid mutable filenames for pinned releases.
* Smoke-test at low, middle, and max zooms.

For database-backed services:

* Create GIST indexes on geometry columns and functional indexes on transformed geometries if queries use them.
* Use materialized views for expensive recurring tile or API queries.
* Keep secrets out of MapLibre styles and client-side URLs.
* Monitor slow tile/API queries and empty tiles separately.
