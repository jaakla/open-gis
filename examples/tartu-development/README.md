# Tartu Development Access — worked example

A complete `open-gis-project/v1` example matching the acceptance scenario in
[issue #4](https://github.com/jaakla/open-gis/issues/4):

> Find suitable development locations near main roads around Tartu and make an
> interactive map.

```text
tartu-development/
├── project.yaml        # canonical manifest (open-gis-project/v1)
├── pipeline.py         # deterministic, boring, rerunnable
├── README.md
├── data/
│   ├── source/         # immutable fetched copies (cadastral, roads, POIs)
│   ├── overrides/
│   │   ├── planned-road.geojson   # manually drawn scenario geometry
│   │   └── closed-poi.geojson     # attribute correction (status)
│   └── derived/        # final-candidates.parquet etc.
├── validation/
│   └── latest-report.json         # machine-readable run validation
└── runs/               # per-execution metadata + input/output hashes
```

## What this example demonstrates

- **Source provenance & timestamps** — each source records URL, method,
  `retrieved_at`, `published_at`, selection bbox/filter and license.
- **Explicit assumptions** — `A1` (planar 2000 m, not network) and `A2`
  (metric area in EPSG:3301) are written down, not implied.
- **Data overrides as first-class GIS** — an attribute correction
  (`OVERRIDE-001`, user-entered "closed" status for a stale POI) and a
  manually drawn planned road (`OVERRIDE-002`) live as real geodata under
  `data/overrides/` with rationale + evidence. Sources are never mutated:
  *immutable source + override layer = effective input*.
- **Deterministic processing** — `pipeline.py` mirrors `processing.steps`,
  pins CRS per metric step, and is rerunnable in a fresh environment.
- **Validation as a pipeline stage** — `validation/latest-report.json` uses
  explicit `passed` / `warning` statuses and never turns "not tested" into a
  pass.
- **Semantic presentation** — `presentation` describes what to show and its
  hierarchy using stable semantic roles (`primary_result`, `planned`, …)
  rather than arbitrary colors.

## Prove reproducibility

> Delete the conversation. Hand this directory to another GIS engineer or
> agent. They can audit, edit, and rerun the analysis without the original
> chat transcript — because everything that matters lives here + `project.yaml`.

### End-to-end run + HTML dashboard (recommended)

`run_e2e.py` runs the full loop and renders a self-contained `dashboard.html`
as a view over the project artifacts (project.yaml + derived data +
validation), landing on real Tartu coordinates:

```bash
python -m venv .e2e-venv && ./.e2e-venv/bin/pip install duckdb pyyaml pyproj
cd examples/tartu-development
../../.e2e-venv/bin/python run_e2e.py
open dashboard.html
```

The run is deterministic (fixed RNG seed): two runs produce byte-identical
derived geometry; only the timestamped `run_id` differs. Outputs written:

* `data/source/parcels.json`, `roads.json` — deterministic source fixture
  (a documented stand-in for the real county cadastre GPKG / ETAK WFS in
  `project.yaml`, which a production rerun swaps in).
* `data/derived/final-candidates.gpkg` (EPSG:3301) + `.json` (EPSG:4326)
* `dashboard.html` — side-bar summary/filters/layers/assumptions/sources/
  overrides/warnings/validation + a MapLibre map of the result.
* `project.qgz` — a first-class QGIS project (a .qgz is the .qgs XML wrapped
  in a zip) referencing the SAME derived GPKG + override layer, project CRS
  EPSG:3301, semantic layer groups. Open it in QGIS to inspect and edit the
  analysis in a professional desktop environment; deliberate edits in the
  editable result layer can be written back into the project overrides.
* `validation/latest-report.json` — machine-readable run validation.

`dashboard.html`, `project.qgz` and `run_e2e.py` are tracked; `data/source/`,
`data/derived/` and the screenshot are regenerable and git-ignored.

### Plain pipeline

`pipeline.py` mirrors `processing.steps` as a boring, inspectable skeleton:

```bash
python -m py_compile pipeline.py
```

A QGIS project (`project.qgz`) is generated automatically by `run_e2e.py` and
references `data/derived/*` + `data/overrides/*` — the same data as the
pipeline, so there is no hidden analytical state. Open `project.qgz` in QGIS
for inspection and manual editing that writes back into the override layer.
