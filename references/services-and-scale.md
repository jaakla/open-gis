# Services and Scale

Use this reference when a request moves beyond local/city/state analysis or asks for global elevation, basemaps, routing, geocoding, reverse geocoding, place search, postcode lookup, or address validation.

The open datasets themselves (Copernicus DEM, Overture, OSM, GeoNames, place-ID namespaces) are catalogued in `data-sources.md`; local tool selection is in `processing.md`; tile serving in `web-delivery.md`. This file is about the **decision**: local vs self-hosted service vs hosted API, and which specific option to reach for first.

## First decision: local, self-hosted service, or hosted API

| Scale / need | Default approach |
|---|---|
| Neighborhood / city / county | Local GDAL, DuckDB, GeoPandas, PostGIS, QGIS. No service needed for analysis. |
| Country / region | Local tools with bbox-filtered cloud-native reads (Overture GeoParquet, COG, STAC), national extracts (Geofabrik, national portals), spatial indexes. Self-hosted routing/geocoding is a one-off build per country. |
| Continental / global batch analytics | Prepartitioned Parquet/COG/Zarr read with predicate pushdown; distributed compute (Sedona) only for true planet-scale joins; precomputed products where they exist. |
| Interactive lookup / search / routing / elevation queries | Hosted API or self-hosted service. Never stand up planet processing to answer point queries. |
| Product-facing basemap or place search | Managed service unless self-hosting is a deliberate product/ops decision with someone owning uptime. |

Rules of thumb that hold in practice:

* **Analysis stays local; interaction goes to services.** Compute buffers, joins, zonal stats, and accessibility on data you control. Use services for the things whose value is freshness, global coverage, or sub-second latency: geocoding, routing with traffic, place search, terrain in a browser.
* **Sample rasters directly instead of calling elevation APIs.** Ten thousand points against a COG with `rasterio` is seconds; against an API it is quota, retries, and a non-reproducible result.
* **Do not download the planet to answer a lookup.** A postcode centroid, a route, or an address match is a service call or a national dataset, not a 70 GB OSM import.
* **Country-scale self-hosting is cheap; planet-scale is a job.** OSRM/Valhalla/Nominatim for one European country builds in under an hour on a laptop. A planet Nominatim import takes days and ~1 TB of NVMe; a planet OSRM car graph needs a large-memory machine. Only go planet-scale with a real reason.
* **Terms of service decide more than technology.** Whether you may store, cache, batch, or display results away from the provider's map is usually the deciding factor between two otherwise equivalent providers.

## The default picks

When nothing in the task forces a different choice, start here. Justify deviations in the project rationale.

| Need | Prototype / small one-off | Self-hosted production | Hosted production |
|---|---|---|---|
| Basemap (web) | OpenFreeMap or CARTO Positron/Voyager with MapLibre | Protomaps PMTiles (planet build or area extract) on object storage + CDN | Protomaps API, MapTiler Cloud, or Stadia Maps; Mapbox/Google/ArcGIS only if you are already in that ecosystem |
| Basemap (QGIS / print) | OSM raster XYZ via QuickMapServices (attribution, low volume) | National WMTS/orthophoto; own PMTiles/MBTiles | MapTiler / Stadia raster tiles |
| Elevation (analysis) | Copernicus GLO-30 COG sampled locally | National LiDAR DTM in local COG | — (do not use APIs for analysis) |
| Elevation (web terrain / profiles) | MapTiler Terrain-RGB free tier | Terrain-RGB/Terrarium tiles from AWS Terrain Tiles or own DEM via `rio rgbify` | MapTiler Elevation API, Mapbox Terrain-DEM, Cesium World Terrain (3D) |
| Routing / isochrones (analysis) | openrouteservice public API, OSRM demo server (light use only) | **Valhalla** (multimodal, isochrones, matrix, time-dependent); OSRM when you need the fastest large car matrices | Stadia Maps (Valhalla), GraphHopper Directions API, openrouteservice |
| Routing (product ETAs with traffic) | — | — | Google Routes API, HERE Routing v8, TomTom Routing, Mapbox Directions |
| Transit accessibility | r5py on GTFS + OSM | r5py / OpenTripPlanner | Google Routes (transit), HERE Public Transit |
| Geocoding (bulk, open data) | Country Nominatim in Docker; national geocoder if one exists | **Pelias** (OSM + OpenAddresses + WOF) or Nominatim | OpenCage or Geocode Earth (results may be stored); Geoapify |
| Geocoding (best global match quality) | — | — | Google Geocoding / Address Validation, HERE Geocoding & Search, Mapbox Geocoding (permanent endpoint if storing) |
| Autocomplete / search box | Photon public instance | Photon or Pelias | Mapbox Search Box, Google Places Autocomplete, HERE Autosuggest |
| Place / POI data for analysis | Overture Places (GERS IDs) | Overture Places + Foursquare OS Places + OSM, deduplicated | Google Places (New) when freshness beats storage restrictions |
| Postcode lookup | National open dataset; GeoNames postal codes as global fallback | postcodes.io (UK), national address registers | GeoPostcodes, Google Address Validation, Loqate, Smarty |

## Hosted service selection checklist

Before recommending a free public endpoint or paid provider, check:

* **Coverage** for the target country, language/script, address format, transport mode, and data freshness. Quality is uneven: a provider excellent in the US may interpolate badly in Estonia or Japan.
* **Accuracy level actually needed**: rooftop, parcel, entrance/access point, interpolated street, postcode centroid, POI, or locality. Do not pay for rooftop when tract assignment is the question.
* **Terms**: storing results, caching duration, batch use, derivative datasets, attribution, display restrictions (must results appear on the provider's own map?), export rights. This is where Google, Mapbox, HERE, and ArcGIS differ most from OpenCage, Geoapify, or self-hosted stacks.
* **Privacy**: whether user addresses, GPS traces, or customer locations may leave your environment. Under GDPR the provider is a processor; you need a data-processing agreement. If in doubt, self-host.
* **Cost and quota**: free tier, per-request pricing, batch pricing, rate limits, SLA, overage controls. Free tiers change often; verify at decision time and record the plan in the rationale.
* **Reproducibility**: results drift as provider data changes. Snapshot responses, record provider/endpoint version, and never re-query inside the reproducible pipeline.

Use hosted services when the task is global, latency-sensitive, quality-sensitive, or operationally expensive to self-host. Prefer self-hosted/open services when privacy, reproducibility, offline use, customization, or high-volume cost dominate.

Volume heuristics for geocoding/routing calls (order of magnitude, verify pricing):

* **< 10k one-off**: public free tiers or a Docker container on a laptop. Do not overthink it.
* **10k – 1M**: paid API with batch pricing and stored results, or a country-level self-hosted instance. Choose by terms and privacy, not price.
* **> 1M recurring**: self-host or negotiate an enterprise contract. Per-request API pricing at this volume is usually more than the machine.

## Basemaps

**Client library**: MapLibre GL JS. Mapbox GL JS has been proprietary since v2 and requires a Mapbox account; only use it when you are buying Mapbox services anyway. Leaflet remains fine for simple raster overlays; OpenLayers for OGC-heavy or non-Web-Mercator work (details in `web-delivery.md`).

Almost every basemap below is rendered from OpenStreetMap data. "Open data" does not mean "free to serve": the distinction that matters is **who runs the tile server**. Anything served from a vendor's domain is a hosted service with its own terms, quota, and failure modes, even when the style and schema are open source.

### Self-host (you serve the tiles)

You own uptime, CDN, and data refresh; in exchange there is no key, no per-tile bill, no terms beyond ODbL attribution, and the map keeps working when the vendor changes pricing.

* **Protomaps PMTiles** — the modern default. Daily planet builds (~120 GB) and `pmtiles extract` for an area; open styles (light, dark, white, grayscale, black). One file on object storage behind a CDN, no tile server (see `web-delivery.md`).
* **Planetiler** — generates OpenMapTiles-schema or Protomaps-schema tiles from an OSM extract; a country takes minutes, the planet a few hours on a 64 GB machine. Use it when you need a custom schema, a pinned data vintage, or a non-OSM data mix. **Tilemaker** is the lighter alternative for small extracts.
* **OpenMapTiles** is a schema and style set, not a service. The *prebuilt* OpenMapTiles downloads and the hosted tiles are MapTiler products (paid, non-commercial exceptions); generate your own with Planetiler if you want them free.
* **OpenFreeMap / VersaTiles** publish their full planet tilesets and container images, so either can be self-hosted as well as used hosted.
* **National raster/vector basemaps** — build MBTiles/PMTiles from national open data (e.g. ETAK-derived tiles for Estonia) when the product is single-country and must look authoritative.

Serve with a static host for PMTiles, or **Martin** / **tileserver-gl** when tiles live in PostGIS/MBTiles.

### Hosted (someone else serves the tiles)

Access models fall into five groups. Know which one you are signing up for; verify current quotas at decision time and record the plan in the rationale.

| Service | Data basis | Key / registration | Free usage | Beyond free | Notes |
|---|---|---|---|---|---|
| **OSM tile.openstreetmap.org** | OSM raster (Carto style) | none, but valid `User-Agent`/`Referer` required | community fair use only; no published quota, may be throttled or blocked | not available; there is no paid tier | OSMF usage policy excludes heavy use, bulk download, and distributed apps with meaningful traffic. QGIS sessions and internal demos only. |
| **OpenFreeMap** | OSM vector, MapLibre styles | none | unlimited, commercial use allowed | donation-funded; no SLA | Best zero-friction option for prototypes and low-risk products. Self-hostable. |
| **VersaTiles** | OSM vector (Shortbread schema) | none | unlimited fair use | community project; no SLA | Same class as OpenFreeMap; European-run. |
| **CARTO basemaps** (Positron, Dark Matter, Voyager) | OSM raster + vector via `basemaps.cartocdn.com` | free, CARTO expects registration/contact for commercial or high-traffic use | no hard published quota | enterprise agreement | Cleanest neutral data-viz backgrounds. Not self-hostable. The company is **CARTO** (formerly CartoDB, renamed 2016). |
| **Protomaps API** | OSM vector (Protomaps schema) | key | small evaluation allowance | flat monthly subscription | Same tiles as the self-host build; pick hosted when you do not want to own the CDN. |
| **MapTiler Cloud** | OSM/OpenMapTiles vector, raster, terrain, satellite | key (free plan) | monthly request allowance on free plan, attribution required | tiered monthly plans, then per-request | Easiest "just works" hosted option for MapLibre; also geocoding, elevation, and static maps. |
| **Stadia Maps** | OSM vector/raster; Stamen (Toner, Terrain, Watercolor), Alidade, OSM Bright | domain/key registration | free tier for non-commercial use | monthly plans | Also hosted Valhalla routing and Pelias geocoding under the same key. |
| **Thunderforest / Jawg** | OSM raster/vector (Outdoors, Transport, Cycle...) | key | hobby tier with monthly tile cap | monthly plans | Thunderforest is the usual choice for outdoor/cycling cartography. |
| **Mapbox** | OSM + proprietary; Streets, Satellite, traffic | key + account | monthly free map loads / tile requests | pay-as-you-go per load; expensive at scale | Best styling tools (Studio). Mapbox GL JS v2+ licence binds you to Mapbox for the client too. |
| **Google Maps Platform** | proprietary | key + billing account | monthly free allowance per SKU | pay-as-you-go per load | Required when displaying Google Places/Directions content. No raster/vector tile access for third-party clients outside the Map Tiles API. |
| **ArcGIS basemap styles (Esri)** | proprietary + OSM-based styles; World Imagery | key (ArcGIS Location Platform) | monthly tile allowance (large, currently millions of tiles) | pay-as-you-go | Natural when the organisation already runs ArcGIS. |
| **HERE / TomTom** | proprietary | key | freemium monthly/daily allowance | pay-as-you-go | Worth it mainly when you already use their routing and traffic. |
| **National agency WMTS/XYZ** | authoritative national data (e.g. Maa- ja Ruumiamet in Estonia) | usually none | free under open-data licence; fair use | not applicable | Often the best single-country basemap: authoritative, free, no key, but no SLA and sometimes strict fair-use or referer rules. |
| **Azure Maps** | proprietary (TomTom-based roads, Microsoft imagery) | key or Entra ID via an Azure subscription | monthly free transaction allowance | pay-as-you-go per 1,000 transactions | Render API serves raster and vector tiles, satellite, traffic and weather overlays; works with MapLibre. The successor to **Bing Maps**, which is being retired — do not start new work on Bing. Natural when the organisation is already on Azure. |

Selection priority:

* **No key, no quota, no SLA** (OSM, OpenFreeMap, VersaTiles, national WMTS): fine when an outage is acceptable and traffic is modest; keep a fallback source in the style and monitor for it. OSM's own tiles are the one entry here that must not be used in a product at all.
* **Free, registration expected** (CARTO): treat as free for demos and dashboards; confirm terms before a customer-facing launch.
* **Key with free tier** (MapTiler, Stadia, Thunderforest, Jawg, Protomaps API, ArcGIS): the sensible default for products with real users and small budgets. Free tiers are usually enough for internal tools; budget for the first paid tier before launch.
* **Key with billing account** (Mapbox, Google, HERE, TomTom): pay-per-load from day one beyond a free allowance; costs scale with popularity, so put a spend cap in place before shipping.
* **Self-host**: the cost is one engineer's attention and a CDN bill; the benefit is independence from every row above.

Attribution is not optional for any of these. Carry the attribution string into the project manifest and the rendered product.

## Elevation

For city/regional raster analysis, process a local DEM/COG: you control resolution, vertical datum, NoData, and hydrologic conditioning. For global point queries, profiles in apps, terrain rendering, and 3D, use prebuilt terrain tiles or elevation APIs. The open DEM catalogue lives in `data-sources.md`.

**Default for batch point/line sampling**: read the COG directly. `/vsicurl/` and `rasterio.sample` handle thousands to millions of points without an API:

```python
import rasterio
with rasterio.open("/vsicurl/https://.../Copernicus_DSM_COG_10_N58_00_E026_00_DEM.tif") as src:
    heights = [v[0] for v in src.sample([(26.72, 58.38), (26.75, 58.36)])]  # lon, lat in src CRS
```

`gdallocationinfo -valonly -geoloc` does the same from the shell. For larger AOIs, clip/mosaic tiles once with `gdalwarp` to a local COG and sample that.

**Open / self-host services**:

* **AWS Terrain Tiles** (former Mapzen; `elevation-tiles-prod` on the AWS Open Data registry) — global Terrarium and Normal PNG tiles, free, no key. The standard self-hosting source for MapLibre `raster-dem` terrain and hillshade. Sources are mixed (SRTM, GMTED, ETOPO, national DEMs), so resolution and vertical datum vary by area.
* **Own DEM → Terrain-RGB**: `rio rgbify` (rio-rgbify) turns a national LiDAR DTM into Mapbox-encoded RGB tiles; serve as PMTiles. Use when you need 1 m detail in a web map.
* **OpenTopoData** — self-hostable elevation API with pluggable datasets (SRTM, ASTER, EU-DEM, GEBCO, NED...). Its public instance is rate-limited to roughly a thousand requests per day; self-host for anything systematic. **Open-Elevation** is the simpler alternative.

**Hosted**:

* **MapTiler** — Terrain-RGB tiles and an Elevation API (point/line/polygon), generous free tier.
* **Mapbox Terrain-DEM** (`mapbox.mapbox-terrain-dem-v1`) — raster-dem tileset for Mapbox GL terrain; the older Terrain-RGB tileset still exists.
* **Google Elevation API** — point and path sampling, returns resolution per result; good global fallback for small interactive workloads; results fall under Google's caching restrictions.
* **ArcGIS Elevation services (Esri)** — point elevation, profiles, viewshed; 3D terrain layers for ArcGIS clients.
* **Cesium World Terrain** (Cesium ion) — quantized-mesh terrain for 3D globes; the default for CesiumJS.

**Always record**:

* Source and native resolution (30 m GLO-30 is not "1 m LiDAR"; SRTM voids are filled differently per product).
* Vertical datum: Copernicus DEM uses **EGM2008**; SRTM uses **EGM96**; national DTMs use national datums (Estonia: **EH2000**); GPS ellipsoidal heights are neither. Mixing them yields errors of tens of metres.
* Surface vs terrain: Copernicus DEM and SRTM are **DSMs** (include canopy and buildings); LiDAR products separate DTM and DSM.
* Interpolation (nearest vs bilinear) and behaviour over water and NoData. For bathymetry use **GEBCO** globally or **EMODnet** in European seas; land DEMs report 0 or NoData over sea.
* Quota and license.

## Routing, isochrones, and matrices

**Self-host** (all build from OSM extracts; add GTFS for transit):

* **Valhalla** — the suggested default. Tiled graph (moderate RAM, planet feasible), multimodal costing (auto, truck, bicycle, pedestrian, motor scooter, transit with GTFS), native isochrones, matrix, time-dependent routing, map-matching, elevation-aware costing. Run via the official Docker image; a country builds in minutes.
* **OSRM** — fastest routes and very large distance/duration matrices (`table` service) for one profile via contraction hierarchies. No native isochrones; profile changes mean a rebuild; planet car graph preprocessing needs a large-memory machine. Choose it when the workload is "millions of car OD pairs".
* **GraphHopper** — Java, solid, custom vehicle models in config, isochrones in the open-source core. The Matrix API is only in the hosted product.
* **pgRouting** — for custom or non-OSM networks (utilities, rail, indoor, pipelines) and SQL-integrated analysis; not for high-throughput request serving.
* **r5py / R5** — transit + multimodal travel-time matrices from GTFS and OSM; the standard for accessibility research. **OpenTripPlanner** for a transit journey-planner service.

**Hosted**:

* **Google Routes API** — traffic-aware ETAs, tolls, transit, worldwide; the reference for product ETAs. Results are Google-map-bound under the terms.
* **HERE Routing v8** — strong truck routing (dimensions, hazmat), traffic, matrix, isolines; historically the fleet/logistics choice.
* **TomTom Routing** — traffic, long-distance EV routing, matrix; generous free daily tier.
* **Mapbox Directions / Matrix / Isochrone / Optimization** — good developer experience, good with Mapbox maps.
* **GraphHopper Directions API**, **openrouteservice** (HeiGIT; free daily quota, also self-hostable), **Stadia Maps** (hosted Valhalla), **Geoapify** — the cheaper open-data-backed tier; fine when traffic is not required.

Choosing: isochrones/accessibility for analysis → self-hosted Valhalla on a bounded extract, snapshot the graph build date; large car matrices → OSRM; delivery ETAs users see → a traffic-aware hosted API; transit → r5py. Record OSM extract date, profile/costing options, and engine version; a route computed on a different extract is a different result.

## Geocoding and address quality

Geocoding is data-product quality work, not just GIS. Classify every match before analysis:

* Exact rooftop / entrance / access point.
* Parcel or building centroid.
* Interpolated along street segment.
* Postcode centroid.
* Locality / admin centroid.
* Failed or ambiguous.

Keep raw input, normalized address, provider, provider ID, confidence/match code, result type, coordinates, and timestamp. Filter downstream analysis by match type; a postcode centroid inside a 500 m buffer analysis is noise.

**Check for a national geocoder first.** Official address registers beat every global provider inside their own country and are usually free:

* **Estonia** — In-ADS (Maa- ja Ruumiamet address data system; gazetteer API and embeddable component). Authoritative addresses with coordinates in EPSG:3301, ADS object IDs, and postcodes.
* **France** — BAN via the national address API (adresse.data.gouv.fr / Géoplateforme); bulk CSV geocoding endpoint.
* **Netherlands** — PDOK Locatieserver (BAG-based).
* **UK** — OS Places API (licensed; free for public sector under PSGA), OS Names for places.
* **USA** — Census Geocoder (free, batch files up to 10k addresses, TIGER-interpolated; ideal for tract/block assignment, not rooftop).
* **Australia** — G-NAF (open address points; load locally).

**Self-host**:

* **Nominatim** — OSM-only. Simple for one country in Docker; no autocomplete by policy; planet import is days and ~1 TB NVMe. Public instance: max 1 request/second, attribution required, no bulk or autocomplete use.
* **Pelias** — OSM + OpenAddresses + Who's On First + GeoNames, Elasticsearch-backed; better addresses and autocomplete than Nominatim, heavier operations. Hosted as **Geocode Earth** by its maintainers and by Stadia Maps.
* **Photon** — OSM, OpenSearch-backed, built for autocomplete; the public instance at komoot is fair-use only.
* **Overture Addresses** — open address points (2024+) from national sources and OpenAddresses; coverage is partial but where present it is authoritative-grade. **OpenAddresses** is the raw collection with per-source licenses.

**Hosted**:

* **Google Geocoding / Places / Address Validation** — highest global match quality and the best validation product. Terms are the trade-off: geocoding results may be cached only for a limited period (historically 30 days), only Place IDs may be stored indefinitely, and content is tied to display on Google maps. Unsuitable when you need a stored, reusable coordinate dataset.
* **HERE Geocoding & Search** — strong in Europe and for logistics; freemium tier.
* **Mapbox Geocoding** — use the *permanent* endpoint (priced higher) when storing results; the temporary endpoint forbids it.
* **ArcGIS geocoding (Esri)** — set `forStorage=true` when persisting results; it is billed differently.
* **OpenCage** — aggregates open data (OSM, OpenAddresses, GeoNames...), explicitly permits storing results, sensible pricing; the default for open-data-friendly projects. **Geoapify** and **LocationIQ** sit in the same tier.
* **Address validation vendors** — Loqate (GBG), Smarty, Melissa, Experian: postal-authority-derived validation, deliverability flags, and CASS/PAF certification when mailing is the actual goal.

For privacy-sensitive workloads (customer lists, patients, employees), geocode locally with a national register or Nominatim/Pelias, or use a provider with a signed data-processing agreement.

## Place search and POI

* **Overture Places** — the default POI dataset for analysis: global, GERS IDs, category taxonomy, confidence scores, CDLA-Permissive-2.0 (more permissive than ODbL). Read it bbox-filtered from S3 via DuckDB (see `data-sources.md`).
* **Foursquare OS Places** — ~100M POIs released under Apache 2.0 in 2024; complements Overture, especially for commercial venues.
* **OSM** — best for public amenities and infrastructure (schools, parks, transit stops), weakest for commercial churn.
* **GeoNames / Wikidata / Who's On First** — gazetteers and place hierarchies, not POIs.

Hosted: **Google Places API (New)** (freshest commercial data; storage restricted to Place IDs), **Mapbox Search Box**, **HERE Discover/Browse**, **TomTom Search**, **Foursquare Places API**, **ArcGIS Places**. Use these when freshness and brand/category normalisation matter more than the ability to keep the data.

Deduplicating POIs across sources is its own task: match on normalised name + category + distance threshold (typically 25–50 m in dense areas), keep every source ID, and record the match confidence.

## Postcodes

Postcodes are operational postal artifacts, not administrative geography. They differ per country in shape and stability:

* **UK** — postcode units (~15 addresses) with centroids in the ONS Postcode Directory and Code-Point Open; polygons are derived, not official. **postcodes.io** is a free, open, self-hostable lookup API on ONSPD.
* **USA** — ZIP codes are USPS delivery routes and PO-box groups with no official polygons; **ZCTAs** from the Census are decennial approximations. Say which one you used.
* **Ireland** — Eircode identifies a single address; the database is licensed.
* **Estonia** — five-digit postcodes are an attribute of the ADS address object (Maa- ja Ruumiamet) and maintained by Omniva; no official polygons.
* **Netherlands** — PC6 codes are on every BAG address (open); PC4 polygons are published by CBS.
* **Global fallback** — GeoNames postal codes (CC BY 4.0, centroids, granularity varies by country). Paid: **GeoPostcodes**, plus the address-validation vendors above.

Never use postcode polygons as stable admin areas for time series; they change with delivery operations.

## Place IDs and crosswalks

Use IDs for joins and deduplication; use names for display. A good place record carries several namespaced IDs (the namespaces are described in `data-sources.md`):

```text
name
geometry
country_code
admin_path
gers_id                    # Overture
osm_type + osm_id
wikidata_qid
geonames_id
unlocode
wof_id
provider_place_id          # namespaced: google:, here:, mapbox:, fsq:
open_location_code
source + source_version
```

Never assume IDs from one provider can be reused with another provider's API. Provider IDs are frequently the only thing you are allowed to store long-term; check which. Crosswalks need provenance, match confidence, and refresh dates.

## Operational hygiene for API workloads

* **Normalise before you call**: trim, case-fold, expand abbreviations consistently, and add country code. The same address in three spellings is three billed requests.
* **Cache by normalised key + provider + endpoint version**, in a local DuckDB/SQLite table, and only where the terms allow. Store the raw JSON response alongside the parsed fields.
* **Checkpoint batch jobs** so a crash at row 400k does not re-bill the first 399k.
* **Back off on 429/5xx** with exponential delay and jitter; respect per-provider rate limits; run public community instances at ≤1 request/second with a descriptive `User-Agent` and contact email.
* **Set a hard spend cap** or quota alert before running anything over a free tier.
* **Send the minimum**: do not post full customer records to a geocoder that only needs the address.

## Recording service results as project sources

API responses are inputs like any other source. Record them in the manifest per `project-spec.md`: `access.method: api`, provider, endpoint and API version, the exact `request_spec` (parameters, profile, costing options), `retrieved_at`, license/terms, and completeness counts for batched calls. Keep the snapshot on disk and make the reproducible pipeline read the snapshot; a pipeline that re-queries a live API is not reproducible. Note in the rationale which alternatives were rejected and why (terms, coverage, privacy, cost).

## Hybrid workflows

Common production pattern:

1. Use a hosted API or self-hosted service for global search, geocoding, place lookup, routing, or elevation point queries.
2. Store the allowed IDs, normalised outputs, match confidence, raw responses, and timestamps as a manifest source.
3. Run downstream spatial analysis locally in DuckDB/PostGIS/GeoPandas on a bounded AOI in a metric CRS.
4. Publish results as PMTiles/COG/API with attribution and service terms carried forward into the product.
