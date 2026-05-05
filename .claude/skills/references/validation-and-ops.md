# Validation and Operations

Cross-cutting checks for geospatial pipelines. Read this before delivering production outputs, publishing tiles, or handing a workflow to another team.

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
