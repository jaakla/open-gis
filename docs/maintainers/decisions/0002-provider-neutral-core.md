# 0002 — Keep the core provider-neutral and isolate agent integrations

- Status: Accepted
- Date: 2026-09-02
- Related: `AGENTS.md`; `evals/adapters/`; `agents/`; `.claude-plugin/`

## Context

OpenMapStack is intended to be consumed and maintained across multiple agents and providers. At the same time, real integrations need provider-specific CLIs, protocols, model identifiers, credentials, event formats, and packaging metadata.

Allowing one provider's concepts to become the generic project/eval architecture would make the skill less portable and cause shared schemas to churn with adapter details.

## Decision

Keep shared skill instructions, project contracts, semantic checks, evidence schemas, and maintainer guidance provider-neutral.

Provider/tool-specific behavior belongs behind explicit compatibility surfaces such as:

- `evals/adapters/` for live execution;
- `.claude-plugin/` for Claude Code distribution compatibility;
- `agents/` for installer/agent metadata;
- provider-specific CI workflow legs or secrets.

Normalize live execution into vendor-neutral evidence (`openmapstack-agent-run/v1`) before the eval runner reasons about it. Raw provider events may be retained for audit/diagnostics but are not shared scoring semantics.

## Consequences

- Adding a new coding agent should normally require an adapter/compatibility layer, not changes to `openmapstack-project/v1`.
- Model IDs, provider environment variables, and protocol quirks must not leak into generic project/check schemas.
- Root `AGENTS.md` is the canonical maintainer bootstrap; tool-specific instruction files should remain thin compatibility shims.
- Provider-specific support may legitimately exist and be tested without making that provider the default architecture.

## Alternatives considered

### Standardize on Claude Code because it has rich project memory/plugin support

Rejected because repository knowledge and contracts would become inaccessible or second-class in Pi, Codex, Copilot, Gemini, and other agents.

### Hide provider differences entirely behind an OpenAI-compatible API

Rejected because CLI agents expose materially different tool loops, permissions, event evidence, and execution semantics. Adapter isolation is more honest than pretending the protocols are identical.