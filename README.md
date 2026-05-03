# open-gis

A Claude Code [skill](https://docs.claude.com/en/docs/claude-code/skills) for production-grade geospatial work using only free and open source tools and open data.

When loaded, it gives Claude opinionated defaults and reference material for the modern open GIS stack — STAC for discovery, GeoParquet + COG + PMTiles for storage, DuckDB and PostGIS for compute, MapLibre and Martin for delivery — plus QGIS, GDAL/OGR, GeoPandas, xarray/rioxarray, PDAL, OSRM/Valhalla, tippecanoe, and more.

## What's in this repo

- [SKILL.md](SKILL.md) — the skill entry point: triggers, global defaults, format and compute decision matrices, anti-patterns, and a quick triage guide.
- [references/data-sources.md](references/data-sources.md) — OSM, Overture, Sentinel/Landsat, regional portals, STAC catalogs.
- [references/formats-and-crs.md](references/formats-and-crs.md) — choosing formats, conversions, projections, EPSG codes.
- [references/processing.md](references/processing.md) — GDAL/OGR, GeoPandas, xarray, DuckDB, PostGIS, PDAL.
- [references/analytics.md](references/analytics.md) — vector/raster analytics, terrain, hydrology, network, point clouds.
- [references/web-delivery.md](references/web-delivery.md) — PMTiles, MVT, Martin, TiTiler, MapLibre, deck.gl.
- [references/qgis.md](references/qgis.md) — QGIS desktop, plugins, PyQGIS, Processing, QGIS MCP.
- [references/validation-and-ops.md](references/validation-and-ops.md) — validation, manifests, attribution, and deployment checks.

Estonia-specific guidance (Maa-amet, ETAK, EPSG:3301 / L-EST97) is included throughout.

## Install

The recommended way is the [skills CLI](https://github.com/vercel-labs/skills), which works for Claude Code, Cursor, OpenCode, Codex, and 50+ other agents.

### Recommended: skills CLI

Install globally (available in every project):

```bash
npx skills add jaakla/open-gis -g
```

Or install into the current project only (committed alongside your code):

```bash
npx skills add jaakla/open-gis
```

Useful flags:

```bash
# Preview what's in the repo without installing
npx skills add jaakla/open-gis --list

# Target a specific agent
npx skills add jaakla/open-gis -g -a claude-code

# Non-interactive (CI-friendly)
npx skills add jaakla/open-gis -g -y
```

Update later with `npx skills update open-gis`. Remove with `npx skills remove open-gis`.

### Manual install (fallback)

If you'd rather not use the CLI, clone directly into your agent's skills directory. For Claude Code:

```bash
# User-level (every project)
git clone https://github.com/jaakla/open-gis.git ~/.claude/skills/open-gis

# Project-level (one repo)
git clone https://github.com/jaakla/open-gis.git .claude/skills/open-gis
```

### Verify

Start Claude Code and run `/skills` — `open-gis` should appear in the list. The expected layout is:

```
<skills-dir>/open-gis/
├── SKILL.md
└── references/
    ├── analytics.md
    ├── data-sources.md
    ├── formats-and-crs.md
    ├── processing.md
    ├── qgis.md
    ├── validation-and-ops.md
    └── web-delivery.md
```

## Use

The skill auto-activates when you ask Claude about geospatial work — terms like GIS, OpenStreetMap, Overture, Sentinel, Landsat, LiDAR, GeoTIFF, shapefile, GeoPackage, raster/vector tiles, isochrones, spatial joins, EPSG codes, and projections will all trigger it. You don't need to invoke it manually.

Example prompts that engage the skill:

- "Pull all buildings in Tartu from Overture and publish them as a PMTiles layer."
- "Compute average NDVI for these polygons from Sentinel-2 over the last 12 months."
- "Reproject this GeoTIFF from EPSG:3301 to EPSG:3857 as a COG."
- "Set up an OSRM routing server from a Estonia OSM extract."
- "Build an isochrone API around these points."

If you want to force the skill to load, you can reference it explicitly:

> Use the open-gis skill to convert this shapefile to GeoParquet.

## What this skill will and won't do

**Will:**
- Recommend modern, cloud-native formats (GeoParquet, COG, PMTiles) and flag legacy patterns (Shapefile output, MBTiles for new deployments).
- Push spatial joins to DuckDB / PostGIS instead of Python loops.
- Discover data via STAC before downloading.
- Preserve license metadata (OSM ODbL, Overture per-source, Sentinel attribution).
- Pin dataset versions for reproducibility (Overture releases, STAC item IDs, OSM extract dates).

**Won't:**
- Trigger on simple location lookups ("what city is this?") or casual map references with no analytical work.
- Recommend proprietary tools when an open equivalent exists.

## License

See repository for license terms.

## Contributing

Issues and PRs welcome at [github.com/jaakla/open-gis](https://github.com/jaakla/open-gis). When adding a new tool or workflow, place it in the matching reference file and add a one-row entry to the relevant decision matrix in [SKILL.md](SKILL.md).
