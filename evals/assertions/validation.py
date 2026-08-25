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
        return failed("project.yaml missing")
    required = set(get_in(proj, "validation.required", []) or [])
    domain = {c.get("name") for c in (get_in(proj, "validation.domain_checks", []) or [])}
    declared = required | domain

    report = load_json(project_root(workspace, project_dir) / report_path)
    if report is None:
        return not_testable(f"no report at {report_path}")
    reported_ids = [c.get("id") for c in report.get("checks", [])]

    missing = declared - set(reported_ids)
    if missing:
        return failed(f"declared checks missing from report: {sorted(missing)}")

    # Each declared check must appear exactly once.
    from collections import Counter

    counts = Counter(reported_ids)
    dupes = [cid for cid in declared if counts.get(cid, 0) > 1]
    if dupes:
        return failed(f"declared checks appear more than once in report: {dupes}")

    return passed(f"all {len(declared)} declared checks present exactly once in report")


def no_implicit_pass(
    workspace: Path, project_dir: str = ".", report_path: str = "validation/latest-report.json"
) -> AssertionResult:
    """Every check must use one of the four explicit statuses; a missing
    status field (silently treated as pass by a lazy renderer) is a failure."""
    report = load_json(project_root(workspace, project_dir) / report_path)
    if report is None:
        return not_testable(f"no report at {report_path}")
    bad = [c.get("id", "?") for c in report.get("checks", []) if c.get("status") not in VALID_STATUSES]
    if bad:
        return failed(f"checks without an explicit passed|failed|warning|not_testable status: {bad}")
    return passed("every check has an explicit status")


def warning_or_failed_propagates_to_status(
    workspace: Path, project_dir: str = ".", report_path: str = "validation/latest-report.json"
) -> AssertionResult:
    report = load_json(project_root(workspace, project_dir) / report_path)
    if report is None:
        return not_testable(f"no report at {report_path}")
    checks = report.get("checks", [])
    has_bad = any(c.get("status") in ("warning", "failed", "not_testable") for c in checks)
    overall = report.get("status")
    if has_bad and overall == "passed":
        return failed(
            "report has warning/failed/not_testable checks but overall status is 'passed' "
            "(non-passed checks must propagate)"
        )
    if not has_bad and overall != "passed":
        return failed(f"all checks passed but overall status is {overall!r}, expected 'passed'")
    return passed(f"overall status {overall!r} correctly reflects check statuses")


def run_record_matches(
    workspace: Path, project_dir: str = ".", report_path: str = "validation/latest-report.json",
    runs_dir: str = "runs",
) -> AssertionResult:
    report = load_json(project_root(workspace, project_dir) / report_path)
    if report is None:
        return not_testable(f"no report at {report_path}")
    run_id = report.get("run_id")
    if not run_id:
        return failed("report has no run_id")
    run_file = project_root(workspace, project_dir) / runs_dir / f"{run_id}.json"
    if not run_file.exists():
        return failed(f"report references run_id {run_id!r} but {run_file} does not exist")
    run_record = load_json(run_file)
    if run_record is None:
        return failed(f"run record {run_file} unreadable")

    for hash_field in ("inputs_hash", "outputs_hash"):
        report_val = report.get(hash_field)
        run_val = run_record.get(hash_field)
        if report_val and run_val and report_val != run_val:
            return failed(f"{hash_field} mismatch between report ({report_val}) and run record ({run_val})")

    return passed(f"report run_id {run_id!r} matches a real run record with consistent hashes")


def no_prose_only_validation(
    workspace: Path, check_id: str, project_dir: str = ".", report_path: str = "validation/latest-report.json"
) -> AssertionResult:
    """A named check must carry machine-checkable evidence (numeric/boolean
    fields beyond status+reason), not just a status and a sentence."""
    report = load_json(project_root(workspace, project_dir) / report_path)
    if report is None:
        return not_testable(f"no report at {report_path}")
    check = next((c for c in report.get("checks", []) if c.get("id") == check_id), None)
    if check is None:
        return failed(f"check {check_id!r} not present in report")
    evidence_keys = set(check.keys()) - {"id", "status", "reason"}
    if not evidence_keys:
        return failed(f"check {check_id!r} has only status/reason — no machine-checkable evidence")
    return passed(f"check {check_id!r} carries evidence fields: {sorted(evidence_keys)}")
