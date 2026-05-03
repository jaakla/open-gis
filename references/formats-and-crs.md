# Formats and CRS

Format selection, conversions, and coordinate reference system handling. Two of the most common sources of subtle errors in GIS work — handle deliberately, not by reflex.

## Format quick reference

### Vector formats

| Format | When to use | Avoid for |
|---|---|---|
| **GeoParquet** | Cloud analytics, columnar workloads, large datasets, anything you'll query with DuckDB or Sedona | Tiny payloads, contexts requiring random write |
| **FlatGeobuf** | Single-file streaming over HTTP with spatial indexing built in; ideal for serving large vector datasets to web clients without a DB | Heavy attribute-side analytics |
| **GeoPackage (.gpkg)** | Desktop interchange (QGIS-friendly), multi-layer single file, when SQLite features matter | Cloud-native pipelines |
| **GeoJSON** | Small API payloads (< few MB), human-readable debugging, web map static layers | Anything beyond a few thousand features |
| **Shapefile** | Reading legacy data only | Producing new output (column truncation to 10 chars, 2GB limit, no UTF-8 by default, multi-file) |
| **GeoArrow** | In-memory interchange between processes; growing tooling | Persistent storage (use GeoParquet) |
| **MVT (Mapbox Vector Tile)** | Wire format inside vector tiles — almost never authored directly | — |

### Raster formats

| Format | When to use |
|---|---|
| **Cloud Optimized GeoTIFF (COG)** | Default for new raster output. Single file, internally tiled, with overviews. HTTP range-requestable. |
| **Zarr** | n-dimensional arrays (time series, climate ensembles), cloud-native chunked storage |
| **NetCDF (.nc)** | Climate/ocean data, multi-dim arrays, established tooling (CDO, xarray) |
| **GeoTIFF (non-COG)** | Legacy compatibility only — convert to COG with `gdal_translate -of COG` |
| **HDF5** | Scientific multi-dim, often the underlying container for satellite L1/L2 products |

### Point cloud formats

| Format | When to use |
|---|---|
| **COPC** | New cloud-optimized LAZ — preferred for new archives |
| **LAZ** | Standard compressed LiDAR; convert to COPC for cloud serving |
| **LAS** | Uncompressed; convert to LAZ or COPC immediately |
| **EPT** (Entwine Point Tile) | Web streaming of huge point clouds — being overtaken by COPC |

### Tile container formats

| Format | When to use |
|---|---|
| **PMTiles** | Modern default. Single file, range-request friendly, serves from S3 or any static host with no tile server. Both vector (MVT) and raster. |
| **MBTiles** | Legacy SQLite-based; convert to PMTiles with `pmtiles convert` for new web deployments |
| **XYZ tile directories** | Static hosting of millions of small files; convert to PMTiles instead |

## Common conversions

### Vector

```bash
# Anything → GeoParquet
ogr2ogr -f Parquet output.parquet input.shp

# GeoParquet validation
gpq validate output.parquet

# Anything → GeoPackage (multi-layer)
ogr2ogr -f GPKG output.gpkg input.shp -nln my_layer

# Shapefile → GeoParquet with reprojection
ogr2ogr -f Parquet -t_srs EPSG:3301 output.parquet input.shp

# Filter while converting
ogr2ogr -f Parquet \
  -where "population > 10000" \
  -sql "SELECT name, population, geometry FROM cities" \
  cities_large.parquet cities.shp

# GeoParquet → PostGIS
ogr2ogr -f PostgreSQL "PG:host=localhost dbname=gis user=postgres" \
  input.parquet -nln cities -lco GEOMETRY_NAME=geom -lco FID=id
```

### Raster

```bash
# Anything → COG (the canonical conversion)
gdal_translate -of COG \
  -co COMPRESS=DEFLATE -co PREDICTOR=2 \
  -co BLOCKSIZE=512 \
  input.tif output_cog.tif

# COG validation
rio cogeo validate output_cog.tif

# Reproject + COG in one step
gdalwarp -t_srs EPSG:3301 -of COG \
  -co COMPRESS=DEFLATE \
  -tr 10 10 -r bilinear \
  input.tif output_3301_cog.tif

# Build virtual mosaic without copying (useful for many tiles)
gdalbuildvrt mosaic.vrt tile_*.tif
gdal_translate -of COG mosaic.vrt full_cog.tif

# Add internal overviews if missing
gdaladdo -r average input.tif 2 4 8 16 32
```

### Point cloud

```bash
# LAS → LAZ
pdal translate input.las output.laz

# LAZ → COPC
pdal translate input.laz output.copc.laz \
  --writers.copc.filename=output.copc.laz

# Or via PDAL pipeline JSON for anything more complex
```

### Tiles

```bash
# MBTiles → PMTiles
pmtiles convert input.mbtiles output.pmtiles

# Inspect a PMTiles file
pmtiles show output.pmtiles

# Extract a bbox subset (useful for previews)
pmtiles extract input.pmtiles output.pmtiles \
  --bbox=24.5,59.3,25.0,59.5
```

## Coordinate Reference Systems

### The four CRS rules to internalize

1. **Storage:** WGS84 (EPSG:4326) is the lingua franca. Almost all open data is delivered in it.
2. **Web rendering:** Web Mercator (EPSG:3857). Web maps assume it.
3. **Metric computation:** ALWAYS a local projected CRS. Never use degrees as if they were meters.
4. **Equal-area requirements:** Web Mercator is NOT equal-area. For area/density/proportion calculations use an equal-area projection (UTM, national grid, or a continental equal-area like EPSG:3035 for Europe).

### Choosing a local projected CRS

| Region | EPSG | Notes |
|---|---|---|
| Estonia | 3301 | L-EST97; the Estonian standard |
| Continental Europe (analysis) | 3035 | LAEA Europe — equal-area |
| US | UTM zones (32613–32619), or 5070 (CONUS Albers, equal-area) | |
| Worldwide local | UTM zone matching your area | Compute zone: `floor((lon + 180) / 6) + 1` |
| Web rendering | 3857 | Mercator, NOT equal-area |
| Storage / interchange | 4326 | WGS84 lat/lon |

### Reprojection commands

```bash
# Vector
ogr2ogr -t_srs EPSG:3301 output.gpkg input.gpkg

# Raster
gdalwarp -t_srs EPSG:3301 -tr 10 10 -r bilinear input.tif output.tif
# -r options: near, bilinear, cubic, cubicspline, lanczos, average, mode
# Use bilinear/cubic for continuous data; near/mode for categorical
```

### Reprojection in Python

```python
# GeoPandas
gdf = gdf.to_crs(epsg=3301)

# Single point
from pyproj import Transformer
t = Transformer.from_crs("EPSG:4326", "EPSG:3301", always_xy=True)
x, y = t.transform(24.7536, 59.4370)  # lon, lat (always_xy=True)

# Raster (rioxarray)
da = da.rio.reproject("EPSG:3301", resampling=Resampling.bilinear)
```

The `always_xy=True` flag is critical: pyproj defaults to "authority order", which is lat,lon for EPSG:4326 — opposite of what most code expects. Always pass `always_xy=True` unless you know exactly why you'd want otherwise.

## OGC axis-order gotchas

OGC services are inconsistent enough that every request should be checked against capabilities metadata:

* WMS 1.1.1 uses `SRS`; WMS 1.3.0 uses `CRS`.
* WMS 1.3.0 with `EPSG:4326` may expect bbox as latitude,longitude order. `CRS:84` keeps longitude,latitude order.
* WFS output may arrive as GML with service CRS axis order, not GeoJSON-style lon/lat.
* WMTS tile matrix sets define their own origin, scale denominator, and CRS; do not assume XYZ/Web Mercator unless the matrix set says so.

When in doubt, request a tiny bbox around a known point and inspect the coordinates before running a full extract.

## CRS troubleshooting checklist

When a layer "looks wrong" (off by a continent, scaled wrong, rotated):

1. **Check declared CRS:** `ogrinfo -so file.gpkg` or `gdf.crs`
2. **Check actual coordinate values:** are they in degree range (-180 to 180) or projected (large meter values)?
3. **Lat/lon swap?** If coordinates declared as EPSG:4326 but the "longitude" is between -90 and 90, you may have an axis-order issue.
4. **Missing CRS metadata?** GeoJSON without a CRS member is assumed EPSG:4326. Older Shapefiles often lack `.prj`. Set explicitly: `gdf.set_crs("EPSG:4326", inplace=True)` (note: `set_crs` declares; `to_crs` reprojects).
5. **Datum shift needed?** Older datasets may use NAD27, ED50, or local datums. Don't ignore — the offset can be hundreds of meters.

## Anti-patterns to flag and fix

* `gdf.buffer(100)` on EPSG:4326 — buffer in degrees is meaningless. Reproject first, buffer, then reproject back.
* `gdf.area` on EPSG:4326 — gives squared degrees.
* Computing area in EPSG:3857 (Web Mercator) — Mercator distortion makes this wrong by up to a factor of cos(latitude).
* Joining two layers without checking `gdf1.crs == gdf2.crs`.
* Assuming a Shapefile without `.prj` is in WGS84 — it might be in a national grid.
* Using `pyproj.Transformer` without `always_xy=True` and silently getting axis order wrong.

## Validity and topology

Geometries that are technically loadable but topologically broken are a frequent source of surprise downstream failures.

```python
from shapely.validation import make_valid

gdf["geometry"] = gdf.geometry.apply(make_valid)

# Or filter out invalid:
invalid = gdf[~gdf.is_valid]
```

In PostGIS:

```sql
-- Find invalid
SELECT id, ST_IsValidReason(geom) FROM layer WHERE NOT ST_IsValid(geom);

-- Repair in place
UPDATE layer SET geom = ST_MakeValid(geom) WHERE NOT ST_IsValid(geom);
```

Always validate after a reprojection, an overlay, or an import from Shapefile.
