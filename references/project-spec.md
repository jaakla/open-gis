# Reproducible GIS Project Specification — `openmapstack-project/v1`

The canonical, runnable unit of OpenMapStack analysis. For any **material multi-stage GIS analysis**, the delivered output is a *project artifact*, not just a map, dashboard, or narrative answer. This file defines that artifact.

Read this before compiling any non-trivial OpenMapStack analysis. Ready scaffolds live in `../templates/`; a fully-worked example matching the acceptance scenario lives in `../examples/tartu-development/`.

**Core principle:** reasoning may be exploratory; the delivered analysis must be deterministic, inspectable, and reproducible.

---

## 1. Project layout

```text
my-analysis/
├── project.yaml          # canonical manifest (openmapstack-project/v1)
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

## 2. `project.yaml` schema (`openmapstack-project/v1`)

The manifest describes *what* the analysis is and *why*, the pipeline describes *how* to run it. Keep this file documented and human-reviewable.

**Normative sources.** Structure — required keys, types, and top-level enums — is
machine-checked by [`openmapstack/schemas/project-v1.schema.json`](../openmapstack/schemas/project-v1.schema.json),
reported as the `manifest.json_schema` check. That file is normative where it and
this document disagree on *shape*. This document is normative for *semantics*: what
each field means, the rules that have no structural form (source pinning, override
provenance, label honesty, hash construction), and the cross-file invariants
`openmapstack validate` enforces beyond the schema. The YAML below is illustrative — a
worked example of the shape, not a second field registry to keep in sync by hand.

### 2.1 Head and interpretation

```yaml
schema: openmapstack-project/v1

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
`statement`/`rationale` requirement is a semantic rule checked by `openmapstack validate`,
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
- For bounded APIs, record the service total (`numberMatched`, `resultCount`, or equivalent), page size, pages fetched, and final returned count under `selection.completeness`. A page filled to its limit is not proof of completeness. `openmapstack validate` also accepts `completeness` at the top level of the source for backward compatibility, but `selection.completeness` is canonical: the counts describe that selection.
- Describe the data you received in `schema` — its CRS, the key, the field roles the analysis depends on, and the columns. Earlier drafts used a bare `expected_fields` list; `schema.columns` supersedes it.
- `access.retrieved_at` and `access.downloaded_at` are interchangeable to the validator, which needs one of the two. Record both when they differ (a cached extract retrieved later than it was published).

#### Pin classes — warehouse and mutable sources

`version.identifier` pins a published release, a STAC item, or a dated
extract. It does not pin a warehouse table: "the parcels table on
2026-08-30" names a moving target unless something froze it. A source whose
bytes come from a query declares **which of two pin classes** froze them:

```yaml
sources:
  parcels:
    access:
      method: postgis
      connection: {ref: "env:PARCELS_DSN"}    # a reference, never a DSN
    warehouse:
      backend: postgis                        # duckdb | postgis (pilot backends)
      account: geo-prod                       # host / project identity, no secrets
      database: gis
      schema: cadastre
      table: parcels
      query_sha256: "sha256:..."              # digest of the exact SELECT used
      schema_sha256: "sha256:..."             # digest of the discovered column list
    pin:
      class: local_snapshot                   # (1) user-approved local copy
      path: data/source/parcels.parquet
      sha256: "sha256:..."                    # real content hash of that file
      captured_at: "2026-08-30T10:00:00Z"
    # -- or --
    pin:
      class: backend_snapshot                 # (2) backend time travel / snapshot id
      identifier: "pg_export_snapshot:00000003-000001A8-1"
      captured_at: "2026-08-30T10:00:00Z"
      retention_until: "2026-12-31T00:00:00Z" # when the backend may drop it
      verification: {at: "2026-08-30T10:05:00Z", status: accessible}
```

| Pin | Reproducible when | Reported as |
|---|---|---|
| `local_snapshot` | the file exists under `data/source/` and matches `sha256` | `pinned`; a missing or edited file is `not_reproducible` |
| `backend_snapshot` | `identifier` names a real snapshot, `retention_until` is in the future, and the last `verification` did not find it inaccessible | `pinned`; expired or inaccessible is `not_reproducible` |
| none | `version.identifier` / `published_at` is present and not a mutable alias (`latest`, `current`, `head`, …) | `pinned` by version identity |

`provenance.every_source_pinned` and `validate`'s `source.pin` check apply
this table. A backend snapshot id plus a timestamp is **not** a pin once the
retention has lapsed; the honest result is `not_reproducible`, never `pinned`
because a string is present. A mutable alias in `version.identifier` is
unpinned whatever else is declared.

**Secrets never enter `project.yaml`.** `access.connection` is a reference —
`env:NAME`, `service:NAME` (a `pg_service.conf` entry), `keyring:NAME`, or
`file:/absolute/path/outside/the/project` — and the manifest is scanned for
password fragments, credentialed URLs, and well-known key shapes
(`source.credentials`, `provenance.no_inline_credentials`). Connector
discovery is read-only, and materialising warehouse data locally requires an
explicit approval; see `references/user-data-sources.md`.

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
  expectations: []                  # optional independently attested answers
```

**Check names are flat identifiers**, not mappings: write `no_duplicate_cadastral_id`, not `no_duplicate: cadastral_id`. Every name in `required` and every `domain_checks[].name` must appear verbatim as a `checks[].id` in the report, so parity is a literal string comparison with no flattening rule to implement.

The `required` list gates "done". Rarely a complete list of what validation that run evaluated is captured in the run *report* (below).

Every required and domain check must appear exactly once in the report. `warning` or `not_testable` checks make the overall run/project status `warning`; only an all-passed report may set `project.status: validated`. The report run ID and hashes must match a real `runs/*.json` record.

Project-specific answers that generic checks cannot know belong in
`validation.expectations[]`, not in assistant prose and not in an assertion the
pipeline silently derives from its own output. Supported checks are
`geodata.row_count`, `geodata.feature_present`, `geodata.feature_absent`,
`geodata.feature_field_equals`, and `geodata.field_range`.

Start with an unverified proposal:

```yaml
validation:
  expectations:
    - id: accepted-parcel-count
      check: geodata.row_count
      args: {path: data/derived/parcels.parquet, equals: 1242}
      attestation:
        status: unverified
        reason: "Awaiting comparison with the cadastral register"
```

`openmapstack verify --json` reports the canonical
`expected_expectation_sha256`. After independently checking the answer, the
reviewer binds the exact check/arguments, current project inputs, and retained
evidence:

```yaml
      attestation:
        status: verified
        verified_by: "reviewer or authority"
        verified_against: "stable source or review-record locator"
        verified_at: "2026-08-30T00:00:00Z"
        evidence_path: validation/evidence/cadastral-count.json  # optional
        evidence_sha256: "sha256:<64 hex characters>"
        expectation_sha256: "sha256:<digest reported by verify>"
        inputs_hash: "sha256:<current runs.latest.inputs_hash>"
```

The verifier uses a fixed check allowlist, rejects unsafe project paths and SQL
identifiers, and does not execute unverified expected values. Editing the check
or arguments, changing `runs.latest.inputs_hash`, or changing a retained local
evidence file makes the attestation stale and produces a warning. Attestation
records reviewer evidence; it is not relabelled as an independently
reproducible oracle in reports.

#### Metamorphic relations — invariants without a golden answer

`validation.metamorphic[]` declares relations that must hold when an input or
parameter is perturbed in a controlled way. They need no frozen answer, so
they transfer to data nobody has an oracle for — and they are **conditional**:
each relation is valid only under preconditions the declaration must state,
and `openmapstack verify --metamorphic` reports `not_testable` with the reason
when a precondition does not hold on the actual data rather than guessing.

```yaml
validation:
  metamorphic:
    - id: parcel-order
      relation: input_permutation_invariance
      source: {path: data/source/parcels.geojson}
      outputs: [candidate_parcels]
      key: cadastral_id
      preconditions:
        tie_break: "candidates are keyed by cadastral_id; no selection depends on input order"
    - id: parcel-duplicates
      relation: duplicate_resistance
      source: {path: data/source/parcels.geojson}
      outputs: [candidate_parcels]
      key: cadastral_id
      preconditions: {dedup_key: cadastral_id, measure: set}
    - id: road-distance-monotonic
      relation: positive_buffer_monotonicity
      parameter: road_distance_m        # declared under runtime.implementation.parameters
      variant: {multiply: 3}
      outputs: [candidate_parcels]
      key: cadastral_id
      preconditions: {predicate: within_distance, expected: superset}
      limits: {timeout_s: 600, max_source_bytes: 67108864}
```

| Relation | Transformation | Expected | Valid only when |
|---|---|---|---|
| `input_permutation_invariance` | source features shuffled (deterministic `seed`) | outputs semantically equal | a deterministic `tie_break` rule is declared and `key` is unique in the output |
| `duplicate_resistance` | every source feature appended once more | outputs equal | the analysis deduplicates on `dedup_key` (unique in the source) and the output is a keyed *set*; counts and sums are rejected |
| `positive_buffer_monotonicity` | a declared numeric parameter increased (`multiply` > 1 or `add` > 0) | every baseline key survives (`superset`) | the parameter drives an inclusion `predicate` (`within_distance`, `intersects_buffer`, `within_buffer`) |

Each relation reruns the canonical entrypoint in an isolated copy prepared like
a clean rerun, perturbs only that copy, compares against the produced outputs,
and deletes the copy. The project's own `data/source/` and `data/overrides/`
are hashed before and after; a variant that mutates them fails. Unknown
relation names, `source` paths outside the immutable trees, non-growing
variants, and `duplicate_resistance` declared for a count or sum are
declaration failures, not skipped checks.

**Counterexamples — do not enable a relation mechanically:**

- a nearest-neighbour join with ties has no order invariance until the tie
  rule is fixed in the pipeline; declare `tie_break` only once it is;
- a facility *count* per parcel is not duplicate-resistant — duplicating a
  facility legitimately changes the count; only a keyed set of facilities is;
- an *exclusion* buffer (parcels farther than N m) is monotonic the other way;
  `positive_buffer_monotonicity` only establishes `superset` for inclusion
  predicates and refuses any other `predicate`;
- a threshold that also changes a classification (`tier1` below 1 km, `tier2`
  below 2 km) keeps the key set but changes attributes; the relation only
  compares keys, so declare it knowing that.

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
    basemap:
      # Required whenever a map is presented. Analysis geometry floating on
      # blank canvas is unreadable: the reader cannot tell which town, which
      # side of the river, or whether the CRS is displaced. Declaring the
      # basemap here is what makes "there is a background map" checkable
      # against the built product rather than a matter of trust.
      id: osm-standard
      kind: raster-xyz                # raster-xyz | raster-wms | vector-style
      tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"]
      attribution: "© OpenStreetMap contributors"   # must be visible in the UI
      default_visible: true
      note: "Reference/background map; not an analysis input."
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
      # ORDER IS BOTTOM-TO-TOP: the first entry is painted first, later
      # entries paint over it, and the basemap sits beneath all of them.
      # See section 5.3 for what this means in a QGIS layer tree, which
      # stacks the opposite way.
      - source: candidate_parcels
        group: analysis
        semantic_role: primary_result
        geometry: polygon           # point | line | polygon | raster
        style:
          visual_priority: primary
          opacity: 0.65
      # A layer holding browser-local draft state declares `persistence`
      # matching editing.draft_persistence. It is in the manifest so the
      # legend is complete, not as a claim about a delivered dataset: no file
      # backs it and no QGIS project can carry it, so the checks that hold the
      # product to the manifest (qgis.layers_match_manifest,
      # qgis.every_declared_layer_renders) skip it instead of failing it.
      - source: draft_overrides
        group: user_overrides
        semantic_role: user_override
        geometry: mixed
        persistence: local_storage
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
    export_format: openmapstack-override-bundle/v1
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

**`presentation.map.basemap` is required whenever `presentation.map` is present**, and its `tiles`/`url` and `attribution` are load-bearing rather than decorative. The dashboard must really request tiles from the declared endpoint and really display the declared attribution; `visual.dashboard_loads_in_browser` fails with `basemap_absent` when the manifest omits the basemap, when no tile request to the declared URL is ever issued, or when the attribution is not visible in the rendered product. Use an official regional service where one exists (Estonia: Maa- ja Ruumiamet WMS) and OSM/Carto XYZ otherwise; see `references/data-sources.md`.

`presentation.map.engine_preference` is a closed enum:

| Value | Meaning |
|---|---|
| `maplibre` | Default. Generate the reproducible delivered web map with MapLibre, normally reading PMTiles. |
| `deck` | Generate a MapLibre basemap plus a deterministic deck.gl overlay for density, flows, 3D magnitude, or very large rendered feature sets. |
| `kepler` | Generate an exploration-first kepler.gl view when UI-driven filtering, time playback, rapid aggregation, or interactive 3D is the primary need. |

Use the literal values above: `deck`, not `deck.gl`; `kepler`, not `kepler.gl`. QGIS is a required companion output for multi-stage analysis and is not a value in this web-renderer enum.

This enum is an authoring rule with no automated gate today: the JSON schema accepts
`presentation` as any object, and `openmapstack validate` does not check the value. The
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

`openmapstack-override-bundle/v1` is JSON with this minimum contract:

```json
{
  "schema": "openmapstack-override-bundle/v1",
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
    parameters:                    # optional; how a variant run turns one knob
      - id: road_distance_m
        type: number               # integer | number | string
        canonical: 2000
        binding: {argument: "--road-distance-m"}   # or {environment: OMS_ROAD_DISTANCE_M}
        step: road_distance        # optional pair: the step that consumes it
        field: max_distance_m      # ... whose value must equal `canonical`
      - id: sample_area            # optional; the knob a sampled run turns
        type: string
        canonical: ""              # a canonical run samples nothing
        role: sample_area          # sample_area | sample_rows | sample_fraction
        sample: "26.68,58.35,26.76,58.39"   # what bare `--sample` binds
        binding: {argument: "--sample-area"}
  environment:
    python: "3.13"
    duckdb: "1.2.x"
    gdal: "3.9.x"
    proj: "9.4.x"

runs:
  latest:                       # the CANONICAL run; never a sampled one
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

`runtime.implementation.parameters` is the versioned parameter-addressing
contract (`openmapstack-parameters/v1`). The canonical run passes nothing and
must produce the accepted result; a binding exists so a metamorphic relation or
a benchmark can run *the same pipeline with one knob turned* without editing
it. When `step`/`field` are given, `openmapstack verify` fails
`project.parameters_match_steps` if the step's declared value drifts from
`canonical` — the same honesty rule as `presentation.controls`.

#### Sampled runs

A wide-area, high-resolution analysis can run for hours before a late step
fails. A **sampled run** executes the same pipeline over a deliberately smaller
slice so that failure arrives in minutes. A parameter opts into this by
declaring a `role`, which is how `openmapstack run --sample`,
`--sample-area`, `--sample-rows`, and `--sample-fraction` address it. (Note
that `--dry-run` is a different thing entirely: it prints the command and
executes nothing.)

Sampling rules on top of the parameter contract above:

- at most one parameter may claim each role;
- `canonical` must be the role's *no sampling* value (`""` for a string, `0`
  for a number) — a canonical run passes nothing, so it must process the
  full inputs;
- `sample` is optional, must differ from `canonical`, and is what bare
  `--sample` binds; without it the role needs an explicit value on the
  command line;
- a sampling parameter must **not** pair `step`/`field`: it selects *input*,
  not a processing threshold, so there is no step value to agree with.

**A sampled run proves the pipeline executes. It never establishes the
result.** Clipping to a test AOI breaks every neighbourhood operation at the
cut; row sampling destroys the spatial coherence a join depends on;
downsampling a raster changes areas and slopes non-linearly. Sampled counts and
aggregates are therefore not answers and must not be surfaced as such or bound
into `validation.expectations`.

That is enforced structurally rather than by convention. A sampled run
legitimately produces a different `inputs_hash` — clipped inputs are different
bytes — so it cannot share the canonical hash chain. On top of that:

- its `runs/<id>.json` record MUST declare `mode: sampled` (a record with no
  `mode` is canonical) and a `sample` object carrying `requested` and
  **`realized`**, plus an optional `scale_factor` in `(0, 1]`. Recording only
  what was requested is a failure: `TABLESAMPLE` and its equivalents
  approximate, so the realized rows, AOI, or resolution are the measurement;
- a canonical record MUST NOT carry a `sample` object;
- `runs.latest` MUST NOT reference a sampled record. It is what `verify`, the
  clean-rerun protocol, and every expectation attestation bind to, so a sampled
  record reaching it would launder a smoke test into a result.

`openmapstack validate` reports this as `runs.sample_isolation`; the same
invariant is exposed to external harnesses as the
`validation.sample_run_not_promoted` check. `openmapstack run --sample*`
additionally re-reads the manifest afterwards and fails if the pipeline
promoted its own sampled run, and reports any declared outputs the sampled run
overwrote in place — those files no longer hash to what the canonical run
recorded, so the project is correctly no longer `validated` until it is re-run
in full.

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
3. **Tiled Raster Basemaps:** Always include an official tiled basemap matching the project region, and prefer one that answers unauthenticated requests. CARTO's raster XYZ tiles (`basemaps.cartocdn.com/rastertiles/…`) now return an *API KEY REQUIRED* watermark, so a project that ships them draws that watermark across every view; CARTO's MapLibre **vector** styles are still open, which is why a dashboard and its QGIS companion may legitimately carry different backgrounds. Every basemap layer must declare its own **complete** `<srs>` — see rule 4.
   - **Maa- ja Ruumiamet Baaskaart (WMS, EPSG:3301):**
     ```xml
     <datasource>contextualWMSLegend=0&amp;crs=EPSG:3301&amp;dpiMode=7&amp;featureCount=10&amp;format=image/png&amp;layers=BAASKAART&amp;styles=&amp;url=https://kaart.maaamet.ee/wms/alus</datasource>
     <provider>wms</provider>
     ```
   - **OpenStreetMap XYZ Tile Layer** (XYZ tiles are always Web Mercator, whatever the project CRS):
     ```xml
     <datasource>type=xyz&amp;url=https://tile.openstreetmap.org/{z}/{x}/{y}.png&amp;zmax=19&amp;zmin=0</datasource>
     <provider>wms</provider>
     ```
4. **Every layer needs a complete `<srs>`, and the project needs `ProjectionsEnabled`.** These are two halves of one requirement: without both, QGIS cannot reproject, and a project whose layers are in more than one CRS shows only the layers that happen to match the map's CRS.
   - A `<maplayer>` with **no** `<srs>` is assumed to be in the **project** CRS and is never reprojected. Omit it on a Web Mercator basemap in an EPSG:3301 project and QGIS reads the projected metres as Web Mercator metres: an extent around Tartu (`x=660900 y=6469325`) resolves to 50.13°N 5.94°E and the background map is drawn from the Belgian Ardennes, ~1500 km away, under correctly placed analysis layers. `openmapstack validate` fails it as `qgis.layer_crs`.
   - An **incomplete** `<srs>` is the quieter version of the same fault. `<spatialrefsys>` carrying only `<srid>`/`<authid>` reads back as an *invalid* CRS — `layer.crs().authid()` still answers `EPSG:3301`, so every static check passes, but QGIS can build no transform from it and silently paints nothing for that layer. Write the full element, with `<wkt>` (or at minimum `<proj4>`) alongside the identifiers:
     ```xml
     <srs>
       <spatialrefsys nativeFormat="Wkt">
         <wkt>PROJCRS["Estonian Coordinate System of 1997",…,ID["EPSG",3301]]</wkt>
         <proj4>+proj=lcc +lat_0=57.5175539305556 +lon_0=24 … +units=m +no_defs</proj4>
         <srsid>1259</srsid><srid>3301</srid><authid>EPSG:3301</authid>
         <description>Estonian Coordinate System of 1997</description>
         <projectionacronym>lcc</projectionacronym>
         <ellipsoidacronym>EPSG:7019</ellipsoidacronym>
         <geographicflag>false</geographicflag>
       </spatialrefsys>
     </srs>
     ```
   - A hand-written `.qgs` must also carry the project property that turns reprojection on. Without it the `<projectCrs>` element is discarded on read however complete it is, and the project opens in whatever CRS the reader's defaults supply:
     ```xml
     <properties>
       <SpatialRefSys><ProjectionsEnabled type="int">1</ProjectionsEnabled></SpatialRefSys>
     </properties>
     ```

   All three traps are specific to hand-written `.qgs` XML. Building through the PyQGIS API (`QgsRasterLayer("type=xyz&url=…", name, "wms")`, `QgsProject.setCrs`, `QgsProject.write`) avoids them: the providers resolve their own CRS and QGIS serialises everything it needs on save. Nothing about a project with these faults looks broken — layers are valid, datasources resolve, the render is not blank — which makes them **confidently wrong maps**, the worst failure mode in the catalogue.

### 5.3 Layer Tree Groups & Semantic Styling
The layer tree groups in `<layer-tree-group>` and map layers in `<projectlayers>` must mirror the web dashboard's visual hierarchy:
- **Analysis Results:** Categorized symbols (`Tier 1 Prime: #2e7d32`, `Tier 2 Good: #f57f17`, `Tier 3: #455a64`) with fill opacity and distinct border colors.
- **Constraints / Catchments:** Semi-transparent buffer fills (alpha 25–35) with dashed outline strokes.
- **POIs:** Circle marker symbols with distinct category fills and white borders.
- **Transportation & Overrides:** Styled solid lines for existing infrastructure and dashed gold lines for scenario overrides (`#ffd54f`).
- **Basemaps:** Official grey WMS basemap checked by default, alternative XYZ tiles unchecked.

**The two renderers stack in opposite directions.** `presentation.map.layers` is ordered **bottom-to-top** (a web map paints later layers over earlier ones), while a QGIS layer tree paints its **first** entry on top. Writing the manifest order straight into the tree therefore inverts the visual hierarchy: it puts the analysis fill above the point layer that belongs on top of it, and an opaque fill then hides those points completely — in a project where every layer still loads valid, every datasource resolves, and the render is not blank. **Build the layer tree in reverse manifest order, with the basemap group last (bottom).** `qgis.every_declared_layer_renders` enforces the consequence rather than the form: each declared layer is removed from an otherwise identical render, and the result must differ, so a layer that paints nothing fails no matter why.

**Mirroring is checkable.** Every group declared in `presentation.map.layer_groups` must appear as a `<layer-tree-group>`, named by either the group's `id` or its `title` — matched case-insensitively, with spaces, underscores and hyphens treated as equivalent. So a group declared `{id: user_overrides, title: Manual additions}` may be named `user_overrides`, `Manual additions`, or `Manual-Additions` in the tree, but not `Extras`. A manifest group with no corresponding tree group means the QGIS project shows less than the dashboard claims, and fails `qgis.groups_match_manifest`.

Deliberate edits made in QGIS editable layers can be exported back into `data/overrides/` to update the pipeline inputs cleanly.

---

## 6. Validation is a pipeline stage

Validation is executable, not prose. `openmapstack-project/v1` runs it and emits a machine-readable report such as `validation/latest-report.json`:

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

Rerun contract: `openmapstack run project.yaml` executes the canonical pipeline and
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
openmapstack validate project.yaml
openmapstack verify project.yaml
openmapstack run project.yaml
openmapstack inspect project.yaml
```

- `validate` performs a full artifact audit: schema and metadata, assumptions,
  source URL/retrieval/version/selection/licensing, bounded-API completeness,
  projected analysis CRS, processing graph, override provenance and referenced
  geodata, declared outputs, report parity and status propagation, override
  application results, and run-record identity/hashes. `--preflight` limits the
  check to inputs and declarations before the first run. `--json` emits
  `openmapstack-validation-result/v1`; `--strict` treats warnings as a failing exit.
- `verify` derives an applicable check plan from the manifest and inspects the
  produced artifacts without requiring a repository-owned golden answer. It
  reports partial execution as warning, evaluates only allowlisted and current
  attestations, optionally performs the independent clean rerun with
  `--rerun`, and optionally executes every declared metamorphic relation with
  `--metamorphic`; `--json` includes applicability coverage and evidence class.
- `run` first performs preflight validation, invokes exactly the pipeline or
  shell-free command in `runtime.implementation`, and then performs the full
  artifact validation. Python pipelines use the interpreter that installed the
  CLI. Projects needing Conda, containers, SQL engines, or another launcher can
  declare `runtime.implementation.command` as a string or argument list. Use
  `--dry-run` to inspect the resolved command.
- `inspect` is read-only. It summarizes identity, CRS, pinned sources,
  overrides, ordered steps, outputs, latest run, and current validation issues;
  `--json` emits `openmapstack-inspection/v1`.

`validate` does not claim to recalculate project-specific domain answers; it
verifies that every declared pipeline check occurs exactly once with an
explicit result. `verify` independently recomputes the bounded artifact
predicates it can address. Exact project answers run only through current,
allowlisted attestations, and semantic fitness still requires authoritative
domain evidence rather than a generic CLI claim.

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
