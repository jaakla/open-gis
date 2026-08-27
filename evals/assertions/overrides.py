"""Override declaration vs application assertions.

See references/project-spec.md section 2.3.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import (
    AssertionResult,
    failed,
    load_json,
    load_project_yaml,
    not_testable,
    passed,
    project_root,
)


def declared_count(workspace: Path, count: int, project_dir: str = ".") -> AssertionResult:
    proj = load_project_yaml(workspace, project_dir)
    if proj is None:
        return failed("project.yaml missing")
    overrides = proj.get("overrides") or []
    if len(overrides) == count:
        return passed(f"{count} overrides declared")
    return failed(f"expected {count} overrides, found {len(overrides)}")


def every_override_has_provenance(workspace: Path, project_dir: str = ".") -> AssertionResult:
    proj = load_project_yaml(workspace, project_dir)
    if proj is None:
        return failed("project.yaml missing")
    overrides = proj.get("overrides") or []
    if not overrides:
        return passed("no overrides declared (vacuously true)")

    missing: list[str] = []
    for o in overrides:
        oid = o.get("id", "?")
        for field in ("id", "action", "rationale", "created_at", "created_by"):
            if not o.get(field):
                missing.append(f"{oid}.{field}")
    if missing:
        return failed(f"overrides missing required provenance fields: {missing}")
    return passed(f"all {len(overrides)} overrides carry id/action/rationale/author/timestamp")


def evidence_not_placeholder(workspace: Path, project_dir: str = ".") -> AssertionResult:
    proj = load_project_yaml(workspace, project_dir)
    if proj is None:
        return failed("project.yaml missing")
    overrides = proj.get("overrides") or []
    placeholder_markers = {"todo", "tbd", "n/a", "none", "..."}
    bad: list[str] = []
    for o in overrides:
        evidence = o.get("evidence") or []
        for e in evidence:
            value = str(e.get("value", "")).strip().lower()
            if not value or value in placeholder_markers:
                bad.append(o.get("id", "?"))
    if bad:
        return failed(f"overrides with placeholder/empty evidence: {bad}")
    return passed("no placeholder evidence found")


def application_status(
    workspace: Path, id: str, status: str, project_dir: str = ".",
    report_path: str = "validation/latest-report.json",
) -> AssertionResult:
    """The run report must record the given override id as applied/rejected/not_testable."""
    report = load_json(project_root(workspace, project_dir) / report_path)
    if report is None:
        return not_testable(f"no validation report found at {report_path}")

    checks = report.get("checks", [])
    override_check = next((c for c in checks if c.get("id") == "overrides_applied"), None)
    results = (override_check or {}).get("results") or report.get("overrides") or []
    entry = next((r for r in results if r.get("id") == id), None)
    if entry is None:
        return failed(f"override {id} has no application result in the report")
    actual = entry.get("status")
    if actual == status:
        return passed(f"override {id} status == {status}")
    return failed(f"override {id} status mismatch: expected {status!r}, got {actual!r}")


def from_value_matches_source(
    workspace: Path, id: str, source_path: str, id_field: str, project_dir: str = ".",
) -> AssertionResult:
    """A modify_attribute override's asserted `from` must actually match the
    immutable source file's current value for the targeted feature/field —
    not merely be declared. `source_path` is the on-disk source file the
    override targets (e.g. data/source/pois.geojson)."""
    proj = load_project_yaml(workspace, project_dir)
    if proj is None:
        return failed("project.yaml missing")
    overrides = proj.get("overrides") or []
    entry = next((o for o in overrides if o.get("id") == id), None)
    if entry is None:
        return failed(f"override {id} not declared")
    if entry.get("action") != "modify_attribute":
        return passed(f"override {id} is not modify_attribute; from-value check not applicable")
    change = entry.get("change") or {}
    if "from" not in change:
        return failed(f"override {id} is modify_attribute but declares no change.from")

    from . import project_root as _project_root

    target = entry.get("target") or {}
    feature_id = target.get("feature_id")
    field = change.get("field")
    src_file = _project_root(workspace, project_dir) / source_path
    if not src_file.exists():
        return not_testable(f"source file {source_path} not found, cannot verify from-value")

    try:
        import duckdb
        con = duckdb.connect()
        con.execute("INSTALL spatial")
        con.execute("LOAD spatial")
        row = con.execute(
            f'SELECT "{field}" FROM ST_Read(\'{src_file.as_posix()}\') WHERE "{id_field}" = ?',
            [feature_id],
        ).fetchone()
    except Exception as exc:  # noqa: BLE001
        return not_testable(f"could not read source {source_path}: {exc}")

    if row is None:
        return failed(f"override {id} target feature {feature_id!r} not found in source {source_path}")

    actual_source_value = row[0]
    declared_from = change.get("from")
    if str(actual_source_value) != str(declared_from):
        return failed(
            f"override {id} asserts from={declared_from!r} but immutable source has "
            f"{field}={actual_source_value!r} for {feature_id!r} — must reject/fail, not apply"
        )
    return passed(f"override {id} from={declared_from!r} matches immutable source value")


def source_files_byte_identical(
    workspace: Path,
    paths: list[str],
    hashes_before: dict[str, str] | Any = None,
    rerun_workspace: str | None = None,
    project_dir: str = ".",
) -> AssertionResult:
    """Verify declared immutable source files never changed, using real bytes only.

    Two independent baselines are supported, and both are recomputed from
    actual files rather than trusted from any declared/authored value:

    - ``hashes_before`` — a runner-captured pre-execution snapshot, normally
      supplied via the ``$SOURCE_HASHES`` magic value. A missing/empty/
      non-mapping baseline, or one missing an entry for a requested path,
      is reported ``not_testable`` rather than silently treated as a pass.
    - ``rerun_workspace`` — a second real workspace (normally the ``$RERUN``
      clean-rerun copy) whose ``paths`` are hashed and compared directly
      against this workspace's files.

    Exactly one of the two must be usable for a given call.
    """
    import hashlib

    if not paths:
        return not_testable("no paths declared to verify byte-identity")

    root = project_root(workspace, project_dir)

    def _hash(target: Path) -> str:
        return "sha256:" + hashlib.sha256(target.read_bytes()).hexdigest()

    if rerun_workspace is not None:
        rerun_root = Path(rerun_workspace)
        if not rerun_root.exists():
            return not_testable(f"rerun workspace {rerun_workspace} does not exist")
        missing: list[str] = []
        mismatches: list[str] = []
        for rel in paths:
            original = root / rel
            rerun = rerun_root / rel
            if not original.is_file() or not rerun.is_file():
                missing.append(rel)
                continue
            if _hash(original) != _hash(rerun):
                mismatches.append(rel)
        if missing:
            return not_testable(f"source files missing in one of the two workspaces: {missing}")
        if mismatches:
            return failed(f"source files mutated across rerun: {mismatches}")
        return passed(f"all {len(paths)} source files byte-identical between workspace and rerun")

    if not isinstance(hashes_before, dict) or not hashes_before:
        return not_testable("no pre-execution hash baseline is available for comparison")

    mismatches = []
    missing = []
    unbaselined: list[str] = []
    for rel in paths:
        expected = hashes_before.get(rel)
        if not expected:
            unbaselined.append(rel)
            continue
        target = root / rel
        if not target.exists():
            missing.append(rel)
            continue
        digest = _hash(target)
        normalized_expected = expected if str(expected).startswith("sha256:") else f"sha256:{expected}"
        if digest != normalized_expected:
            mismatches.append(rel)
    if unbaselined:
        return not_testable(f"no pre-execution hash baseline for: {unbaselined}")
    if missing:
        return not_testable(f"source files missing, cannot compare: {missing}")
    if mismatches:
        return failed(f"source files mutated since pre-execution baseline: {mismatches}")
    return passed(f"all {len(paths)} source files byte-identical to pre-execution baseline")
