# Processing Stack

The actual mechanics of moving and transforming data: GDAL/OGR for the C-foundation, Python for orchestration and analysis, DuckDB and PostGIS for SQL-side compute, PDAL for point clouds.

## Choosing the right layer

| Task profile | Use |
|---|---|
| Single conversion or reprojection | GDAL/OGR CLI (`gdalwarp`, `ogr2ogr`) |
| Iterative analysis, < 50M features | DuckDB Spatial in a notebook or script |
| Production query layer, multi-user | PostGIS |
| Multi-band raster time series, lazy/dask-backed | xarray + rioxarray + odc-stac |
| Vector wrangling in Python | GeoPandas (Shapely 2.x backend) |
| Truly distributed (>100M features, multi-node) | Apache Sedona |
| Point cloud | PDAL pipelines |

The default mental model: **CLI for one-shots, DuckDB for analytics, PostGIS for serving, Python for glue and ML.**

## Runtime and environment defaults

Use `conda-forge` or containers for GDAL/PROJ/GEOS/QGIS stacks. Pip-only environments are acceptable only when the project already pins and tests the native dependencies.

On macOS, modern Debian/Ubuntu, and Fedora 38+, `pip` against the system Python is blocked by PEP 668 (`error: externally-managed-environment`). Default to a venv for any pipeline you do not run via conda or a container:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Pin DuckDB explicitly. The Homebrew CLI and the PyPI wheel drift by patch release; spatial functions (`ST_SetCRS`, `always_xy := true`, `ST_Read` options) and even base SQL behaviour can differ between, say, 1.5.0 and 1.5.2. If a script uses both interfaces, install the same version in both and exercise the SQL through whichever one you ship — a query that runs fine via `duckdb.connect()` may error on `duckdb` CLI.

Minimum checks before debugging data issues:

```bash
gdalinfo --version
ogrinfo --formats | grep -E 'Parquet|GPKG|FlatGeobuf'
projinfo EPSG:3301
duckdb -c "INSTALL spatial; LOAD spatial; SELECT duckdb_proj_version();"
python -c "import geopandas, shapely, pyproj, rasterio; print(geopandas.__version__)"
```

For reproducible projects, commit an `environment.yml`, `pixi.toml`, or Dockerfile, and record tool versions in the data manifest.

## GDAL / OGR — the foundation

These ship with QGIS and are available on every Linux distribution.

### Inspection

```bash
# Vector
ogrinfo -so input.gpkg                # summary, all layers
ogrinfo -al -so input.gpkg layer_name # one layer's schema, extent, CRS

# Raster
gdalinfo input.tif                    # full metadata, CRS, extent, bands
gdalinfo -stats input.tif             # with computed band statistics
gdalinfo -mm input.tif                # min/max only (faster)
```

### `ogr2ogr` — the universal vector ETL tool

```bash
# Format conversion
ogr2ogr -f Parquet output.parquet input.gpkg

# Reprojection
ogr2ogr -t_srs EPSG:3301 output.gpkg input.gpkg

# Spatial filter (clip to bbox)
ogr2ogr -spat 24.5 59.3 25.0 59.5 output.gpkg input.gpkg

# Attribute filter
ogr2ogr -where "type = 'highway'" output.gpkg input.gpkg

# Combined: reproject, clip, filter, rename layer
ogr2ogr -f Parquet \
  -t_srs EPSG:3301 \
  -spat 24.5 59.3 25.0 59.5 -spat_srs EPSG:4326 \
  -where "amenity IN ('cafe','restaurant')" \
  -nln food_places \
  food.parquet osm-pois.gpkg

# Load to PostGIS (fast bulk import)
ogr2ogr -f PostgreSQL "PG:host=localhost dbname=gis user=postgres" \
  input.gpkg \
  -nln target_table -lco GEOMETRY_NAME=geom -lco FID=id \
  -nlt PROMOTE_TO_MULTI \
  -lco UNLOGGED=ON \
  --config PG_USE_COPY YES
```

`PG_USE_COPY YES` switches to PostgreSQL `COPY` protocol — significantly faster than INSERT for bulk loads.

### `gdalwarp` — raster reprojection and mosaicking

```bash
# Reproject + resample
gdalwarp -t_srs EPSG:3301 -tr 10 10 -r bilinear input.tif output.tif

# Cut to a polygon
gdalwarp -cutline aoi.gpkg -crop_to_cutline input.tif clipped.tif

# Mosaic many tiles
gdalwarp -of COG -co COMPRESS=DEFLATE tile_*.tif mosaic.tif

# Or build a virtual mosaic without copying (often the right move):
gdalbuildvrt mosaic.vrt tile_*.tif
gdal_translate -of COG mosaic.vrt mosaic_cog.tif
```

### Resampling method selection

| Data type | Method |
|---|---|
| Continuous (elevation, temperature, reflectance) | `bilinear` (default), `cubic`, `cubicspline`, `lanczos` |
| Categorical (land cover classes, masks) | `near` or `mode` |
| Aggregating (high-res → low-res continuous) | `average` |

Never use `bilinear` on categorical data — it invents interpolated class values that don't exist.

## Python — GeoPandas and Shapely 2

The Shapely 2.0 rewrite (2023) made GeoPandas dramatically faster for vectorized ops. Treat geometry columns as arrays, not as iterables.

### Idiomatic patterns

```python
import geopandas as gpd

gdf = gpd.read_file("input.gpkg")
gdf = gpd.read_parquet("input.parquet")  # GeoParquet

# Push filters into readers when possible
subset = gpd.read_file("input.gpkg", bbox=(24.5, 59.3, 25.0, 59.5), engine="pyogrio")
parquet_subset = gpd.read_parquet("input.parquet", bbox=(24.5, 59.3, 25.0, 59.5))

# Vectorized operations (fast)
gdf["area_m2"] = gdf.to_crs(3301).area
gdf["centroid"] = gdf.geometry.centroid
gdf_buffered = gdf.to_crs(3301).buffer(500).to_crs(4326)

# Spatial join (R-tree-backed automatically in GeoPandas)
joined = gpd.sjoin(points, polygons, how="left", predicate="within")

# Nearest-neighbor join
nearest = gpd.sjoin_nearest(points, lines, distance_col="dist_m")

# Output
gdf.to_parquet("out.parquet")  # GeoParquet
gdf.to_file("out.gpkg", driver="GPKG")
```

### Anti-patterns in Python

```python
# DON'T: row-by-row iteration
for idx, row in gdf.iterrows():
    row["new"] = row.geometry.area  # slow

# DO: vectorized
gdf["new"] = gdf.geometry.area

# DON'T: nested loops for spatial joins
for p in points.geometry:
    for poly in polys.geometry:
        if poly.contains(p): ...

# DO: sjoin
gpd.sjoin(points, polys, predicate="within")
```

## Apache Sedona — distributed processing

When data exceeds single-machine memory (e.g. >100M features), Sedona on PySpark is the standard.

```python
from sedona.spark import *

config = SedonaContext.builder().appName("sedona-app").getOrCreate()
sedona = SedonaContext.create(config)

# Read GeoParquet
df = sedona.read.format("geoparquet").load("s3://bucket/huge_dataset.parquet")
df.createOrReplaceTempView("points")

# Distributed spatial join
result = sedona.sql("""
    SELECT p.*, poly.name 
    FROM points p, polygons poly 
    WHERE ST_Within(p.geometry, poly.geometry)
""")
```

## Python — xarray + rioxarray for raster

The modern raster stack. Replaces single-file rasterio for n-dimensional or time-series work.

```python
import rioxarray
import xarray as xr

# Open single raster
da = rioxarray.open_rasterio("input.tif", chunks={"x": 1024, "y": 1024})

# Reproject
da_3301 = da.rio.reproject("EPSG:3301")

# Clip to AOI
import geopandas as gpd
aoi = gpd.read_file("aoi.gpkg")
da_clipped = da.rio.clip(aoi.geometry, aoi.crs)

# Math is just xarray
ndvi = (da.sel(band=4) - da.sel(band=3)) / (da.sel(band=4) + da.sel(band=3))

# Write COG
ndvi.rio.to_raster("ndvi.tif", driver="COG", compress="DEFLATE")
```

### STAC → xarray (the canonical satellite ingest pattern)

```python
from pystac_client import Client
import odc.stac
import planetary_computer

client = Client.open(
    "https://planetarycomputer.microsoft.com/api/stac/v1",
    modifier=planetary_computer.sign_inplace,
)
items = client.search(
    collections=["sentinel-2-l2a"],
    bbox=[24.5, 59.3, 25.0, 59.5],
    datetime="2025-06-01/2025-08-31",
    query={"eo:cloud_cover": {"lt": 20}},
).item_collection()

ds = odc.stac.load(
    items,
    bands=["B04", "B08"],         # red, nir
    chunks={"x": 1024, "y": 1024},
    resolution=10,
    crs="EPSG:3301",
    bbox=[24.5, 59.3, 25.0, 59.5],
)

# Lazy NDVI per timestep
ndvi = (ds.B08 - ds.B04) / (ds.B08 + ds.B04)
ndvi_max = ndvi.max(dim="time").compute()  # materialize
ndvi_max.rio.to_raster("ndvi_max.tif", driver="COG")
```

`stackstac` is a leaner alternative; `odc-stac` is more featureful and is generally preferred.

## DuckDB Spatial — the analytics workhorse

DuckDB with the spatial and httpfs extensions is the most consequential addition to the open GIS stack in years. Single-binary install, embedded, reads GeoParquet over HTTP, has `ST_*` functions.

### Setup

```sql
INSTALL spatial; LOAD spatial;
INSTALL httpfs; LOAD httpfs;   -- for S3/HTTP reads
INSTALL h3 FROM community; LOAD h3;  -- for H3 indexing
```

### Reading

```sql
-- Local GeoParquet
SELECT count(*), ST_Extent(ST_Extent_Agg(geometry))
FROM read_parquet('buildings.parquet');

-- Remote S3 with predicate pushdown on bbox
-- Replace OVERTURE_RELEASE with a currently retained Overture release.
SELECT id, names.primary AS name
FROM read_parquet(
  's3://overturemaps-us-west-2/release/OVERTURE_RELEASE/theme=places/type=place/*.parquet'
)
WHERE bbox.xmin > 24.5 AND bbox.xmax < 25.0
  AND bbox.ymin > 59.3 AND bbox.ymax < 59.5;

-- GeoPackage / Shapefile / FlatGeobuf via ST_Read
SELECT * FROM ST_Read('input.gpkg');
```

### DuckDB Spatial — failure modes worth knowing in advance

* **CRS labels must match exactly.** DuckDB Spatial 1.5+ refuses `ST_Intersects(EPSG:4326, OGC:CRS84)` even though the coordinates are identical. Reads from GeoJSON tag geometries `OGC:CRS84` (RFC 7946 default), reads from GeoParquet may say `EPSG:4326`, and `ST_Read` of an Esri Shapefile preserves whatever the `.prj` says. Normalise on read: `ST_SetCRS(geom, 'OGC:CRS84')` (or `'EPSG:4326'` — pick one and stick to it). When transforming, use the same string as the source: `ST_Transform(geom, 'OGC:CRS84', 'EPSG:3301', always_xy := true)`. See `formats-and-crs.md` for the rule in full.
* **GeoParquet round-trips as `GEOMETRY`, not BLOB.** If you write `ST_AsWKB(geometry) AS geom_wkb` and then re-read the parquet, the column comes back typed as `GEOMETRY('OGC:CRS84')` — not `BLOB`. Don't wrap it in `ST_GeomFromWKB` on re-read; just use it. This bites whenever the writing and reading happen in different scripts.
* **`ST_Read` on osmium-exported GeoJSON often crashes on duplicate column names.** OSM features carry tags like `clc:code` that osmium can emit twice; DuckDB rejects the file with *“table 'st_read' has duplicate column name 'clc:code'”*. Two workarounds: pre-clean with `ogr2ogr -select 'name,leisure,landuse,…' clean.geojson dirty.geojson`, or read via `pyogrio.read_dataframe(path, columns=[…])` with an explicit allowlist and hand the geometry back to DuckDB through `register()`.
* **OSM tag names need quoting.** Many tags are SQL reserved words (`natural`, `cross`) or contain colons (`addr:street`, `garden:type`). Always double-quote them in DuckDB SQL: `"natural"`, `"garden:type" AS garden_type`. Forgetting one of these produces a confusing `Parser Error: syntax error at or near ","`.

### Spatial operations in SQL

```sql
-- Buildings within 200m of a road.
-- Transform lon/lat input to a metric CRS first; ST_DWithin is planar.
WITH b AS (
  SELECT id, ST_Transform(geometry, 'EPSG:4326', 'EPSG:3301', always_xy := true) AS geom
  FROM buildings
),
r AS (
  SELECT id, ST_Transform(geom, 'EPSG:4326', 'EPSG:3301', always_xy := true) AS geom
  FROM roads
)
SELECT b.*
FROM b
JOIN r ON ST_DWithin(b.geom, r.geom, 200);

-- Aggregate to H3 cells at resolution 9
SELECT h3_latlng_to_cell(ST_Y(ST_Centroid(geom)), ST_X(ST_Centroid(geom)), 9) AS h3,
       count(*) AS n
FROM places GROUP BY h3;

-- Export
COPY (SELECT * FROM analysis_result)
TO 'out.parquet' (FORMAT PARQUET, COMPRESSION ZSTD);
```

### Persistence

```bash
# Persistent DB so tables survive between sessions
duckdb my_gis.duckdb
```

## PostGIS — production query layer

When data needs to live in a queryable, multi-user database with mature tooling, PostGIS remains the standard.

### Setup

```sql
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS postgis_raster;       -- if needed
CREATE EXTENSION IF NOT EXISTS pgrouting;            -- for routing
CREATE EXTENSION IF NOT EXISTS h3;                   -- via h3-pg
```

### Indexing — non-negotiable at scale

```sql
-- Every geometry column needs a GIST index
CREATE INDEX idx_buildings_geom ON buildings USING GIST (geom);

-- For very large append-only tables ordered spatially, BRIN is cheap
CREATE INDEX idx_pings_geom ON pings USING BRIN (geom) WITH (pages_per_range = 32);

-- Check that indexes are used
EXPLAIN ANALYZE SELECT * FROM buildings WHERE ST_Intersects(geom, ST_MakeEnvelope(...));
```

### Common patterns

```sql
-- Spatial join
SELECT b.id, d.name AS district
FROM buildings b
JOIN districts d ON ST_Within(b.geom, d.geom);

-- Distance-based with meters: either store/project a metric geometry column,
-- or use geography for lon/lat point/radius queries.
SELECT b.id
FROM buildings b
WHERE ST_DWithin(
  b.geom::geography,
  ST_SetSRID(ST_MakePoint(24.75, 59.44), 4326)::geography,
  500
);

-- Cluster
SELECT id, ST_ClusterKMeans(geom, 10) OVER () AS cluster_id
FROM points;

-- Vector tile generation in SQL
SELECT ST_AsMVT(tile, 'layer_name', 4096, 'geom')
FROM (
  SELECT id, name,
         ST_AsMVTGeom(geom, ST_TileEnvelope(z, x, y), 4096, 64, true) AS geom
  FROM features
  WHERE geom && ST_TileEnvelope(z, x, y)
) AS tile;
```

`ST_AsMVT` + `ST_TileEnvelope` is the modern in-database tile-generation pattern; pair with Martin or pg_tileserv to serve.

## PDAL — point cloud pipelines

PDAL pipelines are JSON specs. Compose readers, filters, and writers.

### Ground classification + DTM generation

```json
{
  "pipeline": [
    "input.laz",
    {
      "type": "filters.smrf",
      "scalar": 1.2,
      "slope": 0.2,
      "threshold": 0.45,
      "window": 16.0
    },
    {
      "type": "filters.range",
      "limits": "Classification[2:2]"
    },
    {
      "type": "writers.gdal",
      "filename": "dtm.tif",
      "resolution": 1.0,
      "output_type": "min",
      "data_type": "float32"
    }
  ]
}
```

Run with `pdal pipeline pipeline.json`.

### Fast format conversion

```bash
pdal translate input.las output.laz                   # uncompressed → compressed
pdal translate input.laz output.copc.laz \
  --writers.copc.filename=output.copc.laz             # → COPC
pdal info input.laz                                   # metadata
pdal info input.laz --stats                           # with stats
```

## Common pipeline shapes

### "Source → DuckDB → analysis → tile / serve"

```
Overture S3 GeoParquet
  → DuckDB query with bbox filter and explicit metric CRS for distance/area
  → ST_AsMVT in DuckDB or write GeoParquet → tippecanoe
  → PMTiles
  → Martin / static S3
  → MapLibre
```

### "Sentinel STAC → xarray → COG → tiles"

```
pystac-client search (cloud<20%)
  → odc.stac.load (lazy, dask)
  → compute index (NDVI, NDWI, etc.)
  → reduce time dim
  → rio-cogeo write
  → TiTiler dynamic tiling, OR pre-tile to PMTiles
```

### "OSM → PostGIS → application"

```
Geofabrik PBF
  → osm2pgsql --slim -G
  → GIST indexes on geom
  → application queries via SQL
  → optional MVT via Martin or pg_tileserv
```

## Pipeline shell-scripting hygiene

Bash is fine for orchestrating tools (`ogr2ogr`, `tippecanoe`, `osmium`, etc.), but inlining substantial Python inside a shell script via `$(cat <<'PY' … PY)` heredocs interacts badly with apostrophes in the embedded code and is hard to debug. Default to *separate* `.py` files invoked from the shell script:

```bash
# in scripts/run.sh
.venv/bin/python scripts/extract_pois.py
ogr2ogr -f Parquet ... pois.parquet
tippecanoe -o pois.pmtiles ...
```

Reserve heredocs for short SQL or for trivial Python that has no quoting risk.

## Performance reminders

* GIST/R-tree indexing is mandatory above ~10k features
* Push joins to the database, not Python
* Chunk size for raster work should align with COG internal block size (default 512×512)
* For Overture S3 queries, the `bbox` struct column enables predicate pushdown — filter on it
* Materialized views in PostGIS for expensive recurring queries
* For dask-backed xarray, keep chunks at 50–500 MB each — too small wastes scheduling overhead, too large blows memory
