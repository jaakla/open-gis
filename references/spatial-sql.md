# Spatial SQL

Use this reference when writing or reviewing spatial SQL for DuckDB Spatial, PostGIS, BigQuery GIS, Snowflake geospatial, Sedona, or similar engines. Keep the workflow tool-neutral: discover the schema, bound the query, validate in SQL, then use a rendered map only after the data checks pass.

## Required workflow

1. **Discover schema before writing SQL.** Inspect available databases/schemas/tables, geometry columns, data types, geometry type, SRID/CRS, row counts, and candidate tables. When multiple tables could answer the question, sample each candidate and prefer the source with richer, cleaner attributes.
2. **Resolve the target area.** For named places, query the actual boundary source first: Overture `division_area`, an admin-boundary table, a user-supplied polygon, or another authoritative boundary. Return candidate rows and choose by subtype/class/admin level, not memory. Preserve bbox constants at full precision.
3. **Draft a bounded query.** Select only needed columns, add attribute/date filters early, add `LIMIT` for exploration, and alias map output geometry as lowercase `geometry` when the consuming map stack expects that convention.
4. **Use bbox overlap plus an exact predicate.** Bbox is a scan gate, not the geography. Use overlap logic so features crossing the target boundary are included, then apply `ST_Intersects`, `ST_Within`, `ST_Covers`, or the exact predicate the analysis requires.
5. **Validate before presenting.** Run cost/dry-run checks where available, row-count checks, geometry validity checks, area/length sanity checks, and query-plan checks for expensive queries.
6. **Review the map only after SQL validation.** Render a map or snapshot when visual output is part of the task, inspect it, then claim visual findings. Do not describe map-visible patterns from table rows alone.

## Bbox pattern

Prefer overlap, not containment:

```sql
-- Correct: feature bbox overlaps the area bbox.
AND feature_xmax >= area_xmin
AND feature_xmin <= area_xmax
AND feature_ymax >= area_ymin
AND feature_ymin <= area_ymax

-- Then apply exact geography.
AND ST_Intersects(feature_geometry, area_geometry)
```

Avoid containment gates for area queries:

```sql
-- Wrong for features that can cross the area edge.
AND feature_xmin >= area_xmin
AND feature_xmax <= area_xmax
```

Containment is only appropriate when the question explicitly asks for features fully inside the bbox, and even then prefer an exact predicate such as `ST_Within` against the real boundary.

## Validation gates

Run the checks that fit the engine and task:

* **Cost / dry run:** BigQuery dry-run bytes, Snowflake warehouse expectations, or cloud engine explain/cost tools. If over budget, tighten bbox, filters, date ranges, columns, or aggregation resolution before executing.
* **Row count:** Run `COUNT(*)` on the filtered result. Zero rows require debugging before presenting: check bbox direction, CRS, target-area match, exact predicate, attribute filters, and column names.
* **Geometry validity:** Use `ST_IsValid`, `ST_IsValidReason`, or engine equivalents after imports, transforms, overlays, dissolves, or simplification.
* **Area / length magnitude:** For polygon outputs, compute total area. For line outputs, compute total length. Report units honestly: geography functions return meters in many engines; planar geometry returns CRS units.
* **Query plan:** Use `EXPLAIN` / `EXPLAIN ANALYZE` for recurring or slow queries. Confirm spatial indexes, bbox pruning, partition pruning, or file pruning are active.
* **Preview rows:** Inspect a small bounded sample for IDs, names, geometry presence, nulls, and surprising categories before exporting or mapping.

## Engine notes

### PostGIS

Discover spatial tables and SRIDs before querying:

```sql
SELECT f_table_schema, f_table_name, f_geometry_column, type, srid
FROM geometry_columns
ORDER BY f_table_schema, f_table_name;
```

Use `&&` as the index-backed bbox gate, then the exact predicate:

```sql
WITH area AS (
  SELECT geom
  FROM admin_boundaries
  WHERE name ILIKE '%Berlin%'
  LIMIT 1
)
SELECT s.id, s.geom AS geometry
FROM segments s
CROSS JOIN area a
WHERE s.geom && a.geom
  AND ST_Intersects(s.geom, a.geom);
```

For meter distances on lon/lat data, use a suitable projected CRS or `geography`. Create GIST indexes on production geometry columns and verify with `EXPLAIN ANALYZE`.

### DuckDB Spatial

For Overture and other GeoParquet datasets with a `bbox` struct, use bbox overlap for predicate pushdown:

```sql
WHERE bbox.xmax >= 24.5
  AND bbox.xmin <= 25.0
  AND bbox.ymax >= 59.3
  AND bbox.ymin <= 59.5
```

When a real boundary is available, combine the bbox gate with `ST_Intersects`. Keep CRS labels consistent (`EPSG:4326` vs `OGC:CRS84`) before joining geometries.

### BigQuery GIS

Use `INFORMATION_SCHEMA` to confirm table and column names. For Overture public data, resolve named areas through `division_area`, copy the full-precision bbox values into the query, and use both bbox overlap and `ST_INTERSECTS`.

Always dry run and cap bytes before execution:

```bash
bq query --use_legacy_sql=false --dry_run --format=json --maximum_bytes_billed=10737418240 'SELECT ...'
```

If estimated bytes exceed budget, rewrite cheaper before running. Use `ST_AREA` and `ST_LENGTH` for geography magnitude checks.

### Snowflake

Confirm Overture Marketplace shares or the relevant database are installed before writing analysis SQL:

```sql
SHOW DATABASES LIKE 'OVERTURE_MAPS__%';
```

Snowflake Overture bbox fields are usually semi-structured values, so cast explicitly:

```sql
AND s.bbox:xmax::float >= 13.08834457397461
AND s.bbox:xmin::float <= 13.761162757873535
AND s.bbox:ymax::float >= 52.33823776245117
AND s.bbox:ymin::float <= 52.67551040649414
AND ST_INTERSECTS(s.geometry, a.geometry)
```

Validate with `COUNT(*)` and bounded previews before exporting. Keep extraction limits explicit unless the user asks for a full export.

### Sedona / Wherobots-style SQL

Do not rely on PostGIS `&&`; use explicit bbox overlap fields when available, then `ST_Intersects`:

```sql
AND s.bbox.xmax >= area_xmin
AND s.bbox.xmin <= area_xmax
AND s.bbox.ymax >= area_ymin
AND s.bbox.ymin <= area_ymax
AND ST_Intersects(s.geometry, a.geometry)
```

Prefer metadata queries that return rows over UI-only `SHOW` output when running through SQL connectors. Confirm geometry column names and CRS before drafting the final query.

## Map / snapshot review

A map is a validation surface, not a substitute for SQL checks.

* Render a map, screenshot, notebook map, or browser view before making visual claims.
* Check that the output is in the expected area, uses the expected geometry type, and is not blank, shifted, inverted, over-zoomed, or missing obvious coverage.
* Look for duplicates, boundary clipping errors, bbox-only rectangular overshoot, wrong CRS, and swapped latitude/longitude.
* Lock the initial view to the scale where the insight is readable.
* If the rendered view contradicts row counts or area/length checks, debug the SQL first.
