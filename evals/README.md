# Open-GIS eval suite

Executable evals that check whether an agent (or a hand-authored reference
project) actually follows the `open-gis-project/v1` contract in
`references/project-spec.md` — not whether it *says* the right things.

> A model/agent should not pass because it says the right things. It passes
> when the produced project can be inspected, executed and independently
> checked.

## Execution modes and score types

Execution mode describes how a project is produced. `fixture` runs committed,
deterministic reference projects with no network or LLM calls. `live` starts in
an empty workspace, copies only explicitly declared input fixtures, invokes an
agent, and grades its output with the same semantic assertions.

```bash
python evals/run.py --mode fixture
```

Score type describes what the result means. The v2 result schema keeps four
denominators separate:

- `contract_ci`: positive reference-project conformance;
- `mutation_tests`: successful detection of deliberately defective projects;
- `agent_benchmark`: live agent task success;
- `integration_visual`: rendered QGIS/browser integration checks.

Cases 001–006 support both fixture and live execution. Cases 901–910 are
fixture-only mutations. Mutation detection is never included in contract or
agent pass rates.

```bash
python evals/run.py --mode live --case 001-basic-spatial-analysis --agent claude_code
```

The runner returns setup exit code `2` if no cases support the selected mode,
so an unsupported selection cannot publish a green `0/0` benchmark.

## Running

```bash
python evals/run.py                        # every case, fixture mode
python evals/run.py --case attribute-override
python evals/run.py --mode fixture
python evals/run.py --json eval-results.json
python evals/run.py --mode live --agent codex --model <model> --timeout 1200
python evals/run.py --repetitions 3 --seed 100
python evals/run.py --list
```

Fixture mode is the default, so an invocation without `--mode` never calls an
agent. `--timeout` applies to each generator or agent invocation;
`--repetitions` runs each selected case multiple times; and `--seed` records a
base seed that is incremented for each repetition.

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
4. Require every configured subprocess or agent invocation to succeed. A
   failure produces `setup_failed` and assertions are not scored.
5. Run every assertion listed in the case's `expected.yaml` against the
   resulting workspace, using the reusable checks in `evals/assertions/`.
6. Write a machine-readable v2 result with case type, execution mode, score
   type, trial, pass/fail per assertion, complete subprocess/agent diagnostics,
   run configuration, and environment metadata. Deleted temporary workspace
   paths are not advertised as retained artifacts.
7. Return `1` for required assertion failures or `2` for setup failures.

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
and writes `.open-gis-clean-rerun.json` as evidence. Project-caused rerun
failures are graded by `rerun.clean_execution_succeeded`; they are not
misclassified as eval setup failures.

`rerun.outputs_semantically_equal` ignores row/feature order and normalizes
GeoJSON geometry representation. Validation evidence ignores standard run IDs,
hashes, and timestamps while still requiring identical check results and
evidence. Additional nondeterministic output fields must be explicitly listed
with `ignored_fields`; they are never silently discarded.

## Directory layout

```text
evals/
├── README.md
├── run.py                  # CLI runner
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
  generator: "python3 {evals_dir}/fixtures/reference_pipeline/gen.py {project_dir}"
  # Cases that test reproducibility opt in explicitly:
  # clean_rerun: {}

live:
  agent: claude_code          # optional; --agent overrides it
  prompt_file: prompt.md
  agent_workdir: project
  fixtures:
    - source: ../../fixtures/mini-tartu/pois.geojson
      destination: project/data/source/pois.geojson

assertions:
  - assert: project.schema_is
    args: { schema: open-gis-project/v1 }
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

`python evals/run.py --mode fixture` runs with no network access and no LLM
account — it is safe to run on every PR. Live agent cases (`--mode live`)
are intended for a separate manual/scheduled workflow with maintainer-owned
secrets; they must never block ordinary PRs when a third-party model or data
endpoint is unavailable. The scheduled workflow installs and verifies the
selected agent CLI before invoking the benchmark; its job remains non-blocking,
but the uploaded JSON preserves setup failures instead of treating them as
passes.

## Result dimensions

`run.py --json` reports pass/fail per assertion and rolls dimensions up within
each score type: GIS correctness, project/reproducibility compliance,
provenance, override handling, validation integrity, presentation contract,
and rerun success. Setup failures are reported but excluded from pass-rate
denominators. There is intentionally no aggregate `16/16` score: a detected
mutation, a conforming fixture, and a successful agent trial answer different
questions.
