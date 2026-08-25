# Case 001 — basic multi-stage spatial analysis

You are given three small local GeoJSON layers under `data/source/` (EPSG:3301):
`parcels.geojson`, `roads.geojson`, `pois.geojson`.

Task: identify parcels that are (a) at least 8,000 m² in area, (b) zoned
`ARIMAA`, `MAATULUNDUSMAA`, or `TOOTMISMAA`, and (c) within 2,000 m (planar
distance, measured in EPSG:3301) of the main road in `roads.geojson`.

Compile this as an `open-gis-project/v1` project: `project.yaml`,
`pipeline.py`, deterministic ordered `processing.steps` with explicit CRS,
a machine-readable `validation/latest-report.json`, and a run record under
`runs/`. Do not perform the distance/area calculation in EPSG:4326.
