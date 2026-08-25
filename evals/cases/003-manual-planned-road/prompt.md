# Case 003 — planned road absent from source geodata

A municipal masterplan describes a proposed connector road that does not
exist in `data/source/roads.geojson`. The user supplies a small GeoJSON
(`data/overrides/planned-road.geojson` — copy it in, do not invent
coordinates) representing the connector.

Task: record it as an `add_feature` override with `geometry_origin:
scenario`, origin/evidence/rationale, and keep it visually/semantically
distinct from authoritative roads in the presentation layer. Recompute the
accessibility analysis using the combined official + scenario road network
where the user asks for it.
