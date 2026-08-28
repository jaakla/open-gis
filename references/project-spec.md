# Reproducible GIS Project Specification — `open-gis-project/v1`

The canonical, runnable unit of Open-GIS analysis. For any **material multi-stage GIS analysis**, the delivered output is a *project artifact*, not just a map, dashboard, or narrative answer. This file defines that artifact.

Read this before compiling any non-trivial Open-GIS analysis. Ready scaffolds live in `../templates/`; a fully-worked example matching the acceptance scenario lives in `../examples/tartu-development/`.

**Core principle:** reasoning may be exploratory; the delivered analysis must be deterministic, inspectable, and reproducible.

---

## 1. Project layout

```text
my-analysis/
├── project.yaml          # canonical manifest (open-gis-project/v1)
├── pipeline.py           # deterministic, readable execution
├── README.md             # human audit/intro
│
├── data/
│   ├── source/           # immutable downloads/copies of external sources
│   ├── overrides/        # manual corrections, additions, scenario geometry
│   └── derived/          # pipeline outputs (GeoParquet, COG, GeoPackage…)
│
├── validation/
│   └── latest-report.json          # machine-readable result of the last run
│
├── runs/
│   └── run-20260825-081503.json    # per-run metadata + hashes
│
├── dashboard.html        # generated web view over the project
└── project.qgz           # QGIS project (first-class view, required for
                          # any multi-stage analysis — see section 5)
```

`validation.required` / `domain_checks` and the `presentation` block live **inside `project.yaml`** by default, so one file stays the canonical manifest. Split them out only when they grow unwieldy, as `validation/validation.yaml` and `styles/presentation.yaml`; a `styles/` directory is also the right home for `maplibre-style.json` and `.qml` snippets when styling is reused across projects.

Run records are named `run-<YYYYMMDD>-<HHMMSS>.json` (UTC) so the run id in `project.yaml`, the validation report, and the file name are the same string.

Not every simple task needs every file, but `project.yaml` is the canonical manifest and should always exist for material work. The **chat transcript is not part of the analytical dependency graph.**

---

## 2. `project.yaml` schema (`open-gis-project/v1`)

The manifest describes *what* the analysis is and *why*, the pipeline describes *how* to run it. Keep this file documented and human-reviewable.

**Normative sources.** Structure — required keys, types, and top-level enums — is
machine-checked by [`open_gis/schemas/project-v1.schema.json`](../open_gis/schemas/project-v1.schema.json),
reported as the `manifest.json_schema` check. That file is normative where it and
this document disagree on *shape*. This document is normative for *semantics*: what
each field means, the rules that have no structural form (source pinning, override
provenance, label honesty, hash construction), and the cross-file invariants
`open-gis validate` enforces beyond the schema. The YAML below is illustrative — a
worked example of the shape, not a second field registry to keep in sync by hand.

### 2.1 Head and interpretation

```yaml
schema: open-gis-project/v1

project:
  id: tartu-development-access
  title: Potential development areas near main roads
  question: >               # the original / ethical restated user question
    Identify potentially developable parcels meeting the specified
    accessibility and land-use criteria.
  created_at: 2026-08-25T08:15:03+03:00
  updated_at: 2026-08-25T08:42:11+03:00
  status: validated        # draft | in_progress | validated | warning | failed

interpretation:
  objective: >             # analyst interpretation of the objective
  assumptions:
    - id: A1
      statement: >-
        "Near a main road" means <= 2000 m planar distance.
      rationale: >-
        User did not specify travel time or road-network accessibility.
    - id: A2
      statement: >-
        Parcel area measured in EPSG:3301.
      rationale: Metric area calculation is required.
```

`interpretation` is where "what the user actually wanted" is pinned, including any rephrasing you did. Every assumption needs a `statement` and `rationale`.

The schema requires every `project.*` and `interpretation.*` key shown above and
closes `project.status` to the five listed values. The per-assumption
`statement`/`rationale` requirement is a semantic rule checked by `open-gis validate`,
not by the schema — a manifest can be schema-valid and still fail the audit.

### 2.2 Sources

Each source records how it was found, retrieved, selected, and licensed — enough to re-fetch exactly.

```yaml
sources:
  cadastral_parcels:
    role: authoritative_input     # authoritative_input | context | reference
    provider: Maa- ja Ruumiamet
    dataset: cadastral parcels
    source_url: https://...
    access:
      method: WFS                # WFS | WMS | STAC | http(s) | api | s3 | local
      retrieved_at: 2026-08-25T08:18:12+03:00
      downloaded_at: 2026-08-25T08:18:12+03:00   # when the bytes were pulled
      file:                      # what actually landed on disk
        name: cadastral.gpkg     # or archive_name + extracted_file
        table_name: "Tartu maakond"   # layer/table bound inside the file
        format: GeoPackage (GPKG/SQLite)
        size_bytes: 55746560
        row_count: 79056
        column_count: 32
      request_spec: >-           # the exact query / bbox / params used
    version:
      published_at: 2026-08-24
      identifier: "..."          # STAC item id, release tag, table/layer id
      etag: "..."                # when the service publishes one
    selection:
      bbox: [25.4, 58.3, 26.9, 58.9]
      filter: "..."
      semantic_predicates:       # decision-critical coded fields
        - field: ownership_code
          domain_value: 1 = municipal
      completeness:              # required for bounded APIs
        matched: 1444            # service-declared total
        returned: 1444           # rows materialized after pagination
        page_size: 1000
        pages: 2
    license:
      name: "..."
      url: "..."
    schema:                      # the shape you received, not the one you hoped for
      crs: EPSG:3301 (L-EST97)
      key: cadastral_id          # stable feature identifier
      area_field: area_m2        # name the roles the analysis depends on
      columns:
        - cadastral_id
        - area_m2
        - geometry
    rationale: >-
      Selected as the authoritative cadastral geometry source instead of
      OSM/Overture.
```

**Rules:**

- **No hallucination of coordinates or mock geodata:** Fabricating coordinates or inventing synthetic baseline geometries is strictly forbidden without explicit user consent. Always fetch and process real, authoritative data from official endpoints (e.g. Maa- ja Ruumiamet S3/WFS, OSM Overpass, STAC). Record exact file names, table/layer names, and download timestamps.
- Pin `version.identifier`/`published_at` (Overture release, STAC item ID, OSM extract date, portal download date). **Pinning to "latest" is not reproducible.**
- Record `access.retrieved_at` and `access.downloaded_at` (when you actually pulled it). It answers "which version/date was the source?"
- Always give a `rationale` for choosing one source over another — especially when you *rejected* an obvious candidate.
- Preserve `license` metadata through every transformation.
- If the question depends on a semantic predicate such as municipal ownership, public access, or active status, document the authoritative field/domain and exact selection expression. Do not turn missing or ambiguous values into the desired category.
- For bounded APIs, record the service total (`numberMatched`, `resultCount`, or equivalent), page size, pages fetched, and final returned count under `selection.completeness`. A page filled to its limit is not proof of completeness. `open-gis validate` also accepts `completeness` at the top level of the source for backward compatibility, but `selection.completeness` is canonical: the counts describe that selection.
- Describe the data you received in `schema` — its CRS, the key, the field roles the analysis depends on, and the columns. Earlier drafts used a bare `expected_fields` list; `schema.columns` supersedes it.
- `access.retrieved_at` and `access.downloaded_at` are interchangeable to the validator, which needs one of the two. Record both when they differ (a cached extract retrieved later than it was published).

### 2.3 Overrides — analyst knowledge as data

**Everything that is not a fact of an external dataset belongs in `overrides`.** Sources are immutable:

```text
immutable source + project override layer = effective analysis input
```

```yaml
overrides:
  - id: OVERRIDE-001
    action: modify_attribute          # add_feature | edit_geometry | replace_geometry
    target:                           # | modify_attribute | hide_source_feature
      source: pois                    #   | merge_features | split_feature
      feature_id: "12345"             #   | add_annotation | add_aoi | add_scenario
    change:
      field: status
      from: active
      to: closed
    rationale: "Source dataset is stale; the facility has closed."
    evidence: [{type: url, value: "https://..."}]
    created_at: 2026-08-25T08:26:00+03:00
    created_by: user         # user | analyst | agent | scenario

  - id: OVERRIDE-002
    action: add_feature
    layer: planned_roads
    properties:
      name: Proposed connector
      status: planned
    geometry_file:
      path: data/overrides/planned-road.geojson
    geometry_origin: user_drawn   # user_drawn | copied | scenario | photo_traced
    rationale: "Planned road absent from machine-readable geodata but relevant."
    evidence: [{type: planning_document, title: "...", page: 37}]
```

**Never silently mutate downloaded source data.** Overrides are first-class and carry provenance, rationale, evidence, author, and timestamp. They answer audit questions: *"Was this geometry imported or manually added?"* and *"Why was this record removed?"*

Before applying an override, validate that its target exists and that any asserted `from` value matches the immutable source. Reject placeholder evidence. The run report records each override as `applied`, `rejected`, or `not_testable`. Hypothetical scenario features do not require an external factual claim, but must use `geometry_origin: scenario`, say that they are hypothetical, and remain separate from authoritative presentation layers.

### 2.4 Processing steps

```yaml
processing:
  analysis_crs: EPSG:3301     # CRS used for metric operations
  storage_crs: EPSG:4326      # CRS used for storage/interchange
  steps:
    - id: load_parcels
      operation: read
      source: cadastral_parcels
      output: parcels_raw
    - id: calculate_area
      operation: calculate_area
      input: parcels_raw
      crs: EPSG:3301          # explicit CRS per metric step
      output_field: area_m2
    - id: select_large
      operation: filter
      input: calculate_area
      expression: "area_m2 >= 20000"
      output: large_parcels
    - id: road_distance
      operation: distance_filter
      input: large_parcels
      target: roads
      max_distance_m: 2000
      output: candidate_parcels
```

- Steps are **ordered and deterministic**.
- Every step declares `operation`, explicit `input`/`output` and any `crs`, parameters, and thresholds. Think: a GIS engineer can rerun each step from the list alone.
- Use symbolic names (`candidate_parcels`) that match `outputs.*`.
- **The step graph must resolve.** Every `input`/`inputs`/`source` symbol is either a key in `sources` or the `output` of an earlier step, and every `outputs.*.generated_by` names a real step. Dangling symbols (a step consuming `all_roads` that nothing produces, or a consumer using a different name than the producer declared) make the manifest unrunnable while still looking complete. Validate this as `manifest_graph_resolves` — it is cheap and catches manifest/pipeline drift that no data check will.

**Labels must match the operation, and the measurement basis must be stated.** A Euclidean buffer is never a "walking catchment"; call it a `2 km straight-line proxy` unless a routable network or isochrone was actually computed, and name derived columns for what they measure (`straightline_time_school_min`, not `walk_time_school_min`). Distance thresholds also need their *reference geometry* recorded as an assumption: `ST_Distance(polygon, point)` measures from the nearest parcel edge, so a 100 ha parcel qualifies when one corner is in range, while a centroid rule would reject it. State which one you used — the two produce materially different result sets on large rural parcels.

### 2.5 Outputs

```yaml
outputs:
  candidate_parcels:
    path: data/derived/candidate-parcels.parquet
    format: GeoParquet
    generated_by: road_distance
  catchment_variants:
    path: data/derived/catchment-variants.json
    format: GeoJSON
    generated_by: buffer_catchments
    role: exploratory_companion    # optional; anything that is NOT the result
    note: >-                       # optional; say what it is and is not
      Precomputed buffers for every radius the view offers. Never the
      accepted result — education_catchments is.
```

Output dataset are defined as first-class, with generated-by traces back to a step.

An output that exists to feed a control rather than to answer the question must
say so. Mark it `role: exploratory_companion` and name the accepted result in
`note`, so no reader mistakes a what-if artifact for the finding.

### 2.6 Validation

```yaml
validation:
  required:
    - geometry_valid
    - crs_known
    - row_count_gt_zero
    - no_duplicate_cadastral_id      # <check>_<field>, one flat identifier
    - no_null_cadastral_id
    - source_semantics_verified
    - source_result_complete
    - overrides_applied
    - manifest_graph_resolves
    - view_controls_match_pipeline   # section 3: the view still matches the run
    - qgis_project_static_valid
    - manifest_report_parity
  domain_checks:
    - name: parcel_area_range
      expression: "area_m2 > 0 AND area_m2 < 100000000"
```

**Check names are flat identifiers**, not mappings: write `no_duplicate_cadastral_id`, not `no_duplicate: cadastral_id`. Every name in `required` and every `domain_checks[].name` must appear verbatim as a `checks[].id` in the report, so parity is a literal string comparison with no flattening rule to implement.

The `required` list gates "done". Rarely a complete list of what validation that run evaluated is captured in the run *report* (below).

Every required and domain check must appear exactly once in the report. `warning` or `not_testable` checks make the overall run/project status `warning`; only an all-passed report may set `project.status: validated`. The report run ID and hashes must match a real `runs/*.json` record.

### 2.7 Presentation semantics

```yaml
presentation:
  intent: analytical_workspace
  primary_view: map
  layout:
    type: map_with_sidebar            # map | map_with_sidebar | full_map | report
    sidebar:
      position: left
      width: medium
      organization: tabs              # tabs | stacked
      section_state: collapsible      # collapsible | static
      tabs:
        - id: analysis
          title: Analysis
          sections: [summary, metrics, criteria, assumptions, warnings]
        - id: map
          title: Map
          sections: [scenario_controls, filters, layer_controls, basemap]
        - id: edit
          title: Edit
          sections: [selected_feature, draw_geometry, draft_overrides, export_bundle]
        - id: provenance
          title: Provenance
          sections: [provenance, overrides, validation, outputs, run_record]
  controls:                           # what the reader may reconfigure, and from where
    reconfigurable: true
    canonical_reset: true
    off_canonical_labelling: required
    filters:
      - id: min_area
        label: Minimum parcel area
        type: range                   # range | choice | multi_select | toggle
        field: area_m2
        unit: m2
        canonical: 20000              # the accepted value the pipeline ran
        step: 5000
      - id: education_threshold
        type: choice
        fields: [dist_school_m, dist_kg_m]
        canonical: 2000
        options: [1000, 1500, 2000, 2500, 3000]
        redraws: education_catchment_variants_geojson
    scenarios:
      - id: scenario_outage
        override: OVERRIDE-001        # the override this switch turns on and off
        canonical: true
        baseline_fields: [dist_school_baseline_m, dist_kg_baseline_m]
  map:
    engine_preference: maplibre       # maplibre | deck | kepler
    interaction:
      feature_select: true
      hover_tooltip: true
      zoom_to_selection: true
    layer_groups:
      - id: analysis
        title: Analysis
        default_open: true
      - id: user_overrides
        title: Manual additions and corrections
        default_open: true
    layers:
      # semantic_role is the closed vocabulary in section 3. One entry per
      # rendered dataset; the group must be a declared layer_groups id.
      - source: candidate_parcels
        group: analysis
        semantic_role: primary_result
        geometry: polygon           # point | line | polygon | raster
        style:
          visual_priority: primary
          opacity: 0.65
  legend:
    visible: true
    mode: semantic
  provenance_ui:
    feature_source_on_click: true
    show_source_timestamp: true
    show_override_badge: true
    show_assumptions: true
  editing:
    allow_draw_geometry: true
    allow_attribute_override: true
    allow_hide_source_feature: true
    allow_add_annotation: true
    draft_persistence: local_storage
    export_format: open-gis-override-bundle/v1
    canonical_application: pipeline_required
    targets:
      candidate_parcels:                  # rendered collection key
        label: Cadastral parcel
        source: cadastral_parcels         # immutable project source
        id_field: cadastral_id            # stable id in the rendered collection
        label_field: address
        fields:
          - view_field: land_use          # rendered/output alias
            source_field: siht1           # asserted source field in the override
            label: Land use
            type: choice                  # text | number | boolean | choice
            options: [ARIMAA, MAATULUNDUSMAA, TOOTMISMAA]
```

`presentation.map.engine_preference` is a closed enum:

| Value | Meaning |
|---|---|
| `maplibre` | Default. Generate the reproducible delivered web map with MapLibre, normally reading PMTiles. |
| `deck` | Generate a MapLibre basemap plus a deterministic deck.gl overlay for density, flows, 3D magnitude, or very large rendered feature sets. |
| `kepler` | Generate an exploration-first kepler.gl view when UI-driven filtering, time playback, rapid aggregation, or interactive 3D is the primary need. |

Use the literal values above: `deck`, not `deck.gl`; `kepler`, not `kepler.gl`. QGIS is a required companion output for multi-stage analysis and is not a value in this web-renderer enum.

This enum is an authoring rule with no automated gate today: the JSON schema accepts
`presentation` as any object, and `open-gis validate` does not check the value. The
contract is this section, not what the tooling happens to let through.

The preference selects an implementation; it does not move canonical state out of the manifest. `presentation` remains the reviewable source of layer, interaction, filter, view, and provenance semantics. Renderer-specific styles or configs are generated artifacts. For `deck`, pin every overlay parameter and validate that each declared layer renders. For `kepler`, pin the package/config version and assert after load that every expected dataset and layer ID is present; a schema mismatch that silently drops a layer fails validation. See `web-delivery.md` for the three renderer lanes and examples.

**Separate analysis semantics from rendering implementation.** The project declares *what* to show and its hierarchy (map/summary/metric/filter/legend/layer control/feature details/table/chart/timeline/provenance/warning). A renderer decides spacing, fonts, colors, controls. Prefer **stable semantic roles** (`primary result`, `source/context`, `constraint`, `excluded`, `warning`, `user_override`, `planned`) over arbitrary agent-chosen hex colors.

#### Dashboard edit mode and draft override bundles

When `presentation.editing` enables an operation, the dashboard may record it as
a browser-local draft. Edit mode never rewrites embedded source data and never
changes `project.status`, the validation report, or a run record. The dashboard
renders the effective preview as immutable base data plus active draft operations.

Drafts are scoped to the project id and base run id. A source refresh therefore
opens a different draft rather than silently applying edits to a new snapshot.
Undo/redo is represented by an event history; the exported bundle contains the
currently active operations and may also retain that history for audit.

`open-gis-override-bundle/v1` is JSON with this minimum contract:

```json
{
  "schema": "open-gis-override-bundle/v1",
  "status": "draft_unvalidated",
  "project": {
    "id": "tartu-development-access",
    "base_run_id": "run-20260825-145400",
    "inputs_hash": "sha256:..."
  },
  "exported_at": "2026-08-25T15:30:00Z",
  "operations": []
}
```

Each operation uses the override fields from section 2.3 and additionally records
the base run, target source version/hash, asserted prior value where applicable,
and `status: draft_unvalidated`. User-drawn coordinates are permitted because
they are explicit user geometry; mark them `geometry_origin: user_drawn` and keep
scenario/annotation/AOI semantics distinct.

The UI must distinguish preview capability:

- `analysis_rules_reapplied`: existing browser rules were re-applied to values the
  canonical pipeline already measured (for example hiding a candidate or changing
  an exported land-use value).
- `map_only`: the map changed, but downstream spatial measurements were not
  recomputed (for example moving a facility or drawing a new road in a phase-1
  editor).

An exported bundle becomes canonical only after a trusted pipeline importer
checks the project/run identity, verifies every source precondition, writes or
references real override geodata, reruns affected processing steps, reports every
override `applied`, `rejected`, or `not_testable`, and emits a new run record.

### 2.8 Runtime, runs, warnings

```yaml
runtime:
  implementation:
    preferred_engine: duckdb-spatial
    pipeline: pipeline.py
    dependencies:                  # project-local files needed in a clean run
      - requirements.lock
      - config/analysis.toml
  environment:
    python: "3.13"
    duckdb: "1.2.x"
    gdal: "3.9.x"
    proj: "9.4.x"

runs:
  latest:
    id: run-20260825-081503
    started_at: "..."
    completed_at: "..."
    status: passed              # passed | warning | failed
    inputs_hash: "sha256:..."   # hash of sources+overrides+pipeline
    outputs_hash: "sha256:..."
    record:                     # optional; the run record is also resolved
      path: runs/run-20260825-081503.json   # by convention from the run id
    validation_report:
      path: validation/latest-report.json

warnings:                        # known, unresolved data-quality limits
  - id: DATA-001
    severity: medium
    layer: pois
    issue: completeness_unknown
    statement: >-
      This dataset may omit recently opened or closed locations.
    mitigation: >-
      Verify material POIs against authoritative local sources before
      consequential decisions.
```

**Warnings** give the explicit confidence/incompleteness handling. The rendered UX surfaces them (don't imply autoconfirmed geodata is current/complete). **Runs** capture what changed between executions and let a new engineer `rerun` tomorrow.

The corresponding `runs/<id>.json` record MUST contain `inputs` and `outputs`
file inventories. Each entry has a project-relative `path` and a SHA-256 of
the real file. Inputs MUST include every file under `data/source/` and
`data/overrides/`, the canonical pipeline or project-local command files, and
every declared runtime dependency. Outputs MUST include every declared
`outputs.*.path`; presentation artifacts may also participate.

`inputs_hash` and `outputs_hash` are canonical file-set hashes, not hashes of
manifest prose. Sort the unique inventory paths lexicographically. For each
path, feed SHA-256 an unsigned eight-byte big-endian length of its UTF-8 path,
then the UTF-8 path, then the complete file bytes. The same aggregate MUST be
recorded in the manifest, validation report, and run record. Validators MUST
recompute per-file and aggregate hashes and reject matching-but-invented,
missing, stale, duplicate, or incomplete inventories.

---

## 3. Semantic presentation primitives

Agents should **not** freely reinvent dashboard UX. The `presentation` block declares the semantics; a renderer implements them. Standard primitives: `map`, `summary`, `metric`, `filter`, `legend`, `layer_control`, `feature_details`, `table`, `chart`, `timeline`, `provenance_panel`, `warning_panel`, `validation_status`.

**Stable semantic roles** (use these; avoid random hex) — eleven discrete
values, not slash-joined pairs:
`primary_result`, `secondary_result`, `source`, `context`, `constraint`, `excluded_area`, `warning`, `user_override`, `planned`, `hypothetical`, `selected_feature`.

Declare one `presentation.map.layers` entry per rendered dataset and give each
a role. Source data, derived results, human corrections and scenario geometry
must not collapse onto a single role — that is the distinction the reader needs
most, and the eval suite fails a view whose layers are all one role.

### Standard GIS UX defaults

For analytical GIS applications prefer this opinionated layout:

```
┌─────────────────────────────────────────────┐
│ Header / project title / status             │
├───────────────┬─────────────────────────────┤
│               │                             │
│ Summary       │                             │
│ Filters       │            MAP              │
│ Layers        │                             │
│               │                             │
├───────────────┴─────────────────────────────┤
│ selection / table / details when needed     │
└─────────────────────────────────────────────┘
```

Recommended layer hierarchy (top to bottom in table, but top = most prominent in the panel):
```
Results
Constraints
Project overrides
Source datasets
Reference/context
Basemap
```
The user must be able to visually distinguish: external source data, derived results, human corrections, and assumption/scenario geometry.

### Reconfigurable views

A static screenshot answers one question; the reader almost always has the next one
(*what if the threshold were 3 km? what does that scenario override actually cost?*).
Offer that as controls — a sidebar organised into tabs of collapsible sections, an
on/off control per layer group, and a live control for each parameter the analysis
turns on. Keep the interactivity small and legible: reconfiguring the published rule,
not a second analysis engine in the browser.

Four rules keep a reconfigurable view honest:

* **The canonical position is the accepted run.** Every control opens at the value
  `project.yaml` declares, and returning to those values must reproduce the published
  numbers exactly. Declare them in `presentation.controls` so the renderer reads the
  analysis rather than restating it, and validate that the declaration still matches
  the thresholds the pipeline ran — a `presentation` block that drifts from the
  pipeline turns the whole view into a confident lie.
* **Off-canonical states must say so.** The moment any control leaves its canonical
  position, label the view as exploratory and offer a one-click reset. A what-if that
  looks identical to the accepted result is worse than no control at all.
* **Re-apply rules; never re-measure geometry.** The browser may re-evaluate a
  published rule against values the pipeline measured in the analysis CRS. It must not
  compute distances, areas, buffers or reprojections of its own — those belong to the
  pipeline, in a projected CRS, in the run record. If a control changes a shape on the
  map (a different buffer radius), materialise that shape in the pipeline and switch
  between precomputed variants.
* **Scenarios switch between measured states.** To make an override reversible in the
  view, export the baseline measurement beside the effective one
  (`dist_kg_m` / `dist_kg_baseline_m`) and let the control choose. Never approximate
  the counterfactual, and never let switching an override off imply the source data
  changed.

### Provenance UX

Make provenance inspectable in the UI, not hidden in a README. Selecting a feature should be able to show:

`Source`, `Dataset`, `Dataset version`, `Retrieved timestamp`, `Original feature ID`, `Transformation chain`, `Manual overrides`, `Override author`, `Override rationale`, `Evidence`, `Validation status`.

At the project level offer: `Sources`, `Assumptions`, `Manual edits`, `Processing steps`, `Validation results`, `Run history`.

---

## 4. Pipeline generation — the boring, inspectable script

For each project, generate a deterministic, readable pipeline where practical: `pipeline.py`, or equivalent `SQL`/`DuckDB SQL`/`GDAL`/`PDAL`/QGIS Processing model/`Makefile`/mixed. It should be **deliberately boring**: leading comments per step documenting source, retrieval timestamp, rationale, and CRS warnings.

```python
# STEP 1
# Load parcels.
#  Source: Layer3 / MaaTee WFS
#  Retrieved: 2026-08-25T08:18:12+03:00
#  Rationale: authoritative cadastral geometry.
parcels = load_parcels(...)

# STEP 2
# Reproject to L-EST97 before metric calcs.
# EPSG:4326 MUST NOT be used for area calculations.
parcels = reproject(parcels, "EPSG:3301")

# STEP 3
# Apply project-specific overrides.
# Corrections live in data/overrides and are logged in project.yaml.
parcels = apply_overrides(parcels, "data/overrides/parcels.geojson")
```

Do not leave important analytical logic only in transient LLM reasoning. The pipeline is the executable record of the `processing.steps`.

**One canonical implementation creates every declared output.** Convenience or end-to-end entrypoints (`run_e2e.py`, a Makefile target, a notebook) may *wrap* `pipeline.py`, but must never duplicate its processing, QGIS generation, or report writing — duplicated logic drifts, and then two commands documented as equivalent quietly produce different analyses:

```python
# run_e2e.py — thin wrapper, no logic of its own
from pipeline import main

if __name__ == "__main__":
    main()
```

A clean-room run must show that every path in `outputs.*`, every QGIS datasource, and `validation/latest-report.json` were produced by that one canonical path.

---

## 5. QGIS project output (`project.qgz`)

Every multi-stage GIS analysis must generate a first-class QGIS project `project.qgz` that is a **layer- and style-perfect companion** to the web dashboard.

### 5.1 Architecture & File Relationships
A `.qgz` file is literally a zip archive containing the `project.qgs` XML document. It must **reference the exact derived datasets and override files** produced by the pipeline — never an independent or disconnected analytical state:

```text
project.yaml
       │
       ├── pipeline.py
       │
       ├── data/
       │   ├── overrides/planned-road.geojson
       │   └── derived/
       │       ├── final-candidates.gpkg
       │       ├── education_catchments.json
       │       └── education_pois.json
       │
       ├── dashboard.html (MapLibre web view)
       └── project.qgz    (QGIS desktop view)
```

### 5.2 Critical QGIS Datasource Rules (Avoiding Non-Spatial Tables)
1. **GeoPackage vector layers:** The datasource string MUST include `layername=`:
   ```xml
   <datasource>./data/derived/final-candidates.gpkg|layername=final-candidates</datasource>
   <provider encoding="UTF-8">ogr</provider>
   ```
   *Warning:* If you omit `|layername=...`, GDAL opens the SQLite file without binding the geometry table, causing QGIS to load it as a non-spatial attribute table.
2. **GeoJSON vector layers:** Use relative paths:
   ```xml
   <datasource>./data/derived/education_catchments.json</datasource>
   <provider encoding="UTF-8">ogr</provider>
   ```
3. **Tiled Raster Basemaps:** Always include an official tiled basemap matching the project region:
   - **Maa- ja Ruumiamet Grey Basemap (Mustvalge põhikaart, EPSG:3301):**
     ```xml
     <datasource>contextualWMSLegend=0&amp;crs=EPSG:3301&amp;dpiMode=7&amp;featureCount=10&amp;format=image/png&amp;layers=pohi_mvr2&amp;styles=&amp;url=https://kaart.maaamet.ee/wms/alus</datasource>
     <provider>wms</provider>
     ```
   - **OpenStreetMap / CartoDB XYZ Tile Layer:**
     ```xml
     <datasource>type=xyz&amp;url=https://tile.openstreetmap.org/{z}/{x}/{y}.png&amp;zmax=19&amp;zmin=0</datasource>
     <provider>wms</provider>
     ```

### 5.3 Layer Tree Groups & Semantic Styling
The layer tree groups in `<layer-tree-group>` and map layers in `<projectlayers>` must mirror the web dashboard's visual hierarchy:
- **Analysis Results:** Categorized symbols (`Tier 1 Prime: #2e7d32`, `Tier 2 Good: #f57f17`, `Tier 3: #455a64`) with fill opacity and distinct border colors.
- **Constraints / Catchments:** Semi-transparent buffer fills (alpha 25–35) with dashed outline strokes.
- **POIs:** Circle marker symbols with distinct category fills and white borders.
- **Transportation & Overrides:** Styled solid lines for existing infrastructure and dashed gold lines for scenario overrides (`#ffd54f`).
- **Basemaps:** Official grey WMS basemap checked by default, alternative XYZ tiles unchecked.

Deliberate edits made in QGIS editable layers can be exported back into `data/overrides/` to update the pipeline inputs cleanly.

---

## 6. Validation is a pipeline stage

Validation is executable, not prose. `open-gis-project/v1` runs it and emits a machine-readable report such as `validation/latest-report.json`:

```json
{
  "run_id": "run-20260825-081503",
  "status": "passed",
  "checks": [
    {"id": "geometry_valid", "status": "passed", "features_checked": 12458},
    {"id": "no_duplicate_cadastral_id", "status": "passed", "duplicates": 0},
    {"id": "poi_completeness", "status": "warning",
     "reason": "No authoritative completeness baseline available"},
    {"id": "pipeline_rerun", "status": "not_testable",
     "reason": "Sources unavailable from this environment"}
  ]
}
```

Agent **MUST distinguish four statuses** and never turn "not tested" into an implicit pass:

| Status | Meaning |
|---|---|
| `passed` | Actually checked, green |
| `failed` | Actually checked, red → blocks "validated" status |
| `warning` | Known limit / soft miss, surfaced in UX |
| `not_testable` | Could not run — **explicit**, not "passed by default" |

Rerun contract: `open-gis run project.yaml` executes the canonical pipeline and
then validates the resulting artifact. A fresh environment with the documented
sources reproduces the project even without the original LLM conversation.

For an independent clean-room check, copy only the manifest, immutable source
data, override data, the declared pipeline/command files, and project-relative
`runtime.implementation.dependencies`. Do not carry derived outputs, validation
reports, run records, caches, rendered presentation artifacts, prompts, or chat
state into the second workspace. Execute the declared canonical entrypoint,
rerun full artifact validation, and compare normalized semantic outputs and
validation evidence. A missing dependency must fail explicitly; undeclared
access back into an eval harness or conversation is not reproducibility.

### 6.1 Project CLI

Install the repository's Python package and use the project manifest or its
directory as the command target:

```bash
open-gis validate project.yaml
open-gis run project.yaml
open-gis inspect project.yaml
```

- `validate` performs a full artifact audit: schema and metadata, assumptions,
  source URL/retrieval/version/selection/licensing, bounded-API completeness,
  projected analysis CRS, processing graph, override provenance and referenced
  geodata, declared outputs, report parity and status propagation, override
  application results, and run-record identity/hashes. `--preflight` limits the
  check to inputs and declarations before the first run. `--json` emits
  `open-gis-validation-result/v1`; `--strict` treats warnings as a failing exit.
- `run` first performs preflight validation, invokes exactly the pipeline or
  shell-free command in `runtime.implementation`, and then performs the full
  artifact validation. Python pipelines use the interpreter that installed the
  CLI. Projects needing Conda, containers, SQL engines, or another launcher can
  declare `runtime.implementation.command` as a string or argument list. Use
  `--dry-run` to inspect the resolved command.
- `inspect` is read-only. It summarizes identity, CRS, pinned sources,
  overrides, ordered steps, outputs, latest run, and current validation issues;
  `--json` emits `open-gis-inspection/v1`.

The CLI does not claim to recalculate geometry validity, row counts, or semantic
fitness independently. Those domain checks belong in the deterministic
pipeline; the CLI verifies that every declared check occurs exactly once with
an explicit `passed`, `failed`, `warning`, or `not_testable` result.

---

## 7. Iteration into the manifest

Agent exploration is allowed and encouraged to be ad-hoc, but every *decision* it makes must land in the artifact:

```
Run 1 → duplicate OSM features → prefer national dataset → update project.yaml rationale → rerun
Run 2 → missing planned road     → add override layer        → rerun
Run 3 → validation passes       → render final dashboard
```

If you discover a problem mid-run, update `project.yaml`, add the override, and rerun — don't patch the chat. The chat transcript is not part of the dependency graph.

---

## 8. Templates & examples

Grab a scaffold from `../templates/` and copy/adapt:

- `templates/project.yaml` — full sparsely commented skeleton
- `templates/pipeline.py` — boring, commented pipeline skeleton
- `templates/presentation.yaml` — semantic presentation defaults
- `templates/validation.yaml` — validation rules + report starters

A worked example implementing the acceptance (Tartu) scenario in `../examples/tartu-development/` contains a complete `project.yaml`, pipeline, overrides, validation, and a QGIS project pass.
