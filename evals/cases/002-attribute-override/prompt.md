# Case 002 — stale attribute requiring a project override

The source POI dataset (`data/source/pois.geojson`) lists `poi-7` ("Test
Kiosk") with `status: active`. The user provides field-survey evidence that
this kiosk closed on 2026-08-20.

Task: do not mutate the source file. Record a `modify_attribute` override
in `project.yaml` that verifies the asserted prior value (`active`) against
the immutable source before applying `status: closed`, with non-placeholder
evidence, author, and timestamp. The effective/exported POI dataset must
reflect `status: closed` for `poi-7`. The run report must record the
override as `applied`.
