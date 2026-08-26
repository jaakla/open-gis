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
│   │   └── planned-road.geojson   # hypothetical connector geometry (OVERRIDE-002)
│   └── derived/        # candidates, proxies, effective POIs, official roads
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
  buffers and tier names say “2 km straight-line proxy”, and the derived columns
  are `straightline_time_school_min` / `straightline_time_kg_min` — no output
  claims a network walking time. Assumption A4 records that distances are
  measured from the nearest parcel *edge*, so a large parcel qualifies when any
  part of it is in range.
- **Two verified overrides, both hypothetical and both isolated.**
  `OVERRIDE-001` is a `modify_attribute` scenario that switches one kindergarten
  (`Ilmatsalu Lasteaed Lepatriinu`) to inactive; the pipeline verifies that the
  target exists and that the asserted prior value (`active = true`) matches the
  immutable source before applying it, rejects placeholder evidence, and reports
  the result per override. `OVERRIDE-002` adds a hypothetical connector road
  through a separate scenario table, and never leaks into the “Official National
  Highways (ETAK)” presentation layer. Neither override rewrites `data/source/`:
  the effective POI layer is written to `data/derived/education_pois.json` with
  an `override_id` and a distinct `map_class`, and both the web map and the QGIS
  project style that class separately.
- **One canonical implementation.** Both documented commands execute
  `pipeline.py`; the E2E wrapper cannot drift from the plain pipeline.
- **Validation/report parity.** All manifest-required and domain checks appear
  in `validation/latest-report.json`, warnings propagate to project status, and
  run IDs/hashes point to a real `runs/*.json` record. `manifest_graph_resolves`
  additionally proves every `processing.steps` input resolves to a source key or
  an earlier step's output, and every `outputs.*.generated_by` names a real step.
- **A reconfigurable view that cannot misrepresent the run.** `dashboard.html`
  organises the sidebar into three tabs (Analysis / Map / Provenance) of
  collapsible sections, gives every layer group an on/off control, and exposes
  the analysis parameters as live controls: minimum parcel area, highway
  distance, education threshold, land use, and one switch per scenario override.
  Every control re-applies the published rule to distances the pipeline already
  measured in EPSG:3301 — the browser never re-measures geometry — and the
  education-threshold control swaps in a precomputed buffer for the radius it
  selects rather than approximating one. The canonical control positions come
  from `presentation.controls` in `project.yaml`, and the moment any control
  leaves them the view labels itself *Reconfigured view — not the accepted run*
  and offers a reset. The `view_controls_match_pipeline` gate fails the run if
  those declared positions ever drift from the thresholds the pipeline ran.
- **A local-first edit mode.** The generated dashboard can select cadastral
  parcels and education facilities, record typed attribute corrections or hide
  operations with rationale/evidence, and draw point/line/polygon scenario,
  annotation, or AOI geometry. Draft operations are stored in browser local
  storage per project + base run, support undo/redo, render as distinct delta
  overlays, and export as `open-gis-override-bundle/v1` JSON. Parcel hide,
  land-use, and area edits immediately re-apply the existing browser rules;
  facility edits and drawn geometry are explicitly labelled map-only until the
  canonical pipeline recomputes spatial measurements. No browser draft changes
  source files, project validation, or the accepted run.
- **QGIS as a first-class view.** The generated project uses relative sources,
  mirrored styles/layer groups, explicit scenario styling, three live basemaps,
  and static archive/source/style validation. If PyQGIS is unavailable, runtime
  loading is reported as `not_testable`, never passed implicitly.

## Current regenerated result

Both scenario overrides are in effect, so these numbers describe the scenario,
not present-day conditions (see warning `SCENARIO-001`):

- Tier 1 — road plus both municipal education proxies: **66 parcels / 849.1 ha**
- Tier 2 — road plus either municipal education proxy: **85 parcels / 2,472.3 ha**
- Tier 3 — road access only: **367 parcels / 6,249.4 ha**
- Total road-accessible candidates: **518 parcels / 9,570.8 ha**
- Candidates whose closest qualifying road is the hypothetical scenario: **60**
- Effective education layer: **26 schools + 34 kindergartens active**, 1 switched
  off by `OVERRIDE-001` (61 authoritative facilities in the immutable source)

Removing `OVERRIDE-001` moves 64 parcels back from Tier 2 to Tier 1
(130 / 3,185.1 ha), which is the point of the scenario: a single kindergarten
carries most of the western cluster's Tier 1 status. That comparison is a switch
in the dashboard's Map tab, because the pipeline exports
`dist_school_baseline_m` / `dist_kg_baseline_m` alongside the effective
distances — the same measurement, taken against the facility set as the
authoritative source publishes it.

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
- `data/derived/education_catchments.json` — canonical 2 km straight-line proxy polygons (64 segments/quadrant)
- `data/derived/education_catchment_variants.json` — the same buffer rule at every
  radius the dashboard control offers (1–3 km), for the effective and the
  override-free facility sets; exploratory companion, never the accepted result
- `data/derived/education_pois.json` — effective facilities (source + overrides, `map_class` + `override_id`)
- `data/derived/main_roads.json` — official ETAK roads only
- `project.qgz`, `dashboard.html`, `validation/latest-report.json`, and `runs/*.json`

Set `OPEN_GIS_USE_QGIS_DOCKER=1` to request native project compilation with the
pinned QGIS container. The deterministic XML generator remains the fallback;
runtime layer validity is still reported separately.
