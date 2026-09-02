# 0003 — Use one clean-rerun protocol for evals and user verification

- Status: Accepted
- Date: 2026-09-02
- Related: `openmapstack/rerun.py`; `openmapstack/verify.py`; `evals/run.py`; issue #13

## Context

Reproducibility is only meaningful if the project can rebuild itself from declared project state rather than from the original chat, eval generator, cached outputs, or undeclared workstation state.

Originally this logic lived close to eval machinery. `openmapstack verify --rerun` needs the same guarantee for user projects. Maintaining two similar rerun implementations would allow benchmark evidence and user QA to drift apart.

## Decision

`openmapstack/rerun.py` owns the clean-rerun protocol. Both user verification and evals call it.

The shared protocol preserves only declared project state, strips conversational/provider ambient state, protects immutable inputs, runs the one canonical implementation, and validates the rebuilt artifacts. Eval-specific exclusions (for example, forbidding the reference generator) are injected by the eval caller rather than hardcoded into the shipped package.

## Consequences

- A reproducibility fix in the shared protocol benefits both project QA and evals.
- `openmapstack/` remains independent of the repository's eval fixture layout.
- A project that only works because undeclared local/session state is present should fail the clean rerun instead of having more ambient state copied into the sandbox.
- Future parameterized/metamorphic execution should extend the same declared-state model rather than create another hidden execution path.

## Alternatives considered

### Keep an eval-specific clean-rerun implementation

Rejected because it would make the benchmark's definition of reproducibility differ from the user's verifier and would inevitably drift.

### Copy the whole original project workspace and delete obvious outputs

Rejected because undeclared caches, config, transcripts, generated helpers, or secrets could silently survive and make a non-reproducible project appear reproducible.