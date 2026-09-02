# Architecture decision records

Use this directory for consequential choices whose rationale or rejected alternatives are likely to matter after the implementation details change.

Do **not** create an ADR for routine refactors, file moves, dependency bumps, or choices already obvious from a narrow owning contract.

## Status values

- `Accepted` — current architecture.
- `Superseded` — retained for rationale/history, with a link to the replacing ADR.
- `Proposed` — under active review; do not describe it elsewhere as current architecture.

## Template

```markdown
# NNNN — Short decision title

- Status: Accepted | Proposed | Superseded
- Date: YYYY-MM-DD
- Related: issue/PR/commit links or repository paths

## Context

What problem or competing constraints made a decision necessary?

## Decision

What boundary/approach is chosen?

## Consequences

What becomes easier/harder? What invariants must future changes preserve?

## Alternatives considered

Only alternatives that were genuinely plausible and whose rejection is useful to remember.
```

When superseding a decision, update both records. The current code/tests remain authoritative for implementation details.