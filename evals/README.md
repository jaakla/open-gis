# Open-GIS eval suite

Executable evals that check whether an agent (or a hand-authored reference
project) actually follows the `open-gis-project/v1` contract in
`references/project-spec.md` — not whether it *says* the right things.

> A model/agent should not pass because it says the right things. It passes
> when the produced project can be inspected, executed and independently
> checked.

## Two eval classes

**Fixture evals (required CI)** — `evals/cases/*` with `mode: fixture`.
Deterministic, local geodata, no network, no LLM calls. Run on every PR:

```bash
python evals/run.py --mode fixture
```

**Live evals (manual/nightly)** — `mode: live`. Invoke an agent adapter
(Claude Code, Codex, …) against a prompt, then run the same assertion
library against whatever project the agent produced. These call out to
real network/LLM resources and must never gate ordinary PR CI.

```bash
python evals/run.py --mode live --case 001-basic-spatial-analysis --agent claude_code
```

The runner returns setup exit code `2` if no live cases are selected. Until
cases explicitly declare `mode: live`, the command above therefore fails
honestly instead of publishing a green `0/0` benchmark.

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
2. Copy the case's `project/` directory (a committed reference project, or
   the fixture the agent should be pointed at) into the workspace.
3. If the case declares a `generator`, run it (e.g. re-run `pipeline.py`)
   inside the workspace; live-mode cases instead invoke an `AgentAdapter`
   with `prompt.md` and a fixture path, then evaluate whatever project the
   agent wrote into the workspace.
4. Require every configured subprocess or agent invocation to succeed. A
   failure produces `setup_failed` and assertions are not scored.
5. Run every assertion listed in the case's `expected.yaml` against the
   resulting workspace, using the reusable checks in `evals/assertions/`.
6. Write a machine-readable aggregate result with pass/fail per assertion,
   complete subprocess/agent diagnostics, duration, run configuration, and
   environment metadata. Deleted temporary workspace paths are not advertised
   as retained artifacts.
7. Return `1` for required assertion failures or `2` for setup failures.

Assertions read real files: `project.yaml`, `validation/latest-report.json`,
`runs/*.json`, and the actual geodata outputs (via DuckDB Spatial). They do
not pattern-match assistant prose.

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
mode: fixture                 # fixture | live
project_dir: project          # relative to the case directory
hard_gate: true                # non-zero exit if any assertion here fails

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

Case definitions are validated before execution. Unknown modes, agents, or
assertions; invalid expectation states; unsafe project-directory paths; and
malformed generator declarations are setup errors rather than benchmark
results.

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

`run.py --json` reports pass/fail per assertion *and* rolls results up into
seven separate dimensions instead of one aggregate score: GIS correctness,
project/reproducibility compliance, provenance, override handling,
validation integrity, presentation contract, and rerun success. A visually
polished project with wrong provenance is visibly different from a fully
compliant one.
