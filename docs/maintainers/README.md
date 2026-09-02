# Maintainer knowledge

This directory holds durable, cross-agent knowledge for people and coding agents maintaining the OpenMapStack codebase.

It is **not** part of the shipped geospatial skill. `SKILL.md`, `references/`, and `templates/` describe the product consumed by agents in other projects; this directory describes how this repository is engineered, why important boundaries exist, and which hard-earned maintenance facts should not need to be rediscovered in every fresh session.

## What belongs here

Promote a discovery here when all of these are true:

1. it is non-obvious from the owning code or tests;
2. it is likely to matter in a future issue or fresh agent session;
3. rediscovering it would be materially expensive or error-prone; and
4. it can be stated without duplicating a more authoritative contract.

Prefer the narrowest durable home:

- `architecture.md` — subsystem boundaries, evidence flows, invariants, and ownership seams;
- `debugging.md` — recurring failure modes, environment traps, and diagnostic routes;
- `decisions/` — significant choices where the rationale and rejected alternatives matter later;
- owning code/tests/evals — executable behavior and guarantees;
- root `AGENTS.md` — repository-wide workflow/routing rules that should be seen on every cold start.

Do not use this directory as a diary, issue backlog, generated code map, or substitute for tests. File locations, function inventories, one-off failures, model-specific observations, and facts cheap to rediscover with `rg`, tests, or Git normally do not belong here.

## Source-of-truth rule

These notes are navigation and rationale, not a competing specification. When a statement here conflicts with an executable contract or owning document, fix this note rather than treating it as authoritative.

Important existing owners include:

- shipped skill behavior: `SKILL.md` and `references/`;
- `openmapstack-project/v1`: `references/project-spec.md` plus `openmapstack/schemas/project-v1.schema.json` and validators;
- automatic verification applicability: `docs/verify-applicability.md`;
- eval execution/scoring: `evals/README.md`, eval schemas, runner, cases, and tests;
- current planned work: GitHub issues, especially the active epics rather than copied roadmap prose here.

## Keeping the area useful

- Date temporary/current-state notes and link the issue that will make them obsolete.
- Remove resolved debugging notes once the underlying trap is fully encoded by tests and no longer needs human context.
- Prefer links to exact owning files/issues over pasted copies of their content.
- Record *why* a boundary exists, not merely *what files exist*.
- When a real-world or live-agent failure escapes the current checks, first preserve the failure as a regression test/eval where practical; the prose note is secondary.

See `AGENTS.md` in this directory for instructions that apply when editing these knowledge files.