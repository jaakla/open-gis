"""Validation-integrity assertions: report/manifest parity, status propagation.

See references/project-spec.md section 2.6 and 6.
"""

from __future__ import annotations

from pathlib import Path

from . import (
    AssertionResult,
    failed,
    get_in,
    load_json,
    load_project_yaml,
    not_testable,
    passed,
    project_root,
)

VALID_STATUSES = {"passed", "failed", "warning", "not_testable"}


def required_all_present(
    workspace: Path, project_dir: str = ".", report_path: str = "validation/latest-report.json"
) -> AssertionResult:
    proj = load_project_yaml(workspace, project_dir)
    if proj is None:
        return failed("project.yaml missing", code="manifest_missing")
    required = set(get_in(proj, "validation.required", []) or [])
    domain = {c.get("name") for c in (get_in(proj, "validation.domain_checks", []) or [])}
    declared = required | domain

    report = load_json(project_root(workspace, project_dir) / report_path)
    if report is None:
        return not_testable(f"no report at {report_path}", code="report_missing")
    reported_ids = [c.get("id") for c in report.get("checks", [])]

    missing = declared - set(reported_ids)
    if missing:
        return failed(f"declared checks missing from report: {sorted(missing)}", code="declared_check_missing")

    # Each declared check must appear exactly once.
    from collections import Counter

    counts = Counter(reported_ids)
    dupes = [cid for cid in declared if counts.get(cid, 0) > 1]
    if dupes:
        return failed(f"declared checks appear more than once in report: {dupes}", code="duplicate_check")

    return passed(f"all {len(declared)} declared checks present exactly once in report")


def no_implicit_pass(
    workspace: Path, project_dir: str = ".", report_path: str = "validation/latest-report.json"
) -> AssertionResult:
    """Every check must use one of the four explicit statuses; a missing
    status field (silently treated as pass by a lazy renderer) is a failure."""
    report = load_json(project_root(workspace, project_dir) / report_path)
    if report is None:
        return not_testable(f"no report at {report_path}", code="report_missing")
    bad = [c.get("id", "?") for c in report.get("checks", []) if c.get("status") not in VALID_STATUSES]
    if bad:
        return failed(
            f"checks without an explicit passed|failed|warning|not_testable status: {bad}",
            code="implicit_status",
        )
    return passed("every check has an explicit status")


def warning_or_failed_propagates_to_status(
    workspace: Path, project_dir: str = ".", report_path: str = "validation/latest-report.json"
) -> AssertionResult:
    report = load_json(project_root(workspace, project_dir) / report_path)
    if report is None:
        return not_testable(f"no report at {report_path}", code="report_missing")
    checks = report.get("checks", [])
    has_bad = any(c.get("status") in ("warning", "failed", "not_testable") for c in checks)
    overall = report.get("status")
    if has_bad and overall == "passed":
        return failed(
            "report has warning/failed/not_testable checks but overall status is 'passed' "
            "(non-passed checks must propagate)",
            code="status_laundering",
        )
    if not has_bad and overall != "passed":
        return failed(
            f"all checks passed but overall status is {overall!r}, expected 'passed'",
            code="status_understated",
        )
    return passed(f"overall status {overall!r} correctly reflects check statuses")


def run_record_matches(
    workspace: Path, project_dir: str = ".", report_path: str = "validation/latest-report.json",
    runs_dir: str = "runs",
) -> AssertionResult:
    from openmapstack.integrity import (
        canonical_file_set_hash,
        declared_input_paths,
        declared_output_paths,
        normalize_digest,
        sha256_file,
    )

    proj = load_project_yaml(workspace, project_dir)
    if proj is None:
        return failed("project.yaml missing", code="manifest_missing")
    root = project_root(workspace, project_dir)
    report = load_json(project_root(workspace, project_dir) / report_path)
    if report is None:
        return not_testable(f"no report at {report_path}", code="report_missing")
    run_id = report.get("run_id")
    if not run_id:
        return failed("report has no run_id", code="run_id_missing")
    run_file = project_root(workspace, project_dir) / runs_dir / f"{run_id}.json"
    if not run_file.exists():
        return failed(
            f"report references run_id {run_id!r} but {run_file} does not exist", code="run_record_missing"
        )
    run_record = load_json(run_file)
    if run_record is None:
        return failed(f"run record {run_file} unreadable", code="run_record_unreadable")

    latest = get_in(proj, "runs.latest", {}) or {}
    for hash_field, inventory_name, required in (
        ("inputs_hash", "inputs", set(declared_input_paths(root, proj))),
        ("outputs_hash", "outputs", set(declared_output_paths(proj))),
    ):
        inventory = run_record.get(inventory_name)
        if not isinstance(inventory, list) or not inventory:
            return failed(
                f"run record has no {inventory_name} inventory",
                code="hash_inventory_missing",
            )
        paths: list[str] = []
        seen: set[str] = set()
        for item in inventory:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                return failed(f"invalid {inventory_name} inventory item", code="hash_inventory_invalid")
            relative = item["path"]
            target = (root / relative).resolve()
            try:
                normalized = target.relative_to(root.resolve()).as_posix()
            except ValueError:
                return failed(f"unsafe inventory path: {relative}", code="hash_inventory_invalid")
            expected_file_hash = normalize_digest(item.get("sha256"))
            if normalized in seen or not target.is_file() or expected_file_hash is None:
                return failed(
                    f"invalid or duplicate inventory file: {relative}", code="hash_inventory_invalid"
                )
            if sha256_file(target) != expected_file_hash:
                return failed(f"inventory hash mismatch: {relative}", code="hash_mismatch")
            seen.add(normalized)
            paths.append(normalized)
        omitted = sorted(required - seen)
        if omitted:
            return failed(
                f"required files omitted from {inventory_name} inventory: {omitted}",
                code="hash_inventory_incomplete",
            )
        actual = canonical_file_set_hash(root, paths)
        labelled = {
            "manifest": normalize_digest(latest.get(hash_field)),
            "report": normalize_digest(report.get(hash_field)),
            "run": normalize_digest(run_record.get(hash_field)),
        }
        if any(value is None for value in labelled.values()):
            return failed(f"{hash_field} missing or malformed: {labelled}", code="hash_missing")
        if any(value != actual for value in labelled.values()):
            return failed(
                f"{hash_field} does not match real files: declared={labelled}, actual={actual}",
                code="hash_mismatch",
            )

    return passed(f"report run_id {run_id!r} matches a real run record with consistent hashes")


def no_prose_only_validation(
    workspace: Path, check_id: str, project_dir: str = ".", report_path: str = "validation/latest-report.json"
) -> AssertionResult:
    """A named check must carry machine-checkable evidence (numeric/boolean
    fields beyond status+reason), not just a status and a sentence."""
    report = load_json(project_root(workspace, project_dir) / report_path)
    if report is None:
        return not_testable(f"no report at {report_path}", code="report_missing")
    check = next((c for c in report.get("checks", []) if c.get("id") == check_id), None)
    if check is None:
        return failed(f"check {check_id!r} not present in report", code="check_missing")
    evidence_keys = set(check.keys()) - {"id", "status", "reason"}
    if not evidence_keys:
        return failed(
            f"check {check_id!r} has only status/reason — no machine-checkable evidence",
            code="prose_only",
        )
    return passed(f"check {check_id!r} carries evidence fields: {sorted(evidence_keys)}")


def report_evidence_recomputes(
    workspace: Path,
    evidence: list[dict],
    project_dir: str = ".",
    report_path: str = "validation/latest-report.json",
) -> AssertionResult:
    """Recompute supported numeric evidence from real geodata files.

    Each declaration names a report check, evidence field, metric, dataset,
    and optional id field. This avoids accepting internally consistent prose
    or invented counters as proof that a GIS check actually ran.
    """
    from .geodata import _connect, _read

    report = load_json(project_root(workspace, project_dir) / report_path)
    if report is None:
        return not_testable(f"no report at {report_path}", code="report_missing")
    if not isinstance(evidence, list) or not evidence:
        return failed("no evidence recomputation declarations", code="evidence_config_missing")
    con = _connect()
    if con is None:
        return not_testable("duckdb spatial not available in this environment", code="duckdb_unavailable")

    checks = {
        str(check.get("id")): check
        for check in report.get("checks", [])
        if isinstance(check, dict) and check.get("id")
    }
    mismatches: list[str] = []
    recomputed: list[dict] = []
    for declaration in evidence:
        if not isinstance(declaration, dict):
            return failed("evidence declaration must be a mapping", code="evidence_config_invalid")
        check_id = declaration.get("check_id")
        evidence_field = declaration.get("evidence_field")
        metric = declaration.get("metric")
        relative = declaration.get("path")
        check = checks.get(str(check_id))
        if check is None:
            return failed(f"check {check_id!r} not present in report", code="check_missing")
        if not all(isinstance(value, str) and value for value in (evidence_field, metric, relative)):
            return failed(f"invalid evidence declaration for {check_id!r}", code="evidence_config_invalid")
        target = project_root(workspace, project_dir) / relative
        if not target.is_file():
            return failed(f"evidence dataset does not exist: {relative}", code="file_missing")
        try:
            relation = _read(con, target)
            if metric == "row_count":
                actual = con.execute(f"SELECT COUNT(*) FROM {relation}").fetchone()[0]
            elif metric == "invalid_geometry_count":
                actual = con.execute(
                    f"SELECT COUNT(*) FROM {relation} WHERE NOT ST_IsValid(geom)"
                ).fetchone()[0]
            elif metric in {"duplicate_count", "null_count"}:
                field = declaration.get("field")
                if not isinstance(field, str) or not field:
                    return failed(
                        f"metric {metric!r} requires field for {check_id!r}",
                        code="evidence_config_invalid",
                    )
                identifier = field.replace('"', '""')
                if metric == "duplicate_count":
                    actual = con.execute(
                        f'SELECT COUNT(*) FROM (SELECT "{identifier}" FROM {relation} '
                        f'GROUP BY "{identifier}" HAVING COUNT(*) > 1) duplicates'
                    ).fetchone()[0]
                else:
                    actual = con.execute(
                        f'SELECT COUNT(*) FROM {relation} WHERE "{identifier}" IS NULL'
                    ).fetchone()[0]
            else:
                return failed(f"unsupported evidence metric: {metric}", code="evidence_config_invalid")
        except Exception as exc:  # noqa: BLE001
            return not_testable(
                f"could not recompute {check_id}.{evidence_field}: {exc}", code="read_error"
            )
        declared = check.get(evidence_field)
        recomputed.append(
            {"check_id": check_id, "field": evidence_field, "declared": declared, "actual": actual}
        )
        if declared != actual:
            mismatches.append(f"{check_id}.{evidence_field}: declared={declared!r}, actual={actual!r}")
    if mismatches:
        return failed(
            f"validation evidence does not match real data: {mismatches}",
            code="evidence_mismatch",
            mismatches=mismatches,
            recomputed=recomputed,
        )
    return passed(f"recomputed {len(recomputed)} report evidence value(s)", recomputed=recomputed)
