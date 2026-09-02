# Maintainer architecture notes

This document records cross-cutting architecture that is easy to misread from one subsystem in isolation. It is deliberately shorter than the product/reference documentation and should not duplicate contracts owned elsewhere.

## Repository purpose and layers

OpenMapStack is a **managed agent-skill product plus its executable QA/support code**.

```text
Shipped guidance/product
  SKILL.md
  references/
  templates/
  examples/
  distribution compatibility (.claude-plugin/, agents/)
            |
            v
Produced openmapstack-project/v1 artifacts
            |
     +------+-------------------+
     |                          |
     v                          v
openmapstack package          evals/
validate / verify / rerun     fixture/live/visual harness
shared semantic checks <------+ 
     |
     v
user/project evidence
```

The distinction matters: `SKILL.md` is the product being developed and evaluated. It is not the maintainer bootstrap for this repository.

### Source-of-truth boundaries

- Agent-facing behavior: `SKILL.md` and the relevant file in `references/`.
- Project contract: `references/project-spec.md`; machine validation is also constrained by `openmapstack/schemas/project-v1.schema.json` and `openmapstack/validation.py`.
- Automatic `verify` plan and applicability: `docs/verify-applicability.md` plus `openmapstack/verify.py`.
- Eval semantics: `evals/README.md`, `evals/schemas/`, `evals/run.py`, case definitions, and tests.
- Roadmap/current work: GitHub issues. Do not mirror their checklists here.

When these disagree, resolve the inconsistency at the owning layer rather than adding another interpretation here.

## Three evidence questions must stay separate

The current architecture intentionally answers three different questions with related machinery but different denominators:

1. **Regression/contract CI** — can deterministic checks recognize compliant fixtures and isolated defects?
2. **Project QA** — does a particular produced project satisfy the checks that are applicable and executable in the current environment?
3. **Agent/model benchmark** — can an agent/model configuration produce a good project from a controlled task?

Issue #13 is the current roadmap owner for this separation. Do not collapse these into one quality score: a green fixture suite, a user project's `verify` report, and a live agent pass rate are different evidence.

## `validate` and `verify` intentionally do different work

`openmapstack validate` is the contract/bookkeeping layer. `openmapstack/validation.py` validates schema/metadata, interpretation, provenance, override declarations, processing/output declarations, presentation, runtime, validation declarations, and—when artifact checks are enabled—the produced files, validation report, and run record.

`openmapstack verify` is evidence-oriented project QA. It builds a plan from the manifest rather than letting the project opt out of checks, then invokes reusable checks against the actual artifacts. Examples include geometry validity, artifact CRS, provenance, validation/run-record consistency, presentation/QGIS consistency, attested expectations, and optionally a clean rerun.

Consequences:

- `validate --preflight` is not a substitute for full artifact health; it deliberately skips checks that require produced artifacts.
- `verify` does not claim to prove every project-specific analytical answer. It proves bounded structural/provenance/artifact/reproducibility predicates and exposes what it could not test.
- New user-facing correctness checks should normally be reusable from `openmapstack/checks/`, not hidden only inside eval scaffolding.

## Shared semantic check library

`openmapstack/checks/` is deliberately shipped with the package and shared by two callers:

- `evals/run.py`, for deterministic and live benchmark grading;
- `openmapstack verify`, for a user's own project.

Checks return `AssertionResult` with one of four states:

- `passed`
- `failed`
- `warning`
- `not_testable`

Expected inability to establish a predicate must not raise or silently pass. Missing dependencies, unsupported artifact formats, or absent addressing information normally become `not_testable` with a stable code where useful.

Most checks are oracle-free. The small set that requires a known project-specific answer is not automatically trusted on user data; it is reachable through `validation.expectations[]` and the attestation machinery in `openmapstack/expectations.py`.

## Attested project-specific expectations

A pipeline cannot certify its own golden answer. For known-answer checks, an expectation is executed only after independent evidence is bound to:

- the exact check and arguments (`expectation_sha256`);
- the current project input hash;
- reviewer/source metadata and evidence digest.

Unverified, incomplete, changed, or input-stale attestations are visible as warnings and their expected value is not executed as trusted evidence. The allowlist is intentionally narrow.

This is a trust boundary: do not expand the expectation surface by exposing arbitrary check functions or executable arguments without equivalent safety and provenance constraints.

## Clean rerun is a project-state boundary

`openmapstack/rerun.py` owns the clean-rerun protocol used by both `verify --rerun` and evals. There must not be a second eval-only implementation.

The rerun workspace preserves only the project manifest, conventional immutable inputs (`data/source/`, `data/overrides/`), the canonical implementation, and explicitly declared dependencies. It then:

1. rejects unsafe/escaping paths and symlinked preserved dependency trees;
2. resolves one canonical command/pipeline;
3. strips `PYTHONPATH` and environment variables whose names match the current conversation/provider blacklist (`ANTHROPIC`, `CHAT`, `CLAUDE`, `CODEX`, `CONVERSATION`, `OPENAI`, `PROMPT`, `TRANSCRIPT`);
4. executes without a shell;
5. verifies immutable-source hashes did not change;
6. runs full artifact validation on the rebuilt project;
7. records machine-readable rerun evidence.

This environment cleanup is deliberately **not** a general sandbox or allowlist today. Variables such as `GEMINI_API_KEY`, `GOOGLE_API_KEY`, and arbitrary service/configuration variables can still survive unless separately removed. Therefore a clean rerun proves independence from the excluded workspace artifacts and the known blacklisted session/provider variables; it does not yet prove independence from all undeclared ambient environment state. A future allowlisted environment would provide the stronger guarantee.

The eval harness additionally forbids reaching back into eval reference generators. That restriction is supplied by the eval caller; the shipped package intentionally does not know that `evals/` exists.

## Connectors are a trust boundary, like attestations

`openmapstack/connectors/` reads user warehouse data on the user's behalf and is held to four rules that must survive any refactor: credentials are resolved from a reference and never recorded; sessions are read-only with a statement timeout; only a single `SELECT` reaches the backend; and nothing is materialised under `data/source/` without an explicit approval flag and within declared row/byte limits. Every message the package emits passes through `openmapstack.sources.redact`.

The DuckDB local connector confines file access to its root (`allowed_directories` + `enable_external_access = false`) and exposes files as views so queries never spell paths. PostGIS has no durable time travel, so its pin is always a local snapshot; the transaction snapshot id is retrieval metadata, not a pin. Unverified backends are refused (`backend_unsupported`) rather than approximated.

Pin classes live in `openmapstack/sources.py` and are shared by `validate` (`source.pin`, `source.credentials`) and `verify` (`provenance.every_source_pinned`, `provenance.no_inline_credentials`). Do not add a third interpretation.

## Metamorphic relations execute the project's own pipeline

`openmapstack/metamorphic.py` reuses the clean-rerun workspace preparation (`openmapstack/rerun.py`), perturbs only the copy, and compares against the produced outputs. A relation is valid only under its declared preconditions; unmet data preconditions are `not_testable`, invalid declarations fail, and unknown relation names are rejected rather than skipped. `runtime.implementation.parameters` (`openmapstack/parameters.py`) is the only sanctioned way to vary a pipeline setting from outside. Keep `metamorphic_evidence` a separate eval dimension from `gis_correctness`: a relation that holds is self-consistency, not a correct answer.

## Integrity and path safety

Project-relative paths are resolved through `openmapstack/project.py` helpers and must remain under the project root. Code that adds new file addressing should reuse the same safety model rather than joining unchecked user paths.

Run/input integrity uses path-aware SHA-256 inventories (`openmapstack/integrity.py`): the relative file name participates in the canonical set hash, not only concatenated bytes. Immutable input inventory includes source/override files plus canonical implementation/dependencies; the manifest itself is excluded from the input hash because it records the resulting digest.

Be careful with resolved vs unresolved roots: temporary/macOS paths can include symlinked prefixes (`/var` vs `/private/var`). Existing helpers resolve roots before `relative_to()` comparisons for this reason.

## Eval architecture

`evals/run.py` has three execution modes and four score types:

- `fixture` -> deterministic `contract_ci` or `mutation_tests`;
- `live` -> `agent_benchmark` through an explicit adapter;
- `visual` -> `integration_visual` for positive cases or `mutation_tests` for visual mutations.

Important invariants:

- fixture mode is the safe default and does not call an agent;
- malformed configuration, zero executed cases, generator/adapter/runtime failure are **setup failures** (exit 2), not scored failures;
- mutation cases require a healthy control and an isolated target failure; unrelated breakage must not satisfy the mutation;
- live results normalize provider-specific execution into `openmapstack-agent-run/v1`; raw vendor events remain audit evidence rather than scoring semantics;
- final assistant prose is retained for audit but correctness comes from produced artifacts and deterministic checks.

The live adapters (`claude_code`, `codex`, `openai_compatible`) are compatibility surfaces. Their model IDs, environment variables, event formats, and quirks must not leak into shared project/check schemas.

## CI/runtime split

The repository deliberately uses different environments for different evidence:

- `.github/workflows/evals.yml` — ordinary PR/push deterministic unit + fixture CI; its unit-test stage installs Playwright/Chromium, and the same workflow later runs a separate Docker fixture gate with runtime networking disabled;
- `.github/workflows/eval-benchmark.yml` — scheduled/manual live agent benchmark, non-blocking for ordinary PRs;
- `.github/workflows/eval-visual.yml` — scheduled/manual QGIS + browser integration in a QGIS container;
- `.github/workflows/plugin.yml` — Claude Code plugin-manifest compatibility only; it is not the generic project CI contract.

Do not conflate ordinary fixture CI with the narrower offline Docker gate. Do not make deterministic project/eval correctness depend on live model credentials or mutable external services merely to increase apparent coverage.

## QGIS and web presentation are views, not analytical state

The canonical analysis remains the project manifest/pipeline/artifacts. QGIS and web products are views over the same declared data and presentation semantics.

Two established QGIS traps have dedicated checks because a project may load successfully while displaying a confidently wrong map:

- hand-written QGIS XML must declare each layer's CRS, including Web-Mercator basemaps;
- web map layer order and QGIS layer-tree paint order are opposite, so naïvely copying manifest order can hide layers.

Runtime/rendered validation supplements structural validation. It does not turn visual plausibility into analytical correctness.

## Active architecture seams

These are intentionally linked rather than restated as roadmap:

- issue #13 — project QA, metamorphic checks, source pinning, and OpenMapBench interoperability;
- issue #11 — optional QGIS generation/edit round-trip evolution;
- issue #6 — standard dashboard renderer and presentation-to-render validation;
- `evals/COVERAGE.md` — explicit current GIS coverage gaps.

When one of these changes a durable boundary above, update this document in the same PR.