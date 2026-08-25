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

```bash
python pipeline.py                       # rerun the whole analysis
python -m py_compile pipeline.py
```

A QGIS project (`project.qgz`) referencing `data/derived/*` and
`data/overrides/*` is the natural next first-class view for inspection and
manual editing that writes back into the override layer.
