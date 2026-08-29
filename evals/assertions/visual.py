"""Visual integration assertions: rendered-map substantiveness and live
browser validation of the generated dashboard.

PR 7 of the eval epic. These checks supplement — never replace — the
deterministic structural assertions: a dashboard that loads cleanly but
shows an empty map, hides a declared warning, or renders a scenario layer
indistinguishable from the baseline must fail here.

Browser checks require Playwright with a Chromium install and return
``not_testable`` (never a silent pass) when the execution environment lacks
them. PNG analysis is stdlib-only so it runs anywhere, including offline
fixture CI.
"""

from __future__ import annotations

import json
import re
import tempfile
import struct
import zlib
from pathlib import Path
from typing import Any

from . import AssertionResult, failed, get_in, load_project_yaml, not_testable, passed, project_root

# A rendered map is considered blank when fewer than this fraction of
# pixels differ from the modal (background) color. Genuine sparse vector
# content - a small parcel in a generous frame, a thin road line - still
# contributes at least ~0.05% ink; a truly blank render (missing layers,
# collapsed extent, displaced CRS) contributes none beyond encoder noise.
_BLANK_MAX_NON_MODAL_FRACTION = 0.0002

# Two screenshots count as "the same image" when fewer than this fraction
# of pixels differ. Headless Chromium renders identical content
# deterministically, so the threshold only absorbs encoder noise; it must
# stay far below the ink of even the smallest declared feature.
_SAME_IMAGE_DIFF_FRACTION = 0.00005


# ---------------------------------------------------------------------------
# Minimal PNG decoding (stdlib only — no Pillow/numpy dependency)
# ---------------------------------------------------------------------------

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def decode_png(path: str | Path) -> tuple[int, int, int, list[bytes]]:
    """Decode an 8-bit PNG into ``(width, height, bytes_per_pixel, rows)``.

    Supports color types 0 (gray), 2 (RGB), 4 (gray+alpha) and 6 (RGBA) —
    everything Chromium screenshots and Qt rendered images produce — with
    all five scanline filters. Anything else raises ValueError.
    """
    data = Path(path).read_bytes()
    if not data.startswith(_PNG_SIGNATURE):
        raise ValueError("not a PNG file")
    pos = len(_PNG_SIGNATURE)
    width = height = bit_depth = color_type = 0
    idat = bytearray()
    while pos + 8 <= len(data):
        length, chunk = struct.unpack(">I4s", data[pos:pos + 8])
        pos += 8
        chunk_data = data[pos:pos + length]
        pos += length + 4  # skip CRC
        if chunk == b"IHDR":
            width, height, bit_depth, color_type = struct.unpack(">IIBB", chunk_data[:10])
        elif chunk == b"IDAT":
            idat.extend(chunk_data)
        elif chunk == b"IEND":
            break
    if bit_depth != 8:
        raise ValueError(f"unsupported bit depth {bit_depth}")
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color_type)
    if channels is None:
        raise ValueError(f"unsupported color type {color_type}")

    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error as exc:
        raise ValueError(f"corrupt or truncated PNG data: {exc}") from exc
    stride = width * channels
    bpp = channels  # filter offset equals channel count for 8-bit depth
    rows: list[bytes] = []
    prev = bytearray(stride)
    cursor = 0
    for _ in range(height):
        if cursor >= len(raw):
            raise ValueError("truncated PNG data")
        filter_type = raw[cursor]
        cursor += 1
        line = bytearray(raw[cursor:cursor + stride])
        cursor += stride
        if len(line) != stride:
            raise ValueError("truncated PNG scanline")
        if filter_type == 1:  # Sub
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif filter_type == 2:  # Up
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif filter_type == 3:  # Average
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif filter_type == 4:  # Paeth
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                up_left = prev[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + _paeth(left, prev[i], up_left)) & 0xFF
        elif filter_type != 0:
            raise ValueError(f"unsupported filter type {filter_type}")
        rows.append(bytes(line))
        prev = line
    return width, height, channels, rows


def image_stats(path: Path) -> dict[str, Any]:
    """Coarse color statistics of a rendered image, robust to encoder noise.

    Colors are quantized to 5 bits per channel before counting so that
    antialiasing dithering cannot make a blank render look "substantive".
    """
    width, height, channels, rows = decode_png(path)
    counts: dict[int, int] = {}
    total = width * height
    for row in rows:
        for i in range(0, len(row), channels):
            key = (row[i] >> 3) << 10 | (row[i + 1] >> 3) << 5 | (row[i + 2] >> 3)
            counts[key] = counts.get(key, 0) + 1
    modal = max(counts.values()) if counts else 0
    return {
        "width": width,
        "height": height,
        "distinct_colors_quantized": len(counts),
        "modal_color_fraction": round(modal / total, 6) if total else 1.0,
        "non_modal_fraction": round(1 - modal / total, 6) if total else 0.0,
    }


def images_differ(path_a: Path, path_b: Path) -> tuple[bool, float]:
    """Compare two decoded images; returns ``(differ, differing_fraction)``.

    Images with different dimensions always differ.
    """
    wa, ha, ca, rows_a = decode_png(path_a)
    wb, hb, cb, rows_b = decode_png(path_b)
    if (wa, ha, ca) != (wb, hb, cb):
        return True, 1.0
    differing = 0
    total = wa * ha
    for row_a, row_b in zip(rows_a, rows_b):
        if row_a == row_b:
            continue
        for i in range(0, len(row_a), ca):
            if row_a[i:i + ca] != row_b[i:i + ca]:
                differing += 1
    fraction = differing / total if total else 0.0
    return fraction > _SAME_IMAGE_DIFF_FRACTION, fraction


def _is_blank(stats: dict[str, Any]) -> bool:
    return (
        stats["non_modal_fraction"] < _BLANK_MAX_NON_MODAL_FRACTION
        or stats["distinct_colors_quantized"] < 2
    )


# ---------------------------------------------------------------------------
# render_substantive: a rendered PNG must show actual map content
# ---------------------------------------------------------------------------

def render_substantive(workspace: Path, path: str, project_dir: str = ".") -> AssertionResult:
    """A rendered map snapshot (PyQGIS render or dashboard screenshot) must
    contain real drawn content. Detects the empty-map failure mode: a valid
    project that renders to a single background color, whether from missing
    layers, a collapsed extent, gross CRS displacement, or styling that
    paints nothing."""
    image_path = project_root(workspace, project_dir) / path
    if not image_path.exists():
        return failed(f"rendered snapshot {path} does not exist", code="snapshot_missing")
    try:
        stats = image_stats(image_path)
    except ValueError as exc:
        return failed(f"snapshot {path} is not decodable: {exc}", code="snapshot_undecodable")
    if _is_blank(stats):
        return failed(
            f"rendered snapshot {path} is blank ({stats['modal_color_fraction']:.1%} one color, "
            f"{stats['distinct_colors_quantized']} quantized colors)",
            code="blank_render",
            stats=stats,
        )
    return passed(
        f"rendered snapshot {path} shows substantive content "
        f"({stats['distinct_colors_quantized']} quantized colors, "
        f"{stats['non_modal_fraction']:.1%} non-background)",
        stats=stats,
    )


# ---------------------------------------------------------------------------
# dashboard_loads_in_browser: live headless-browser validation
# ---------------------------------------------------------------------------

def _playwright():
    from playwright.sync_api import sync_playwright  # type: ignore

    return sync_playwright


_MAP_SELECTOR = '[data-testid="map"], #map, .maplibregl-map, canvas'
_LEGEND_SELECTOR = '[data-testid="legend"], #legend, .legend'
_PROVENANCE_SELECTOR = '[data-testid="provenance"], #provenance, .provenance'
_WARNINGS_SELECTOR = '[data-testid="warnings"], #warnings, .warnings'
_RESET_SELECTOR = '[data-testid="canonical-reset"], #reset'


def _first_visible(page: Any, selector: str) -> Any | None:
    for element in page.query_selector_all(selector):
        try:
            if element.bounding_box() and element.bounding_box()["width"] > 0:
                return element
        except Exception:  # noqa: BLE001
            continue
    return None


def _screenshot_map(page: Any, output_path: Path) -> str | None:
    element = _first_visible(page, _MAP_SELECTOR)
    if element is None:
        return "no visible map element"
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        element.screenshot(path=str(output_path))
    except Exception as exc:  # noqa: BLE001
        return f"map screenshot failed: {exc}"
    return None


def _settle(page: Any, settle_ms: int) -> None:
    """Wait for background-map tiles and render to settle: prefer the page's
    network going idle (tiles, CDN), then a fixed grace period. Best effort —
    an environment without network simply uses the grace period."""
    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:  # noqa: BLE001
        pass
    page.wait_for_timeout(settle_ms)


_PROBLEM_CODES = [
    ("basemap", "basemap_absent"),
    ("warning", "warning_not_visible"),
    ("legend", "legend_absent"),
    ("provenance", "provenance_absent"),
    ("reset", "canonical_reset_failed"),
    ("scenario", "scenario_layer_indistinguishable"),
    ("blank", "blank_map"),
    ("toggle", "layer_group_not_rendered"),
    ("layer group", "layer_group_not_rendered"),
]


def _primary_code(problems: list[str]) -> str:
    """Derive a stable machine-readable code from the first problem's
    category so mutation cases can pin the exact defect they inject."""
    for problem in problems:
        lowered = problem.lower()
        for needle, code in _PROBLEM_CODES:
            if needle in lowered:
                return code
    return "dashboard_visual_failure"


def _checkbox_states(page: Any) -> dict[str, bool]:
    return page.evaluate(
        """() => Object.fromEntries(
            [...document.querySelectorAll('input[type="checkbox"]')]
            .map(cb => [cb.dataset.layerGroup || cb.dataset.scenario || cb.id || cb.name || '', cb.checked])
        )"""
    )


def dashboard_loads_in_browser(
    workspace: Path,
    project_dir: str = ".",
    dashboard: str = "dashboard.html",
    screenshots_dir: str | None = None,
    desktop_size: str = "1280x800",
    mobile_size: str = "390x844",
    settle_ms: int = 800,
) -> AssertionResult:
    """Open the generated dashboard in headless Chromium and verify the
    manifest's presentation claims against the actually rendered product.

    Fails on page/console errors, absent map, blank map, absent
    legend/provenance panels that the manifest declares visible, manifest
    warnings not visible in the product, layer controls missing or not
    affecting the render, a scenario control whose layer is
    indistinguishable from the baseline, and a broken canonical reset.
    Captures desktop and mobile screenshots as retained evidence.
    """
    proj = load_project_yaml(workspace, project_dir)
    if proj is None:
        return failed("project.yaml missing", code="manifest_missing")
    dashboard_path = project_root(workspace, project_dir) / dashboard
    if not dashboard_path.exists():
        return failed(f"{dashboard} does not exist", code="file_missing")

    try:
        sync_playwright = _playwright()
    except ImportError:
        return not_testable(
            "Playwright is not installed in this execution environment", code="playwright_unavailable"
        )

    label = re.sub(r"[^A-Za-z0-9_.-]+", "_", project_dir.strip("./")) or "project"

    warnings = proj.get("warnings") or []
    layer_groups = get_in(proj, "presentation.map.layer_groups", []) or []
    scenarios = get_in(proj, "presentation.controls.scenarios", []) or []
    legend_visible = bool(get_in(proj, "presentation.legend.visible"))
    provenance_declared = bool(get_in(proj, "presentation.provenance_ui"))
    canonical_reset = bool(get_in(proj, "presentation.controls.canonical_reset"))

    # Screenshot comparisons (toggle effects, scenario distinguishability,
    # blank-map detection) always run. When no retained screenshots_dir is
    # declared, a throwaway temp directory holds the intermediate frames.
    tmp_context = None
    if screenshots_dir:
        screenshot_dir = workspace / screenshots_dir
    else:
        tmp_context = tempfile.TemporaryDirectory(prefix="openmapstack-visual-")
        screenshot_dir = Path(tmp_context.name)

    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch()
            except Exception as exc:  # noqa: BLE001
                return not_testable(
                    f"headless browser unavailable in this environment: {exc}",
                    code="browser_unavailable",
                )
            try:
                context = browser.new_context(viewport=_viewport(desktop_size))
                page = context.new_page()
                page_errors: list[str] = []
                console_errors: list[str] = []
                requested_urls: list[str] = []
                page.on("pageerror", lambda exc: page_errors.append(str(exc)))
                page.on(
                    "console",
                    lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
                )
                page.on("request", lambda request: requested_urls.append(request.url))
                page.goto(dashboard_path.as_uri())
                _settle(page, settle_ms)

                if page_errors:
                    return failed(
                        f"dashboard raised {len(page_errors)} page error(s): {page_errors[:3]}",
                        code="browser_page_error",
                        errors=page_errors,
                    )
                if console_errors:
                    return failed(
                        f"dashboard logged {len(console_errors)} console error(s): {console_errors[:3]}",
                        code="browser_console_error",
                        errors=console_errors,
                    )

                problems: list[str] = []
                evidence: dict[str, Any] = {}

                # --- map present and substantive -------------------------
                if _first_visible(page, _MAP_SELECTOR) is None:
                    problems.append("no visible map element")
                else:
                    shot = screenshot_dir / f"{label}-desktop.png" if screenshot_dir else None
                    if shot is not None:
                        error = _screenshot_map(page, shot)
                        if error:
                            problems.append(f"desktop map screenshot: {error}")
                        else:
                            stats = image_stats(shot)
                            evidence["desktop_map_stats"] = stats
                            if _is_blank(stats):
                                problems.append("map renders blank on desktop")

                # --- declared panels actually visible --------------------
                if legend_visible and _first_visible(page, _LEGEND_SELECTOR) is None:
                    problems.append("manifest declares legend visible but no legend is rendered")
                if provenance_declared and _first_visible(page, _PROVENANCE_SELECTOR) is None:
                    problems.append("manifest declares provenance_ui but no provenance panel is rendered")
                if warnings:
                    panel = _first_visible(page, _WARNINGS_SELECTOR)
                    body_text = page.inner_text("body")
                    for w in warnings:
                        warning_id = str(w.get("id", ""))
                        if panel is None:
                            problems.append(f"manifest warning {warning_id} has no visible warning panel")
                            break
                        if warning_id and warning_id not in body_text:
                            problems.append(f"manifest warning {warning_id} not visible in the rendered product")

                # --- declared interactive basemap is real -----------------
                basemap = get_in(proj, "presentation.map.basemap")
                if basemap:
                    # Match any tile under the basemap's URL template:
                    # "https://host/{z}/{x}/{y}.png" -> "https://host/".
                    tile_prefix = ((basemap.get("tiles") or [""])[0] or "").split("{z}")[0]
                    tile_requests = [url for url in requested_urls if tile_prefix and url.startswith(tile_prefix)]
                    if _first_visible(page, f'{_MAP_SELECTOR}, .maplibregl-canvas') is None:
                        problems.append("manifest declares a basemap but no interactive map canvas is rendered")
                    if not tile_requests:
                        problems.append(
                            f"manifest declares basemap {basemap.get('id')!r} but the product never "
                            f"requested its tiles ({tile_prefix}/...) — the background map is not interactive"
                        )
                    attribution = basemap.get("attribution")
                    if attribution and attribution not in page.inner_text("body"):
                        problems.append(
                            f"basemap attribution {attribution!r} required by the manifest is not visible "
                            "in the rendered product"
                        )

                # --- layer toggles must affect the render ----------------
                checkboxes = page.query_selector_all('input[type="checkbox"][data-layer-group]')
                if layer_groups and not checkboxes:
                    problems.append("manifest declares layer groups but the product has no layer toggles")
                initial_states = _checkbox_states(page)
                for group in layer_groups:
                    group_id = group.get("id")
                    control = page.query_selector(f'input[type="checkbox"][data-layer-group="{group_id}"]')
                    if control is None:
                        problems.append(f"layer group {group_id} has no toggle control")
                        continue
                    baseline = screenshot_dir / f"{label}-group-{group_id}-before.png" if screenshot_dir else None
                    toggled = screenshot_dir / f"{label}-group-{group_id}-after.png" if screenshot_dir else None
                    if baseline is None:
                        continue
                    error = _screenshot_map(page, baseline)
                    if error:
                        problems.append(f"layer group {group_id}: {error}")
                        continue
                    control.uncheck()
                    _settle(page, settle_ms)
                    error = _screenshot_map(page, toggled)
                    if error:
                        problems.append(f"layer group {group_id}: {error}")
                        continue
                    differ, fraction = images_differ(baseline, toggled)
                    if not differ:
                        problems.append(
                            f"layer group {group_id} toggle does not change the rendered map "
                            f"(layer absent or indistinguishable)"
                        )
                    control.check()
                    _settle(page, settle_ms)

                # --- scenario layer must be distinguishable --------------
                for scenario in scenarios:
                    scenario_id = scenario.get("id")
                    control = page.query_selector(f'input[type="checkbox"][data-scenario="{scenario_id}"]')
                    if control is None:
                        problems.append(f"scenario {scenario_id} has no toggle control")
                        continue
                    if screenshot_dir is None:
                        continue
                    baseline = screenshot_dir / f"{label}-scenario-{scenario_id}-before.png"
                    toggled = screenshot_dir / f"{label}-scenario-{scenario_id}-after.png"
                    error = _screenshot_map(page, baseline)
                    if error:
                        problems.append(f"scenario {scenario_id}: {error}")
                        continue
                    control.uncheck()
                    _settle(page, settle_ms)
                    error = _screenshot_map(page, toggled)
                    if error:
                        problems.append(f"scenario {scenario_id}: {error}")
                        continue
                    differ, fraction = images_differ(baseline, toggled)
                    if not differ:
                        problems.append(
                            f"scenario {scenario_id} is indistinguishable from the authoritative baseline "
                            f"when toggled off"
                        )
                    control.check()
                    _settle(page, settle_ms)

                # --- canonical reset -------------------------------------
                if canonical_reset:
                    reset = _first_visible(page, _RESET_SELECTOR)
                    if reset is None:
                        buttons = page.query_selector_all("button")
                        reset = next(
                            (b for b in buttons if re.search(r"reset|canonical", (b.inner_text() or "").lower())),
                            None,
                        )
                    if reset is None:
                        problems.append("manifest declares canonical_reset but no reset control exists")
                    else:
                        for cb in page.query_selector_all('input[type="checkbox"]'):
                            cb.uncheck()
                        page.wait_for_timeout(settle_ms)
                        reset.click()
                        page.wait_for_timeout(settle_ms)
                        if _checkbox_states(page) != initial_states:
                            problems.append("canonical reset does not restore the canonical control state")

                # --- mobile snapshot --------------------------------------
                mobile_context = browser.new_context(viewport=_viewport(mobile_size))
                mobile_page = mobile_context.new_page()
                mobile_page.goto(dashboard_path.as_uri())
                _settle(mobile_page, settle_ms)
                if screenshot_dir is not None:
                    error = _screenshot_map(mobile_page, screenshot_dir / f"{label}-mobile.png")
                    if error:
                        problems.append(f"mobile map screenshot: {error}")
                    else:
                        stats = image_stats(screenshot_dir / f"{label}-mobile.png")
                        evidence["mobile_map_stats"] = stats
                        if _is_blank(stats):
                            problems.append("map renders blank on mobile viewport")
                mobile_context.close()
                context.close()

                if problems:
                    return failed("; ".join(problems), code=_primary_code(problems), problems=problems)
                return passed(
                    "dashboard loads cleanly; map, legend, provenance, toggles, "
                    "scenario and canonical reset all render as the manifest declares",
                    evidence=evidence,
                )
            finally:
                browser.close()
    except Exception as exc:  # noqa: BLE001
        return not_testable(f"browser validation could not run: {type(exc).__name__}: {exc}", code="browser_error")
    finally:
        if tmp_context is not None:
            tmp_context.cleanup()


def _viewport(size: str) -> dict[str, int]:
    match = re.fullmatch(r"(\d+)x(\d+)", size.strip())
    if not match:
        raise ValueError(f"invalid viewport size {size!r}; expected WxH")
    return {"width": int(match.group(1)), "height": int(match.group(2))}
