# Case 006 — presentation consistency

Two different prompts against the mini-Tartu fixture should produce
different analyses (case 001's plain accessibility screen, and case 003's
scenario-road variant) but the same Open-GIS UX semantics: stable
analytical workspace layout class, layer-group taxonomy, semantic roles,
provenance section, validation/warning representation, and editing
capability declarations. Do not require byte-identical HTML or exact
colors — only stable structural/semantic conventions.

Create two complete projects: the plain analysis under `project_a/` and the
scenario-road variant under `project_b/`. Their source fixtures are already
placed under each project's `data/source/`; the supplied planned road for
`project_b` is under `data/overrides/planned-road.geojson`.
