# Tartu Development Access & Education Proxy — worked example

A complete `open-gis-project/v1` example for the scenario in
[issue #4](https://github.com/jaakla/open-gis/issues/4):

> Find suitable development locations near main roads around Tartu with 25-minute
> walking access to municipal schools and kindergartens, and make an interactive map.

The requested walking criterion is explicitly represented as a **2,000 m
straight-line screening proxy**, not a pedestrian-network isochrone. The project
is therefore correctly reported as `warning`, with that methodological limit and
the education source's unstated reuse license surfaced in the validation report.

```text
tartu-development/
├── project.yaml        # canonical manifest (open-gis-project/v1)
├── pipeline.py         # sole processing/QGIS/dashboard implementation
├── run_e2e.py          # thin convenience wrapper around pipeline.py
├── dashboard.html      # generated MapLibre analytical view
├── project.qgz         # generated QGIS desktop project
├── data/
│   ├── source/         # immutable authoritative snapshots + fetch metadata
│   ├── overrides/
│   │   └── planned-road.geojson   # explicit hypothetical scenario (OVERRIDE-002)
│   └── derived/        # candidates, proxies, verified POIs, official roads
├── validation/latest-report.json
└── runs/              # real per-run metadata and hashes
```

## What the corrected example demonstrates

- **Semantically authoritative education data.** Schools and kindergartens come
  from Tartu City Government ArcGIS Feature Services. Schools must satisfy
  `Omand=1`, exclude outside-city type `Liik=5`, and be active; kindergartens
  must satisfy `Liik=10` and be active. Missing ownership is never defaulted.
  The current snapshot contains 26 schools and 35 kindergartens.
- **Completeness-safe roads.** ETAK WFS filtering is performed server-side and
  paginated until `numberReturned == numberMatched` (1,444 official primary and
  secondary road segments across two pages).
- **Honest method semantics.** Metric operations use EPSG:3301. Education
  buffers and tier names say “2 km straight-line proxy”; they do not claim a
  network walking isochrone.
- **Scenario isolation.** `OVERRIDE-002` is explicitly hypothetical, is validated
  as applied, affects analysis through a separate scenario table, and never
  leaks into the “Official National Highways (ETAK)” presentation layer.
- **One canonical implementation.** Both documented commands execute
  `pipeline.py`; the E2E wrapper cannot drift from the plain pipeline.
- **Validation/report parity.** All manifest-required and domain checks appear
  in `validation/latest-report.json`, warnings propagate to project status, and
  run IDs/hashes point to a real `runs/*.json` record.
- **QGIS as a first-class view.** The generated project uses relative sources,
  mirrored styles/layer groups, explicit scenario styling, three live basemaps,
  and static archive/source/style validation. If PyQGIS is unavailable, runtime
  loading is reported as `not_testable`, never passed implicitly.

## Current regenerated result

- Tier 1 — road plus both municipal education proxies: **130 parcels / 3,185.1 ha**
- Tier 2 — road plus either municipal education proxy: **21 parcels / 136.3 ha**
- Tier 3 — road access only: **367 parcels / 6,249.4 ha**
- Total road-accessible candidates: **518 parcels / 9,570.8 ha**
- Candidates whose closest qualifying road is the hypothetical scenario: **60**

## Run

```bash
python -m venv .e2e-venv
./.e2e-venv/bin/pip install duckdb pyyaml pyproj
cd examples/tartu-development
../../.e2e-venv/bin/python pipeline.py
```

The equivalent convenience command is:

```bash
../../.e2e-venv/bin/python run_e2e.py
```

Outputs include:

- `data/source/Tartu_maakond_KATASTER_GPKG.gpkg` — 79,056 cadastral parcels
- `data/source/etak_main_roads.geojson` — completeness-verified ETAK main roads
- `data/source/tartu_municipal_education.geojson` — normalized official municipal facilities
- `data/derived/final-candidates.gpkg`, `.parquet`, `.json`
- `data/derived/education_catchments.json` — 2 km straight-line proxy polygons
- `data/derived/education_pois.json` — verified active municipal facilities
- `data/derived/main_roads.json` — official ETAK roads only
- `project.qgz`, `dashboard.html`, `validation/latest-report.json`, and `runs/*.json`

Set `OPEN_GIS_USE_QGIS_DOCKER=1` to request native project compilation with the
pinned QGIS container. The deterministic XML generator remains the fallback;
runtime layer validity is still reported separately.
