#!/usr/bin/env python3
"""Open-GIS eval runner.

    python evals/run.py                       # every fixture case
    python evals/run.py --case attribute-override
    python evals/run.py --mode fixture
    python evals/run.py --mode live --agent claude_code --model <model>
    python evals/run.py --json eval-results.json
    python evals/run.py --list

Fixture mode is the explicit default: ordinary invocations never call an
agent or mutable external service. Live mode invokes an AgentAdapter and is
intended for manual or scheduled benchmarks.

Exit codes:
    0  every executed case passed
    1  one or more assertions failed
    2  setup/runtime failure, malformed configuration, or zero cases executed
"""

from __future__ import annotations

import argparse
import importlib
import json
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

EVALS_DIR = Path(__file__).resolve().parent
REPO_ROOT = EVALS_DIR.parent
CASES_DIR = EVALS_DIR / "cases"
RESULTS_DIR = EVALS_DIR / "results"
KNOWN_MODES = {"fixture", "live"}
KNOWN_AGENTS = {"claude_code", "codex"}

sys.path.insert(0, str(EVALS_DIR))

from assertions import AssertionResult, STATUSES  # noqa: E402

DIMENSIONS = {
    "project": "reproducibility_compliance",
    "overrides": "override_handling",
    "provenance": "provenance",
    "geodata": "gis_correctness",
    "validation": "validation_integrity",
    "qgis": "presentation_contract",
    "presentation": "presentation_contract",
    "rerun": "rerun_success",
}


@dataclass
class SetupFailure(Exception):
    """A failure that invalidates a trial before assertions can be graded."""

    stage: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"{self.stage}: {self.message}"


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _resolve_assertion(name: str):
    module_name, _, fn_name = name.partition(".")
    if not fn_name:
        raise ValueError(f"assertion name must be '<module>.<function>', got {name!r}")
    module = importlib.import_module(f"assertions.{module_name}")
    fn = getattr(module, fn_name, None)
    if fn is None or not callable(fn):
        raise ValueError(f"assertions.{module_name} has no callable function {fn_name!r}")
    return module_name, fn


def _validate_relative_dir(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty relative path")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{field_name} must remain inside the case workspace, got {value!r}")
    return value


def _validate_case(case: Any, expected_path: Path) -> dict[str, Any]:
    if not isinstance(case, dict):
        raise ValueError(f"{expected_path}: expected a YAML mapping")

    case_id = case.get("id")
    if not isinstance(case_id, str) or not case_id.strip():
        raise ValueError(f"{expected_path}: id must be a non-empty string")

    mode = case.get("mode", "fixture")
    if mode not in KNOWN_MODES:
        raise ValueError(f"{expected_path}: unknown mode {mode!r}; expected one of {sorted(KNOWN_MODES)}")

    _validate_relative_dir(case.get("project_dir", "project"), "project_dir")

    generator = case.get("generator")
    if generator is not None and (not isinstance(generator, str) or not generator.strip()):
        raise ValueError(f"{expected_path}: generator must be a non-empty command string")
    rerun_generator = case.get("rerun_generator")
    if rerun_generator is not None and (
        not isinstance(rerun_generator, str) or not rerun_generator.strip()
    ):
        raise ValueError(f"{expected_path}: rerun_generator must be a non-empty command string")

    extra_generators = case.get("extra_generators") or {}
    if not isinstance(extra_generators, dict):
        raise ValueError(f"{expected_path}: extra_generators must be a mapping")
    for project_dir, command in extra_generators.items():
        _validate_relative_dir(project_dir, "extra_generators key")
        if not isinstance(command, str) or not command.strip():
            raise ValueError(f"{expected_path}: extra generator for {project_dir!r} must be a command string")

    agent = case.get("agent", "claude_code")
    if mode == "live" and agent not in KNOWN_AGENTS:
        raise ValueError(f"{expected_path}: unknown agent {agent!r}; expected one of {sorted(KNOWN_AGENTS)}")

    assertions = case.get("assertions")
    if not isinstance(assertions, list) or not assertions:
        raise ValueError(f"{expected_path}: assertions must be a non-empty list")
    for index, entry in enumerate(assertions):
        location = f"{expected_path}: assertions[{index}]"
        if not isinstance(entry, dict):
            raise ValueError(f"{location} must be a mapping")
        name = entry.get("assert")
        if not isinstance(name, str) or not name:
            raise ValueError(f"{location}.assert must be a non-empty string")
        try:
            _resolve_assertion(name)
        except (ImportError, AttributeError, ValueError) as exc:
            raise ValueError(f"{location}: {exc}") from exc
        args = entry.get("args", {})
        if args is not None and not isinstance(args, dict):
            raise ValueError(f"{location}.args must be a mapping")
        expect = entry.get("expect", "passed")
        if expect not in STATUSES:
            raise ValueError(f"{location}.expect must be one of {list(STATUSES)}, got {expect!r}")
        hard_gate = entry.get("hard_gate", case.get("hard_gate", True))
        if not isinstance(hard_gate, bool):
            raise ValueError(f"{location}.hard_gate must be true or false")

    return case


def _load_case(case_dir: Path) -> dict[str, Any]:
    expected_path = case_dir / "expected.yaml"
    try:
        with expected_path.open("r", encoding="utf-8") as fh:
            case = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"could not load {expected_path}: {exc}") from exc
    return _validate_case(case, expected_path)


def _prepare_workspace(case_dir: Path, case_def: dict[str, Any]) -> Path:
    workspace = Path(tempfile.mkdtemp(prefix=f"open-gis-eval-{case_dir.name}-"))
    project_dirs = {case_def.get("project_dir", "project")}
    project_dirs.update((case_def.get("extra_generators") or {}).keys())
    for project_dir_name in project_dirs:
        project_src = case_dir / project_dir_name
        if project_src.exists():
            shutil.copytree(project_src, workspace / project_dir_name, dirs_exist_ok=True)
        else:
            (workspace / project_dir_name).mkdir(parents=True, exist_ok=True)
    return workspace


def _output_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _execute_command(command: str, cwd: Path, timeout_s: int | float) -> dict[str, Any]:
    """Execute a configured eval command and retain complete diagnostic evidence."""
    started = time.monotonic()
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        return {
            "command": command,
            "cwd": str(cwd),
            "returncode": proc.returncode,
            "timed_out": False,
            "duration_s": time.monotonic() - started,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "command": command,
            "cwd": str(cwd),
            "returncode": None,
            "timed_out": True,
            "duration_s": time.monotonic() - started,
            "stdout": _output_text(exc.stdout),
            "stderr": _output_text(exc.stderr),
            "timeout_s": timeout_s,
        }


def _require_command_success(result: dict[str, Any], stage: str) -> None:
    if result.get("timed_out"):
        raise SetupFailure(stage, f"command timed out after {result.get('timeout_s')}s", result)
    if result.get("returncode") != 0:
        raise SetupFailure(stage, f"command exited with status {result.get('returncode')}", result)


def _format_command(command: str, project_path: Path) -> str:
    return command.format(repo_root=REPO_ROOT, evals_dir=EVALS_DIR, project_dir=project_path)


def _load_adapter(agent_name: str):
    if agent_name not in KNOWN_AGENTS:
        raise SetupFailure("agent_preflight", f"unknown agent {agent_name!r}")
    try:
        adapter_module = importlib.import_module(f"adapters.{agent_name}")
        adapter_cls_name = "".join(part.capitalize() for part in agent_name.split("_")) + "Adapter"
        adapter_cls = getattr(adapter_module, adapter_cls_name)
        return adapter_cls()
    except (ImportError, AttributeError) as exc:
        raise SetupFailure("agent_preflight", f"could not load adapter {agent_name!r}: {exc}") from exc


def _agent_result_dict(agent_result: Any) -> dict[str, Any]:
    return {
        "agent": agent_result.agent,
        "model": agent_result.model,
        "success": agent_result.success,
        "returncode": agent_result.returncode,
        "command": agent_result.command,
        "duration_s": agent_result.duration_s,
        "stdout": agent_result.stdout,
        "stderr": agent_result.stderr,
        "metadata": agent_result.metadata,
    }


def _portable_result_paths(
    value: Any,
    workspace: Path,
    rerun_workspace: Path | None,
    *,
    keep_workspace: bool,
) -> Any:
    """Replace paths to deleted temp workspaces with stable evidence tokens."""
    if keep_workspace:
        return value
    replacements = [(str(workspace), "$WORKSPACE")]
    if rerun_workspace is not None:
        replacements.insert(0, (str(rerun_workspace), "$RERUN_WORKSPACE"))
    if isinstance(value, str):
        for path, token in replacements:
            value = value.replace(path, token)
        return value
    if isinstance(value, list):
        return [
            _portable_result_paths(item, workspace, rerun_workspace, keep_workspace=keep_workspace)
            for item in value
        ]
    if isinstance(value, dict):
        return {
            key: _portable_result_paths(item, workspace, rerun_workspace, keep_workspace=keep_workspace)
            for key, item in value.items()
        }
    return value


def run_case(
    case_dir: Path,
    mode_filter: str | None,
    *,
    agent_override: str | None = None,
    model: str | None = None,
    timeout_s: int | float = 900,
    seed: int | None = None,
    trial: int = 1,
) -> dict[str, Any]:
    """Execute one case/trial, keeping setup failures out of assertion scores."""
    case_def = _load_case(case_dir)
    case_id = case_def.get("id", case_dir.name)
    case_mode = case_def.get("mode", "fixture")

    if mode_filter and case_mode != mode_filter:
        return {
            "id": case_id,
            "trial": trial,
            "mode": case_mode,
            "status": "skipped",
            "skipped": True,
            "reason": f"mode={case_mode} != --mode {mode_filter}",
        }

    started = time.monotonic()
    workspace = _prepare_workspace(case_dir, case_def)
    project_dir = case_def.get("project_dir", "project")
    project_path = workspace / project_dir
    keep_workspace = bool(case_def.get("keep_workspace", False))
    rerun_workspace_path: Path | None = None
    generator_result: dict[str, Any] | None = None
    extra_generator_results: list[dict[str, Any]] = []
    rerun_generator_result: dict[str, Any] | None = None
    agent_run: dict[str, Any] | None = None
    assertion_results: list[dict[str, Any]] = []
    dimension_totals: dict[str, dict[str, int]] = {}

    try:
        if case_mode == "fixture":
            generator = case_def.get("generator")
            if generator:
                command = _format_command(generator, project_path)
                generator_result = _execute_command(command, project_path, timeout_s)
                _require_command_success(generator_result, "generator")

            for extra_dir_name, extra_cmd in (case_def.get("extra_generators") or {}).items():
                extra_path = workspace / extra_dir_name
                command = _format_command(extra_cmd, extra_path)
                result = _execute_command(command, extra_path, timeout_s)
                result["project_dir"] = extra_dir_name
                extra_generator_results.append(result)
                _require_command_success(result, f"extra_generator:{extra_dir_name}")

        elif case_mode == "live":
            agent_name = agent_override or case_def.get("agent", "claude_code")
            adapter = _load_adapter(agent_name)
            if not adapter.is_available():
                raise SetupFailure(
                    "agent_preflight",
                    f"required executable {adapter.executable!r} for agent {agent_name!r} was not found on PATH",
                    {"agent": agent_name, "executable": adapter.executable},
                )
            prompt_path = case_dir / case_def.get("prompt_file", "prompt.md")
            if not prompt_path.is_file():
                raise SetupFailure("agent_preflight", f"prompt file does not exist: {prompt_path}")
            prompt = prompt_path.read_text(encoding="utf-8")
            fixture_path = case_dir / case_def["fixture"] if case_def.get("fixture") else None
            if fixture_path is not None and not fixture_path.exists():
                raise SetupFailure("agent_preflight", f"fixture does not exist: {fixture_path}")
            agent_result = adapter.run(
                prompt,
                project_path,
                fixture=fixture_path,
                timeout_s=int(timeout_s),
                model=model,
                seed=seed,
            )
            agent_run = _agent_result_dict(agent_result)
            if not agent_result.success:
                message = f"agent {agent_name!r} failed"
                if agent_result.returncode is not None:
                    message += f" with status {agent_result.returncode}"
                raise SetupFailure("agent_execution", message, agent_run)

        rerun_generator_cmd = case_def.get("rerun_generator")
        if rerun_generator_cmd:
            rerun_workspace_path = Path(tempfile.mkdtemp(prefix=f"open-gis-eval-{case_dir.name}-rerun-"))
            command = _format_command(rerun_generator_cmd, rerun_workspace_path)
            rerun_generator_result = _execute_command(command, rerun_workspace_path, timeout_s)
            _require_command_success(rerun_generator_result, "rerun_generator")

        for entry in case_def.get("assertions", []):
            assert_name = entry["assert"]
            args = dict(entry.get("args", {}) or {})
            if rerun_workspace_path is not None and args.get("rerun_workspace") == "$RERUN":
                args["rerun_workspace"] = str(rerun_workspace_path)
            expect = entry.get("expect", "passed")
            module_name, fn = _resolve_assertion(assert_name)

            try:
                result: AssertionResult = fn(project_path, **args)
            except Exception as exc:  # noqa: BLE001
                raise SetupFailure(
                    "assertion_execution",
                    f"{assert_name} raised {type(exc).__name__}: {exc}",
                    {"assert": assert_name, "args": args},
                ) from exc

            matched = result.status == expect
            dim = DIMENSIONS.get(module_name, "other")
            bucket = dimension_totals.setdefault(dim, {"passed": 0, "failed": 0})
            bucket["passed" if matched else "failed"] += 1

            assertion_results.append({
                "assert": assert_name,
                "args": args,
                "expect": expect,
                "actual_status": result.status,
                "detail": result.detail,
                "matched_expectation": matched,
                "hard_gate": entry.get("hard_gate", case_def.get("hard_gate", True)),
                "data": result.data,
            })

        hard_failures = [a for a in assertion_results if a["hard_gate"] and not a["matched_expectation"]]
        status = "assertions_failed" if hard_failures else "passed"
        return _portable_result_paths({
            "id": case_id,
            "trial": trial,
            "seed": seed,
            "mode": case_mode,
            "status": status,
            "skipped": False,
            "duration_s": time.monotonic() - started,
            "workspace": str(workspace) if keep_workspace else None,
            "workspace_retained": keep_workspace,
            "generator": generator_result,
            "extra_generators": extra_generator_results,
            "rerun_generator": rerun_generator_result,
            "agent_run": agent_run,
            "assertions": assertion_results,
            "dimension_totals": dimension_totals,
            "hard_failures": [a["assert"] for a in hard_failures],
        }, workspace, rerun_workspace_path, keep_workspace=keep_workspace)
    except SetupFailure as exc:
        return _portable_result_paths({
            "id": case_id,
            "trial": trial,
            "seed": seed,
            "mode": case_mode,
            "status": "setup_failed",
            "skipped": False,
            "duration_s": time.monotonic() - started,
            "workspace": str(workspace) if keep_workspace else None,
            "workspace_retained": keep_workspace,
            "generator": generator_result,
            "extra_generators": extra_generator_results,
            "rerun_generator": rerun_generator_result,
            "agent_run": agent_run,
            "assertions": assertion_results,
            "dimension_totals": dimension_totals,
            "hard_failures": [],
            "setup_error": {"stage": exc.stage, "message": exc.message, "data": exc.data},
        }, workspace, rerun_workspace_path, keep_workspace=keep_workspace)
    except Exception as exc:  # noqa: BLE001
        return _portable_result_paths({
            "id": case_id,
            "trial": trial,
            "seed": seed,
            "mode": case_mode,
            "status": "setup_failed",
            "skipped": False,
            "duration_s": time.monotonic() - started,
            "workspace": str(workspace) if keep_workspace else None,
            "workspace_retained": keep_workspace,
            "generator": generator_result,
            "extra_generators": extra_generator_results,
            "rerun_generator": rerun_generator_result,
            "agent_run": agent_run,
            "assertions": assertion_results,
            "dimension_totals": dimension_totals,
            "hard_failures": [],
            "setup_error": {"stage": "runner", "message": f"{type(exc).__name__}: {exc}", "data": {}},
        }, workspace, rerun_workspace_path, keep_workspace=keep_workspace)
    finally:
        if not keep_workspace:
            shutil.rmtree(workspace, ignore_errors=True)
        if rerun_workspace_path is not None:
            shutil.rmtree(rerun_workspace_path, ignore_errors=True)


def discover_cases(only: str | None) -> list[Path]:
    dirs = sorted(p for p in CASES_DIR.iterdir() if p.is_dir() and (p / "expected.yaml").exists())
    if not only:
        for case_dir in dirs:
            _load_case(case_dir)
        return dirs

    direct_match = [case_dir for case_dir in dirs if case_dir.name == only]
    if direct_match:
        _load_case(direct_match[0])
        return direct_match

    matches = []
    for case_dir in dirs:
        if _load_case(case_dir).get("id") == only:
            matches.append(case_dir)
    return matches


def rollup_dimensions(case_results: list[dict[str, Any]]) -> dict[str, Any]:
    rollup: dict[str, dict[str, int]] = {}
    for case in case_results:
        if case.get("status") in {"skipped", "setup_failed"}:
            continue
        for dim, counts in (case.get("dimension_totals") or {}).items():
            bucket = rollup.setdefault(dim, {"passed": 0, "failed": 0})
            bucket["passed"] += counts.get("passed", 0)
            bucket["failed"] += counts.get("failed", 0)
    return rollup


def _environment() -> dict[str, Any]:
    return {
        "python": platform.python_version(),
        "python_executable": sys.executable,
        "platform": platform.platform(),
        "repo_root": str(REPO_ROOT),
    }


def build_summary(results: list[dict[str, Any]], run_config: dict[str, Any]) -> dict[str, Any]:
    ran = [r for r in results if r.get("status") != "skipped"]
    passed = [r for r in ran if r.get("status") == "passed"]
    assertion_failures = [r for r in ran if r.get("status") == "assertions_failed"]
    setup_failures = [r for r in ran if r.get("status") == "setup_failed"]
    return {
        "schema": "open-gis-eval-results/v1",
        "run_config": run_config,
        "environment": _environment(),
        "cases_total": len(results),
        "cases_run": len(ran),
        "cases_skipped": len(results) - len(ran),
        "cases_passed": len(passed),
        "cases_assertions_failed": len(assertion_failures),
        "cases_setup_failed": len(setup_failures),
        "cases_failed": len(assertion_failures) + len(setup_failures),
        "run_setup_failed": False,
        "setup_errors": [],
        "dimensions": rollup_dimensions(ran),
        "results": results,
    }


def _write_summary(summary: dict[str, Any], json_path: str | None) -> None:
    if json_path:
        out_path = Path(json_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
        print(f"\nWrote {out_path}")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / "latest.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--case", help="run only this case id/directory name")
    parser.add_argument(
        "--mode",
        choices=sorted(KNOWN_MODES),
        default="fixture",
        help="execution mode filter (default: fixture)",
    )
    parser.add_argument("--agent", choices=sorted(KNOWN_AGENTS), help="override the live-case agent adapter")
    parser.add_argument("--model", help="model passed to the selected live agent")
    parser.add_argument("--timeout", type=_positive_int, default=900, help="per generator/agent timeout in seconds")
    parser.add_argument("--repetitions", type=_positive_int, default=1, help="trials per selected case")
    parser.add_argument("--seed", type=int, help="base seed recorded for the run; incremented per repetition")
    parser.add_argument("--json", help="write full machine-readable results to this path")
    parser.add_argument("--list", action="store_true", help="list discovered cases and exit")
    args = parser.parse_args(argv)

    run_config = {
        "mode": args.mode,
        "agent": args.agent,
        "model": args.model,
        "timeout_s": args.timeout,
        "repetitions": args.repetitions,
        "seed": args.seed,
        "case": args.case,
    }

    try:
        case_dirs = discover_cases(args.case)
    except (OSError, ValueError) as exc:
        print(f"Invalid eval configuration: {exc}", file=sys.stderr)
        summary = build_summary([], run_config)
        summary["run_setup_failed"] = True
        summary["setup_errors"] = [{"stage": "configuration", "message": str(exc)}]
        _write_summary(summary, args.json)
        return 2

    if not case_dirs:
        message = f"No eval cases found (case={args.case!r})"
        print(message, file=sys.stderr)
        summary = build_summary([], run_config)
        summary["run_setup_failed"] = True
        summary["setup_errors"] = [{"stage": "selection", "message": message}]
        _write_summary(summary, args.json)
        return 2

    if args.list:
        for case_dir in case_dirs:
            case_def = _load_case(case_dir)
            print(f"{case_def.get('id', case_dir.name):40s} mode={case_def.get('mode', 'fixture')}")
        return 0

    results: list[dict[str, Any]] = []
    for case_dir in case_dirs:
        for trial in range(1, args.repetitions + 1):
            trial_seed = args.seed + trial - 1 if args.seed is not None else None
            trial_started = time.monotonic()
            try:
                result = run_case(
                    case_dir,
                    args.mode,
                    agent_override=args.agent,
                    model=args.model,
                    timeout_s=args.timeout,
                    seed=trial_seed,
                    trial=trial,
                )
            except Exception as exc:  # noqa: BLE001
                result = {
                    "id": case_dir.name,
                    "trial": trial,
                    "seed": trial_seed,
                    "mode": args.mode,
                    "status": "setup_failed",
                    "skipped": False,
                    "duration_s": time.monotonic() - trial_started,
                    "workspace": None,
                    "workspace_retained": False,
                    "generator": None,
                    "extra_generators": [],
                    "rerun_generator": None,
                    "agent_run": None,
                    "assertions": [],
                    "dimension_totals": {},
                    "hard_failures": [],
                    "setup_error": {
                        "stage": "runner",
                        "message": f"{type(exc).__name__}: {exc}",
                        "data": {},
                    },
                }
            results.append(result)
            if result.get("status") == "skipped":
                print(f"SKIP   {result['id']:40s} ({result['reason']})")
                break
            marker = {"passed": "PASS", "assertions_failed": "FAIL", "setup_failed": "ERROR"}[
                result["status"]
            ]
            suffix = f" trial={trial}" if args.repetitions > 1 else ""
            print(f"{marker:5s}  {result['id']:40s} {result['duration_s']:.2f}s{suffix}", end="")
            if result["status"] == "assertions_failed":
                print(f"  -- failed: {', '.join(result['hard_failures'])}")
            elif result["status"] == "setup_failed":
                error = result["setup_error"]
                print(f"  -- {error['stage']}: {error['message']}")
            else:
                print()

    summary = build_summary(results, run_config)
    if summary["cases_run"] == 0:
        summary["run_setup_failed"] = True
        summary["setup_errors"] = [{"stage": "selection", "message": f"zero cases executed for mode={args.mode!r}"}]
        print(f"ERROR  zero cases executed for mode={args.mode!r}", file=sys.stderr)

    _write_summary(summary, args.json)

    print(
        f"\n{summary['cases_passed']}/{summary['cases_run']} executed trials passed "
        f"({summary['cases_skipped']} skipped, "
        f"{summary['cases_assertions_failed']} assertion failures, "
        f"{summary['cases_setup_failed']} setup failures)"
    )

    if summary["run_setup_failed"] or summary["cases_setup_failed"]:
        return 2
    if summary["cases_assertions_failed"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
