# Tartu Development Access & Education Catchment — worked example

A complete `open-gis-project/v1` example matching the acceptance scenario in
[issue #4](https://github.com/jaakla/open-gis/issues/4):

> Find suitable development locations near main roads around Tartu with 25-minute
> walking access to municipal schools and kindergartens, and make an interactive map.

```text
tartu-development/
├── project.yaml        # canonical manifest (open-gis-project/v1)
├── pipeline.py         # deterministic, executable DuckDB Spatial pipeline
├── run_e2e.py          # end-to-end runner (data fetch, spatial joins, QGIS .qgz, dashboard)
├── dashboard.html      # standalone interactive MapLibre analytical dashboard
├── project.qgz         # first-class QGIS desktop project referencing derived datasets
├── README.md
├── data/
│   ├── source/         # official source downloads (Maa- ja Ruumiamet GPKG, ETAK WFS, OSM POIs)
│   ├── overrides/
│   │   ├── planned-road.geojson   # scenario geometry (OVERRIDE-002)
│   │   └── closed-poi.geojson     # attribute correction (OVERRIDE-001)
│   └── derived/        # final-candidates.gpkg, .parquet, .json, catchments, pois, roads
├── validation/
│   └── latest-report.json         # machine-readable run validation
└── runs/               # per-execution metadata + input/output hashes
```

## What this example demonstrates

- **Real source provenance & timestamps** — exact S3 download URLs, WFS query
  specifications, and OSM Overpass endpoints with file hashes, row counts (79,056
  cadastral parcels, 5,000 road segments, 93 educational POIs), and full schemas.
- **Explicit assumptions** — `A1` (highway proximity <= 2,000 m), `A2` (metric
  area in EPSG:3301), and `A3` (25-minute walking catchment modeled as 2,000 m buffer
  at 4.8 km/h).
- **Multi-criteria spatial evaluation** — combines arterial transport access with
  pedestrian educational catchments:
  - **Tier 1 (Prime Candidates)**: Highway access <= 2 km **AND** <= 25 min walk
    to both municipal school and kindergarten (73 parcels, 2,586.9 ha).
  - **Tier 2 (Secondary Candidates)**: Highway access <= 2 km **AND** <= 25 min
    walk to school or kindergarten (8 parcels, 169.5 ha).
  - **Tier 3 (Highway Access Only)**: Highway access <= 2 km, > 25 min walk to
    educational facilities (214 parcels, 3,906.5 ha).
- **Data overrides as first-class GIS** — an attribute correction
  (`OVERRIDE-001`, user-entered "closed" status for a stale POI) and a
  manually drawn planned road (`OVERRIDE-002`) live as real geodata under
  `data/overrides/` with rationale + evidence. Sources are never mutated:
  *immutable source + override layer = effective input*.
- **Deterministic processing** — `pipeline.py` mirrors `processing.steps`,
  runs in DuckDB Spatial, and is 100% rerunnable in a fresh environment.
- **Validation as a pipeline stage** — `validation/latest-report.json` records
  geometry validity, duplicate checks, row counts, and domain expressions.
- **Semantic presentation** — `dashboard.html` provides an interactive MapLibre
  map with color-coded suitability tiers, 25-minute walking catchment buffers,
  educational POIs with popups, highway networks, and a complete provenance panel.

## Prove reproducibility

> Delete the conversation. Hand this directory to another GIS engineer or
> agent. They can audit, edit, and rerun the analysis without the original
> chat transcript — because everything that matters lives here + `project.yaml`.

### End-to-end run + HTML dashboard (recommended)

`run_e2e.py` runs the full loop using real Estonian open datasets and renders
`dashboard.html` and `project.qgz`:

```bash
python -m venv .e2e-venv && ./.e2e-venv/bin/pip install duckdb pyyaml pyproj
cd examples/tartu-development
../../.e2e-venv/bin/python run_e2e.py
open dashboard.html
```

Outputs written:

* `data/source/Tartu_maakond_KATASTER_GPKG.gpkg` (79,056 real parcels from Maa- ja Ruumiamet S3)
* `data/source/etak_roads.geojson` (Environment Agency GeoServer WFS road network)
* `data/source/education_pois.geojson` (Schools and kindergartens in Tartu area)
* `data/derived/final-candidates.gpkg` (EPSG:3301) + `.parquet` + `.json` (EPSG:4326)
* `data/derived/education_catchments.json` (25-min walking buffers in EPSG:4326)
* `data/derived/education_pois.json` (Educational POIs in EPSG:4326)
* `data/derived/main_roads.json` (Highway network in EPSG:4326)
* `dashboard.html` — interactive multi-layer MapLibre dashboard with click tooltips and provenance cards.
* `project.qgz` — first-class QGIS desktop project referencing the derived GeoPackage and overrides.
* `validation/latest-report.json` — machine-readable validation report.

`dashboard.html`, `project.qgz`, `run_e2e.py`, and `pipeline.py` are tracked;
downloaded and derived `data/` files and screenshots are git-ignored.

### Plain pipeline

`pipeline.py` executes the standalone processing logic in DuckDB Spatial:

```bash
python pipeline.py
```

A QGIS project (`project.qgz`) is generated automatically by `run_e2e.py` and
references `data/derived/*` + `data/overrides/*` — the same data as the
pipeline, so there is no hidden analytical state. Open `project.qgz` in QGIS
for inspection and manual editing that writes back into the override layer.
