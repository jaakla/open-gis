# open-gis

**Geospatial questions →  reproducible, validated GIS analysis project (with very nice interactive map).**

Install:
```bash
npx skills add jaakla/open-gis -g
```

Open-gis gives your favorite AI agent: Claude Code, Codex, Cursor, OpenCode, and 50+ other agents a production workflow from **authoritative data discovery** through analysis to interactive web and GIS deliverables. Material workflows become inspectable and repeatable well-defined projects in a `yaml` file with pinned sources, explicit assumptions and CRS choices, deterministic processing, isolated overrides, machine-readable validation, and surfaced provenance.

It is open-first and cloud-native by default, built on shoulders of the awesome Open GIS stack: STAC for discovery; GeoParquet, COG, and PMTiles for storage and delivery; DuckDB and PostGIS for compute; and QGIS, MapLibre, and Martin for presentation. It also uses GDAL/OGR, GeoPandas, xarray/rioxarray, PDAL, routing engines, spatial SQL, and pragmatic hosted services when scale or reliability requires them.

## What's in this repo

- [SKILL.md](SKILL.md) — the skill entry point: triggers, global defaults, format and compute decision matrices, anti-patterns, and a quick triage guide.
- [references/data-sources.md](references/data-sources.md) - lists OSM, Overture, Sentinel/Landsat, regional portals, STAC catalogs and others.
- [references/services-and-scale.md](references/services-and-scale.md) - depending on case use local installs or hosted/SaaS services for global-scale basemaps, elevation, routing, geocoding, place search, and postcodes.
- [references/formats-and-crs.md](references/formats-and-crs.md) - how to choose formats, conversions, projections, EPSG codes.
- [references/processing.md](references/processing.md) - when and how to use GDAL/OGR, GeoPandas, xarray, DuckDB, PostGIS, PDAL and other open geo processing tools.
- [references/analytics.md](references/analytics.md) — do vector/raster analytics, terrain, hydrology, network, point clouds, geocoding etc.
- [references/web-delivery.md](references/web-delivery.md) — renderer selection for maps, PMTiles, MVT, Martin, TiTiler, MapLibre, deck.gl, kepler.gl, and lonboard formats and engines.
- [references/qgis.md](references/qgis.md) — QGIS desktop, plugins, PyQGIS, Processing, QGIS MCP.
- [references/validation-and-ops.md](references/validation-and-ops.md) — validation, manifests, attribution, and deployment checks, including the machine-readable reproducible-project contract.
- [references/project-spec.md](references/project-spec.md) — the specific`open-gis-project/v1` schema: compiling any material analysis into a reproducible GIS project (`project.yaml`, pipeline, source provenance, overrides, validation, semantic presentation, QGIS output).
- [templates/](templates/) — ready scaffolds (`project.yaml`, `pipeline.py`, `presentation.yaml`, `validation.yaml`) for new projects.
- [examples/tartu-development/](examples/tartu-development/) — a fully-worked reproducible project matching the acceptance scenario: source provenance + timestamps, explicit assumptions, two verified project overrides (a scenario attribute change with prior-value verification, and hypothetical scenario geometry), deterministic pipeline, machine-readable validation, and semantic presentation.
- [evals/](evals/) — the eval suite proving agents actually follow the `open-gis-project/v1` contract: `python evals/run.py --mode fixture` runs deterministic, no-network, no-LLM checks against real generated artifacts (schema, GIS correctness, overrides, validation integrity, presentation contract, and clean reruns), plus adversarial cases and a pluggable Claude Code/Codex live-agent benchmark.
- [`open_gis/`](open_gis/) — the installable `open-gis validate/run/inspect` CLI for auditing and executing `open-gis-project/v1` projects.
- [`.claude-plugin/`](.claude-plugin/) — Claude Code plugin and marketplace manifests, so the repository can also be installed with `/plugin install`. Validated in CI by [`.github/workflows/plugin.yml`](.github/workflows/plugin.yml).

My local Estonia-specific guidance (Maa- ja Ruumiamet, ETAK, EPSG:3301 / L-EST97) is included for convenience. But all the global sources are incuded for world-wide coverage.

## Install

The recommended way is the [skills CLI](https://github.com/vercel-labs/skills), which works for Claude Code, Cursor, OpenCode, Codex, and 50+ other agents.

### Recommended: skills CLI

Install globally (available in every project):

```bash
npx skills add jaakla/open-gis -g
```

Update later with `npx skills update open-gis`. Remove with `npx skills remove open-gis`.

### Claude Code plugin (optional)

Claude Code users can install the same repository as a plugin instead. This adds
versioned installs, `/plugin update`, and project-scoped installs that a team
picks up from a repository's `.claude/settings.json`:

```bash
/plugin marketplace add jaakla/open-gis
/plugin install open-gis@open-gis
```

The repository is its own marketplace, so no separate marketplace repo is
needed. The plugin wraps the same root `SKILL.md` — nothing is duplicated, and
the skills-CLI install path above keeps working unchanged.

### Install the project CLI

The skills installer loads the agent instructions; the Python package provides
the project commands. From a clone of this repository:

```bash
python3 -m pip install .
open-gis --version
```

For development, the commands can also run directly without installation:

```bash
python3 -m open_gis --help
```

### Manual install (fallback)

If you'd rather not use the CLI, clone directly into your agent's skills directory. For Claude Code:

```bash
# User-level (every project)
git clone https://github.com/jaakla/open-gis.git ~/.claude/skills/open-gis

# Project-level (one repo)
git clone https://github.com/jaakla/open-gis.git .claude/skills/open-gis
```

### Verify

Start Claude Code and run `/skills open-gis` should appear in the list. The expected layout is:

```
<skills-dir>/open-gis/
├── SKILL.md
├── references/
│   ├── analytics.md
│   ├── data-sources.md
│   ├── formats-and-crs.md
│   ├── processing.md
│   ├── project-spec.md
│   ├── qgis.md
│   ├── services-and-scale.md
│   ├── spatial-sql.md
│   ├── validation-and-ops.md
│   └── web-delivery.md
├── templates/
│   ├── project.yaml
│   ├── pipeline.py
│   ├── presentation.yaml
│   └── validation.yaml
├── examples/
│   └── tartu-development/
└── .claude-plugin/          # Claude Code plugin + marketplace manifests
    ├── plugin.json
    └── marketplace.json
```

## Use

The skill auto-activates when you ask Claude about geospatial work — terms like GIS, OpenStreetMap, Overture, Sentinel, Landsat, LiDAR, GeoTIFF, shapefile, GeoPackage, raster/vector tiles, isochrones, spatial joins, EPSG codes, and projections will all trigger it. You don't need to invoke it manually, but sometimes hinting "use open-gis skills" helps.

Example prompts that engage the skill:

- "Pull all buildings in Tartu from Overture and publish them as a PMTiles layer."
- "Compute average NDVI for these polygons from Sentinel-2 over the last 12 months."
- "Reproject this GeoTIFF from EPSG:3301 to EPSG:3857 as a COG."
- "Set up an OSRM routing server from a Estonia OSM extract."
- "Build an isochrone API around these points."

If you want to force the skill to load, you can reference it explicitly:

> Use the open-gis skill to convert this shapefile to GeoParquet.

## Project CLI

The CLI operates on an `open-gis-project/v1` manifest. A project directory may
be supplied in place of its `project.yaml` file.

```bash
# Audit the complete artifact, including outputs, report, and run record.
open-gis validate path/to/project.yaml

# Run the one canonical pipeline, then validate what it produced.
open-gis run path/to/project.yaml

# Review sources, versions, overrides, ordered steps, outputs, and latest run.
open-gis inspect path/to/project.yaml
```

Useful automation options:

```bash
open-gis validate project.yaml --json --output validation/cli-report.json
open-gis validate project.yaml --strict       # warnings also return non-zero
open-gis validate project.yaml --preflight    # skip not-yet-generated artifacts
open-gis run project.yaml --dry-run
open-gis run project.yaml --json
open-gis inspect project.yaml --json
```

`validate` checks manifest structure, source retrieval/version/licensing data,
CRS declarations, processing graph resolution, override provenance and files,
output existence, validation-report parity/status propagation, override
application results, and run-record identity/hashes. GIS-specific checks such as
geometry validity remain the pipeline's responsibility; the CLI verifies that
each declared check appears exactly once with an explicit result.

Normal validation warnings return exit code 0 so known limitations remain
representable. Failures return 1; malformed invocation or an unstartable runtime
returns 2. `--strict` makes warnings return 1.

## What this skill will and won't do

**Will:**
- Recommend modern, cloud-native formats (GeoParquet, COG, PMTiles) and flag legacy patterns (Shapefile output, MBTiles for new deployments).
- Push spatial joins to DuckDB / PostGIS instead of Python loops.
- Discover data via STAC before downloading.
- Preserve license metadata (OSM ODbL, Overture per-source, Sentinel attribution).
- Pin dataset versions for reproducibility (Overture releases, STAC item IDs, OSM extract dates).
- Compile material multi-stage analysis into a reproducible GIS project (`project.yaml` + pipeline + overrides + validation), deriving the final map/dashboard from it.

**Won't:**
- Trigger on simple location lookups ("what city is this?") or casual map references with no analytical work.
- Default to proprietary services when an open/self-hosted option fits the scale, quality, privacy, and budget.

## License

Licensed under the [MIT License](LICENSE).

## Contributing

Issues and PRs welcome at [github.com/jaakla/open-gis](https://github.com/jaakla/open-gis). When adding a new tool or workflow, place it in the matching reference file and add a one-row entry to the relevant decision matrix in [SKILL.md](SKILL.md).
