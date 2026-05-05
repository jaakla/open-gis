# Services and Scale

Use this reference when a request moves beyond local/city/state analysis or asks for global elevation, basemaps, routing, geocoding, reverse geocoding, place search, postcode lookup, or address validation.

## First decision: local, cloud-native, or hosted service

| Scale / need | Default approach |
|---|---|
| Neighborhood / city / small state | Local GDAL, DuckDB, GeoPandas, PostGIS, QGIS are usually fine |
| Country / region | Use local tools with partitioning, bbox filters, indexes, cloud-native reads, or national services |
| Continental / global batch analytics | Prefer cloud-native datasets, prepartitioned Parquet/COG/Zarr, distributed compute, or precomputed products |
| Interactive lookup/search/routing/elevation | Prefer hosted APIs or SaaS if coverage, latency, SLA, and terms fit |
| Product-facing basemap or place search | Use managed services unless self-hosting is a deliberate product/ops choice |

Do not download or process planet-scale data just to answer point lookups, postcode lookups, route calculations, or geocoding tasks. Use existing services when they provide the needed quality and legal terms.

## Hosted service selection checklist

Before recommending a free public endpoint or paid SaaS provider, check:

* Coverage for the target country, language, address format, transport mode, and data freshness.
* Accuracy level needed: rooftop, parcel, interpolated street, postcode centroid, POI, route access point, or coarse locality.
* Terms: caching, storing results, batch use, derivative data, attribution, display restrictions, and export rights.
* Privacy: whether user addresses, GPS traces, or customer locations can leave the user's environment.
* Cost and quota: free tier, per-request pricing, batch pricing, rate limits, SLA, support, and overage controls.
* Reproducibility: whether results can change over time and whether provider IDs/version fields are returned.

Use SaaS when the task is global, latency-sensitive, quality-sensitive, or operationally expensive to self-host. Prefer self-hosted/open services when privacy, reproducibility, offline use, customization, or high-volume cost dominate.

## Service patterns by capability

| Capability | Open/self-host path | Hosted/SaaS path | Notes |
|---|---|---|---|
| Basemaps | Protomaps/PMTiles, OpenMapTiles, Planetiler, Tilemaker | MapTiler Cloud, Mapbox, Stadia Maps, Esri basemaps, CartoDB | Do not use `tile.openstreetmap.org` for production or heavy traffic; OSMF tiles are best-effort community infrastructure |
| Elevation point/profile | OpenTopoData self-hosted or public API for small jobs; local COG/DEM for analysis | MapTiler Elevation API, Mapbox Terrain-DEM/Terrain-RGB, Google Elevation API, Esri Elevation service, Cesium World Terrain for 3D | Track vertical datum, source resolution, interpolation, water/bathymetry behavior, and quota |
| Routing / isochrones / matrices | OSRM, Valhalla, GraphHopper, pgRouting, r5py; self-host from OSM/GTFS | Google Routes API, Mapbox Directions/Isochrone, HERE Routing, TomTom Routing, GraphHopper Directions API, OpenRouteService | Global routing is expensive to preprocess and keep fresh; hosted traffic/turn restrictions often justify SaaS |
| Geocoding / reverse geocoding | Nominatim, Pelias, Photon, OpenAddresses, GeoNames; national address services | Google Geocoding/Places/Address Validation, Mapbox Geocoding/Search, HERE Geocoding & Search, TomTom Search, Esri Geocoding, Geoapify | Public Nominatim is for light use only; for systematic geocoding self-host or use a paid service |
| Place search / POI | Overture places, OSM tags, GeoNames, Wikidata, Who's On First | Google Places, Mapbox Search, HERE Discover, TomTom Search, Foursquare, Esri Places | POI freshness and brand/category normalization are often better in SaaS |
| Postcode lookup | National postal/open-data portals, GeoNames postal codes, OpenAddresses | GeoPostcodes, Google Address Validation, HERE, Loqate/Smarty-style address vendors | Postcodes vary by country: points, ranges, delivery routes, or polygons; avoid treating them as stable admin areas |

## Global elevation guidance

For city/regional raster analysis, local DEM/COG processing is usually better because you control resolution, vertical datum, NoData, and hydrologic conditioning. For global point queries, elevation profiles, terrain rendering, and UI interaction, use prebuilt elevation APIs or terrain tiles.

Always record:

* Elevation source and resolution.
* Vertical datum/geoid if known.
* Interpolation method.
* Whether water/bathymetry is included.
* API quota and license terms.

## Geocoding and address quality

Geocoding is not just GIS; it is data product quality. For addresses, evaluate match quality before analysis:

* Exact rooftop/access point.
* Parcel or building centroid.
* Interpolated street address.
* Postcode centroid.
* Locality/admin centroid.
* Failed or ambiguous match.

Keep raw input, normalized address, provider, provider ID, confidence/match code, result type, coordinates, and timestamp. For privacy-sensitive workloads, geocode locally or with a provider contract that covers the data.

## Place IDs and crosswalks

Use IDs for joins and deduplication; use names for display. A good place record often carries multiple IDs:

```text
name
geometry
country_code
admin_path
osm_type + osm_id
wikidata_qid
geonames_id
unlocode
provider_place_id
open_location_code
source + source_version
```

Never assume IDs from one provider can be reused with another provider's API. Crosswalks need provenance, match confidence, and refresh dates.

## Hybrid workflows

Common production pattern:

1. Use SaaS/API for global search, geocoding, place lookup, routing, or elevation point queries.
2. Store allowed stable IDs, normalized outputs, match confidence, and timestamps.
3. Run downstream spatial analysis locally in DuckDB/PostGIS/GeoPandas on a bounded AOI.
4. Publish results as PMTiles/COG/API with attribution and service terms carried forward.
