"""Console entry point for OpenMapStack project operations."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from collections.abc import Sequence

import yaml

from pathlib import Path
from typing import Any

from . import __version__
from .project import ProjectError, get_in, load_json, load_project, project_path, step_outputs
from .sampling import Sample, SamplingError, declared_sample, resolve_sample, run_record_errors
from .validation import ValidationResult, validate_project
from .verify import VerifyResult, verify_project

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
    sample_group = run_parser.add_argument_group(
        "sampled runs",
        "Run the same pipeline over a deliberately smaller slice so a late failure "
        "arrives in minutes. A sampled run proves the pipeline executes; it never "
        "establishes the analysis result and can never become the canonical run.",
    )
    sample_group.add_argument(
        "--sample",
        action="store_true",
        help="sampled run using every sampling parameter's declared `sample:` value",
    )
    sample_group.add_argument(
        "--sample-area",
        metavar="BBOX",
        help="sampled run over this test AOI (binds the role: sample_area parameter)",
    )
    sample_group.add_argument(
        "--sample-rows",
        type=int,
        metavar="N",
        help="sampled run capped at N rows (binds the role: sample_rows parameter)",
    )
    sample_group.add_argument(
        "--sample-fraction",
        type=float,
        metavar="PERCENT",
        help="sampled run over PERCENT of rows (binds the role: sample_fraction parameter)",
    )
    run_parser.set_defaults(handler=_cmd_run)

    verify_parser = subparsers.add_parser(
        "verify",
        help="check produced artifacts without requiring a golden answer",
    )
    verify_parser.add_argument("project", nargs="?", default="project.yaml", help="project.yaml or its directory")
    verify_parser.add_argument(
        "--rerun",
        action="store_true",
        help="also rebuild the project from source in a clean workspace and compare",
    )
    verify_parser.add_argument(
        "--rerun-timeout",
        type=float,
        default=1800.0,
        help="seconds allowed for the clean rerun's canonical entrypoint (default: 1800)",
    )
    verify_parser.add_argument(
        "--metamorphic",
        action="store_true",
        help="also execute every declared validation.metamorphic relation in an isolated variant workspace",
    )
    verify_parser.add_argument("--strict", action="store_true", help="return failure when warnings or not-testable checks exist")
    verify_parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    verify_parser.add_argument("--output", type=Path, help="also write the JSON result to this path")
    verify_parser.add_argument("--verbose", action="store_true", help="show passed checks in text output")
    verify_parser.set_defaults(handler=_cmd_verify)

    source_parser = subparsers.add_parser(
        "source",
        help="read-only discovery and approval-gated snapshots of a warehouse source",
    )
    source_commands = source_parser.add_subparsers(dest="source_command", required=True)
    discover_parser = source_commands.add_parser("discover", help="list tables/files, geometry columns, SRIDs, and row estimates")
    discover_parser.add_argument("project", help="project.yaml or its directory")
    discover_parser.add_argument("--source", required=True, help="key under sources.* declaring warehouse.backend")
    discover_parser.add_argument("--timeout", type=float, default=60.0, help="statement timeout in seconds (default: 60)")
    discover_parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    discover_parser.set_defaults(handler=_cmd_source_discover)
    snapshot_parser = source_commands.add_parser(
        "snapshot",
        help="plan a query snapshot under data/source/; materialise only with --approve",
    )
    snapshot_parser.add_argument("project", help="project.yaml or its directory")
    snapshot_parser.add_argument("--source", required=True, help="key under sources.* declaring warehouse.backend")
    query_group = snapshot_parser.add_mutually_exclusive_group(required=True)
    query_group.add_argument("--query", help="one read-only SELECT statement")
    query_group.add_argument("--query-file", type=Path, help="file containing one read-only SELECT statement")
    snapshot_parser.add_argument("--destination", required=True, help="project-relative .parquet path under data/source/")
    snapshot_parser.add_argument("--approve", action="store_true", help="actually write the snapshot (default is a dry run)")
    snapshot_parser.add_argument("--write-manifest", action="store_true", help="after a materialised snapshot, record the pin and warehouse metadata in project.yaml (rewrites the file; YAML comments are not preserved)")
    snapshot_parser.add_argument("--timeout", type=float, default=60.0, help="statement timeout in seconds (default: 60)")
    snapshot_parser.add_argument("--max-rows", type=int, default=100_000, help="refuse queries returning more rows (default: 100000)")
    snapshot_parser.add_argument("--max-bytes", type=int, default=256 * 1024 * 1024, help="refuse snapshots larger than this (default: 256 MiB)")
    snapshot_parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    snapshot_parser.set_defaults(handler=_cmd_source_snapshot)

    checks_parser = subparsers.add_parser("checks", help="list the versioned check catalogue (openmapstack-check-api/v1)")
    checks_parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    checks_parser.set_defaults(handler=_cmd_checks)

    check_parser = subparsers.add_parser("check", help="run one named check against a project workspace")
    check_parser.add_argument("name", help="check name, e.g. geodata.geometry_all_valid")
    check_parser.add_argument("workspace", nargs="?", default=".", help="project directory (default: .)")
    check_parser.add_argument(
        "--arg",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="check argument; VALUE is parsed as JSON when it is valid JSON, else used as a string",
    )
    check_parser.add_argument("--json", action="store_true", help="emit the openmapstack-check-result/v1 record")
    check_parser.set_defaults(handler=_cmd_check)

    api_parser = subparsers.add_parser("api-info", help="report API, schema, and package versions for consumers")
    api_parser.add_argument("--require-api", help="check API the consumer was built for")
    api_parser.add_argument("--min-version", help="oldest package version the consumer was tested against")
    api_parser.add_argument("--require-check", action="append", default=[], help="a check the consumer needs; repeatable")
    api_parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    api_parser.set_defaults(handler=_cmd_api_info)

    snapshot_parser = subparsers.add_parser(
        "skill-snapshot",
        help="copy SKILL.md, references/, and templates/ into a hashed, inspectable snapshot",
    )
    snapshot_mode = snapshot_parser.add_mutually_exclusive_group(required=True)
    snapshot_mode.add_argument("--out", type=Path, help="destination directory (must be empty or absent)")
    snapshot_mode.add_argument("--inspect", type=Path, metavar="DIR", help="re-verify an existing snapshot instead of creating one")
    snapshot_parser.add_argument("--source", type=Path, help="skill root holding SKILL.md (default: nearest ancestor of the current directory)")
    snapshot_parser.add_argument("--json", action="store_true", help="emit the snapshot manifest or inspection as JSON")
    snapshot_parser.set_defaults(handler=_cmd_skill_snapshot)

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


def _cmd_verify(args: argparse.Namespace) -> int:
    try:
        result = verify_project(
            args.project,
            rerun=args.rerun,
            rerun_timeout_s=args.rerun_timeout,
            metamorphic=args.metamorphic,
        )
    except ProjectError as exc:
        print(f"openmapstack: {exc}", file=sys.stderr)
        return 2
    payload = result.to_dict()
    if args.json:
        print(_json(payload))
    else:
        _print_verify(result, verbose=args.verbose)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(_json(payload) + "\n", encoding="utf-8")
        if not args.json:
            print(f"Wrote {args.output}")
    return 0 if result.ok(strict=args.strict) else 1


def _print_verify(result: VerifyResult, *, verbose: bool, stream: Any = None) -> None:
    out = stream or sys.stdout
    for run in result.checks:
        if run.result.status == "passed" and not verbose:
            continue
        mark = STATUS_MARKS.get(run.result.status, run.result.status.upper())
        target = run.args.get("path")
        label = f"{run.name} [{target}]" if target else run.name
        print(f"{mark} {label}: {run.result.detail}", file=out)
    counts = result.counts
    coverage = result.coverage
    print(
        f"{result.status.upper()}: {result.project_file} "
        f"({counts['passed']} passed, {counts['warning']} warnings, "
        f"{counts['not_testable']} not testable, {counts['failed']} failed; "
        f"{coverage['executed']}/{coverage['applicable']} applicable checks executed)",
        file=out,
    )
    if counts["not_testable"]:
        print(
            "  NOTE  some checks could not run here; install openmapstack[geo] "
            "for geodata checks, QGIS for the .qgz checks",
            file=out,
        )


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
        sample = _requested_sample(args, project)
        command = _pipeline_command(project_file, project, args.pipeline_args)
        if sample is not None:
            command = command + sample.argv
    except (ProjectError, SamplingError) as exc:
        if args.json:
            print(_json({"schema": "openmapstack-run-result/v1", "status": "failed", "phase": "preflight", "error": str(exc)}))
        else:
            print(f"openmapstack run: {exc}", file=sys.stderr)
        return 2

    mode = "sampled" if sample is not None else "canonical"
    display_command = shlex.join(command)
    if args.dry_run:
        payload = {
            "schema": "openmapstack-run-result/v1",
            "status": preflight.status,
            "phase": "dry_run",
            "mode": mode,
            "project_file": str(project_file),
            "cwd": str(project_file.parent),
            "command": command,
            "validation": preflight.to_dict(),
        }
        if sample is not None:
            payload["sample"] = sample.to_dict()
        if args.json:
            print(_json(payload))
        else:
            print(f"Preflight: {preflight.status}")
            print(f"Would run ({mode}): {display_command}")
        return 0

    environment = None
    if sample is not None:
        environment = dict(os.environ)
        environment.update(sample.environment)
        environment["OPENMAPSTACK_RUN_MODE"] = "sampled"

    # A sampled run may overwrite the declared outputs in place. Fingerprint
    # them first so the clobber is reported here rather than surfacing later
    # as an unexplained outputs_hash mismatch.
    outputs_before = _declared_output_digests(project_file.parent, project) if sample is not None else {}
    latest_before = get_in(project, "runs", "latest", "id")

    if not args.json:
        print(f"Preflight: {preflight.status}")
        print(f"Running ({mode}): {display_command}")
    try:
        completed = subprocess.run(
            command,
            cwd=project_file.parent,
            check=False,
            text=True,
            capture_output=args.json,
            env=environment,
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
            "mode": mode,
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

    if sample is not None:
        return _report_sampled_run(
            args, project_file, command, completed, sample, outputs_before, latest_before, preflight
        )

    validation = validate_project(project_file, artifacts=True)
    payload = {
        "schema": "openmapstack-run-result/v1",
        "status": validation.status,
        "phase": "complete",
        "mode": mode,
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


def _requested_sample(args: argparse.Namespace, project: dict[str, Any]) -> Sample | None:
    """The sampled run the command line asked for, or ``None`` for a canonical run."""
    explicit: dict[str, Any] = {}
    for role, value in (
        ("sample_area", args.sample_area),
        ("sample_rows", args.sample_rows),
        ("sample_fraction", args.sample_fraction),
    ):
        if value is not None:
            explicit[role] = value
    if not explicit and not args.sample:
        return None
    requested: dict[str, Any] = dict(declared_sample(project)) if args.sample else {}
    requested.update(explicit)
    if not requested:
        raise SamplingError(
            "--sample needs a runtime.implementation.parameters entry with a sampling "
            "role and a `sample:` value, or an explicit --sample-area/--sample-rows/"
            "--sample-fraction"
        )
    return resolve_sample(project, requested)


def _declared_output_digests(root: Path, project: dict[str, Any]) -> dict[str, str]:
    """SHA-256 of every declared output that exists right now."""
    from .integrity import declared_output_paths, sha256_file

    digests: dict[str, str] = {}
    for relative in declared_output_paths(project):
        target = root / relative
        if target.is_file():
            digests[relative] = sha256_file(target)
    return digests


def _report_sampled_run(
    args: argparse.Namespace,
    project_file: Path,
    command: list[str],
    completed: subprocess.CompletedProcess,
    sample: Sample,
    outputs_before: dict[str, str],
    latest_before: Any,
    preflight: ValidationResult,
) -> int:
    """Report a sampled run, refusing to let it stand in for the canonical one.

    The artifacts now describe a slice, so re-auditing them as the finished
    analysis would be asking the wrong question -- the reported validation is
    the pre-run one, which is the last point at which the tree described the
    canonical project. What a sampled run is graded on instead is narrow: the
    pipeline must not have promoted itself into ``runs.latest``, and any record
    it wrote must state what it realized.
    """
    root = project_file.parent
    validation = preflight
    status = "passed"
    problems: list[str] = []

    try:
        _, after = load_project(project_file)
    except ProjectError as exc:
        after = {}
        problems.append(f"project.yaml is unreadable after the sampled run: {exc}")
    latest_after = get_in(after, "runs", "latest", "id") if after else None
    if after and latest_after != latest_before:
        problems.append(
            f"the sampled run moved runs.latest from {latest_before!r} to {latest_after!r}; "
            f"a sampled run cannot become the canonical run of record"
        )

    # Any run record the pipeline just wrote must declare its realized sample;
    # reuse the shipped check so the CLI and the check API cannot drift.
    for record_path in sorted((root / "runs").glob("*.json")) if (root / "runs").is_dir() else []:
        try:
            record = load_json(record_path)
        except ProjectError:
            continue
        if isinstance(record, dict):
            problems.extend(f"runs/{record_path.name}: {problem}" for problem in run_record_errors(record))

    outputs_after = _declared_output_digests(root, after or {})
    overwritten = sorted(
        path
        for path, digest in outputs_after.items()
        if path in outputs_before and outputs_before[path] != digest
    )
    if problems:
        status = "failed"

    payload = {
        "schema": "openmapstack-run-result/v1",
        "status": status,
        "phase": "complete",
        "mode": "sampled",
        "project_file": str(project_file),
        "command": command,
        "returncode": completed.returncode,
        "sample": sample.to_dict(),
        "canonical_outputs_overwritten": overwritten,
        "promotion_problems": problems,
        "validation": validation.to_dict(),
        "validation_phase": "preflight",
    }
    if args.json:
        payload["stdout"] = completed.stdout
        payload["stderr"] = completed.stderr
        print(_json(payload))
    else:
        for problem in problems:
            print(f"FAIL runs.sample_isolation: {problem}", file=sys.stderr)
        if not problems:
            print(
                "Sampled run complete. It proves the pipeline executes; it does not "
                "establish the analysis result."
            )
        if overwritten:
            print(
                "WARN  the declared outputs now hold sampled data: "
                f"{', '.join(overwritten)}. Re-run without --sample before validating.",
                file=sys.stderr,
            )
    if problems:
        return 1
    return 1 if (overwritten and args.strict) else 0


def _cmd_skill_snapshot(args: argparse.Namespace) -> int:
    from .snapshot import SnapshotError, create_skill_snapshot, find_skill_root, inspect_skill_snapshot

    try:
        if args.inspect is not None:
            report = inspect_skill_snapshot(args.inspect)
            if args.json:
                print(_json(report))
            else:
                state = "intact" if report["intact"] else "TAMPERED"
                print(f"{state}: {report['snapshot']} ({report['file_count']} files, {report['content_sha256']})")
                for problem in report["problems"]:
                    print(f"  {problem}")
            return 0 if report["intact"] else 1
        source = args.source or find_skill_root()
        if source is None:
            raise SnapshotError("no skill root found; pass --source DIR holding SKILL.md, references/, and templates/")
        manifest = create_skill_snapshot(source, args.out)
    except SnapshotError as exc:
        if args.json:
            print(_json({"schema": "openmapstack-skill-snapshot-error/v1", "error": str(exc)}))
        else:
            print(f"openmapstack skill-snapshot: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(_json(manifest))
    else:
        print(f"Wrote {args.out} ({manifest['file_count']} files, {manifest['content_sha256']})")
    return 0


def _cmd_checks(args: argparse.Namespace) -> int:
    from .api import CHECK_API_VERSION, list_checks

    descriptors = list_checks()
    if args.json:
        print(_json({"schema": "openmapstack-check-catalogue/v1", "api_version": CHECK_API_VERSION,
                     "package_version": __version__, "checks": [d.to_dict() for d in descriptors]}))
        return 0
    print(f"{CHECK_API_VERSION} ({len(descriptors)} checks, openmapstack {__version__})")
    for descriptor in descriptors:
        required = [p.name for p in descriptor.parameters if p.required]
        optional = [p.name for p in descriptor.parameters if not p.required]
        oracle = "" if descriptor.oracle_free else "  [known-answer]"
        print(f"  {descriptor.name:48s} {descriptor.dimension}{oracle}")
        if required or optional:
            print(f"      args: required={required} optional={optional}")
    return 0


def _parse_check_args(pairs: Sequence[str]) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    for pair in pairs:
        key, separator, value = pair.partition("=")
        if not separator or not key.strip():
            raise ValueError(f"--arg expects KEY=VALUE, got {pair!r}")
        try:
            parsed[key.strip()] = json.loads(value)
        except json.JSONDecodeError:
            parsed[key.strip()] = value
    return parsed


def _cmd_check(args: argparse.Namespace) -> int:
    from .api import CheckAPIError, run_check

    try:
        record = run_check(args.name, args.workspace, _parse_check_args(args.arg))
    except (CheckAPIError, ValueError) as exc:
        if args.json:
            print(_json({"schema": "openmapstack-check-result/v1", "status": "not_testable", "code": "consumer_error", "error": str(exc)}))
        else:
            print(f"openmapstack check: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(_json(record))
    else:
        mark = STATUS_MARKS.get(record["status"], record["status"].upper())
        code = f" [{record['code']}]" if record.get("code") else ""
        print(f"{mark} {record['check']}{code}: {record['detail']}")
    return 0 if record["status"] != "failed" else 1


def _cmd_api_info(args: argparse.Namespace) -> int:
    from .api import CHECK_API_VERSION, CheckAPIError, api_info, negotiate

    info = api_info()
    negotiation = None
    if args.require_api or args.min_version or args.require_check:
        try:
            negotiation = negotiate(
                required_api=args.require_api or CHECK_API_VERSION,
                min_package_version=args.min_version,
                required_checks=list(args.require_check),
            )
        except CheckAPIError as exc:
            print(f"openmapstack api-info: {exc}", file=sys.stderr)
            return 2
        info["negotiation"] = negotiation
    if args.json:
        print(_json(info))
    else:
        print(f"openmapstack {info['package_version']}: {info['check_api_version']}, {info['checks']} checks "
              f"({info['oracle_free_checks']} oracle-free), project schema {info['project_schema']}")
        if negotiation is not None:
            print("compatible" if negotiation["compatible"] else "INCOMPATIBLE: " + "; ".join(negotiation["problems"]))
    if negotiation is not None and not negotiation["compatible"]:
        return 1
    return 0


def _cmd_source_discover(args: argparse.Namespace) -> int:
    from .connectors import ConnectorError, ConnectorLimits, discover_source

    try:
        project_file, project = load_project(args.project)
        discovery = discover_source(
            project, args.source, project_root=project_file.parent, limits=ConnectorLimits(timeout_s=args.timeout)
        )
    except (ProjectError, ConnectorError) as exc:
        return _source_error(exc, args.json)
    payload = discovery.to_dict()
    if args.json:
        print(_json(payload))
        return 0
    print(f"{discovery.backend} source {args.source!r}: {len(discovery.tables)} table(s)/file(s); read-only session: {discovery.read_only}")
    for table in discovery.tables:
        location = f"{table.schema}.{table.name}" if table.schema else table.name
        srid = f"EPSG:{table.srid}" if table.srid else "srid unknown"
        rows = f"~{table.row_estimate} rows" if table.row_estimate is not None else "rows unknown"
        print(f"  {location} [{table.kind}] geometry={table.geometry_column or '-'} {srid} {rows}")
    for note in discovery.notes:
        print(f"  NOTE  {note}")
    return 0


def _cmd_source_snapshot(args: argparse.Namespace) -> int:
    from .connectors import ConnectorError, ConnectorLimits, apply_snapshot_to_manifest, snapshot_source

    query = args.query
    if args.query_file is not None:
        try:
            query = args.query_file.read_text(encoding="utf-8")
        except OSError as exc:
            return _source_error(exc, args.json)
    try:
        project_file, project = load_project(args.project)
        record = snapshot_source(
            project,
            args.source,
            query,
            args.destination,
            project_root=project_file.parent,
            approve=args.approve,
            limits=ConnectorLimits(timeout_s=args.timeout, max_rows=args.max_rows, max_bytes=args.max_bytes),
        )
        if args.write_manifest and record.get("materialized"):
            updated = apply_snapshot_to_manifest(project, args.source, record)
            project_file.write_text(yaml.safe_dump(updated, sort_keys=False, allow_unicode=True), encoding="utf-8")
            record["manifest_written"] = str(project_file)
    except (ProjectError, ConnectorError) as exc:
        return _source_error(exc, args.json)
    if args.json:
        print(_json(record))
        return 0
    plan = record["plan"]
    print(f"{record['backend']} source {args.source!r}: query {plan['query_sha256']} returns {plan['row_count']} row(s), {len(plan['columns'])} column(s)")
    if not record["materialized"]:
        print(f"DRY RUN: nothing written to {args.destination}; re-run with --approve to materialise")
        return 0
    print(f"Wrote {args.destination} ({record['rows']} rows, {record['bytes']} bytes, {record['sha256']})")
    if record.get("manifest_written"):
        print(f"Updated {record['manifest_written']}")
    else:
        print("Add to project.yaml under sources.%s:" % args.source)
        print(yaml.safe_dump({"pin": record["pin"], "warehouse": record["warehouse"]}, sort_keys=False).rstrip())
    return 0


def _source_error(exc: Exception, as_json: bool) -> int:
    code = getattr(exc, "code", type(exc).__name__)
    if as_json:
        print(_json({"schema": "openmapstack-source-error/v1", "status": "failed", "code": code, "error": str(exc)}))
    else:
        print(f"openmapstack source: {exc} [{code}]", file=sys.stderr)
    return 2


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
