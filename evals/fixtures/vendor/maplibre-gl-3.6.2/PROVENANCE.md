# Vendored: MapLibre GL JS 3.6.2

The reference fixture's dashboard is a graded specimen. Loading its map
library from a CDN made every visual-eval result depend on a third party's
uptime: an unreachable `unpkg.com` yields `maplibregl is not defined`, a page
error, and mutation cases failing on the wrong `expect_code` — a red build
caused by someone else's outage rather than by the code under test. So the
library is vendored and pinned here, and `gen.py` copies it into each
generated project.

| | |
|---|---|
| Package | `maplibre-gl` |
| Version | 3.6.2 |
| Retrieved | 2026-08-29 |
| Source | `https://unpkg.com/maplibre-gl@3.6.2/dist/` |
| License | BSD-3-Clause — <https://github.com/maplibre/maplibre-gl-js/blob/v3.6.2/LICENSE.txt> |

## Checksums

```
c46084df69bbaa995b301a515274a86ec53905c78459b80dccbc27a0c0b8d13b  maplibre-gl.js
731181d400d65a8b09d842f55b70bc4dc11010b15b8549e2c65a69d233fbdd2e  maplibre-gl.css
```

Verify with `sha256sum -c` after any update, and bump the version in
`gen.py`'s `_MAPLIBRE_VENDOR` alongside the directory name so the generated
dashboard and the vendored bytes cannot drift apart.

Tiles still come from the network (`tile.openstreetmap.org`). That is
deliberate and safe for grading: `visual.dashboard_loads_in_browser` counts
tile *requests*, which Chromium issues whether or not the host answers, so an
unreachable tile server cannot turn a good dashboard into a failing one.
