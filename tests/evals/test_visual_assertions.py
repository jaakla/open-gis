"""Direct tests for evals/assertions/visual.py (PR 7 visual integration).

PNG analysis is stdlib-only and always tested. Browser checks require a
Playwright Chromium install and are skipped (never silently passed) when it
is unavailable — the scheduled visual-integration environment provides it.
"""

from __future__ import annotations

import struct
import unittest
import zipfile
from pathlib import Path

from .helpers import make_workspace, write_project  # noqa: E402  (also wires sys.path for `assertions`)

from openmapstack.checks import visual  # noqa: E402


# ---------------------------------------------------------------------------
# Synthetic PNG helpers (filter-0 encoding plus a Sub-filtered variant so the
# decoder's unfiltering logic is exercised, not just its parsing)
# ---------------------------------------------------------------------------

def _chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", visual.zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def encode_png(width: int, height: int, pixels: list[bytes], color_type: int = 6, filter_type: int = 0) -> bytes:
    """pixels: list of rows, each row a flat byte sequence of RGBA triples+alpha."""
    ihdr = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    raw = bytearray()
    prev = bytearray(len(pixels[0]))
    for row in pixels:
        raw.append(filter_type)
        if filter_type == 0:
            raw.extend(row)
        elif filter_type == 1:  # Sub: encode delta from left pixel
            bpp = len(row) // width
            for i in range(len(row)):
                left = row[i - bpp] if i >= bpp else 0
                raw.append((row[i] - left) & 0xFF)
        elif filter_type == 2:  # Up
            for i in range(len(row)):
                raw.append((row[i] - prev[i]) & 0xFF)
        prev = bytearray(row)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", bytes(visual.zlib.compress(bytes(raw))))
        + _chunk(b"IEND", b"")
    )


def solid_png(path: Path, width: int = 40, height: int = 30, color: tuple = (255, 255, 255, 255)) -> Path:
    row = bytes(color) * width
    path.write_bytes(encode_png(width, height, [row] * height))
    return path


def content_png(path: Path, width: int = 400, height: int = 300) -> Path:
    """White background with a substantial colored rectangle and a line."""
    row = bytes((255, 255, 255, 255)) * width
    marked = (
        bytes((255, 255, 255, 255)) * 100
        + bytes((200, 40, 40, 255)) * 150
        + bytes((255, 255, 255, 255)) * 150
    )
    pixels = [row] * height
    for y in range(60, 240):
        pixels[y] = marked
    path.write_bytes(encode_png(width, height, pixels))
    return path


def filtered_png(path: Path, width: int = 4, height: int = 4) -> Path:
    """A gradient image encoded half with Sub and half with Up filters."""
    pixels = []
    for y in range(height):
        row = bytearray()
        for x in range(width):
            row.extend((y * 40 + x * 30, 10, 200, 255))
        pixels.append(bytes(row))
    half = height // 2
    raw = bytearray()
    prev = bytearray(len(pixels[0]))
    for index, row in enumerate(pixels):
        filter_type = 1 if index < half else 2
        raw.append(filter_type)
        bpp = 4
        if filter_type == 1:
            for i in range(len(row)):
                left = row[i - bpp] if i >= bpp else 0
                raw.append((row[i] - left) & 0xFF)
        else:
            for i in range(len(row)):
                raw.append((row[i] - prev[i]) & 0xFF)
        prev = bytearray(row)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", bytes(visual.zlib.compress(bytes(raw))))
        + _chunk(b"IEND", b"")
    )
    return path


# ---------------------------------------------------------------------------
# PNG analysis
# ---------------------------------------------------------------------------

class PngDecodeTests(unittest.TestCase):
    def test_unfiltered_roundtrip(self) -> None:
        import tempfile

        path = Path(tempfile.mkdtemp()) / "unfiltered.png"
        solid_png(path, 8, 6)
        width, height, channels, rows = visual.decode_png(path)
        self.assertEqual((width, height, channels), (8, 6, 4))
        self.assertEqual(len(rows), 6)
        self.assertEqual(rows[0], bytes((255, 255, 255, 255)) * 8)

    def test_sub_and_up_filters_decode(self) -> None:
        import tempfile

        path = Path(tempfile.mkdtemp()) / "filtered.png"
        filtered_png(path)
        width, height, channels, rows = visual.decode_png(path)
        self.assertEqual((width, height), (4, 4))
        self.assertEqual(rows[0][0:4], bytes((0, 10, 200, 255)))
        self.assertEqual(rows[3][0:4], bytes((120, 10, 200, 255)))
        self.assertEqual(rows[3][12:16], bytes((210, 10, 200, 255)))

    def _encode(self, pixels: list[bytes], width: int, height: int, color_type: int, filter_types: list[int]) -> bytes:
        ihdr = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
        raw = bytearray()
        prev = bytearray(len(pixels[0]))
        for index, row in enumerate(pixels):
            raw.append(filter_types[index])
            if filter_types[index] == 0:
                raw.extend(row)
            elif filter_types[index] == 3:  # Average
                for i in range(len(row)):
                    left = row[i - 1] if i >= 1 else 0  # bpp=1 for gray
                    raw.append((row[i] - ((left + prev[i]) >> 1)) & 0xFF)
            elif filter_types[index] == 4:  # Paeth
                for i in range(len(row)):
                    left = row[i - 1] if i >= 1 else 0
                    up_left = prev[i - 1] if i >= 1 else 0
                    raw.append((row[i] - visual._paeth(left, prev[i], up_left)) & 0xFF)
            prev = bytearray(row)
        return (
            b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", bytes(visual.zlib.compress(bytes(raw))))
            + _chunk(b"IEND", b"")
        )

    def test_average_paeth_and_gray_rgb_color_types_decode(self) -> None:
        import tempfile

        base = Path(tempfile.mkdtemp())
        # 1x4 grayscale rows: first Average-filtered, second Paeth-filtered.
        gray_rows = [bytes((10, 20, 30, 40)), bytes((50, 60, 70, 80))]
        (base / "gray.png").write_bytes(self._encode(gray_rows, 4, 2, color_type=0, filter_types=[3, 4]))
        width, height, channels, rows = visual.decode_png(base / "gray.png")
        self.assertEqual((width, height, channels), (4, 2, 1))
        self.assertEqual(rows[0], gray_rows[0])
        self.assertEqual(rows[1], gray_rows[1])

        # 1x2 RGB rows, Sub-filtered (bpp=3).
        rgb_row = bytes((1, 2, 3, 4, 5, 6))
        ihdr = struct.pack(">IIBBBBB", 2, 1, 8, 2, 0, 0, 0)
        raw = bytearray(b"\x01")
        for i in range(len(rgb_row)):
            left = rgb_row[i - 3] if i >= 3 else 0
            raw.append((rgb_row[i] - left) & 0xFF)
        (base / "rgb.png").write_bytes(
            b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", bytes(visual.zlib.compress(bytes(raw))))
            + _chunk(b"IEND", b"")
        )
        width, height, channels, rows = visual.decode_png(base / "rgb.png")
        self.assertEqual((width, height, channels), (2, 1, 3))
        self.assertEqual(rows[0], rgb_row)

    def test_truncated_and_bad_filter_and_bad_signature_raise(self) -> None:
        import tempfile

        base = Path(tempfile.mkdtemp())
        truncated = bytearray(solid_png(base / "t.png", 4, 4).read_bytes())
        (base / "truncated.png").write_bytes(bytes(truncated[: len(truncated) // 2]))
        with self.assertRaises(ValueError):
            visual.decode_png(base / "truncated.png")

        ihdr = struct.pack(">IIBBBBB", 2, 1, 8, 6, 0, 0, 0)
        raw = bytearray(b"\x07") + bytes(8)  # invalid filter, then pixel data
        (base / "badfilter.png").write_bytes(
            b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", bytes(visual.zlib.compress(bytes(raw))))
            + _chunk(b"IEND", b"")
        )
        with self.assertRaises(ValueError):
            visual.decode_png(base / "badfilter.png")

        (base / "notpng.png").write_bytes(b"GIF89a whatever")
        with self.assertRaises(ValueError):
            visual.decode_png(base / "notpng.png")


class ImageStatsAndDiffTests(unittest.TestCase):
    def test_stats_and_differ_on_known_images(self) -> None:
        import tempfile

        base = Path(tempfile.mkdtemp())
        solid_png(base / "solid.png", 20, 10)
        content_png(base / "content.png", 400, 300)

        stats = visual.image_stats(base / "solid.png")
        self.assertEqual((stats["width"], stats["height"]), (20, 10))
        self.assertEqual(stats["distinct_colors_quantized"], 1)
        self.assertTrue(visual._is_blank(stats))

        content_stats = visual.image_stats(base / "content.png")
        self.assertGreaterEqual(content_stats["distinct_colors_quantized"], 2)
        self.assertFalse(visual._is_blank(content_stats))

        differ, fraction = visual.images_differ(base / "solid.png", base / "solid.png")
        self.assertFalse(differ)
        self.assertEqual(fraction, 0.0)

        differ, fraction = visual.images_differ(base / "solid.png", base / "content.png")
        self.assertTrue(differ)
        self.assertEqual(fraction, 1.0)  # different dimensions

    def test_render_substantive_undecodable(self) -> None:
        workspace = make_workspace()
        (workspace / "broken.png").write_bytes(b"not a png at all")
        result = visual.render_substantive(workspace, "broken.png")
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data["code"], "snapshot_undecodable")

    def test_viewport_parsing(self) -> None:
        self.assertEqual(visual._viewport("1280x800"), {"width": 1280, "height": 800})
        with self.assertRaises(ValueError):
            visual._viewport("wide")

    def test_not_a_png_raises(self) -> None:
        import tempfile

        path = Path(tempfile.mkdtemp()) / "bogus.png"
        path.write_bytes(b"definitely not a png")
        with self.assertRaises(ValueError):
            visual.decode_png(path)


class RenderSubstantiveTests(unittest.TestCase):
    def test_missing_snapshot_fails(self) -> None:
        workspace = make_workspace()
        result = visual.render_substantive(workspace, "missing.png")
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data["code"], "snapshot_missing")

    def test_blank_render_fails(self) -> None:
        workspace = make_workspace()
        solid_png(workspace / "render.png")
        result = visual.render_substantive(workspace, "render.png")
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data["code"], "blank_render")

    def test_substantive_render_passes(self) -> None:
        workspace = make_workspace()
        content_png(workspace / "render.png")
        result = visual.render_substantive(workspace, "render.png")
        self.assertEqual(result.status, "passed", result.detail)
        self.assertIn("stats", result.data)


# ---------------------------------------------------------------------------
# Browser validation (requires Playwright + Chromium)
# ---------------------------------------------------------------------------

def _pyqgis_available() -> bool:
    try:
        import qgis.core  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch()
            browser.close()
        return True
    except Exception:  # noqa: BLE001
        return False


DASHBOARD_BASE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{title}</title></head>
<body style="margin:0">
<div id="mapwrap">
<svg id="map" data-testid="map" width="400" height="300" xmlns="http://www.w3.org/2000/svg">
  <g data-layer-group="analysis"><rect x="20" y="20" width="120" height="90" fill="rgb(34,160,107)"/></g>
  <g data-layer-group="user_overrides">{override_features}</g>
  {scenario_group}
</svg>
</div>
<div id="sidebar">
{legend}{provenance}{warnings}
<section class="panel">
  <label><input type="checkbox" data-layer-group="analysis" checked> Analysis</label>
  <label><input type="checkbox" data-layer-group="user_overrides" checked> Overrides</label>
  {scenario_checkbox}
  <button data-testid="canonical-reset" id="reset" type="button">Reset</button>
</section>
</div>
<script>
function applyVisibility() {{
  document.querySelectorAll('g[data-layer-group]').forEach((g) => {{
    const cb = document.querySelector('input[data-layer-group="' + g.dataset.layerGroup + '"]');
    g.style.display = cb && cb.checked ? '' : 'none';
  }});
  document.querySelectorAll('g[data-scenario-group]').forEach((g) => {{
    const cb = document.querySelector('input[data-scenario="' + g.dataset.scenarioGroup + '"]');
    g.style.display = cb && cb.checked ? '' : 'none';
  }});
}}
const initial = {{}};
document.querySelectorAll('input[type="checkbox"]').forEach((cb) => {{
  initial[cb.dataset.layerGroup || cb.dataset.scenario] = cb.checked;
  cb.addEventListener('change', applyVisibility);
}});
document.getElementById('reset').addEventListener('click', () => {{
  document.querySelectorAll('input[type="checkbox"]').forEach((cb) => {{
    const key = cb.dataset.layerGroup || cb.dataset.scenario;
    if (key in initial) cb.checked = initial[key];
  }});
  applyVisibility();
}});
applyVisibility();
</script>
</body></html>
"""


def write_dashboard(workspace: Path, html: str, filename: str = "dashboard.html") -> Path:
    path = workspace / filename
    path.write_text(html, encoding="utf-8")
    return path


def manifest(
    *,
    legend: bool = True,
    provenance: bool = True,
    warnings: list | None = None,
    scenarios: list | None = None,
    groups: list | None = None,
    basemap: dict | None = None,
    canonical_reset: bool = True,
) -> dict:
    presentation: dict = {
        "legend": {"visible": legend},
        "provenance_ui": {"show_source_timestamp": provenance},
        "map": {"layers": [], "layer_groups": groups if groups is not None else [
            {"id": "analysis", "title": "Analysis"},
            {"id": "user_overrides", "title": "Overrides"},
        ], "basemap": basemap},
        "controls": {"canonical_reset": canonical_reset, "scenarios": scenarios or []},
    }
    return {
        "schema": "openmapstack-project/v1",
        "project": {"id": "t", "title": "t", "question": "q", "status": "validated",
                    "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z"},
        "presentation": presentation,
        "warnings": warnings or [],
    }


@unittest.skipUnless(_chromium_available(), "Playwright Chromium is not installed")
class DashboardBrowserTests(unittest.TestCase):
    def test_healthy_dashboard_passes_with_screenshots(self) -> None:
        workspace = make_workspace()
        # project_dir is a subdirectory (like real eval workspaces) so the
        # screenshots_dir "../shots" resolves inside the workspace.
        write_project(workspace, manifest(scenarios=[{"id": "planned-road", "override": "OVERRIDE-002"}]), project_dir="proj")
        write_dashboard(workspace / "proj", DASHBOARD_BASE.format(
            title="healthy",
            override_features='<circle cx="300" cy="60" r="10" fill="rgb(29,78,216)"/>',
            scenario_group='<g data-scenario-group="planned-road"><line x1="40" y1="280" x2="380" y2="150" stroke="rgb(217,131,36)" stroke-width="6"/></g>',
            legend='<div data-testid="legend">legend</div>',
            provenance='<div data-testid="provenance">provenance</div>',
            warnings="",
            scenario_checkbox='<label><input type="checkbox" data-scenario="planned-road" checked> Scenario</label>',
        ))
        # The runner passes the project directory itself as `workspace`.
        result = visual.dashboard_loads_in_browser(workspace / "proj", screenshots_dir="../shots")
        self.assertEqual(result.status, "passed", result.detail)
        screenshots = list((workspace / "shots").glob("*.png"))
        self.assertTrue(any("desktop" in p.name for p in screenshots))
        self.assertTrue(any("mobile" in p.name for p in screenshots))
        self.assertTrue(any("scenario-planned-road-before" in p.name for p in screenshots))

    def test_page_error_fails(self) -> None:
        workspace = make_workspace()
        write_project(workspace, manifest())
        write_dashboard(workspace, DASHBOARD_BASE.format(
            title="broken", override_features="", scenario_group="", legend="", provenance="",
            warnings="", scenario_checkbox="",
        ).replace("applyVisibility();", 'throw new Error("injected");'))
        result = visual.dashboard_loads_in_browser(workspace)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data["code"], "browser_page_error")

    def test_console_error_fails(self) -> None:
        workspace = make_workspace()
        write_project(workspace, manifest())
        write_dashboard(workspace, DASHBOARD_BASE.format(
            title="console", override_features="", scenario_group="", legend="", provenance="",
            warnings="", scenario_checkbox="",
        ).replace("applyVisibility();", 'console.error("injected console failure");'))
        result = visual.dashboard_loads_in_browser(workspace)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data["code"], "browser_console_error")

    def test_declared_warning_without_panel_fails(self) -> None:
        workspace = make_workspace()
        write_project(workspace, manifest(warnings=[{
            "id": "DATA-001", "severity": "medium", "issue": "completeness_unknown",
            "statement": "completeness cannot be established", "mitigation": "n/a",
        }]))
        write_dashboard(workspace, DASHBOARD_BASE.format(
            title="silent", override_features='<circle cx="300" cy="60" r="10" fill="rgb(29,78,216)"/>',
            scenario_group="", legend='<div data-testid="legend">legend</div>',
            provenance='<div data-testid="provenance">p</div>', warnings="", scenario_checkbox="",
        ))
        result = visual.dashboard_loads_in_browser(workspace)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data["code"], "warning_not_visible")
        self.assertTrue(any("DATA-001" in problem for problem in result.data["problems"]))

    def test_declared_warning_visible_passes(self) -> None:
        workspace = make_workspace()
        write_project(workspace, manifest(warnings=[{
            "id": "DATA-001", "severity": "medium", "issue": "completeness_unknown",
            "statement": "completeness cannot be established", "mitigation": "n/a",
        }]))
        write_dashboard(workspace, DASHBOARD_BASE.format(
            title="warned", override_features='<circle cx="300" cy="60" r="10" fill="rgb(29,78,216)"/>',
            scenario_group="", legend='<div data-testid="legend">legend</div>',
            provenance='<div data-testid="provenance">p</div>',
            warnings='<div data-testid="warnings"><ul><li><b>DATA-001</b> completeness_unknown</li></ul></div>',
            scenario_checkbox="",
        ))
        result = visual.dashboard_loads_in_browser(workspace)
        self.assertEqual(result.status, "passed", result.detail)

    def test_missing_legend_fails(self) -> None:
        workspace = make_workspace()
        write_project(workspace, manifest(legend=True))
        write_dashboard(workspace, DASHBOARD_BASE.format(
            title="nolegend", override_features="", scenario_group="",
            legend="", provenance='<div data-testid="provenance">p</div>', warnings="", scenario_checkbox="",
        ))
        result = visual.dashboard_loads_in_browser(workspace)
        self.assertEqual(result.status, "failed")
        self.assertTrue(any("legend" in problem for problem in result.data["problems"]))

    def test_missing_provenance_fails(self) -> None:
        workspace = make_workspace()
        write_project(workspace, manifest(provenance=True))
        write_dashboard(workspace, DASHBOARD_BASE.format(
            title="noprov", override_features="", scenario_group="",
            legend='<div data-testid="legend">legend</div>', provenance="", warnings="", scenario_checkbox="",
        ))
        result = visual.dashboard_loads_in_browser(workspace)
        self.assertEqual(result.status, "failed")
        self.assertTrue(any("provenance" in problem for problem in result.data["problems"]))

    def test_missing_map_element_fails(self) -> None:
        workspace = make_workspace()
        write_project(workspace, manifest())
        write_dashboard(workspace, DASHBOARD_BASE.format(
            title="nomap", override_features="", scenario_group="",
            legend='<div data-testid="legend">legend</div>',
            provenance='<div data-testid="provenance">p</div>', warnings="", scenario_checkbox="",
        ).replace('<svg id="map" data-testid="map" width="400" height="300" xmlns="http://www.w3.org/2000/svg">', '<svg id="notamap" width="400" height="300" xmlns="http://www.w3.org/2000/svg" style="display:none">'))
        result = visual.dashboard_loads_in_browser(workspace)
        self.assertEqual(result.status, "failed")
        self.assertTrue(any("map" in problem for problem in result.data["problems"]))

    def test_scenario_layer_indistinguishable_fails(self) -> None:
        workspace = make_workspace()
        write_project(workspace, manifest(scenarios=[{"id": "planned-road", "override": "OVERRIDE-002"}]))
        # The scenario checkbox exists but no scenario group: toggling it can
        # never change the map — exactly the "hidden scenario layer" defect.
        write_dashboard(workspace, DASHBOARD_BASE.format(
            title="invisible-scenario", override_features="", scenario_group="",
            legend='<div data-testid="legend">legend</div>',
            provenance='<div data-testid="provenance">p</div>', warnings="",
            scenario_checkbox='<label><input type="checkbox" data-scenario="planned-road" checked> Scenario</label>',
        ))
        result = visual.dashboard_loads_in_browser(workspace)
        self.assertEqual(result.status, "failed")
        self.assertTrue(any("scenario" in problem and "indistinguishable" in problem for problem in result.data["problems"]))

    def test_layer_group_without_effect_fails(self) -> None:
        workspace = make_workspace()
        write_project(workspace, manifest())
        html = DASHBOARD_BASE.format(
            title="static-group", override_features="", scenario_group="",
            legend='<div data-testid="legend">legend</div>',
            provenance='<div data-testid="provenance">p</div>', warnings="", scenario_checkbox="",
        )
        # Remove the analysis group's drawn content so toggling changes nothing.
        html = html.replace('<g data-layer-group="analysis"><rect x="20" y="20" width="120" height="90" fill="rgb(34,160,107)"/></g>', '<g data-layer-group="analysis"></g>')
        write_dashboard(workspace, html)
        result = visual.dashboard_loads_in_browser(workspace)
        self.assertEqual(result.status, "failed")
        self.assertTrue(any("analysis" in problem and "indistinguishable" in problem for problem in result.data["problems"]))

    def test_failed_toggle_screenshot_does_not_cascade(self) -> None:
        """A screenshot failure mid-toggle must restore the control.

        Unchecking `analysis` here hides the whole map element, so the
        post-toggle screenshot fails. If the check left the box unchecked,
        every later group would fail the same way; the report must name the
        one broken toggle, not a cascade.
        """
        workspace = make_workspace()
        write_project(workspace, manifest())
        html = DASHBOARD_BASE.format(
            title="vanishing-map", override_features='<circle cx="300" cy="60" r="20" fill="rgb(29,78,216)"/>',
            scenario_group="", legend='<div data-testid="legend">legend</div>',
            provenance='<div data-testid="provenance">p</div>', warnings="", scenario_checkbox="",
        ).replace(
            "applyVisibility();\n</script>",
            "document.querySelector('input[data-layer-group=\"analysis\"]').addEventListener('change', (e) => {"
            " document.getElementById('map').style.display = e.target.checked ? '' : 'none'; });\n"
            "applyVisibility();\n</script>",
        )
        write_dashboard(workspace, html)
        result = visual.dashboard_loads_in_browser(workspace)
        self.assertEqual(result.status, "failed")
        problems = result.data["problems"]
        self.assertEqual(
            [p for p in problems if p.startswith("layer group")],
            ["layer group analysis: no visible map element"],
            problems,
        )

    def test_never_settling_map_is_reported_not_read_as_a_working_toggle(self) -> None:
        """A map whose content keeps changing on its own — a raster basemap
        fading in, an animated layer — makes any two captures differ, which
        would read as "the toggle worked" for a toggle that does nothing.
        The comparison must report that it could not get a stable frame
        instead of scoring the difference it cannot attribute.
        """
        workspace = make_workspace()
        write_project(workspace, manifest())
        html = DASHBOARD_BASE.format(
            title="never-settles", override_features='<circle cx="300" cy="60" r="20" fill="rgb(29,78,216)"/>',
            scenario_group="", legend='<div data-testid="legend">legend</div>',
            provenance='<div data-testid="provenance">p</div>', warnings="", scenario_checkbox="",
        )
        # The analysis group draws nothing, so its toggle is dead, while a
        # separate rectangle repaints forever.
        html = html.replace(
            '<g data-layer-group="analysis"><rect x="20" y="20" width="120" height="90" fill="rgb(34,160,107)"/></g>',
            '<g data-layer-group="analysis"></g>'
            '<rect id="churn" x="150" y="150" width="140" height="120" fill="rgb(10,10,10)"/>',
        ).replace(
            "applyVisibility();\n</script>",
            # Repaint every animation frame with a continuously varying
            # colour: a fixed interval would alias against the capture
            # cadence and land on the same frame twice by luck.
            "let n = 0;"
            "(function churnFrame() {"
            "  const v = (n = (n + 17) % 256);"
            "  document.getElementById('churn').setAttribute('fill', 'rgb(' + v + ',' + v + ',' + v + ')');"
            "  requestAnimationFrame(churnFrame);"
            "})();\napplyVisibility();\n</script>",
        )
        write_dashboard(workspace, html)
        result = visual.dashboard_loads_in_browser(workspace)
        self.assertEqual(result.status, "failed", result.detail)
        self.assertTrue(
            any("analysis" in p and "never stopped changing" in p for p in result.data["problems"]),
            result.data["problems"],
        )

    def test_broken_reset_fails(self) -> None:
        workspace = make_workspace()
        write_project(workspace, manifest())
        html = DASHBOARD_BASE.format(
            title="badreset", override_features="", scenario_group="",
            legend='<div data-testid="legend">legend</div>',
            provenance='<div data-testid="provenance">p</div>', warnings="", scenario_checkbox="",
        )
        # Reset restores nothing: clicking it is a no-op for checkbox states.
        html = html.replace("if (key in initial) cb.checked = initial[key];", "if (false) cb.checked = initial[key];")
        write_dashboard(workspace, html)
        result = visual.dashboard_loads_in_browser(workspace)
        self.assertEqual(result.status, "failed")
        self.assertTrue(any("reset" in problem for problem in result.data["problems"]))

    def test_missing_reset_control_fails(self) -> None:
        workspace = make_workspace()
        write_project(workspace, manifest())
        html = DASHBOARD_BASE.format(
            title="noreset", override_features="", scenario_group="",
            legend='<div data-testid="legend">legend</div>',
            provenance='<div data-testid="provenance">p</div>', warnings="", scenario_checkbox="",
        ).replace('<button data-testid="canonical-reset" id="reset" type="button">Reset</button>', "")
        # Keep the script from throwing so we exercise the "no reset control"
        # detection rather than a page error.
        html = html.replace("document.getElementById('reset').addEventListener",
                            "(document.getElementById('reset')||{addEventListener(){}}).addEventListener")
        write_dashboard(workspace, html)
        result = visual.dashboard_loads_in_browser(workspace)
        self.assertEqual(result.status, "failed")
        self.assertTrue(any("reset" in problem for problem in result.data["problems"]))

    def test_dashboard_file_missing_fails(self) -> None:
        workspace = make_workspace()
        write_project(workspace, manifest())
        result = visual.dashboard_loads_in_browser(workspace)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data["code"], "file_missing")


@unittest.skipUnless(_chromium_available(), "Playwright Chromium is not installed")
class BasemapBrowserTests(unittest.TestCase):
    """The manifest-declared interactive background map (OSM/Carto/... tiles)
    must really be present: tile requests fired, attribution visible."""

    def _serve_tiles(self):
        import http.server
        import threading

        tile_png = encode_png(1, 1, [bytes((200, 200, 200, 255))])
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                outer.tile_requests.append(self.path)
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(tile_png)))
                self.end_headers()
                self.wfile.write(tile_png)

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.tile_requests = []
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}/{{z}}/{{x}}/{{y}}.png"

    def basemap(self, tiles_url):
        return {
            "id": "test-tiles",
            "kind": "raster-xyz",
            "tiles": [tiles_url],
            "attribution": "© Test tile provider",
            "default_visible": True,
        }

    def healthy_basemap_html(self, tiles_url, *, include_img=True, include_attribution=True, include_canvas=True):
        canvas = ('<canvas id="map" data-testid="map" width="200" height="150">'
                  '</canvas><script>const c=document.querySelector("canvas");'
                  'const g=c.getContext("2d");g.fillStyle="#34a06b";g.fillRect(10,10,100,60);'
                  'g.fillStyle="#1d4ed8";g.beginPath();g.arc(160,110,8,0,7);g.fill();</script>'
                  ) if include_canvas else '<div data-testid="map" style="display:none"></div>'
        img = f'<img src="{tiles_url.replace("{z}/{x}/{y}.png", "0/0/0.png")}" alt="">' if include_img else ""
        attribution = '<div class="maplibregl-ctrl-attrib">© Test tile provider</div>' if include_attribution else ""
        return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>t</title></head>
<body style="margin:0">
<div id="mapwrap">{canvas}{img}</div>
<div data-testid="legend">legend</div>
<div data-testid="provenance">provenance</div>
{attribution}
</body></html>"""

    def test_declared_basemap_with_tiles_attribution_and_canvas_passes(self) -> None:
        workspace = make_workspace()
        tiles_url = self._serve_tiles()
        write_project(workspace, manifest(basemap=self.basemap(tiles_url), groups=[], canonical_reset=False))
        write_dashboard(workspace, self.healthy_basemap_html(tiles_url))
        result = visual.dashboard_loads_in_browser(workspace)
        self.assertEqual(result.status, "passed", result.detail)
        self.assertGreater(len(self.tile_requests), 0)

    def test_basemap_without_tile_requests_fails(self) -> None:
        workspace = make_workspace()
        tiles_url = self._serve_tiles()
        write_project(workspace, manifest(basemap=self.basemap(tiles_url)))
        write_dashboard(workspace, self.healthy_basemap_html(tiles_url, include_img=False))
        result = visual.dashboard_loads_in_browser(workspace)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data["code"], "basemap_absent")
        self.assertTrue(any("never" in problem and "tiles" in problem for problem in result.data["problems"]))

    def test_basemap_without_attribution_fails(self) -> None:
        workspace = make_workspace()
        tiles_url = self._serve_tiles()
        write_project(workspace, manifest(basemap=self.basemap(tiles_url)))
        write_dashboard(workspace, self.healthy_basemap_html(tiles_url, include_attribution=False))
        result = visual.dashboard_loads_in_browser(workspace)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data["code"], "basemap_absent")
        self.assertTrue(any("attribution" in problem for problem in result.data["problems"]))

    def test_basemap_without_interactive_canvas_fails(self) -> None:
        workspace = make_workspace()
        tiles_url = self._serve_tiles()
        write_project(workspace, manifest(basemap=self.basemap(tiles_url)))
        write_dashboard(workspace, self.healthy_basemap_html(tiles_url, include_canvas=False))
        result = visual.dashboard_loads_in_browser(workspace)
        self.assertEqual(result.status, "failed")
        # There is no visible map element at all here, so that — not the
        # downstream basemap complaint — is the primary code. Both defects
        # are still reported, each carrying the code of the check that
        # found it.
        self.assertEqual(result.data["code"], "map_absent")
        self.assertIn("basemap_absent", result.data["problem_codes"])
        self.assertTrue(any("interactive map canvas" in problem for problem in result.data["problems"]))


class PlaywrightUnavailableTests(unittest.TestCase):
    def test_missing_playwright_is_not_testable(self) -> None:
        workspace = make_workspace()
        write_project(workspace, manifest())
        write_dashboard(workspace, DASHBOARD_BASE.format(
            title="x", override_features="", scenario_group="", legend="",
            provenance='<div data-testid="provenance">p</div>', warnings="", scenario_checkbox="",
        ))
        original = visual._playwright

        def _raise():
            raise ImportError("No module named 'playwright'")

        visual._playwright = _raise
        try:
            result = visual.dashboard_loads_in_browser(workspace)
        finally:
            visual._playwright = original
        self.assertEqual(result.status, "not_testable")
        self.assertEqual(result.data["code"], "playwright_unavailable")


@unittest.skipUnless(_chromium_available(), "Playwright Chromium is not installed")
class BrowserErrorClassificationTests(unittest.TestCase):
    """`not_testable` is reserved for an environment that cannot run the
    check. Anything that goes wrong after the dashboard is open is evidence
    about the product and must be graded as a failure."""

    def test_crash_after_dashboard_opens_is_a_failure(self) -> None:
        workspace = make_workspace()
        write_project(workspace, manifest())
        write_dashboard(workspace, DASHBOARD_BASE.format(
            title="x", override_features="", scenario_group="",
            legend='<div data-testid="legend">legend</div>',
            provenance='<div data-testid="provenance">p</div>', warnings="", scenario_checkbox="",
        ))
        original = visual._checkbox_states

        def _raise(page):
            raise RuntimeError("simulated mid-inspection crash")

        visual._checkbox_states = _raise
        try:
            result = visual.dashboard_loads_in_browser(workspace)
        finally:
            visual._checkbox_states = original
        self.assertEqual(result.status, "failed", result.detail)
        self.assertEqual(result.data["code"], "browser_check_error")
        self.assertIn("simulated mid-inspection crash", result.detail)


# ---------------------------------------------------------------------------
# QGIS static assertions added by PR 7 (no PyQGIS needed)
# ---------------------------------------------------------------------------

def _write_qgz(workspace: Path, xml: str) -> None:
    with zipfile.ZipFile(workspace / "project.qgz", "w") as zf:
        zf.writestr("project.qgs", xml)


QGS_XML = """<?xml version="1.0"?>
<qgis version="3.44.0" projectname="">
 <layer-tree-group>
  <layer-tree-group name="analysis" checked="Qt::Checked" expanded="1">
   <layer-tree-layer source="./data/a.geojson|layername=a" name="A" id="a1" checked="Qt::Checked"/>
  </layer-tree-group>
  <layer-tree-group name="user_overrides" checked="Qt::Checked" expanded="1">
   <layer-tree-layer source="./data/b.geojson|layername=b" name="B" id="b1" checked="Qt::Checked"/>
  </layer-tree-group>
 </layer-tree-group>
 <projectlayers>
  <maplayer type="vector" geometry="Polygon">
   <id>a1</id><layername>A</layername>
   <datasource>./data/a.geojson|layername=a</datasource>
   <provider encoding="UTF-8">ogr</provider>
   <renderer-v2 type="singleSymbol" symbollevels="0"><symbols></symbols></renderer-v2>
  </maplayer>
  <maplayer type="vector" geometry="Point">
   <id>b1</id><layername>B</layername>
   <datasource>./data/b.geojson|layername=b</datasource>
   <provider encoding="UTF-8">ogr</provider>
   <renderer-v2 type="singleSymbol" symbollevels="0"><symbols></symbols></renderer-v2>
  </maplayer>
 </projectlayers>
</qgis>
"""

from openmapstack.checks.qgis import groups_match_manifest, styles_declared  # noqa: E402


class QgisStaticVisualTests(unittest.TestCase):
    def test_styles_and_groups_pass(self) -> None:
        workspace = make_workspace()
        write_project(workspace, manifest())
        _write_qgz(workspace, QGS_XML)
        self.assertEqual(styles_declared(workspace).status, "passed")
        self.assertEqual(groups_match_manifest(workspace).status, "passed")

    def test_styles_declared_error_paths(self) -> None:
        import tempfile

        workspace = make_workspace()
        write_project(workspace, manifest())
        # Not a zip.
        (workspace / "project.qgz").write_bytes(b"nope")
        result = styles_declared(workspace)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data["code"], "not_a_zip")
        # A zip without a .qgs document.
        with zipfile.ZipFile(workspace / "project.qgz", "w") as zf:
            zf.writestr("other.txt", "x")
        result = styles_declared(workspace)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data["code"], "no_qgs_document")
        # A .qgs document without maplayers.
        with zipfile.ZipFile(workspace / "project.qgz", "w") as zf:
            zf.writestr("project.qgs", "<?xml version=\"1.0\"?><qgis></qgis>")
        result = styles_declared(workspace)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data["code"], "no_layers")
        # Missing manifest.
        empty = make_workspace()
        (empty / "project.qgz").write_bytes(b"nope")
        result = styles_declared(empty)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data["code"], "not_a_zip")

    def test_groups_match_manifest_error_paths(self) -> None:
        workspace = make_workspace()
        write_project(workspace, manifest())
        (workspace / "project.qgz").write_bytes(b"nope")
        result = groups_match_manifest(workspace)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data["code"], "not_a_zip")
        # No manifest at all.
        empty = make_workspace()
        result = groups_match_manifest(empty)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data["code"], "manifest_missing")
        # Manifest without layer groups: vacuous pass.
        write_project(empty, manifest(groups=[]))
        result = groups_match_manifest(empty)
        self.assertEqual(result.status, "passed")

    def test_unstyled_layer_fails(self) -> None:
        workspace = make_workspace()
        write_project(workspace, manifest())
        _write_qgz(workspace, QGS_XML.replace(
            '<renderer-v2 type="singleSymbol" symbollevels="0"><symbols></symbols></renderer-v2>\n  </maplayer>\n  <maplayer type="vector" geometry="Point">',
            "</maplayer>\n  <maplayer type=\"vector\" geometry=\"Point\">",
            1,
        ))
        result = styles_declared(workspace)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data["code"], "missing_layer_style")

    def test_manifest_group_missing_from_qgis_fails(self) -> None:
        workspace = make_workspace()
        write_project(workspace, manifest(groups=[
            {"id": "analysis", "title": "Analysis"},
            {"id": "user_overrides", "title": "Overrides"},
            {"id": "scenario", "title": "Scenario"},
        ]))
        _write_qgz(workspace, QGS_XML)
        result = groups_match_manifest(workspace)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data["code"], "layer_group_missing_from_qgis")

    def test_tree_groups_may_be_named_by_title(self) -> None:
        """QGIS layer trees are authored for readers, and the spec's own
        examples name groups by title. Matching only the raw id would fail
        projects that follow the spec, so id or title both satisfy it —
        case- and separator-insensitively."""
        workspace = make_workspace()
        write_project(workspace, manifest(groups=[
            {"id": "analysis", "title": "Analysis Results"},
            {"id": "user_overrides", "title": "Manual additions"},
        ]))
        _write_qgz(workspace, QGS_XML
                   .replace('name="analysis"', 'name="Analysis Results"')
                   .replace('name="user_overrides"', 'name="Manual-Additions"'))
        result = groups_match_manifest(workspace)
        self.assertEqual(result.status, "passed", result.detail)

    def test_unrelated_tree_group_name_still_fails(self) -> None:
        workspace = make_workspace()
        write_project(workspace, manifest(groups=[
            {"id": "analysis", "title": "Analysis Results"},
            {"id": "user_overrides", "title": "Manual additions"},
        ]))
        _write_qgz(workspace, QGS_XML.replace('name="user_overrides"', 'name="Extras"'))
        result = groups_match_manifest(workspace)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data["code"], "layer_group_missing_from_qgis")
        self.assertEqual(result.data["missing"], ["user_overrides"])

    def test_manifest_group_missing_fails_when_file_absent(self) -> None:
        workspace = make_workspace()
        write_project(workspace, manifest())
        result = groups_match_manifest(workspace)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data["code"], "file_missing")




class ManifestLayerResolutionTests(unittest.TestCase):
    """`_manifest_layer_files` maps a manifest layer's `source` key onto the
    files a QGIS project should be carrying."""

    def test_only_known_format_suffixes_are_stripped(self) -> None:
        from openmapstack.checks.qgis import _manifest_layer_files

        resolved = _manifest_layer_files({
            "outputs": {
                "candidate_parcels": {"path": "data/derived/candidate-parcels.parquet"},
                "candidate_parcels_geojson": {"path": "data/derived/candidate-parcels.geojson"},
                "education_pois": {"path": "data/derived/education_pois.geojson"},
            },
        })
        # The format variant resolves to its base dataset key...
        self.assertEqual(
            sorted(resolved["candidate_parcels"]),
            ["data/derived/candidate-parcels.geojson", "data/derived/candidate-parcels.parquet"],
        )
        # ...but a plain underscore in a dataset name is not a format suffix,
        # so `education_pois` must not also claim the key `education`.
        self.assertNotIn("education", resolved)

    def test_override_geometry_files_resolve_by_layer_id(self) -> None:
        from openmapstack.checks.qgis import _manifest_layer_files

        resolved = _manifest_layer_files({
            "outputs": {},
            "overrides": [{"layer": "planned_roads", "geometry_file": {"path": "data/overrides/planned-road.geojson"}}],
        })
        self.assertEqual(resolved["planned_roads"], ["data/overrides/planned-road.geojson"])


class RemoteBasemapSourceTests(unittest.TestCase):
    def test_recognises_xyz_and_wms_provider_uris(self) -> None:
        from openmapstack.checks.qgis import _is_remote_basemap_source

        self.assertTrue(_is_remote_basemap_source(
            "type=xyz&url=https://tile.openstreetmap.org/{z}/{x}/{y}.png&zmax=19"))
        # QGIS emits provider URI keys in no guaranteed order: a real WMS
        # layer commonly starts with crs=, not url=.
        self.assertTrue(_is_remote_basemap_source(
            "crs=EPSG:3301&format=image/png&layers=BAASKAART&styles&url=https://gis.example/wms"))
        self.assertFalse(_is_remote_basemap_source("./data/derived/candidate-parcels.geojson"))




class ProblemCodeTests(unittest.TestCase):
    """Codes come from the check that found the defect, not from matching
    words in the human-readable message."""

    def test_primary_code_is_the_first_recorded_code(self) -> None:
        problems = visual._Problems()
        self.assertFalse(problems)
        self.assertEqual(problems.primary_code, "dashboard_visual_failure")
        problems.add("blank_map", "map renders blank on desktop")
        problems.add("legend_absent", "no legend is rendered")
        self.assertTrue(problems)
        self.assertEqual(problems.primary_code, "blank_map")
        self.assertEqual(problems.codes, ["blank_map", "legend_absent"])

    def test_a_group_named_basemap_does_not_borrow_the_basemap_code(self) -> None:
        # The old substring inference read "basemap" out of this message and
        # reported `basemap_absent` for a broken layer toggle.
        problems = visual._Problems()
        problems.add("layer_group_not_rendered", "layer group basemap: toggle does not change the rendered map")
        self.assertEqual(problems.primary_code, "layer_group_not_rendered")




class EveryDeclaredLayerRendersTests(unittest.TestCase):
    """The PyQGIS-dependent body runs only in the integration container;
    these cover the paths reachable without it."""

    @unittest.skipIf(_pyqgis_available(), "PyQGIS present: the manifest path is exercised for real")
    def test_without_pyqgis_it_is_not_testable(self) -> None:
        from openmapstack.checks.qgis import every_declared_layer_renders

        workspace = make_workspace()
        write_project(workspace, manifest())
        result = every_declared_layer_renders(workspace)
        # In an interpreter that does have PyQGIS this reports on the real
        # project instead; either way it must never be a silent pass.
        self.assertIn(result.status, {"not_testable", "failed"})
        if result.status == "not_testable":
            self.assertEqual(result.data["code"], "pyqgis_unavailable")
        else:
            self.assertEqual(result.data["code"], "file_missing")

    @unittest.skipIf(_pyqgis_available(), "PyQGIS present: the manifest path is exercised for real")
    def test_missing_manifest_is_reported(self) -> None:
        from openmapstack.checks.qgis import every_declared_layer_renders

        result = every_declared_layer_renders(make_workspace())
        self.assertEqual(result.status, "not_testable")




class EveryLayerDeclaresCrsTests(unittest.TestCase):
    """A layer with no declared CRS is assumed to be in the project CRS and
    never reprojected — the failure that painted a Web Mercator basemap
    1500 km from an Estonian project's data."""

    _SRS = "<srs><spatialrefsys><authid>EPSG:3301</authid></spatialrefsys></srs>"

    def _qgz(self, workspace: Path, *layers: str) -> None:
        body = "".join(layers)
        _write_qgz(workspace, f'<?xml version="1.0"?><qgis><projectlayers>{body}</projectlayers></qgis>')

    def test_all_layers_with_crs_pass(self) -> None:
        from openmapstack.checks.qgis import every_layer_declares_crs

        workspace = make_workspace()
        self._qgz(
            workspace,
            f"<maplayer><layername>parcels</layername>{self._SRS}</maplayer>",
            '<maplayer><layername>basemap</layername>'
            "<srs><spatialrefsys><authid>EPSG:3857</authid></spatialrefsys></srs></maplayer>",
        )
        result = every_layer_declares_crs(workspace)
        self.assertEqual(result.status, "passed", result.detail)
        self.assertEqual(result.data["declared"], {"parcels": "EPSG:3301", "basemap": "EPSG:3857"})

    def test_layer_without_crs_fails(self) -> None:
        from openmapstack.checks.qgis import every_layer_declares_crs

        workspace = make_workspace()
        self._qgz(
            workspace,
            f"<maplayer><layername>parcels</layername>{self._SRS}</maplayer>",
            # A tiled basemap with no <srs>: exactly the reference-project bug.
            "<maplayer><layername>OpenStreetMap (background)</layername>"
            "<datasource>type=xyz&amp;url=https://tile.openstreetmap.org/{z}/{x}/{y}.png</datasource>"
            "</maplayer>",
        )
        result = every_layer_declares_crs(workspace)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data["code"], "layer_crs_undeclared")
        self.assertEqual(result.data["missing"], ["OpenStreetMap (background)"])

    def test_error_paths(self) -> None:
        from openmapstack.checks.qgis import every_layer_declares_crs

        workspace = make_workspace()
        result = every_layer_declares_crs(workspace)
        self.assertEqual(result.data["code"], "file_missing")
        (workspace / "project.qgz").write_bytes(b"nope")
        self.assertEqual(every_layer_declares_crs(workspace).data["code"], "not_a_zip")
        _write_qgz(workspace, '<?xml version="1.0"?><qgis></qgis>')
        self.assertEqual(every_layer_declares_crs(workspace).data["code"], "no_layers")


if __name__ == "__main__":
    unittest.main()
