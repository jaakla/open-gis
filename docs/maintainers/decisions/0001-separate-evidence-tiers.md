# 0001 — Keep regression CI, project QA, and agent benchmarks separate

- Status: Accepted
- Date: 2026-09-02
- Related: issue #13; `openmapstack/verify.py`; `evals/run.py`

## Context

The repository uses many of the same semantic checks in three settings, but a green result means something different in each:

- deterministic fixtures/mutations test the contract and checkers;
- `openmapstack verify` evaluates one produced project and exposes applicability limits;
- live benchmarks evaluate an agent/model configuration over controlled tasks.

Combining these into one pass rate or score would create false equivalence between checker regression evidence, project evidence, and model performance.

## Decision

Maintain separate evidence tiers and denominators. Share checker implementations where their predicates are genuinely common, but keep execution semantics, setup failures, applicability, and reporting appropriate to the question being answered.

Fixture conformance, mutation detection, project verification, visual integration, and live agent success must not be collapsed into one generic quality number.

## Consequences

- `evals/run.py` keeps distinct score types (`contract_ci`, `mutation_tests`, `agent_benchmark`, `integration_visual`).
- `verify` reports project-specific applicability/execution coverage rather than benchmark success rates.
- Setup failures stay outside scored benchmark denominators.
- Public benchmark orchestration can evolve separately from the shipped user-project verifier while reusing stable checks.

This produces more report surfaces, but each number has a defensible meaning.

## Alternatives considered

### One unified OpenMapStack score

Rejected because a score that mixes mutation detection, fixture correctness, unavailable project checks, and stochastic agent trials has no stable interpretation.

### Separate checker implementations for each tier

Rejected because the same semantic predicate would drift between CI, user QA, and benchmark grading. Shared checks are preferable where the predicate is actually identical.