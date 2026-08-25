"""Project-contract assertions: schema, manifest graph, status/report agreement.

See references/project-spec.md sections 1, 2.1, 2.4, 4.
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
    warning,
)


def exists(workspace: Path, project_dir: str = ".", path: str = "") -> AssertionResult:
    """A declared file exists relative to the project root."""
    target = project_root(workspace, project_dir) / path
    if target.exists():
        return passed(f"{path} exists")
    return failed(f"{path} does not exist")


def parses(workspace: Path, project_dir: str = ".") -> AssertionResult:
    proj = load_project_yaml(workspace, project_dir)
    if proj is None:
        return failed("project.yaml missing or unreadable")
    return passed("project.yaml parses", schema=proj.get("schema"))


def schema_is(workspace: Path, schema: str, project_dir: str = ".") -> AssertionResult:
    proj = load_project_yaml(workspace, project_dir)
    if proj is None:
        return failed("project.yaml missing")
    actual = proj.get("schema")
    if actual == schema:
        return passed(f"schema == {schema}")
    return failed(f"schema mismatch: expected {schema!r}, got {actual!r}")


def status_is(workspace: Path, status: str, project_dir: str = ".") -> AssertionResult:
    proj = load_project_yaml(workspace, project_dir)
    if proj is None:
        return failed("project.yaml missing")
    actual = get_in(proj, "project.status")
    if actual == status:
        return passed(f"project.status == {status}")
    return failed(f"project.status mismatch: expected {status!r}, got {actual!r}")


def status_in(workspace: Path, statuses: list[str], project_dir: str = ".") -> AssertionResult:
    proj = load_project_yaml(workspace, project_dir)
    if proj is None:
        return failed("project.yaml missing")
    actual = get_in(proj, "project.status")
    if actual in statuses:
        return passed(f"project.status {actual!r} in {statuses}")
    return failed(f"project.status {actual!r} not in {statuses}")


def status_agrees_with_validation_report(
    workspace: Path, project_dir: str = ".", report_path: str = "validation/latest-report.json"
) -> AssertionResult:
    """project.status must not be 'validated' unless the referenced report
    status is 'passed' (all required checks passed, none warning/failed/not_testable)."""
    proj = load_project_yaml(workspace, project_dir)
    if proj is None:
        return failed("project.yaml missing")
    proj_status = get_in(proj, "project.status")
    report = load_json(project_root(workspace, project_dir) / report_path)
    if report is None:
        if proj_status == "validated":
            return failed("project.status is 'validated' but no validation report exists")
        return not_testable("no validation report to cross-check against project.status")

    report_status = report.get("status")
    if proj_status == "validated" and report_status != "passed":
        return failed(
            f"project.status is 'validated' but report status is {report_status!r} "
            "(warning/failed/not_testable must never be laundered into validated)"
        )
    if proj_status == "warning" and report_status == "failed":
        return failed("project.status is 'warning' but report status is 'failed'")
    return passed(f"project.status {proj_status!r} agrees with report status {report_status!r}")


def graph_resolves(workspace: Path, project_dir: str = ".") -> AssertionResult:
    """Every step input/inputs/source symbol is a sources key or an earlier
    step's output; every outputs.*.generated_by names a real step."""
    proj = load_project_yaml(workspace, project_dir)
    if proj is None:
        return failed("project.yaml missing")

    sources = set((proj.get("sources") or {}).keys())
    steps = get_in(proj, "processing.steps", []) or []
    outputs = proj.get("outputs") or {}

    produced: set[str] = set()
    step_ids: set[str] = set()
    errors: list[str] = []

    for step in steps:
        step_id = step.get("id")
        if step_id:
            step_ids.add(step_id)

        for key in ("input", "source", "override", "target"):
            val = step.get(key)
            if isinstance(val, str) and key in ("input", "source"):
                if val not in sources and val not in produced:
                    errors.append(
                        f"step {step_id!r} {key}={val!r} resolves to neither a source nor a prior output"
                    )
        inputs = step.get("inputs")
        if isinstance(inputs, list):
            for val in inputs:
                if val not in sources and val not in produced:
                    errors.append(
                        f"step {step_id!r} inputs contains {val!r}, resolves to neither a source nor a prior output"
                    )

        out = step.get("output")
        if isinstance(out, str):
            for name in out.split(","):
                produced.add(name.strip())
        elif isinstance(out, list):
            produced.update(out)

    for out_key, out_def in outputs.items():
        gen_by = out_def.get("generated_by") if isinstance(out_def, dict) else None
        if gen_by and gen_by not in step_ids:
            errors.append(f"outputs.{out_key}.generated_by={gen_by!r} does not name a real step")

    if errors:
        return failed("; ".join(errors), errors=errors)
    return passed(
        f"graph resolves: {len(steps)} steps, {len(produced)} produced symbols, "
        f"{len(outputs)} outputs all traced to real steps"
    )


def one_canonical_pipeline(
    workspace: Path, project_dir: str = ".", pipeline_path: str = "pipeline.py", wrapper_paths: list[str] | None = None
) -> AssertionResult:
    """Convenience/E2E entrypoints must wrap pipeline.py, not duplicate its logic.

    Heuristic (static, no LLM): a wrapper file should be short and should
    import from the pipeline module rather than redefining its own
    top-level `run_pipeline`/`write_validation`/`write_qgis_project`-shaped
    functions.
    """
    root = project_root(workspace, project_dir)
    pipeline_file = root / pipeline_path
    if not pipeline_file.exists():
        return failed(f"{pipeline_path} does not exist")

    wrapper_paths = wrapper_paths or []
    duplicated: list[str] = []
    for wrapper_rel in wrapper_paths:
        wrapper_file = root / wrapper_rel
        if not wrapper_file.exists():
            continue
        text = wrapper_file.read_text(encoding="utf-8", errors="ignore")
        pipeline_module = Path(pipeline_path).stem
        imports_pipeline = f"import {pipeline_module}" in text or f"from {pipeline_module}" in text
        # crude duplication smell: wrapper defines its own "def main(" AND
        # does not import the pipeline module at all.
        if "def main(" in text and not imports_pipeline:
            duplicated.append(wrapper_rel)

    if duplicated:
        return failed(
            f"wrapper(s) {duplicated} define their own main() without importing {pipeline_path}"
        )
    return passed(f"{pipeline_path} is the canonical implementation; wrappers import it")


def declared_files_exist(workspace: Path, files: list[str], project_dir: str = ".") -> AssertionResult:
    root = project_root(workspace, project_dir)
    missing = [f for f in files if not (root / f).exists()]
    if missing:
        return failed(f"missing declared files: {missing}", missing=missing)
    return passed(f"all {len(files)} declared files exist")


def assumptions_have_rationale(workspace: Path, project_dir: str = ".") -> AssertionResult:
    proj = load_project_yaml(workspace, project_dir)
    if proj is None:
        return failed("project.yaml missing")
    assumptions = get_in(proj, "interpretation.assumptions", []) or []
    if not assumptions:
        return warning("no assumptions declared")
    missing = [
        a.get("id", "?")
        for a in assumptions
        if not a.get("statement") or not a.get("rationale")
    ]
    if missing:
        return failed(f"assumptions missing statement/rationale: {missing}")
    return passed(f"all {len(assumptions)} assumptions have statement + rationale")
