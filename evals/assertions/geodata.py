"""GIS-correctness assertions that inspect real geodata via DuckDB Spatial.

See references/project-spec.md section 2.4 (processing) and 6 (validation).
No dependency on any one LLM; these run against whatever files the pipeline
(or agent) actually produced.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from spatial import connect_spatial

from . import AssertionResult, failed, not_testable, passed, project_root


def _connect():
    """Load only a preinstalled Spatial extension; grading never downloads."""
    return connect_spatial()


def _read(con, path: Path):
    suffix = path.suffix.lower()
    if suffix == ".parquet":
        # DuckDB Spatial's native GEOMETRY type round-trips through Parquet;
        # read_parquet keeps that typed column, unlike routing through GDAL.
        return f"read_parquet('{path.as_posix()}')"
    return f"ST_Read('{path.as_posix()}')"


def row_count(workspace: Path, path: str, equals: int | None = None, at_least: int | None = None,
              at_most: int | None = None, project_dir: str = ".") -> AssertionResult:
    target = project_root(workspace, project_dir) / path
    if not target.exists():
        return failed(f"{path} does not exist", code="file_missing")
    con = _connect()
    if con is None:
        return not_testable("duckdb spatial not available in this environment", code="duckdb_unavailable")
    try:
        rel = _read(con, target)
        count = con.execute(f"SELECT COUNT(*) FROM {rel}").fetchone()[0]
    except Exception as exc:  # noqa: BLE001
        return not_testable(f"could not read {path}: {exc}", code="read_error")

    if equals is not None and count != equals:
        return failed(f"{path} row count {count} != expected {equals}", code="row_count_equals")
    if at_least is not None and count < at_least:
        return failed(f"{path} row count {count} < minimum {at_least}", code="row_count_at_least")
    if at_most is not None and count > at_most:
        return failed(f"{path} row count {count} > maximum {at_most}", code="row_count_at_most")
    return passed(f"{path} row count {count} satisfies constraints", row_count=count)


def geometry_all_valid(workspace: Path, path: str, project_dir: str = ".") -> AssertionResult:
    target = project_root(workspace, project_dir) / path
    if not target.exists():
        return failed(f"{path} does not exist", code="file_missing")
    con = _connect()
    if con is None:
        return not_testable("duckdb spatial not available in this environment", code="duckdb_unavailable")
    try:
        rel = _read(con, target)
        total, invalid = con.execute(
            f"SELECT COUNT(*), SUM(CASE WHEN NOT ST_IsValid(geom) THEN 1 ELSE 0 END) FROM {rel}"
        ).fetchone()
    except Exception as exc:  # noqa: BLE001
        return not_testable(f"could not validate geometry in {path}: {exc}", code="read_error")
    invalid = invalid or 0
    if invalid:
        return failed(f"{path}: {invalid}/{total} features have invalid geometry", code="invalid_geometry")
    return passed(f"{path}: all {total} features have valid geometry")


def no_duplicate_ids(workspace: Path, path: str, id_field: str, project_dir: str = ".") -> AssertionResult:
    target = project_root(workspace, project_dir) / path
    if not target.exists():
        return failed(f"{path} does not exist", code="file_missing")
    con = _connect()
    if con is None:
        return not_testable("duckdb spatial not available in this environment", code="duckdb_unavailable")
    try:
        rel = _read(con, target)
        dup = con.execute(
            f'SELECT COUNT(*) FROM (SELECT "{id_field}" FROM {rel} '
            f'GROUP BY "{id_field}" HAVING COUNT(*) > 1) t'
        ).fetchone()[0]
    except Exception as exc:  # noqa: BLE001
        return not_testable(f"could not check duplicates in {path}: {exc}", code="read_error")
    if dup:
        return failed(f"{path}: {dup} duplicate value(s) for {id_field}", code="duplicate_ids")
    return passed(f"{path}: no duplicate {id_field} values")


def no_null_ids(workspace: Path, path: str, id_field: str, project_dir: str = ".") -> AssertionResult:
    target = project_root(workspace, project_dir) / path
    if not target.exists():
        return failed(f"{path} does not exist", code="file_missing")
    con = _connect()
    if con is None:
        return not_testable("duckdb spatial not available in this environment", code="duckdb_unavailable")
    try:
        rel = _read(con, target)
        nulls = con.execute(f'SELECT COUNT(*) FROM {rel} WHERE "{id_field}" IS NULL').fetchone()[0]
    except Exception as exc:  # noqa: BLE001
        return not_testable(f"could not check nulls in {path}: {exc}", code="read_error")
    if nulls:
        return failed(f"{path}: {nulls} null {id_field} values", code="null_ids")
    return passed(f"{path}: no null {id_field} values")


def feature_field_equals(
    workspace: Path, path: str, id_field: str, id: Any, field: str, equals: Any,
    project_dir: str = ".",
) -> AssertionResult:
    target = project_root(workspace, project_dir) / path
    if not target.exists():
        return failed(f"{path} does not exist", code="file_missing")
    con = _connect()
    if con is None:
        return not_testable("duckdb spatial not available in this environment", code="duckdb_unavailable")
    try:
        rel = _read(con, target)
        row = con.execute(
            f'SELECT "{field}" FROM {rel} WHERE "{id_field}" = ?', [id]
        ).fetchone()
    except Exception as exc:  # noqa: BLE001
        return not_testable(f"could not query {path}: {exc}", code="read_error")
    if row is None:
        return failed(f"{path}: no feature with {id_field} = {id!r}", code="feature_not_found")
    actual = row[0]
    if str(actual) != str(equals):
        return failed(
            f"{path}: feature {id!r} field {field} = {actual!r}, expected {equals!r}",
            code="field_value_mismatch",
        )
    return passed(f"{path}: feature {id!r} field {field} == {equals!r}")


def feature_present(workspace: Path, path: str, id_field: str, id: Any, project_dir: str = ".") -> AssertionResult:
    target = project_root(workspace, project_dir) / path
    if not target.exists():
        return failed(f"{path} does not exist", code="file_missing")
    con = _connect()
    if con is None:
        return not_testable("duckdb spatial not available in this environment", code="duckdb_unavailable")
    try:
        rel = _read(con, target)
        row = con.execute(f'SELECT 1 FROM {rel} WHERE "{id_field}" = ? LIMIT 1', [id]).fetchone()
    except Exception as exc:  # noqa: BLE001
        return not_testable(f"could not query {path}: {exc}", code="read_error")
    if row is None:
        return failed(f"{path}: feature {id_field}={id!r} not present (expected inclusion)", code="feature_missing")
    return passed(f"{path}: feature {id_field}={id!r} present")


def feature_absent(workspace: Path, path: str, id_field: str, id: Any, project_dir: str = ".") -> AssertionResult:
    target = project_root(workspace, project_dir) / path
    if not target.exists():
        return not_testable(f"{path} does not exist, cannot confirm exclusion", code="file_missing")
    con = _connect()
    if con is None:
        return not_testable("duckdb spatial not available in this environment", code="duckdb_unavailable")
    try:
        rel = _read(con, target)
        row = con.execute(f'SELECT 1 FROM {rel} WHERE "{id_field}" = ? LIMIT 1', [id]).fetchone()
    except Exception as exc:  # noqa: BLE001
        return not_testable(f"could not query {path}: {exc}", code="read_error")
    if row is not None:
        return failed(f"{path}: feature {id_field}={id!r} present but expected excluded", code="feature_present")
    return passed(f"{path}: feature {id_field}={id!r} correctly excluded")


def field_range(
    workspace: Path, path: str, field: str, min: float | None = None, max: float | None = None,
    project_dir: str = ".",
) -> AssertionResult:
    target = project_root(workspace, project_dir) / path
    if not target.exists():
        return failed(f"{path} does not exist", code="file_missing")
    con = _connect()
    if con is None:
        return not_testable("duckdb spatial not available in this environment", code="duckdb_unavailable")
    try:
        rel = _read(con, target)
        clauses = []
        if min is not None:
            clauses.append(f'"{field}" < {min}')
        if max is not None:
            clauses.append(f'"{field}" > {max}')
        where = " OR ".join(clauses) if clauses else "FALSE"
        out_of_range = con.execute(f"SELECT COUNT(*) FROM {rel} WHERE {where}").fetchone()[0]
    except Exception as exc:  # noqa: BLE001
        return not_testable(f"could not check range in {path}: {exc}", code="read_error")
    if out_of_range:
        return failed(
            f"{path}: {out_of_range} feature(s) with {field} outside [{min}, {max}]",
            code="field_out_of_range",
        )
    return passed(f"{path}: all features have {field} within [{min}, {max}]")


def crs_not_used_for_metrics(workspace: Path, project_dir: str = ".",
                              forbidden_crs: tuple[str, ...] = ("EPSG:4326", "EPSG:3857")) -> AssertionResult:
    """Require a declared projected CRS for actual metric operations.

    Read, write, storage, and reprojection steps may legitimately mention a
    geographic CRS. Distance/area/buffer/length/nearest operations may not.
    """
    from . import get_in, load_project_yaml

    proj = load_project_yaml(workspace, project_dir)
    if proj is None:
        return failed("project.yaml missing", code="manifest_missing")
    analysis_crs = get_in(proj, "processing.analysis_crs")
    if not isinstance(analysis_crs, str) or not analysis_crs.strip():
        return failed("processing.analysis_crs is required", code="analysis_crs_missing")
    steps = get_in(proj, "processing.steps", []) or []
    metric_tokens = ("area", "buffer", "distance", "length", "nearest", "proximity")
    metric_steps = [
        step
        for step in steps
        if isinstance(step, dict)
        and any(token in str(step.get("operation", "")).lower() for token in metric_tokens)
    ]
    if metric_steps and analysis_crs.upper() in {item.upper() for item in forbidden_crs}:
        return failed(
            f"processing.analysis_crs is {analysis_crs}, forbidden for metric operations",
            code="forbidden_analysis_crs",
        )
    forbidden = {item.upper() for item in forbidden_crs}
    bad_steps = [
        step.get("id")
        for step in metric_steps
        if isinstance(step.get("crs"), str) and step["crs"].upper() in forbidden
    ]
    if bad_steps:
        return failed(
            f"steps using forbidden CRS for metric operation: {bad_steps}",
            code="forbidden_step_crs",
        )
    return passed(
        f"analysis_crs {analysis_crs} is valid for {len(metric_steps)} metric operation(s); "
        "storage/load/reprojection steps were excluded"
    )


def dataset_crs_is(
    workspace: Path,
    path: str,
    expected: str,
    geometry_field: str = "geom",
    project_dir: str = ".",
) -> AssertionResult:
    """Read CRS metadata from the actual dataset rather than the manifest."""
    target = project_root(workspace, project_dir) / path
    if not target.exists():
        return failed(f"{path} does not exist", code="file_missing")
    con = _connect()
    if con is None:
        return not_testable("duckdb spatial not available in this environment", code="duckdb_unavailable")
    try:
        rel = _read(con, target)
        rows = con.execute(
            f'SELECT DISTINCT ST_CRS("{geometry_field}") FROM {rel}'
        ).fetchall()
    except Exception as exc:  # noqa: BLE001
        return not_testable(f"could not inspect CRS metadata in {path}: {exc}", code="read_error")
    actual = sorted({str(row[0]).upper() for row in rows if row and row[0]})
    if not actual:
        return failed(f"{path} has no readable CRS metadata", code="dataset_crs_missing")
    if actual != [expected.upper()]:
        return failed(
            f"{path} CRS metadata {actual} != expected {expected.upper()}",
            code="dataset_crs_mismatch",
            actual=actual,
            expected=expected.upper(),
        )
    return passed(f"{path} actual CRS metadata is {expected.upper()}", actual=actual)
