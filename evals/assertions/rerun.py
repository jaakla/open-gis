"""Clean-rerun and reproducibility assertions."""

from __future__ import annotations

import hashlib
import json
import shlex
from pathlib import Path
from typing import Any

from . import failed, load_json, load_project_yaml, not_testable, passed, project_root

CLEAN_RERUN_EVIDENCE = ".open-gis-clean-rerun.json"
DEFAULT_IGNORED_FIELDS = {
    "completed_at",
    "created_at",
    "generated_at",
    "inputs_hash",
    "outputs_hash",
    "run_id",
    "started_at",
    "timestamp",
    "updated_at",
}


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _normalize_number(value: Any) -> Any:
    if isinstance(value, float):
        if value == 0:
            return 0.0
        return round(value, 12)
    return value


def _canonical_sequence(values: list[Any], *, reversible: bool) -> list[Any]:
    normalized = [_normalize_json(item, set()) for item in values]
    candidates = [normalized]
    if reversible:
        candidates.append(list(reversed(normalized)))
    return min(candidates, key=_stable_json)


def _canonical_ring(coordinates: list[Any]) -> list[Any]:
    if not coordinates:
        return []
    points = [_normalize_json(point, set()) for point in coordinates]
    if len(points) > 1 and points[0] == points[-1]:
        points = points[:-1]
    if not points:
        return []
    rotations: list[list[Any]] = []
    for sequence in (points, list(reversed(points))):
        rotations.extend(sequence[index:] + sequence[:index] for index in range(len(sequence)))
    canonical = min(rotations, key=_stable_json)
    return canonical + [canonical[0]]


def _normalize_geometry(geometry: Any, ignored_fields: set[str]) -> Any:
    if not isinstance(geometry, dict):
        return _normalize_json(geometry, ignored_fields)
    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates")
    result = {
        key: _normalize_json(value, ignored_fields)
        for key, value in geometry.items()
        if key not in ignored_fields and key not in {"coordinates", "geometries"}
    }
    if geometry_type == "Point":
        result["coordinates"] = _normalize_json(coordinates, ignored_fields)
    elif geometry_type == "MultiPoint" and isinstance(coordinates, list):
        result["coordinates"] = sorted(
            (_normalize_json(point, ignored_fields) for point in coordinates), key=_stable_json
        )
    elif geometry_type == "LineString" and isinstance(coordinates, list):
        result["coordinates"] = _canonical_sequence(coordinates, reversible=True)
    elif geometry_type == "MultiLineString" and isinstance(coordinates, list):
        result["coordinates"] = sorted(
            (_canonical_sequence(line, reversible=True) for line in coordinates), key=_stable_json
        )
    elif geometry_type == "Polygon" and isinstance(coordinates, list):
        rings = [_canonical_ring(ring) for ring in coordinates]
        result["coordinates"] = rings[:1] + sorted(rings[1:], key=_stable_json)
    elif geometry_type == "MultiPolygon" and isinstance(coordinates, list):
        polygons = []
        for polygon in coordinates:
            rings = [_canonical_ring(ring) for ring in polygon]
            polygons.append(rings[:1] + sorted(rings[1:], key=_stable_json))
        result["coordinates"] = sorted(polygons, key=_stable_json)
    elif geometry_type == "GeometryCollection":
        result["geometries"] = sorted(
            (_normalize_geometry(item, ignored_fields) for item in geometry.get("geometries", [])),
            key=_stable_json,
        )
    else:
        result["coordinates"] = _normalize_json(coordinates, ignored_fields)
    return result


def _normalize_json(value: Any, ignored_fields: set[str]) -> Any:
    if isinstance(value, dict):
        value_type = value.get("type")
        if value_type in {
            "Point",
            "MultiPoint",
            "LineString",
            "MultiLineString",
            "Polygon",
            "MultiPolygon",
            "GeometryCollection",
        }:
            return _normalize_geometry(value, ignored_fields)
        result = {
            key: _normalize_json(item, ignored_fields)
            for key, item in value.items()
            if key not in ignored_fields
        }
        if value_type == "FeatureCollection" and isinstance(result.get("features"), list):
            result["features"] = sorted(result["features"], key=_stable_json)
        return result
    if isinstance(value, list):
        return [_normalize_json(item, ignored_fields) for item in value]
    return _normalize_number(value)


def _sql_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _parquet_snapshot(path: Path, ignored_fields: set[str]) -> dict[str, Any]:
    import duckdb

    escaped_path = path.as_posix().replace("'", "''")
    connection = duckdb.connect()
    try:
        connection.execute("LOAD spatial")
        columns = connection.execute(
            f"DESCRIBE SELECT * FROM read_parquet('{escaped_path}')"
        ).fetchall()
        selected: list[str] = []
        schema: list[dict[str, str]] = []
        names: list[str] = []
        for name, type_name, *_ in columns:
            names.append(name)
            schema.append({"name": name, "type": type_name})
            identifier = _sql_identifier(name)
            if str(type_name).upper().startswith("GEOMETRY"):
                selected.append(f"ST_AsGeoJSON({identifier})")
            else:
                selected.append(identifier)
        rows = connection.execute(
            f"SELECT {', '.join(selected)} FROM read_parquet('{escaped_path}')"
        ).fetchall()
    finally:
        connection.close()

    normalized_rows = []
    for row in rows:
        item: dict[str, Any] = {}
        for name, value, column in zip(names, row, schema, strict=True):
            if column["type"].upper().startswith("GEOMETRY") and isinstance(value, str):
                value = _normalize_geometry(json.loads(value), ignored_fields)
            else:
                value = _normalize_json(value, ignored_fields)
            item[name] = value
        normalized_rows.append(item)
    return {"schema": schema, "rows": sorted(normalized_rows, key=_stable_json)}


def _semantic_snapshot(path: Path, ignored_fields: set[str]) -> Any:
    suffix = path.suffix.lower()
    if suffix in {".json", ".geojson"}:
        return _normalize_json(json.loads(path.read_text(encoding="utf-8")), ignored_fields)
    if suffix in {".parquet", ".geoparquet"}:
        return _parquet_snapshot(path, ignored_fields)
    return {"sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def clean_execution_succeeded(workspace: Path, rerun_workspace: str) -> Any:
    """Require the runner-owned clean rerun and post-run validation to succeed."""
    evidence_path = Path(rerun_workspace) / CLEAN_RERUN_EVIDENCE
    evidence = load_json(evidence_path)
    if evidence is None:
        return not_testable(f"clean-rerun evidence is missing: {evidence_path}")
    if evidence.get("status") != "passed":
        return failed(
            f"clean rerun failed during {evidence.get('stage')}: {evidence.get('error', 'unknown error')}",
            rerun_stage=evidence.get("stage"),
        )
    return passed(
        "canonical entrypoint succeeded in a clean workspace and artifacts revalidated",
        preserved_paths=evidence.get("preserved_paths", []),
    )


def outputs_semantically_equal(
    workspace: Path,
    rerun_workspace: str,
    paths: list[str],
    project_dir: str = ".",
    ignored_fields: list[str] | None = None,
) -> Any:
    """Compare outputs after normalizing row, feature, and geometry representation."""
    root = project_root(workspace, project_dir)
    rerun_root = Path(rerun_workspace)
    if not rerun_root.exists():
        return not_testable(f"rerun workspace {rerun_workspace} does not exist")
    ignored = set(ignored_fields or [])
    missing: list[str] = []
    mismatches: list[str] = []
    errors: dict[str, str] = {}
    for relative in paths:
        original = root / relative
        rerun = rerun_root / relative
        if not original.is_file() or not rerun.is_file():
            missing.append(relative)
            continue
        try:
            if _semantic_snapshot(original, ignored) != _semantic_snapshot(rerun, ignored):
                mismatches.append(relative)
        except Exception as exc:  # noqa: BLE001
            errors[relative] = f"{type(exc).__name__}: {exc}"
    if missing:
        return failed(f"outputs missing in one of the two runs: {missing}", missing=missing)
    if errors:
        return not_testable("could not normalize one or more outputs", errors=errors)
    if mismatches:
        return failed(f"semantic outputs changed across clean rerun: {mismatches}", mismatches=mismatches)
    return passed(f"all {len(paths)} outputs are semantically equal across the clean rerun")


def outputs_hash_stable(
    workspace: Path, rerun_workspace: str, paths: list[str], project_dir: str = "."
) -> Any:
    """Backward-compatible byte comparison; prefer outputs_semantically_equal."""
    root = project_root(workspace, project_dir)
    rerun_root = Path(rerun_workspace)
    missing: list[str] = []
    mismatches: list[str] = []
    for relative in paths:
        original = root / relative
        rerun = rerun_root / relative
        if not original.is_file() or not rerun.is_file():
            missing.append(relative)
        elif original.read_bytes() != rerun.read_bytes():
            mismatches.append(relative)
    if missing:
        return not_testable(f"outputs missing in one of the two runs: {missing}")
    if mismatches:
        return failed(f"outputs not hash-stable across clean rerun: {mismatches}")
    return passed(f"all {len(paths)} declared outputs byte-identical across independent reruns")


def validation_report_reproducible(
    workspace: Path,
    rerun_workspace: str,
    project_dir: str = ".",
    report_path: str = "validation/latest-report.json",
    ignored_fields: list[str] | None = None,
) -> Any:
    """Compare complete validation evidence, excluding nondeterministic metadata."""
    root = project_root(workspace, project_dir)
    report_a = load_json(root / report_path)
    report_b = load_json(Path(rerun_workspace) / report_path)
    if report_a is None or report_b is None:
        return not_testable("validation report missing in one of the two runs")

    explicit_ignored = set(ignored_fields or [])
    normalized_a = _normalize_json(
        {key: value for key, value in report_a.items() if key not in DEFAULT_IGNORED_FIELDS},
        explicit_ignored,
    )
    normalized_b = _normalize_json(
        {key: value for key, value in report_b.items() if key not in DEFAULT_IGNORED_FIELDS},
        explicit_ignored,
    )
    for normalized in (normalized_a, normalized_b):
        if isinstance(normalized, dict) and isinstance(normalized.get("checks"), list):
            normalized["checks"] = sorted(normalized["checks"], key=_stable_json)
    if normalized_a != normalized_b:
        return failed(
            "validation evidence changed across rerun "
            f"(status {report_a.get('status')!r} vs {report_b.get('status')!r})"
        )
    return passed(f"validation evidence reproduces with status {report_a.get('status')!r}")


def no_chat_dependency(workspace: Path, project_dir: str = ".") -> Any:
    """Require declared canonical dependencies to avoid transcript-like state."""
    root = project_root(workspace, project_dir)
    project = load_project_yaml(workspace, project_dir)
    implementation = (
        ((project or {}).get("runtime") or {}).get("implementation") or {}
        if isinstance(project, dict)
        else {}
    )
    paths: list[str] = []
    pipeline = implementation.get("pipeline") if isinstance(implementation, dict) else None
    if isinstance(pipeline, str):
        paths.append(pipeline)
    command = implementation.get("command") if isinstance(implementation, dict) else None
    if isinstance(command, str):
        try:
            command = shlex.split(command)
        except ValueError as exc:
            return failed(f"canonical command cannot be parsed: {exc}")
    if isinstance(command, list):
        for token in command:
            if not isinstance(token, str) or token.startswith("-"):
                continue
            candidate = root / token
            try:
                candidate.resolve().relative_to(root.resolve())
            except ValueError:
                continue
            if candidate.is_file():
                paths.append(token)
    dependencies = implementation.get("dependencies", []) if isinstance(implementation, dict) else []
    if isinstance(dependencies, list):
        paths.extend(item for item in dependencies if isinstance(item, str))
    if not paths:
        return not_testable("canonical pipeline/dependencies are not declared")

    forbidden = ["chat_history", "conversation.json", "transcript.txt", "chat_log"]
    hits: list[str] = []
    for relative in paths:
        path = root / relative
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        hits.extend(f"{relative}:{token}" for token in forbidden if token in text)
    if hits:
        return failed(f"canonical project dependencies reference conversation state: {hits}")
    return passed("canonical project dependencies contain no chat/transcript references")
