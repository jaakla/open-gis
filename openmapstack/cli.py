"""Console entry point for OpenMapStack project operations."""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .project import ProjectError, get_in, load_project, project_path, step_outputs
from .validation import ValidationResult, validate_project

STATUS_MARKS = {
    "passed": "PASS",
    "warning": "WARN",
    "not_testable": "N/A ",
    "failed": "FAIL",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openmapstack",
        description="Validate, run, and inspect reproducible OpenMapStack projects.",
    )
    parser.add_argument("--version", action="version", version=f"openmapstack {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser("validate", help="validate a project manifest and its artifacts")
    validate_parser.add_argument("project", nargs="?", default="project.yaml", help="project.yaml or its directory")
    validate_parser.add_argument("--preflight", action="store_true", help="skip generated output, report, and run-record checks")
    validate_parser.add_argument("--strict", action="store_true", help="return failure when warnings or not-testable checks exist")
    validate_parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    validate_parser.add_argument("--output", type=Path, help="also write the JSON result to this path")
    validate_parser.add_argument("--verbose", action="store_true", help="show passed checks in text output")
    validate_parser.set_defaults(handler=_cmd_validate)

    run_parser = subparsers.add_parser("run", help="run the canonical pipeline and validate its artifacts")
    run_parser.add_argument("project", nargs="?", default="project.yaml", help="project.yaml or its directory")
    run_parser.add_argument("--dry-run", action="store_true", help="validate preflight and print the command without executing it")
    run_parser.add_argument("--strict", action="store_true", help="return failure when post-run validation has warnings")
    run_parser.add_argument("--json", action="store_true", help="capture pipeline output and emit machine-readable JSON")
    run_parser.add_argument(
        "--pipeline-arg",
        action="append",
        default=[],
        dest="pipeline_args",
        metavar="ARG",
        help="pass one argument to the pipeline; repeat as needed (use --pipeline-arg=--flag for flags)",
    )
    run_parser.set_defaults(handler=_cmd_run)

    inspect_parser = subparsers.add_parser("inspect", help="summarize a project for audit and review")
    inspect_parser.add_argument("project", nargs="?", default="project.yaml", help="project.yaml or its directory")
    inspect_parser.add_argument("--json", action="store_true", help="emit the full summary as JSON")
    inspect_parser.add_argument("--checks", action="store_true", help="include passed validation checks in text output")
    inspect_parser.set_defaults(handler=_cmd_inspect)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except KeyboardInterrupt:
        print("openmapstack: interrupted", file=sys.stderr)
        return 130
    except BrokenPipeError:
        return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    result = validate_project(args.project, artifacts=not args.preflight)
    payload = result.to_dict()
    if args.json:
        print(_json(payload))
    else:
        _print_validation(result, verbose=args.verbose)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(_json(payload) + "\n", encoding="utf-8")
        if not args.json:
            print(f"Wrote {args.output}")
    return 0 if result.ok(strict=args.strict) else 1


def _cmd_run(args: argparse.Namespace) -> int:
    preflight = validate_project(args.project, artifacts=False)
    if not preflight.ok():
        if args.json:
            print(_json({"schema": "openmapstack-run-result/v1", "status": "failed", "phase": "preflight", "validation": preflight.to_dict()}))
        else:
            print("Preflight validation failed.", file=sys.stderr)
            _print_validation(preflight, verbose=False, stream=sys.stderr)
        return 1

    try:
        project_file, project = load_project(args.project)
        command = _pipeline_command(project_file, project, args.pipeline_args)
    except ProjectError as exc:
        if args.json:
            print(_json({"schema": "openmapstack-run-result/v1", "status": "failed", "phase": "preflight", "error": str(exc)}))
        else:
            print(f"openmapstack run: {exc}", file=sys.stderr)
        return 2

    display_command = shlex.join(command)
    if args.dry_run:
        payload = {
            "schema": "openmapstack-run-result/v1",
            "status": preflight.status,
            "phase": "dry_run",
            "project_file": str(project_file),
            "cwd": str(project_file.parent),
            "command": command,
            "validation": preflight.to_dict(),
        }
        if args.json:
            print(_json(payload))
        else:
            print(f"Preflight: {preflight.status}")
            print(f"Would run: {display_command}")
        return 0


    if not args.json:
        print(f"Preflight: {preflight.status}")
        print(f"Running: {display_command}")
    try:
        completed = subprocess.run(
            command,
            cwd=project_file.parent,
            check=False,
            text=True,
            capture_output=args.json,
        )
    except OSError as exc:
        if args.json:
            print(_json({"schema": "openmapstack-run-result/v1", "status": "failed", "phase": "execute", "command": command, "error": str(exc)}))
        else:
            print(f"openmapstack run: could not start pipeline: {exc}", file=sys.stderr)
        return 2


    if completed.returncode != 0:
        payload = {
            "schema": "openmapstack-run-result/v1",
            "status": "failed",
            "phase": "execute",
            "project_file": str(project_file),
            "command": command,
            "returncode": completed.returncode,
        }
        if args.json:
            payload["stdout"] = completed.stdout
            payload["stderr"] = completed.stderr
            print(_json(payload))
        else:
            print(f"Pipeline failed with exit code {completed.returncode}.", file=sys.stderr)
        return 1

    validation = validate_project(project_file, artifacts=True)
    payload = {
        "schema": "openmapstack-run-result/v1",
        "status": validation.status,
        "phase": "complete",
        "project_file": str(project_file),
        "command": command,
        "returncode": completed.returncode,
        "validation": validation.to_dict(),
    }
    if args.json:
        payload["stdout"] = completed.stdout
        payload["stderr"] = completed.stderr
        print(_json(payload))
    else:
        _print_validation(validation, verbose=False)
    return 0 if validation.ok(strict=args.strict) else 1


def _cmd_inspect(args: argparse.Namespace) -> int:
    try:
        project_file, project = load_project(args.project)
    except ProjectError as exc:
        if args.json:
            print(_json({"schema": "openmapstack-inspection/v1", "status": "failed", "error": str(exc)}))
        else:
            print(f"openmapstack inspect: {exc}", file=sys.stderr)
        return 2

    validation = validate_project(project_file, artifacts=True)
    summary = _inspection(project_file, project, validation)
    if args.json:
        print(_json(summary))
    else:
        _print_inspection(summary, validation, show_checks=args.checks)
    return 0


def _pipeline_command(project_file: Path, project: dict[str, Any], pipeline_args: Sequence[str]) -> list[str]:
    implementation = get_in(project, "runtime", "implementation")
    if not isinstance(implementation, dict):
        raise ProjectError("runtime.implementation is missing")
    declared_command = implementation.get("command")
    if declared_command is not None:
        if isinstance(declared_command, str):
            command = shlex.split(declared_command)
        elif isinstance(declared_command, list) and all(isinstance(item, str) and item for item in declared_command):
            command = list(declared_command)
        else:
            raise ProjectError("runtime.implementation.command must be a string or list of strings")
        if not command:
            raise ProjectError("runtime.implementation.command is empty")
    else:
        pipeline = implementation.get("pipeline")
        target = project_path(project_file.parent, pipeline)
        if target is None:
            raise ProjectError("runtime.implementation.pipeline must be a safe project-relative path")
        if not target.is_file():
            raise ProjectError(f"pipeline does not exist: {pipeline}")
        relative = str(target.relative_to(project_file.parent))
        if target.suffix.lower() == ".py":
            command = [sys.executable, relative]
        elif target.stat().st_mode & 0o111:
            command = [relative if relative.startswith("./") else f"./{relative}"]
        else:
            raise ProjectError("non-Python pipelines need an executable file or runtime.implementation.command")
    return command + list(pipeline_args)


def _inspection(project_file: Path, project: dict[str, Any], validation: ValidationResult) -> dict[str, Any]:
    root = project_file.parent
    metadata = project.get("project") or {}
    sources = project.get("sources") or {}
    overrides = project.get("overrides") or []
    steps = get_in(project, "processing", "steps", default=[]) or []
    outputs = project.get("outputs") or {}
    source_items = []
    for key, source in sources.items() if isinstance(sources, dict) else []:
        source = source if isinstance(source, dict) else {}
        source_items.append(
            {
                "key": key,
                "provider": source.get("provider"),
                "dataset": source.get("dataset"),
                "retrieved_at": get_in(source, "access", "retrieved_at") or get_in(source, "access", "downloaded_at"),
                "version": get_in(source, "version", "identifier") or get_in(source, "version", "published_at"),
                "license": get_in(source, "license", "name"),
                "source_url": source.get("source_url"),
            }
        )
    override_items = []
    for override in overrides if isinstance(overrides, list) else []:
        if isinstance(override, dict):
            override_items.append(
                {
                    "id": override.get("id"),
                    "action": override.get("action"),
                    "created_by": override.get("created_by"),
                    "geometry_file": get_in(override, "geometry_file", "path"),
                }
            )
    step_items = []
    for index, step in enumerate(steps if isinstance(steps, list) else []):
        if isinstance(step, dict):
            step_items.append(
                {
                    "order": index + 1,
                    "id": step.get("id"),
                    "operation": step.get("operation"),
                    "outputs": step_outputs(step),
                }
            )
    output_items = []
    for key, output in outputs.items() if isinstance(outputs, dict) else []:
        output = output if isinstance(output, dict) else {}
        target = project_path(root, output.get("path"))
        output_items.append(
            {
                "key": key,
                "path": output.get("path"),
                "format": output.get("format"),
                "generated_by": output.get("generated_by"),
                "exists": bool(target and target.exists()),
            }
        )
    warnings = project.get("warnings") if isinstance(project.get("warnings"), list) else []
    return {
        "schema": "openmapstack-inspection/v1",
        "project_file": str(project_file),
        "project": {
            "schema": project.get("schema"),
            "id": metadata.get("id"),
            "title": metadata.get("title"),
            "question": metadata.get("question"),
            "status": metadata.get("status"),
            "updated_at": metadata.get("updated_at"),
        },
        "crs": {
            "analysis": get_in(project, "processing", "analysis_crs"),
            "storage": get_in(project, "processing", "storage_crs"),
        },
        "sources": source_items,
        "overrides": override_items,
        "steps": step_items,
        "outputs": output_items,
        "warnings": warnings,
        "latest_run": get_in(project, "runs", "latest"),
        "validation": validation.to_dict(),
    }


def _print_validation(result: ValidationResult, *, verbose: bool, stream: Any = None) -> None:
    stream = stream or sys.stdout
    for check in result.checks:
        if not verbose and check.status == "passed":
            continue
        location = f" [{check.path}]" if check.path else ""
        print(f"{STATUS_MARKS.get(check.status, check.status.upper()):4s} {check.id}{location}: {check.message}", file=stream)
    counts = result.counts
    print(
        f"{result.status.upper()}: {result.project_file} "
        f"({counts['passed']} passed, {counts['warning']} warnings, "
        f"{counts['not_testable']} not testable, {counts['failed']} failed)",
        file=stream,
    )


def _print_inspection(summary: dict[str, Any], validation: ValidationResult, *, show_checks: bool) -> None:
    project = summary["project"]
    print(f"{project.get('title') or '(untitled project)'}")
    print(f"  id: {project.get('id')}  schema: {project.get('schema')}  declared status: {project.get('status')}")
    print(f"  manifest: {summary['project_file']}")
    print(f"  CRS: analysis={summary['crs']['analysis']}  storage={summary['crs']['storage']}")
    print(f"  sources: {len(summary['sources'])}  overrides: {len(summary['overrides'])}  steps: {len(summary['steps'])}  outputs: {len(summary['outputs'])}")
    for source in summary["sources"]:
        print(f"    source {source['key']}: {source.get('provider')} / {source.get('dataset')} ({source.get('version')})")
    for override in summary["overrides"]:
        print(f"    override {override.get('id')}: {override.get('action')} by {override.get('created_by')}")
    missing_outputs = [item["path"] for item in summary["outputs"] if not item["exists"]]
    if missing_outputs:
        print(f"  missing outputs: {', '.join(str(item) for item in missing_outputs)}")
    latest = summary.get("latest_run") or {}
    print(f"  latest run: {latest.get('id')} ({latest.get('status')})")
    _print_validation(validation, verbose=show_checks)


def _json(value: object) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False, default=str)


if __name__ == "__main__":
    raise SystemExit(main())
