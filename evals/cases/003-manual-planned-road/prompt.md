# Assess access with a planned connector road

A municipal masterplan describes a proposed connector road that is not present
in the authoritative road layer. Use the supplied geometry in
`data/overrides/planned-road.geojson`; do not invent replacement coordinates.
The project also includes a field-survey correction to a POI that should be
recorded alongside the road scenario.

Record the road as a scenario addition with its origin, evidence, and rationale
and keep it visually and semantically distinct from authoritative roads. Apply
the POI correction as a separate documented override. Recompute the
accessibility analysis using the combined official and scenario road network.
