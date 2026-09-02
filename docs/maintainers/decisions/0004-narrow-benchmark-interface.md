# 0004 — A narrow versioned check API instead of an exported benchmark harness

- Status: Accepted
- Date: 2026-09-02
- Related: issue #13 (C1–C3); `openmapstack/api.py`; `openmapstack/snapshot.py`; `docs/openmapbench-interop.md`; `evals/run.py`

## Context

OpenMapBench needs to grade produced projects with the same checks this repository uses, record which skill snapshot and model configuration produced each result, and compare a plain arm with a skill arm. Two easy paths were available: let OpenMapBench vendor `openmapstack/checks/`, or export `evals/run.py` as the benchmark harness. Both couple the user-facing package to model-provider orchestration and let the two check libraries drift.

## Decision

Publish a small, versioned interface and nothing more:

- `openmapstack-check-api/v1` (`api_info`, `negotiate`, `list_checks`, `run_check`, `verify --json`) with packaged JSON schemas for results;
- `openmapstack-skill-snapshot/v1` for the controlled copy of the shipped skill;
- `openmapstack-benchmark-arm/v1` as the provenance tuple a published arm must record;
- `openmapstack-benchmark-task/v1` bundles exported from the eval cases.

Reporting dimensions are owned by `openmapstack.api.DIMENSIONS` and imported by the eval runner. Agent adapters, leaderboard policy, and run isolation remain in OpenMapBench.

## Consequences

- OpenMapBench upgrades by pinning a check API major and a minimum package version; `negotiate()` refuses to grade with an unknown API rather than grading wrongly.
- New checks are additive; renaming or removing one is a major bump with a migration note.
- Setup failures (`check_error`, adapter failure, missing CLI) stay outside scored denominators on both sides.
- Paired `plain`/`oms` runs in this repository exist to prove task parity and provenance recording; the public comparison is OpenMapBench's.

## Alternatives considered

### Vendor the check library into OpenMapBench

Rejected: two copies of the same predicate drift, and a benchmark grading with a stale copy silently rewards a project the shipped verifier would fail.

### Export `evals/run.py` as the benchmark harness

Rejected: the runner's adapters, credential handling, and artifact layout are provider-specific compatibility surfaces. Making them the generic architecture would leak vendor terminology into shared interfaces and tie package releases to harness releases.
