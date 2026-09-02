# Maintainer knowledge editing rules

These instructions apply when adding or changing files under `docs/maintainers/`.

The goal is to preserve durable cross-agent knowledge without creating a second, stale specification of the repository.

## Before adding a note

Ask:

- Is this fact non-obvious from code/tests and expensive to rediscover?
- Will it plausibly matter after the current issue/session ends?
- Is there already a more authoritative place for it?
- Can the durable lesson be encoded as a test/eval instead of prose?

If the answer points to code, a test, an eval, an issue, `SKILL.md`, `references/`, or `docs/verify-applicability.md`, update that owner first. Add a maintainer note only for context, rationale, cross-cutting navigation, or a recurring trap that the owner cannot express well by itself.

## Writing rules

- Keep notes provider- and coding-agent-neutral unless documenting an explicit adapter/integration surface.
- Link to owning files and GitHub issues instead of copying long specifications or roadmaps.
- Separate accepted architecture from temporary known debt.
- Date temporary sections and state the issue/condition that should remove them.
- Do not record secrets, local absolute paths, credentials, private session URLs, model transcripts, or ephemeral benchmark output.
- Do not add generated inventories of functions/files; agents can inspect the repository.
- Prefer a small number of maintained documents over one file per incident.

## Promotion from tool-specific memory

Claude/Gemini/Pi/Codex/Copilot session or auto-memory may contain useful discoveries, but it is only a convenience cache. Promote a discovery when it meets the durability test above:

- workflow/routing rule -> root `AGENTS.md`;
- cross-cutting architecture/invariant -> `architecture.md`;
- recurring diagnostic trap -> `debugging.md`;
- consequential choice with alternatives/rationale -> `decisions/`;
- behavioral guarantee -> owning code plus tests/evals.

Never make a tool-specific memory the sole record of a project invariant.

## Review discipline

When modifying these files, check that referenced code/issue state is still current. Delete or rewrite stale notes rather than accumulating historical layers. Significant architectural changes should update the relevant note in the same PR.