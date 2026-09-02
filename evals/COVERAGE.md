# Eval coverage matrix

What the suite actually covers, per the dimensions the epic (issue #7, PR 8)
asks to track. "Positive" = a compliant reference project graded with
deterministic assertions; "Mutation" = a single injected defect that must be
detected with a pinned failure code against a healthy control twin.

Legend: ✅ covered · ⚠️ partially covered · ❌ not covered (tracked below)

## Geometry

| Failure mode | Positive | Mutation |
|---|---|---|
| Polygon selection (regular parcels) | 001, 009, 010, 014 | — |
| Polygon vs multipolygon | ⚠️ donut hole handling in 010 (Polygon with interior ring); true MultiPolygon inputs not yet exercised | — |
| Polygon hole incorrectly filled | 010 (hole-aware area 15 000 m²) | 917 `hole-filled` |
| Boundary semantics (`contains` vs `intersects`) | 009 (boundary-touching parcel qualifies) | 916 `contains-vs-intersects` |
| Empty / null / invalid geometry | 011 (bow tie + null geometry excluded and reported) | 918 `invalid-accepted` |
| Nearest-neighbour ties / duplicate identifiers | ⚠️ duplicate-identifier resistance in 008; tie-breaking by distance not yet exercised | 915 `join-double-count` |
| Hallucinated geometry | 001 | 901 `hallucinated-geometry` |
| LineString metric analysis | 001, 009 (road buffers/distances) | — |

## Operation

| Operation family | Positive | Mutation |
|---|---|---|
| Areal + distance filtering (projected CRS) | 001 | 901, 902 |
| Many-to-many spatial join | 008 | 915 |
| Boundary/touch selection | 009 | 916 |
| Hole-aware area measurement | 010 | 917 |
| Geometry validation pipeline | 011 | 918 |
| Format parity (same result, three storages) | 014 | 919 |
| Reprojection before metric use | 007 | 914 |
| Attribute override chains | 012, 013 | 920, 921 |

## CRS

| Risk | Positive | Mutation |
|---|---|---|
| Mixed input CRS with correct reprojection | 007 (WGS84 source + EPSG:3301 analysis; sub-mm round trip) | — |
| Legitimate EPSG:4326 storage followed by projected metric analysis | 007 | — |
| CRS/axis-order or output-metadata mismatch | 007 (real 3301 coordinates vs declared CRS cross-checked) | 914 `crs-metadata-mismatch` (relabelled output CRS) |
| Wrong analysis CRS | 001 (analysis_crs enforced) | 902 `wrong-crs` |
| Geographic CRS used for metric operations | every case (`geodata.crs_not_used_for_metrics`) | — |
| Complete QGIS layer CRS + project reprojection enabled | every visual-leg case | 922 `qgis-incomplete-crs` |

## Source

| Risk | Positive | Mutation |
|---|---|---|
| Pinned versions | every case | 906 `unpinned-source` |
| Pin classes (hash-matched local snapshot) | every case (all three fixture sources carry `pin: local_snapshot`) | 911 `mutated-source` (byte identity) |
| Expired / inaccessible backend snapshot is `not_reproducible` | unit tests | 926 `expired-backend-snapshot` |
| No credentials in `project.yaml`; connections by reference | every case | 927 `inline-credentials` |
| Completeness reporting | 001 (mini-Tartu) | 907 `incomplete-pagination` |
| Source immutability (byte identity) | every case (auto-inserted check) | 911 `mutated-source` |
| Provider/access metadata | every case | — |

## Override handling

| Risk | Positive | Mutation |
|---|---|---|
| Prior-value mismatch rejected without source mutation | 012 (O-002 rejected, source untouched) | 920 `conflict-ignored` |
| Ordered override chain (stage N+1 depends on stage N) | 013 (O-004 → O-005) | 921 `ordered-swapped` |
| Override applied to output | 001, 012 | 904 `override-not-applied` |
| Hidden feature removed from effective view | 012 (O-003 hide) | — |
| Provenance/evidence of overrides | 012, 013 | 905 `override-from-mismatch` |

## Format / backend

| Format | Positive | Mutation |
|---|---|---|
| GeoJSON | 007, 012, 013, 014 | 914 (declared-CRS mismatch), 919 (dropped rows) |
| GeoParquet | 007, 008, 009, 010, 011, 014 | 919 |
| GeoPackage | 014 | 919 |
| DuckDB Spatial | every case (grading oracle) | — |
| PostGIS / warehouse canary | ❌ deferred: needs a live warehouse service; not runnable in the offline fixture gate. Track as a scheduled-container case. | ❌ |

## Presentation / QGIS

| Risk | Positive | Mutation |
|---|---|---|
| QGIS runtime load + non-blank render | 001, 006 (visual legs) | 910 `qgis-false-success` |
| Every declared QGIS layer changes rendered pixels | 001, 006 (visual legs) + worked-example CI | 922 `qgis-incomplete-crs` (static twin) |
| Manifest↔QGIS layer/CRS reconciliation | 001, 006 | — |
| Interactive basemap (tiles + attribution) | 001, 006 (MapLibre + OSM XYZ) | 913 `basemap-missing` |
| Manifest claims visible in the product | 001, 006 | 912 `dashboard-silent-warnings` |
| Layer toggles / scenario distinguishability / canonical reset | 001, 006 | — |

## Prompt style (live-only cases, graded with the same assertion library)

| Style | Case | Oracle |
|---|---|---|
| Fully specified (baseline) | 001–006 | exact golden artifacts |
| Underspecified ("make me a useful map") | 070 | structural conformance + declared assumptions; no silent threshold invention is asserted structurally |
| Contradictory constraints | 071 | empty candidate set + warning propagates to project status |
| Missing attribute, "do not invent" | 072 | empty candidate set + warning; no invented data |
| Non-English (Estonian) specification | 073 | identical golden artifacts to 001 (P1, P2, P5 in; P3, P4 out) |

## Metamorphic oracles

| Property | Where |
|---|---|
| Reprojection invariance (4326-stored input must recover the surveyed 3301 line) | 007 (round-trip verified to sub-mm; golden distances/areas baked) |
| Duplicate-input resistance | 008 (duplicated poi-z must not inflate the join) |
| Input-order permutation | 015 `parcel-order` (source shuffled, outputs must be equal); mutation 923 `permutation_changed_output` |
| Monotonic buffer behaviour | 015 `road-distance-monotonic` (threshold tripled through the declared parameter, baseline keys must survive); mutation 924 `monotonicity_violated` |
| Duplicate-input resistance (declared) | 015 `parcel-duplicates` (every parcel appended once more, outputs must be equal); mutation 925 `duplicates_changed_output` |
| Invalid-precondition refusal | unit tests: count/sum semantics, missing tie-break, non-growing variant, unsupported format, source already duplicated, oversize source |

## Known gaps (tracked)

1. **PostGIS / warehouse canary** — needs a live service; candidate design is
   a scheduled-container case in the visual/benchmark workflow.
2. **True MultiPolygon inputs** and **nearest-neighbour tie-breaking**.
3. **CRS round-trip, subset-additivity, and area-scale metamorphic relations** (the framework rejects them as undeclared rather than guessing).
4. **Raster analysis** (the suite is vector-only so far).
