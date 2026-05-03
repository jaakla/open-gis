# Analytics

Concrete patterns for vector, raster, terrain, network, and point cloud analysis. The general rule: choose the layer (DuckDB / PostGIS / xarray / specialized tool) by data shape, not by familiarity.

## Vector analytics

### Spatial joins at scale

For up to ~50M features on a single machine, DuckDB Spatial is the fastest path:

```sql
-- Buildings tagged with their administrative division
SELECT b.id, b.height, d.division_id, d.name
FROM read_parquet('buildings.parquet') b
JOIN read_parquet('divisions.parquet') d
  ON ST_Within(b.geometry, d.geometry);
```

Above that, push to PostGIS with proper GIST indexes, or to Sedona for distributed.

### H3 / S2 cell pre-aggregation

For very large point datasets, aggregate to H3 cells before any expensive join:

```sql
-- DuckDB with h3 extension
SELECT h3_latlng_to_cell(ST_Y(ST_Centroid(geom)), ST_X(ST_Centroid(geom)), 8) AS h3,
       count(*) AS n
FROM big_points
GROUP BY h3;
```

H3 resolution choice (rough guide; average area and edge length vary by latitude and pentagon proximity):

| Resolution | Avg hex area | Avg edge length | Typical use |
|---|---:|---:|---|
| 6 | ~36 km2 | ~3.7 km | regional grid |
| 7 | ~5.2 km2 | ~1.4 km | city district |
| 8 | ~0.74 km2 | ~531 m | neighborhood |
| 9 | ~0.105 km2 | ~201 m | block / facility catchment |
| 10 | ~0.015 km2 | ~76 m | parcel-scale screening |

Most H3 APIs expect latitude, longitude order. When coordinates come from WKB/GeoJSON geometries, use `ST_Y(...)` for latitude and `ST_X(...)` for longitude.

### Clustering

```python
# DBSCAN on coordinates (project first)
from sklearn.cluster import DBSCAN
import geopandas as gpd
import numpy as np

gdf_3301 = gdf.to_crs(3301)
coords = np.column_stack([gdf_3301.geometry.x, gdf_3301.geometry.y])
labels = DBSCAN(eps=200, min_samples=5).fit_predict(coords)
gdf["cluster"] = labels  # -1 means noise
```

```sql
-- KMeans in PostGIS
SELECT id, geom, ST_ClusterKMeans(geom, 10) OVER () AS cluster_id
FROM points;
```

### Hotspot detection

PySAL covers spatial autocorrelation properly:

```python
from libpysal.weights import Queen
from esda.moran import Moran_Local

w = Queen.from_dataframe(gdf)
w.transform = "r"
li = Moran_Local(gdf["value"], w)
gdf["lisa_p"] = li.p_sim
gdf["lisa_q"] = li.q  # quadrant: 1=HH, 2=LH, 3=LL, 4=HL
```

### Geocoding

* **Nominatim** — OSM-based; self-host with `mediagis/nominatim-docker` for any volume
* **Pelias** — multi-source (OSM + WhosOnFirst + OpenAddresses)
* **Photon** — fast OSM-based, autocomplete-friendly

For one-off small jobs, the public Nominatim is fine but rate-limited (1 req/sec, attribution required). For anything systematic, self-host.

```python
# Self-hosted Nominatim
import requests
r = requests.get(
    "http://localhost:8080/search",
    params={"q": "Tartu mnt 25, Tallinn", "format": "json", "limit": 1},
).json()
```

## Raster analytics

### Vegetation indices and band math

xarray makes this trivial — no need for `gdal_calc.py` for anything Python-side:

```python
ds = odc.stac.load(items, bands=["B03", "B04", "B08", "B11"], chunks={"x": 1024})

ndvi = (ds.B08 - ds.B04) / (ds.B08 + ds.B04)        # vegetation
ndwi = (ds.B03 - ds.B08) / (ds.B03 + ds.B08)        # open water (McFeeters)
mndwi = (ds.B03 - ds.B11) / (ds.B03 + ds.B11)       # open water, urban-resistant
ndmi = (ds.B08 - ds.B11) / (ds.B08 + ds.B11)        # vegetation moisture (Gao NDWI)
ndbi = (ds.B11 - ds.B08) / (ds.B11 + ds.B08)        # built-up

ndvi_max = ndvi.max(dim="time")
ndvi_max.rio.to_raster("ndvi_max.tif", driver="COG")
```

### Time-series reductions

```python
# Median over time (cloud-robust composite)
median = ds.median(dim="time")

# Anomaly vs long-term mean
anomaly = ds - ds.mean(dim="time")

# Linear trend per pixel (via polyfit)
trend = ds.polyfit(dim="time", deg=1).polyfit_coefficients.sel(degree=1)
```

### Zonal statistics — use `exactextract`

`exactextract` is significantly faster than `rasterstats` and handles partial pixel coverage correctly (which matters for any AOI smaller than ~100 pixels).

```python
from exactextract import exact_extract
import geopandas as gpd

zones = gpd.read_file("zones.gpkg")
result = exact_extract(
    "ndvi.tif",
    zones,
    ops=["mean", "stdev", "min", "max", "count"],
    output="pandas",
)
zones = zones.join(result)
```

In PostGIS:

```sql
SELECT z.id, (ST_SummaryStats(ST_Clip(r.rast, z.geom))).*
FROM zones z, rasters r
WHERE ST_Intersects(r.rast, z.geom);
```

### Cloud masking for Sentinel-2

Use the SCL (Scene Classification Layer) band that comes with L2A products:

```python
ds = odc.stac.load(items, bands=["B04", "B08", "SCL"], chunks={"x": 1024})

# SCL classes: 0=NoData, 1=Saturated, 3=CloudShadow, 8=CloudMedium, 9=CloudHigh, 10=ThinCirrus
mask = ~ds.SCL.isin([0, 1, 3, 8, 9, 10])
clean = ds.where(mask)
```

For higher quality, use `s2cloudless` masks where available.

### Raster correctness checklist

Before computing indices or statistics:

* Confirm band names, units, scale/offset, and NoData from `gdalinfo` or STAC asset metadata.
* Align mixed-resolution bands deliberately. Sentinel-2 B03/B04/B08 are 10m; B11 is 20m, so choose resampling intentionally before NDMI/MNDWI/NDBI.
* Keep categorical bands and masks on nearest-neighbor resampling.
* Preserve CRS and transform after xarray operations; verify `rio.crs`, `rio.transform()`, and output bounds.
* Avoid calling `.values` on large dask-backed arrays. Sample training pixels or process by chunks.

### Classification

For land cover classification on raster stacks, scikit-learn fits well:

```python
import xarray as xr
from sklearn.ensemble import RandomForestClassifier

# Stack bands as features. For full scenes, sample pixels or train chunk-wise;
# do not materialize a continental stack with `.values`.
features = xr.concat([ds.B02, ds.B03, ds.B04, ds.B08], dim="band").transpose(..., "band")
X = features.values.reshape(-1, features.sizes["band"])  # only for small AOIs
X_train = ...  # sampled labelled pixels
y_train = ...  # from labelled samples

clf = RandomForestClassifier(n_estimators=100).fit(X_train, y_train)
y_pred = clf.predict(X)
classified = y_pred.reshape(features.shape[:-1])
```

For deep learning on EO data, **TorchGeo** ships pretrained models on Sentinel-2 and Landsat. **segment-geospatial** (samgeo) wraps Segment Anything for satellite imagery — useful for footprint extraction.

## Terrain & hydrology

`gdaldem` covers basics; **WhiteboxTools** is the comprehensive open suite. **GRASS** has the deepest hydrology routines.

### Quick terrain derivatives via GDAL

```bash
gdaldem slope dtm.tif slope.tif -compute_edges
gdaldem aspect dtm.tif aspect.tif
gdaldem hillshade dtm.tif hillshade.tif -z 1.0 -az 315 -alt 45
gdaldem TPI dtm.tif tpi.tif    # topographic position index
gdaldem roughness dtm.tif roughness.tif
```

### Whitebox Tools — broader analysis

```python
from whitebox import WhiteboxTools
wbt = WhiteboxTools()

wbt.fill_depressions("dtm.tif", "filled.tif")
wbt.d8_pointer("filled.tif", "d8.tif")
wbt.d8_flow_accumulation("d8.tif", "flowacc.tif", out_type="cells")
wbt.extract_streams("flowacc.tif", "streams.tif", threshold=1000)
wbt.watershed("d8.tif", "outlets.shp", "watersheds.tif")
```

### GRASS for serious hydrology

`r.watershed`, `r.terraflow`, `r.stream.extract` — all more rigorous than GDAL's basics. Run via `grass --tmp-location` for one-shot scripts, or via the `grass-session` Python package.

## Network analysis

### Choosing a routing engine

| Engine | Strengths | Drawbacks |
|---|---|---|
| **OSRM** | Fastest car routing, simple HTTP API, mature Docker image | Single profile per instance |
| **Valhalla** | Multi-modal, dynamic costing options, isochrones built in, tile-based architecture | More complex setup |
| **GraphHopper** | Multi-modal, strong public-transit support, JVM | Memory-hungry |
| **OpenRouteService** | Hosted + self-host, multi-modal, isochrones, matrix | Hosted has rate limits |
| **pgRouting** | Routing inside PostGIS, integrates with rest of DB | Slower than dedicated engines for large graphs |
| **r5py** | Multi-modal accessibility (transit + walk + bike) for research | Java under the hood, batch-oriented |

### Self-host pattern (Valhalla example)

```bash
# Pull extract
wget https://download.geofabrik.de/europe/estonia-latest.osm.pbf -P data/

# Generate config and tiles
docker run --rm -v $PWD/data:/data \
  ghcr.io/gis-ops/docker-valhalla/valhalla:latest

# Run server
docker run -d -p 8002:8002 -v $PWD/data:/data \
  ghcr.io/gis-ops/docker-valhalla/valhalla:latest
```

```python
import requests
r = requests.post("http://localhost:8002/route", json={
    "locations": [
        {"lat": 59.437, "lon": 24.754},  # Tallinn
        {"lat": 58.378, "lon": 26.729},  # Tartu
    ],
    "costing": "auto",
}).json()
```

### Isochrones

Valhalla has `/isochrone`; OpenRouteService has `/v2/isochrones`. Both return GeoJSON polygons.

### Network from OSM in one line

```python
import osmnx as ox
G = ox.graph_from_place("Tallinn, Estonia", network_type="walk")
nodes, edges = ox.graph_to_gdfs(G)
```

For accessibility analysis (population within X minutes), use **pandana** (contraction hierarchies, very fast for many origins) or **r5py** (multi-modal with transit).

## Point cloud analytics

### Standard workflow

1. **Inspect** — `pdal info input.laz --stats` — confirm classification status, density, extent.
2. **Classify ground** if not already — PDAL `filters.smrf` (Simple Morphological Filter) is the standard.
3. **Extract DTM** — ground points → raster minimum or TIN.
4. **Extract DSM** — all returns → raster maximum.
5. **Compute CHM** — DSM − DTM = canopy height model.
6. **Per-feature heights** — zonal statistics of CHM over building footprints.

### Building height per footprint (concrete recipe)

```bash
# 1. Generate DTM (ground only, 1m resolution)
pdal pipeline dtm-pipeline.json    # uses filters.smrf + writers.gdal min

# 2. Generate DSM (all returns)
pdal pipeline dsm-pipeline.json    # writers.gdal max

# 3. CHM
gdal_calc.py -A dsm.tif -B dtm.tif --outfile=chm.tif --calc="A-B"

# 4. Median CHM per building footprint
exactextract -p buildings.gpkg -r chm.tif -s "median" \
  -o buildings_with_height.gpkg
```

## Mobility / accessibility analysis

A common end-to-end pattern: "what fraction of population is within N minutes of facility type X by transit?"

```
1. Population raster (e.g. WorldPop or GHSL)
2. Facility points (Overture places, filtered)
3. r5py multi-modal isochrones from each facility
4. Union isochrones, rasterize at population grid resolution
5. Sum population inside vs outside
```

`r5py` handles steps 3–5; it's the go-to for multi-modal accessibility with realistic transit schedules (GTFS).

## Quick selection guide

| Question shape | Tool |
|---|---|
| "How many X within Y of Z?" | DuckDB or PostGIS spatial join |
| "Hotspots / clusters of X?" | DBSCAN (small), H3 aggregation (large), PySAL LISA (statistical) |
| "Time-series of vegetation / water / built-up?" | xarray + odc.stac on Sentinel-2 |
| "Travel time / accessibility?" | Valhalla, OSRM, or r5py |
| "Statistics per zone?" | exactextract (fast) or PostGIS ST_SummaryStats |
| "Slope / aspect / hillshade?" | gdaldem |
| "Watershed / flow accumulation?" | WhiteboxTools or GRASS |
| "Building heights from LiDAR?" | PDAL + exactextract over CHM |
| "Land cover classification?" | scikit-learn or TorchGeo on Sentinel stacks |
