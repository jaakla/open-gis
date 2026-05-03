# QGIS — Desktop, Plugins, and MCP

QGIS is the open desktop GIS — full cartographic production, the Processing toolbox (a unified front-end for GDAL/GRASS/SAGA/WhiteboxTools/OTB), a mature plugin ecosystem (1500+ plugins), and a PyQGIS scripting API. It also has multiple MCP integrations now, which enable agentic / LLM-driven QGIS workflows.

## When QGIS is the right tool

* Cartographic production — print maps, atlases, styling
* Visual exploration of unfamiliar data (faster than coding for a one-shot look)
* Automating workflows that combine many GDAL/GRASS/SAGA/WhiteboxTools/OTB algorithms — the Processing toolbox unifies them
* Producing client-friendly outputs (a `.qgz` project a non-developer can open and tweak)
* Field-data collection workflows (with QField)
* Serving WMS/WFS/WMTS from authored projects (QGIS Server)

## When QGIS is NOT the right tool

* Headless data pipelines — use GDAL/Python/DuckDB directly
* Cloud-native analytics on remote GeoParquet — DuckDB is faster and cleaner
* Reproducible research without a desktop user — script with PyQGIS standalone or skip QGIS entirely
* Real-time / streaming workloads

## Installation

```bash
# Long-term release (recommended for production)
sudo apt install qgis qgis-plugin-grass

# Latest release via QGIS official repos (better than distro packages, often)
# See https://qgis.org/resources/installation-guide/

# Cross-platform alternative: conda-forge
conda install -c conda-forge qgis
```

QGIS bundles its own Python and a pinned GDAL. For headless use cases, the `osgeo/gdal` Docker image plus PyQGIS installed alongside is a common pattern.

## Processing toolbox — the unified algorithm front-end

The single most useful feature in QGIS for power users. Exposes algorithms from GDAL, GRASS, SAGA, WhiteboxTools, OTB (Orfeo Toolbox), native QGIS, and any plugin that registers algorithms — all through one consistent dialog and Python API.

### From the GUI

* `Processing → Toolbox` (Ctrl+Alt+T)
* Search by name across all providers
* Run any algorithm with a uniform parameter dialog
* Drag-and-drop into the **Graphical Modeler** to chain algorithms into reusable models (saved as `.model3` files)
* Hit the "History" button to see the equivalent PyQGIS code for any run — gold for converting GUI exploration to scripts

### From PyQGIS

```python
import processing

# Any algorithm by ID
result = processing.run("native:buffer", {
    "INPUT": "input.gpkg",
    "DISTANCE": 100,
    "DISSOLVE": False,
    "OUTPUT": "buffered.gpkg",
})

# GDAL via Processing
processing.run("gdal:warpreproject", {
    "INPUT": "in.tif",
    "TARGET_CRS": "EPSG:3301",
    "RESAMPLING": 1,  # bilinear
    "OUTPUT": "out.tif",
})

# WhiteboxTools through Processing (after enabling the WBT provider)
processing.run("wbt:FillDepressions", {...})

# List all available algorithms
for alg in QgsApplication.processingRegistry().algorithms():
    print(alg.id(), "-", alg.displayName())
```

### Headless (standalone) PyQGIS

```python
import sys
from qgis.core import QgsApplication

QgsApplication.setPrefixPath("/usr", True)
qgs = QgsApplication([], False)
qgs.initQgis()

import processing
from processing.core.Processing import Processing
Processing.initialize()

# ...run algorithms here...

qgs.exitQgis()
```

This pattern lets a Python script use the full Processing toolbox without launching the GUI. Useful when DuckDB/GeoPandas don't cover a specific algorithm but Processing does.

## Plugin ecosystem — the parts worth knowing

QGIS has 1500+ plugins; most aren't worth the time. The list below is the high-signal subset organized by purpose.

### Data acquisition

* **QuickMapServices** — instant access to many basemap providers (OSM, ESRI, Google, etc.) including CartoDB and historical maps. The single most-installed plugin.
* **QuickOSM** — Overpass query builder with sensible presets. Pulls OSM data into QGIS as a layer in seconds.
* **HCMGIS** — bulk download tools for OSM, country boundaries, basemaps
* **MetaSearch** *(built-in)* — OGC catalog client; supports CSW, OGC API Records, and STAC for raster discovery
* **QGIS-STAC plugin** — STAC catalog browsing and item loading directly into QGIS
* **OpenTopography DEM Downloader** — pulls SRTM and other DEMs for an AOI

### Vector processing

* **mmqgis** — extra vector operations, attribute manipulation, geocoding utilities
* **MMQGIS Buffer Wedge** and similar geometric extras
* **Group Stats** — pivot-table-style attribute aggregation
* **Refactor Fields** *(built-in via Processing)* — field type conversion, renaming
* **Lat Lon Tools** — coordinate parsing, MGRS conversion, copy/paste coords

### Raster / remote sensing

* **Semi-Automatic Classification Plugin (SCP)** — full Sentinel-2 / Landsat workflow: download, atmospheric correction, classification, change detection. Heavyweight but comprehensive.
* **Orfeo Toolbox (OTB)** *(via Processing provider)* — segmentation, feature extraction, ML classification on imagery. Originally CNES, very capable.
* **DZetsaka** — supervised classification with multiple algorithms
* **Profile Tool** — elevation profile drawing
* **Serval** — fast raster cell editing

### Terrain & hydrology

* **WhiteboxTools** *(via Processing provider after install)* — comprehensive terrain, hydrology, LiDAR
* **GRASS** *(via Processing provider, ships with QGIS)* — `r.watershed`, `r.terraflow`, etc.
* **SAGA** *(via Processing provider)* — broad raster analysis library

### LiDAR / point cloud

* **LAStools** *(via Processing provider after install)* — fast LiDAR processing (some tools require a license for commercial use; `las2las`, `las2dem` etc. are free)
* QGIS 3.18+ has **native point cloud rendering** (LAS/LAZ + COPC + EPT) — no plugin needed for visualization

### Network analysis

* **QNEAT3** — comprehensive network analysis: shortest path, isochrones, OD matrices on QGIS layers
* **Network Analysis** *(built-in via Processing)* — basic shortest path
* **pgRoutingLayer** — bridge to pgRouting in PostGIS

### Cartography

* **qgis2web** — export QGIS map to a static Leaflet / OpenLayers / MapBox GL site. Quick way to get a styled web map.
* **Qgis2threejs** — export 3D scenes to web (Three.js)
* **QGIS Resource Sharing** — community-published symbol libraries, color ramps, print templates
* **MapSwipe Tool** — swipe-compare two layers (great for before/after imagery)
* **Atlas** *(built-in)* — generate one map per feature in a coverage layer (e.g. parcel atlases, tourist guides)

### Time series

* **Time Manager** — animate temporal data (somewhat superseded by QGIS native temporal controller in 3.14+, but still useful for older versions / specific workflows)

### Database / Server / Cloud

* **DB Manager** *(built-in)* — PostGIS, SpatiaLite, GeoPackage browser and SQL console. Underrated.
* **MapTiler** plugin — direct connection to MapTiler hosted services (paid)
* **STAC API Browser** — browse STAC catalogs

### Field collection

* **QField** / **QFieldCloud** — companion mobile app for offline field data collection synced back to QGIS projects. Production-grade.
* **Mergin Maps** — alternative cloud-sync field collection by Lutra Consulting

### Development / power users

* **Plugin Reloader** — hot-reload during plugin development
* **Plugin Builder** — scaffold a new plugin
* **Script Runner** — run PyQGIS scripts without opening the Python console

### Plugins that have been replaced — avoid

* **OpenLayers Plugin** — deprecated in favor of QuickMapServices
* **Time Manager** — partially superseded by built-in temporal controller for simple cases
* **OpenStreetMap (built-in OSM downloader)** — superseded by QuickOSM

## Default styling and project setup

For a clean baseline, every new project should:

1. Set project CRS deliberately (`Project → Properties → CRS`) — don't drift on the first layer's CRS by accident
2. For Estonia: `EPSG:3301`
3. Enable on-the-fly reprojection (it's on by default in QGIS 3+, but worth confirming)
4. Set project ellipsoid to match (for measurements)
5. Save styles as `.qml` next to data files for reusability

## QGIS Server

A QGIS project (`.qgz`) becomes a WMS / WFS / WMTS / OGC API Features service via QGIS Server. Lightweight (FastCGI), no separate publishing step — the styled project IS the service.

```bash
# Docker-based deployment
docker run -d -p 8080:80 \
  -v $PWD/projects:/io/data \
  camptocamp/qgis-server:latest
```

When useful: an authored cartographic style needs to be served as a tiled web map without re-implementing the styling in MapLibre.

## QGIS MCP — agentic QGIS

There are now several MCP servers that connect QGIS to LLMs, enabling prompt-driven map creation, layer loading, processing algorithm execution, and arbitrary PyQGIS execution.

### Implementations

| Server | Repo | Notes |
|---|---|---|
| **QGIS MCP (original)** | `jjsantos01/qgis_mcp` | First implementation, BlenderMCP-inspired. Plugin + Python MCP server. Tested on QGIS 3.22+. |
| **QGIS MCP (extended)** | `nkarasiak/qgis-mcp` | Broader tool surface organized into groups: system, project, layer, features, selection, style, canvas, render, processing, code, batch, layer_tree, plugins, variables, settings, expression, transform, message_log, layer_property |
| **QGIS2OllamaMCP** | `anitagraser/QGIS2OllamaMCP` | Anita Graser's fork targeted at local Ollama models rather than Claude. Same pattern. |

All follow the same architecture: a QGIS plugin runs a socket server inside QGIS; an external MCP server (Python) bridges between MCP clients (Claude Desktop, Claude Code) and that socket.

### Architecture

```
Claude Desktop / Claude Code
    │  (MCP over stdio)
    ▼
qgis_mcp_server.py  (the MCP server, runs externally)
    │  (TCP socket, default localhost:9876)
    ▼
QGIS plugin "QGIS MCP"  (runs inside a QGIS GUI session)
    │
    ▼
PyQGIS API → live QGIS canvas / project / processing toolbox
```

### Setup outline

1. Clone the plugin repo, copy the `qgis_mcp_plugin` folder into the QGIS plugins directory
   (Linux: `~/.local/share/QGIS/QGIS3/profiles/default/python/plugins/`)
2. Restart QGIS, enable the plugin, click "Start Server"
3. Configure the MCP client (`claude_desktop_config.json` or equivalent):
   ```json
   {
     "mcpServers": {
       "qgis": {
         "command": "uv",
         "args": [
           "--directory", "/path/to/qgis_mcp/src/qgis_mcp",
           "run", "qgis_mcp_server.py"
         ]
       }
     }
   }
   ```
4. Restart the MCP client; QGIS tools appear

### Typical capabilities exposed

* `ping` / `get_qgis_info` — connectivity check
* `create_new_project` / `load_project` / save
* `add_vector_layer` / `add_raster_layer` / `remove_layer`
* `get_layers` — list current layers
* `zoom_to_layer`, `set_canvas_extent`
* `execute_processing` — run any algorithm by ID with a parameter dict
* `execute_code` — arbitrary PyQGIS execution (powerful, dangerous — see safety)
* `render_map` — export the canvas to PNG/PDF
* `get_features`, `select_by_expression`, `set_layer_style`

### When a QGIS MCP shines

* Iterative cartographic styling — "make the buildings darker, drop the labels under zoom 10, add a north arrow"
* Exploratory data inspection — load a layer, ask questions about its attributes, run a clustering algorithm, render
* Mixed workflows where the LLM should reason about *what to do next* between processing steps
* Teaching / demo scenarios

### Safety considerations

`execute_code` runs arbitrary Python in the QGIS process with full filesystem access. The same applies to `add_vector_layer` if it accepts paths to network drives or DB connections. Use only with projects whose data you control. The Merit MCP preview/confirm pattern would be a useful enhancement for any QGIS MCP exposing destructive operations — currently most don't have it.

### When to skip QGIS MCP and use other tools

* Pure batch processing — `processing.run` from a standalone PyQGIS script is more reproducible than going through the MCP socket
* Headless servers — QGIS MCP requires a running QGIS GUI session
* Production pipelines — MCP is for interactive workflows, not scheduled jobs

## PyQGIS quick reference

Worth knowing even if mostly using the MCP, because the MCP often expects you to specify processing algorithm IDs and parameter dicts.

```python
from qgis.core import (
    QgsProject, QgsVectorLayer, QgsRasterLayer,
    QgsCoordinateReferenceSystem, QgsPointXY,
)

# Add a layer to current project
layer = QgsVectorLayer("buildings.gpkg", "Buildings", "ogr")
QgsProject.instance().addMapLayer(layer)

# Set project CRS
QgsProject.instance().setCrs(QgsCoordinateReferenceSystem("EPSG:3301"))

# Iterate features
for f in layer.getFeatures():
    geom = f.geometry()
    attrs = f.attributes()
    # ...

# Run a processing algorithm
import processing
out = processing.run("native:buffer", {
    "INPUT": layer, "DISTANCE": 100, "OUTPUT": "memory:"
})["OUTPUT"]

# Style from QML
layer.loadNamedStyle("style.qml")
layer.triggerRepaint()
```

## Troubleshooting common QGIS pain points

* **"GeoParquet shows up but won't load"** — your GDAL is old. QGIS 3.34+ with GDAL 3.8+ has stable GeoParquet support; older versions are flaky.
* **"CRS warning on every layer load"** — set the project CRS first, then load layers with matching CRS or with explicit reprojection.
* **"Processing algorithm not found"** — the relevant provider (GRASS / SAGA / WhiteboxTools / OTB) isn't enabled. `Settings → Options → Processing → Providers`.
* **"WhiteboxTools provider missing"** — install the WhiteboxTools binary separately, then point the plugin to it.
* **"Plugin doesn't show up after install"** — restart QGIS; check `Plugins → Manage and Install → Installed`; check the Python console for import errors.
