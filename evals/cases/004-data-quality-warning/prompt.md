# Case 004 — incomplete/uncertain source

The POI source metadata does not publish a completeness baseline (no
matched/returned count). Task: do not silently claim completeness. Create a
`poi_completeness` warning check, add a project-level warning describing the
uncertainty and mitigation, and make sure the overall project/run status
becomes `warning` (never `validated`). The warning must be surfaced in the
presentation semantics (a `warnings_panel`/`warnings` section), not only in
prose.
