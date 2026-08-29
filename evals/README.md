# OpenMapStack eval suite

Executable evals that check whether an agent-generated analysis (or a
hand-authored reference project) reaches the right analytical answer, respects
the GIS-method guardrails in `SKILL.md`, and reruns reproducibly — with the
`openmapstack-project/v1` contract in `references/project-spec.md` as the substrate
that makes all three independently checkable rather than a matter of trusting
the agent's narration.

> A model/agent should not pass because it says the right things. It passes
> when the produced project can be inspected, executed and independently
> checked.

## Execution modes and score types

Execution mode describes how a project is produced. `fixture` runs committed,
deterministic reference projects with no network or LLM calls. `live` starts in
an empty workspace, copies only explicitly declared input fixtures, invokes an
agent, and grades its output with the same semantic assertions. `visual`
executes the fixture generator in a richer validation environment (real PyQGIS
and a headless Chromium) and grades it under the separate `integration_visual`
score type.

```bash
python evals/run.py --mode fixture
```

Score type describes what the result means. The v2 result schema keeps four
denominators separate:

- `contract_ci`: positive reference-project conformance;
- `mutation_tests`: successful detection of deliberately defective projects;
- `agent_benchmark`: live agent task success;
- `integration_visual`: rendered QGIS/browser integration checks.

Cases 001–006 support both fixture and live execution (001 and 006 also run a
visual leg). Cases 901–911 are fixture-only mutations; case 912 is a
visual-only mutation that proves a dashboard which hides a manifest warning
fails in a real browser. Mutation detection is never included in contract or
agent pass rates.

```bash
python3 evals/run.py --mode live \
  --agent claude_code \
  --model claude-sonnet-4-6 \
  --skill-mode enabled \
  --case 001-basic-spatial-analysis \
  --case 002-attribute-override \
  --case 004-data-quality-warning \
  --case 005-reproducible-rerun \
  --repetitions 3
```

The runner returns setup exit code `2` if no cases support the selected mode,
so an unsupported selection cannot publish a green `0/0` benchmark.

## Visual integration (PyQGIS + browser)

Fixture CI proves the deterministic contracts; it deliberately cannot prove
that the produced QGIS project actually renders or that the dashboard behaves
like the manifest claims. The `visual` mode covers exactly that gap in a
capable environment (weekly schedule + manual dispatch:
`.github/workflows/eval-visual.yml`, container `qgis/qgis:3.44-trixie` plus
Playwright Chromium):

- `qgis.runtime_load` — every layer loads valid under PyQGIS and the
  controlled-extent render succeeds; a blank render (missing layers,
  collapsed extent, displaced CRS) fails with `blank_render`;
- `qgis.layers_match_manifest` — every layer the manifest's
  `presentation.map.layers` claims is loaded with a resolvable CRS and the
  declared geometry family (not_testable without PyQGIS, so it is a soft
  gate outside the integration environment);
- `qgis.styles_declared` / `qgis.groups_match_manifest` — static checks that
  every map layer declares a renderer and that the .qgz layer tree mirrors
  the manifest's layer groups (they run in fixture CI too);
- `visual.render_substantive` — a rendered snapshot must contain real drawn
  content (std-lib PNG analysis, no extra dependencies);
- `visual.dashboard_loads_in_browser` — the dashboard opens in headless
  Chromium with no page or console errors, the map/legend/provenance the
  manifest declares are actually visible, manifest warnings appear in the
  product, every layer-group toggle changes the rendered map, the scenario
  layer stays distinguishable from the authoritative baseline, the canonical
  reset restores control state, and desktop + mobile screenshots are kept.
- **Interactive basemap enforcement** — when the manifest declares
  `presentation.map.basemap` (OSM/Carto XYZ, regional WMS, ...), the product
  must actually render an interactive background map: a map canvas, real
  tile requests to the declared tile URL, and the basemap attribution
  visible in the UI (`basemap_absent` otherwise). This mirrors the skill's
  own rule that a regional tiled basemap is mandatory, and mutation case
  913 proves a basemap-less dashboard fails. The reference fixture ships a
  genuine MapLibre dashboard with an OSM raster basemap, and its `.qgz`
  carries the same XYZ basemap layer in a `basemap` tree group.

Visual checks supplement the numerical/structural assertions — every visual
leg also runs the full semantic assertion library against the generated
project — and never replace them.

Local run:

```bash
pip install -r evals/requirements.txt
pip install "playwright>=1.40" && python3 -m playwright install chromium
python3 evals/run.py --mode visual
```

An assertion whose dependency is missing reports `not_testable` — never a
silent pass. The PyQGIS assertions are declared as soft gates, so a run
without PyQGIS still completes; `visual.dashboard_loads_in_browser` is a
hard gate, so Chromium is required for `--mode visual` to pass at all.
Reading a `not_testable` as a pass is exactly the mistake this suite
exists to prevent: check the per-assertion statuses, not just the case
verdict.

Retained per-trial evidence lives under `evals/results/<run-id>/visual/
<case>/<trial>/`: `grading.json`, the generated project, the PyQGIS render,
and every dashboard screenshot.

## Running

```bash
python evals/run.py                        # every case, fixture mode
python evals/run.py --case attribute-override
python evals/run.py --mode fixture
python evals/run.py --json eval-results.json
python evals/run.py --mode live --agent codex --model <exact-model-id> --timeout 1200
python evals/run.py --repetitions 3 --seed 100
python evals/run.py --list
```

Fixture mode is the default, so an invocation without `--mode` never calls an
agent. `--timeout` applies to each generator or agent invocation;
`--repetitions` runs each selected case multiple times; and `--seed` records a
base seed that is incremented for each repetition.

Live mode requires an explicit `--model`: a run with an unknown CLI default is
not publishable benchmark evidence. `--case` is repeatable. The first benchmark
set is 001, 002, 004, and 005; use at least three repetitions for routine runs
and five for published comparisons.

## Auditable live benchmark bundles

Every retained live trial is written before its temporary workspace is removed:

```text
evals/results/<run-id>/<agent>/<case>/<trial>/
├── prompt.md
├── events.ndjson
├── stdout.txt
├── stderr.txt
├── agent.json
├── generated-project/
└── grading.json
```

`agent.json` uses the vendor-neutral `openmapstack-agent-run/v1` schema. It records
the actual adapter, resolved/requested model, exact installed CLI version,
permission policy, structured completion state, exit status, duration, token
usage, cost where the CLI exposes it, and the repository/skill revision. Raw
vendor events remain in `events.ndjson`; assistant final-answer prose is stored
for audit but never participates in grading. `grading.json` contains the same
deterministic assertion results used by fixture mode, and `generated-project/`
is copied before cleanup so every claim can be checked independently.

By default, live mode copies only `SKILL.md`, `references/`, and `templates/`
into an isolated `benchmark-context/` beside the empty project, prepends an
instruction to read that controlled snapshot, and records its commit and
content hash. Eval cases and expected projects are never exposed. Use
`--skill-mode disabled` for an explicit no-skill baseline; do not mix enabled
and disabled trials in one published denominator.

Use `--run-id` to assign a stable external run name and `--results-dir` to move
the bundle root. The runner refuses to overwrite an existing run directory.
`--no-retain-artifacts` exists only for deliberate adapter smoke tests.

The scheduled workflow runs a non-blocking matrix for both adapters and uploads
the complete bundles. Configure repository variables
`CLAUDE_BENCHMARK_MODEL` and `CODEX_BENCHMARK_MODEL` with exact model ids and
the matching API-key secrets. Claude API keys that require workspace scoping
also need the `ANTHROPIC_WORKSPACE_ID` secret containing the `wrkspc_...` ID;
the workflow sends it as the `anthropic-workspace-id` request header. Manual
runs may select one adapter and override its model. The default is three
repetitions; publication runs should request five.

Exit codes and result states are deliberately distinct:

- `0`: every executed trial has `status: passed`;
- `1`: at least one trial has `status: assertions_failed`;
- `2`: malformed configuration, setup/runtime failure, or zero executed cases.

Generator timeouts/nonzero exits, failed agent invocations, missing agent CLIs,
and assertion implementation exceptions are setup failures. Assertions are not
graded after such a failure, so unrelated breakage cannot satisfy a negative
case that expects assertion status `failed`.

## How a case runs

For each case directory under `evals/cases/<id>/`:

1. Create an isolated temp workspace (`tempfile.mkdtemp`).
2. In fixture mode, copy the case's committed project into the workspace. In
   live mode, create an empty project and copy only `live.fixtures` to their
   declared destinations. Reference outputs are never exposed to the agent.
3. Run `fixture.generator`, or invoke the configured live `AgentAdapter` with
   `prompt.md`, then evaluate whatever project it produced.
   Mutation cases also run their explicit `mutation.control_generator` in a
   separate workspace. The one target assertion must pass on that healthy
   twin before the mutant is eligible for scoring.
4. Require every configured subprocess or agent invocation to succeed. A
   failure produces `setup_failed` and assertions are not scored.
5. Run every assertion listed in the case's `expected.yaml` against the
   resulting workspace, using the reusable checks in `evals/assertions/`.
6. Write a machine-readable v2 result with case type, execution mode, score
   type, trial, pass/fail per assertion, complete subprocess/agent diagnostics,
   run configuration, and environment metadata. Live mode first copies the
   prompt, raw events/streams, normalized agent record, generated project, and
   grading record into its retained per-trial artifact bundle.
7. Return `1` for required assertion failures or `2` for setup failures.

Mutation definitions must contain exactly one non-passing target assertion.
All other assertions are isolation guards and must pass on both the healthy
control and the mutant. An unhealthy control is `setup_failed`, excluded from
the mutation-score denominator, and reported as an invalid mutation. Results
publish detected, survived, invalid, and isolated counts plus an explicit
mutation score.

Assertions read real files: `project.yaml`, `validation/latest-report.json`,
`runs/*.json`, and the actual geodata outputs (via DuckDB Spatial). They do
not pattern-match assistant prose.

## Genuine clean reruns

A mode may declare `clean_rerun: {}`. The runner then creates a second empty
workspace and preserves only:

- `project.yaml`;
- `data/source/` and `data/overrides/`;
- the canonical `runtime.implementation.pipeline` or project-local files named
  by `runtime.implementation.command`;
- project-relative files listed in `runtime.implementation.dependencies`.

Derived outputs, reports, run records, caches, presentation artifacts, prompts,
and conversation-related environment variables are excluded. The runner invokes
the canonical entrypoint without a shell, performs full artifact validation,
and writes `.openmapstack-clean-rerun.json` as evidence. Project-caused rerun
failures are graded by `rerun.clean_execution_succeeded`; they are not
misclassified as eval setup failures.

`rerun.outputs_semantically_equal` ignores row/feature order and normalizes
GeoJSON geometry representation. Validation evidence ignores standard run IDs,
hashes, and timestamps while still requiring identical check results and
evidence. Additional nondeterministic output fields must be explicitly listed
with `ignored_fields`; they are never silently discarded.

A `clean_rerun` also recomputes real sha256 hashes of every file under the
rerun copy's `data/source/` and `data/overrides/` before and after the
canonical entrypoint runs. Any change fails the rerun at the
`source_integrity` stage — a pipeline cannot claim reproducibility while
mutating its own declared-immutable inputs.

## Source hashes and immutability

`fixture.source_baseline` and `live.fixtures` both declare the real,
checked-in file a case treats as ground truth. Before the generator or agent
runs, the runner hashes that origin file directly (never a declared/authored
hash) and makes the result available to assertions as the `$SOURCE_HASHES`
magic value for the `hashes_before` argument:

```yaml
fixture:
  generator: "{python} {evals_dir}/fixtures/reference_pipeline/gen.py {project_dir}"
  source_baseline:
    - { source: ../../fixtures/mini-tartu/pois.geojson, destination: data/source/pois.geojson }

assertions:
  - assert: overrides.source_files_byte_identical
    args: { hashes_before: "$SOURCE_HASHES", paths: [data/source/pois.geojson] }
```

`overrides.source_files_byte_identical` also accepts `rerun_workspace: "$RERUN"`
to compare a workspace's source files directly against a clean-rerun copy.
A missing/empty baseline, or a baseline missing a requested path, is reported
`not_testable` — it can never be silently treated as "no mismatch found".

`openmapstack.validation` independently requires every declared output to exist
and to appear, correctly hashed, in the run record's output inventory
(`runs.latest` fails otherwise), and reports any file under `data/derived/`
that is not a declared output as `outputs.undeclared_derived_files` (`warning`
by policy, never a silent pass).

## Directory layout

```text
evals/
├── README.md
├── run.py                  # CLI runner
├── spatial.py              # controlled, load-only DuckDB Spatial access
├── prepare_spatial.py      # explicit pre-network-disable installation step
├── Dockerfile.offline      # network-disabled fixture acceptance image
├── adapters/               # LLM/agent adapters for live mode (Phase 4)
│   ├── base.py
│   ├── claude_code.py
│   └── codex.py
├── assertions/             # reusable, semantic assertion functions
│   ├── project.py          # schema, graph resolution, status/report agreement
│   ├── geodata.py          # CRS, geometry validity, row/area/CRS tolerances
│   ├── provenance.py       # source pinning, license, immutability
│   ├── overrides.py        # override declaration vs application
│   ├── validation.py       # report/manifest parity, status propagation
│   ├── qgis.py             # static .qgz validity
│   └── presentation.py     # semantic roles, layer groups, controls parity
├── cases/                  # one directory per eval case
│   └── <id>/
│       ├── prompt.md       # live mode: what to ask an agent
│       ├── expected.yaml   # declarative assertion list (see below)
│       └── project/        # fixture-mode: a committed reference project
├── fixtures/               # small, checked-in geodata used by multiple cases
└── results/                # gitignored except .gitkeep; run.py --json output
```

## Case format (`expected.yaml`)

```yaml
id: attribute-override
case_type: positive           # positive | mutation
modes: [fixture, live]
score_types:
  fixture: contract_ci
  live: agent_benchmark
project_dir: project          # relative to the case directory
hard_gate: true               # non-zero exit if any assertion here fails

fixture:
  generator: "{python} {evals_dir}/fixtures/reference_pipeline/gen.py {project_dir}"
  # Cases that test reproducibility opt in explicitly:
  # clean_rerun: {}

# Required only for case_type: mutation. This command must produce the healthy
# twin and the case must declare exactly one expect != passed target.
mutation:
  control_generator: "{python} {evals_dir}/fixtures/reference_pipeline/gen.py {project_dir}"

live:
  agent: claude_code          # optional; --agent overrides it
  prompt_file: prompt.md
  agent_workdir: project
  fixtures:
    - source: ../../fixtures/mini-tartu/pois.geojson
      destination: project/data/source/pois.geojson

assertions:
  - assert: project.schema_is
    args: { schema: openmapstack-project/v1 }
  - assert: project.graph_resolves
  - assert: overrides.declared_count
    args: { count: 1 }
  - assert: overrides.application_status
    args: { id: OVERRIDE-001, status: applied }
  - assert: geodata.row_count
    args: { path: data/derived/pois.parquet, equals: 12 }
  - assert: geodata.feature_field_equals
    args: { path: data/derived/pois.parquet, id_field: poi_id, id: poi-7, field: status, equals: closed }
  - assert: validation.no_implicit_pass
  - assert: validation.required_all_present
```

Generator commands expand four placeholders before execution:

| Placeholder     | Expands to                                                        |
|-----------------|-------------------------------------------------------------------|
| `{python}`      | `sys.executable` — the interpreter running `evals/run.py`         |
| `{repo_root}`   | Repository root                                                    |
| `{evals_dir}`   | The `evals/` directory                                             |
| `{project_dir}` | The case's project directory inside the temporary workspace        |

Always write `{python}`, never a bare `python3`. A hardcoded `python3` resolves
against `PATH` rather than the active interpreter, so it escapes a virtualenv
(the generators then fail on a missing `duckdb`), and it does not exist on a
stock Windows install.

Each `assert` name maps to a Python function `evals/assertions/<module>.<fn>`
taking `(workspace: Path, **args) -> AssertionResult`. `AssertionResult` is
`{status: passed|failed|warning|not_testable, detail: str}` — the same
four-state vocabulary the project spec itself requires, so an eval can never
silently treat "could not check" as a pass.

Case definitions are validated before execution. `score_types` keys must
exactly match `modes`; mode-specific settings live under `fixture` and `live`.
Unknown modes, case/score types, agents, or assertions; invalid expectation
states; unsafe workspace paths; escaped fixture sources; and malformed
generator declarations are setup errors rather than benchmark results.

The structural contracts are published as
`evals/schemas/case-v2.schema.json` and
`evals/schemas/results-v2.schema.json`; cases and emitted summaries are
validated before use. `{python}` in a generator command resolves to the
interpreter running `evals/run.py`, avoiding dependency drift between an
active virtual environment and another `python3` on `PATH`.

Produced manifests are validated by `project.conforms_to_schema` against the
packaged `openmapstack/schemas/project-v1.schema.json`. Checking only the value of
the manifest's `schema:` field is not sufficient.

## Adding a regression eval

Whenever a GIS-correctness or reproducibility bug is fixed in this repo or a
generated project, add a minimal case here that would have caught it:

1. Create `evals/cases/<NNN>-<short-name>/`.
2. Add the smallest fixture/reference project that reproduces the bug.
3. Write `expected.yaml` asserting the specific invariant that was violated
   (not a specific implementation).
4. Confirm the case fails against the buggy state and passes once fixed.
5. Add it to the negative-case list below if it demonstrates a
   plausible-but-wrong workflow the suite must keep rejecting.

## CI

`python evals/run.py --mode fixture` requires no LLM account and is safe to
run on every PR. Grading and generated pipelines call only `LOAD spatial`;
they never execute `INSTALL` or silently fall back to a network download.
`python evals/prepare_spatial.py` is the sole explicit preparation path and
installs into `OPENMAPSTACK_SPATIAL_EXTENSION_DIR` when configured. CI builds
`evals/Dockerfile.offline`, prepares Spatial while the image is built, and
then runs the complete unit and fixture suite with `docker run --network none`.
Any hidden extension download or mutable service call therefore fails the
offline acceptance gate. Live agent cases (`--mode live`)
are intended for a separate manual/scheduled workflow with maintainer-owned
secrets; they must never block ordinary PRs when a third-party model or data
endpoint is unavailable. The scheduled workflow installs and verifies the
selected agent CLI before invoking the benchmark; its job remains non-blocking,
but the uploaded JSON preserves setup failures instead of treating them as
passes. The visual integration (`.github/workflows/eval-visual.yml`) is
likewise scheduled/manual only: it runs `--mode visual` inside the
`qgis/qgis:3.44-trixie` container with Playwright Chromium and uploads the
rendered snapshots as evidence.

## Result dimensions

`run.py --json` reports pass/fail per assertion and rolls dimensions up within
each score type: GIS correctness, project/reproducibility compliance,
provenance, override handling, validation integrity, presentation contract,
and rerun success. Setup failures are reported but excluded from pass-rate
denominators. There is intentionally no aggregate `16/16` score: a detected
mutation, a conforming fixture, and a successful agent trial answer different
questions.

CI also publishes `assertion-coverage.json` and `assertion-coverage.xml` from
branch coverage over the complete `evals/assertions/` package. The current
gate is 70%; the report includes PyQGIS integration paths that are intentionally
unavailable in the ordinary Python job, so the actual percentage remains
visible rather than omitting those modules. Direct tests cover every public
assertion, including positive/negative and unavailable-dependency behavior
where applicable.

## Canonical run hashes and evidence

Every run record contains explicit `inputs` and `outputs` inventories. Each
entry carries a project-relative `path` and the real file SHA-256. The
aggregate is recomputed over the inventory sorted by path; for every file the
digest consumes an eight-byte big-endian path length, the UTF-8 path, then the
file bytes. All source/override/implementation inputs and every declared
output must participate. Missing files, stale per-file hashes, omitted files,
malformed hashes, and mutually matching but invented aggregates fail.

Source baselines are runner-owned hard gates. When fixture or live inputs have
a pre-execution baseline, `run.py` injects byte-identity validation even if a
case author omitted it and requires the baseline to cover the complete
`data/source/` and `data/overrides/` trees.

`validation.report_evidence_recomputes` independently derives supported row,
geometry-validity, duplicate-ID, and null-ID counters from actual geodata.
`geodata.dataset_crs_is` reads real dataset CRS metadata rather than trusting
the manifest.
