#!/usr/bin/env python3
"""OpenMapStack eval runner.

    python evals/run.py                       # every fixture case
    python evals/run.py --case attribute-override
    python evals/run.py --mode fixture
    python evals/run.py --mode visual      # PyQGIS + headless-browser integration
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
import math
import os
import platform
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from statistics import median
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

EVALS_DIR = Path(__file__).resolve().parent
REPO_ROOT = EVALS_DIR.parent
CASES_DIR = EVALS_DIR / "cases"
RESULTS_DIR = EVALS_DIR / "results"
CLEAN_RERUN_EVIDENCE = ".openmapstack-clean-rerun.json"
KNOWN_MODES = {"fixture", "live", "visual"}
KNOWN_AGENTS = {"claude_code", "codex"}
KNOWN_CASE_TYPES = {"mutation", "positive"}
KNOWN_SCORE_TYPES = {
    "agent_benchmark",
    "contract_ci",
    "integration_visual",
    "mutation_tests",
}

sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(EVALS_DIR))

from assertions import AssertionResult, STATUSES  # noqa: E402
from openmapstack.schema import validation_errors  # noqa: E402
from openmapstack.validation import validate_project  # noqa: E402


def _load_eval_schema(name: str) -> dict[str, Any]:
    return json.loads((EVALS_DIR / "schemas" / name).read_text(encoding="utf-8"))


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


def _validate_command(value: Any, field_name: str, expected_path: Path) -> None:
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise ValueError(f"{expected_path}: {field_name} must be a non-empty command string")


def _validate_rerun_config(config: dict[str, Any], mode: str, expected_path: Path) -> None:
    _validate_command(config.get("rerun_generator"), f"{mode}.rerun_generator", expected_path)
    if "clean_rerun" in config and not isinstance(config["clean_rerun"], dict):
        raise ValueError(f"{expected_path}: {mode}.clean_rerun must be a mapping")
    if isinstance(config.get("clean_rerun"), dict) and config["clean_rerun"]:
        raise ValueError(f"{expected_path}: {mode}.clean_rerun has unknown options: {sorted(config['clean_rerun'])}")
    if "clean_rerun" in config and config.get("rerun_generator") is not None:
        raise ValueError(f"{expected_path}: {mode} cannot declare both clean_rerun and rerun_generator")


def _validate_case(case: Any, expected_path: Path) -> dict[str, Any]:
    if not isinstance(case, dict):
        raise ValueError(f"{expected_path}: expected a YAML mapping")

    schema_errors = validation_errors(case, _load_eval_schema("case-v2.schema.json"))
    if schema_errors:
        raise ValueError(f"{expected_path}: case schema validation failed: {'; '.join(schema_errors)}")

    case_id = case.get("id")
    if not isinstance(case_id, str) or not case_id.strip():
        raise ValueError(f"{expected_path}: id must be a non-empty string")

    case_type = case.get("case_type")
    if case_type not in KNOWN_CASE_TYPES:
        raise ValueError(f"{expected_path}: case_type must be one of {sorted(KNOWN_CASE_TYPES)}, got {case_type!r}")

    if "mode" in case:
        raise ValueError(f"{expected_path}: legacy field 'mode' is not supported; use 'modes'")

    modes = case.get("modes")
    if not isinstance(modes, list) or not modes:
        raise ValueError(f"{expected_path}: modes must be a non-empty list")
    if any(not isinstance(mode, str) or mode not in KNOWN_MODES for mode in modes):
        raise ValueError(f"{expected_path}: modes must contain only {sorted(KNOWN_MODES)}, got {modes!r}")
    if len(modes) != len(set(modes)):
        raise ValueError(f"{expected_path}: modes must not contain duplicates")

    score_types = case.get("score_types")
    if not isinstance(score_types, dict):
        raise ValueError(f"{expected_path}: score_types must map each mode to a score type")
    if set(score_types) != set(modes):
        raise ValueError(f"{expected_path}: score_types keys must exactly match modes; expected {sorted(modes)}, got {sorted(score_types)}")
    for mode, score_type in score_types.items():
        if score_type not in KNOWN_SCORE_TYPES:
            raise ValueError(f"{expected_path}: unknown score type {score_type!r} for mode {mode!r}; expected one of {sorted(KNOWN_SCORE_TYPES)}")
        if score_type == "contract_ci" and mode != "fixture":
            raise ValueError(f"{expected_path}: contract_ci is only valid for fixture mode")
        if score_type == "agent_benchmark" and mode != "live":
            raise ValueError(f"{expected_path}: agent_benchmark is only valid for live mode")
        if score_type == "integration_visual" and mode != "visual":
            raise ValueError(f"{expected_path}: integration_visual is only valid for visual mode")

    if "visual" in modes and case_type != "mutation" and "fixture" not in modes:
        raise ValueError(
            f"{expected_path}: visual mode executes the fixture generator; positive cases must also declare fixture mode"
        )

    if case_type == "mutation":
        if not modes or set(modes) - {"fixture", "visual"} or any(score_types[m] != "mutation_tests" for m in modes):
            raise ValueError(
                f"{expected_path}: mutation cases must map every mode (fixture and/or visual) to mutation_tests"
            )
        mutation_config = case.get("mutation")
        if not isinstance(mutation_config, dict):
            raise ValueError(f"{expected_path}: mutation cases require a mutation mapping")
        _validate_command(
            mutation_config.get("control_generator"),
            "mutation.control_generator",
            expected_path,
        )
        if not mutation_config.get("control_generator"):
            raise ValueError(f"{expected_path}: mutation.control_generator is required for a healthy twin")
        unknown_mutation_options = set(mutation_config) - {"control_generator"}
        if unknown_mutation_options:
            raise ValueError(f"{expected_path}: mutation has unknown options: {sorted(unknown_mutation_options)}")
    elif "mutation_tests" in score_types.values():
        raise ValueError(f"{expected_path}: positive cases cannot contribute to mutation_tests")
    elif "mutation" in case:
        raise ValueError(f"{expected_path}: positive cases cannot declare mutation configuration")

    _validate_relative_dir(case.get("project_dir", "project"), "project_dir")

    for mode in modes:
        config = case.get(mode)
        if config is None and mode == "visual":
            # visual mode reuses the fixture execution config unless a
            # dedicated visual block is declared.
            config = case.get("fixture")
        if not isinstance(config, dict):
            raise ValueError(f"{expected_path}: {mode} must be a configuration mapping")

    if "fixture" in modes:
        fixture_config = case["fixture"]
        _validate_command(fixture_config.get("generator"), "fixture.generator", expected_path)
        _validate_rerun_config(fixture_config, "fixture", expected_path)
        extra_generators = fixture_config.get("extra_generators") or {}
        if not isinstance(extra_generators, dict):
            raise ValueError(f"{expected_path}: fixture.extra_generators must be a mapping")
        for project_dir, command in extra_generators.items():
            _validate_relative_dir(project_dir, "fixture.extra_generators key")
            _validate_command(command, f"fixture.extra_generators[{project_dir!r}]", expected_path)
        source_baseline = fixture_config.get("source_baseline") or []
        if not isinstance(source_baseline, list):
            raise ValueError(f"{expected_path}: fixture.source_baseline must be a list")
        for index, entry in enumerate(source_baseline):
            location = f"{expected_path}: fixture.source_baseline[{index}]"
            if not isinstance(entry, dict):
                raise ValueError(f"{location} must be a mapping")
            source = entry.get("source")
            if not isinstance(source, str) or not source.strip() or Path(source).is_absolute():
                raise ValueError(f"{location}.source must be a non-empty relative path")
            _validate_relative_dir(entry.get("destination"), f"{location}.destination")

    if "live" in modes:
        live_config = case["live"]
        _validate_rerun_config(live_config, "live", expected_path)
        agent = live_config.get("agent", "claude_code")
        if agent not in KNOWN_AGENTS:
            raise ValueError(f"{expected_path}: unknown agent {agent!r}; expected one of {sorted(KNOWN_AGENTS)}")
        prompt_file = live_config.get("prompt_file", "prompt.md")
        _validate_relative_dir(prompt_file, "live.prompt_file")
        _validate_relative_dir(
            live_config.get("agent_workdir", case.get("project_dir", "project")),
            "live.agent_workdir",
        )
        live_fixtures = live_config.get("fixtures") or []
        if not isinstance(live_fixtures, list):
            raise ValueError(f"{expected_path}: live.fixtures must be a list")
        for index, fixture in enumerate(live_fixtures):
            location = f"{expected_path}: live.fixtures[{index}]"
            if not isinstance(fixture, dict):
                raise ValueError(f"{location} must be a mapping")
            source = fixture.get("source")
            if not isinstance(source, str) or not source.strip() or Path(source).is_absolute():
                raise ValueError(f"{location}.source must be a non-empty relative path")
            _validate_relative_dir(fixture.get("destination"), f"live.fixtures[{index}].destination")

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
        expect_code = entry.get("expect_code")
        if expect_code is not None:
            if not isinstance(expect_code, str) or not expect_code.strip():
                raise ValueError(f"{location}.expect_code must be a non-empty string")
            if expect == "passed":
                raise ValueError(f"{location}.expect_code requires expect to be non-'passed'")
        elif case_type == "mutation" and expect != "passed":
            raise ValueError(
                f"{location}: mutation cases must declare expect_code alongside expect={expect!r} so status alone cannot satisfy the injected defect"
            )
        entry_modes = entry.get("modes")
        if entry_modes is not None:
            if not isinstance(entry_modes, list) or not entry_modes or any(
                not isinstance(m, str) or m not in KNOWN_MODES for m in entry_modes
            ):
                raise ValueError(f"{location}.modes must be a non-empty list of {sorted(KNOWN_MODES)}")
            if any(m not in modes for m in entry_modes):
                raise ValueError(f"{location}.modes must be a subset of the case modes {modes}")
        hard_gate = entry.get("hard_gate", case.get("hard_gate", True))
        if not isinstance(hard_gate, bool):
            raise ValueError(f"{location}.hard_gate must be true or false")

    assertion_names = {entry["assert"] for entry in assertions}
    if case_type == "mutation":
        targets = [entry for entry in assertions if entry.get("expect", "passed") != "passed"]
        if len(targets) != 1:
            raise ValueError(f"{expected_path}: mutation cases must declare exactly one non-passing target assertion; found {len(targets)}")
        if not targets[0].get("hard_gate", case.get("hard_gate", True)):
            raise ValueError(f"{expected_path}: mutation target assertion must be a hard gate")
    for mode in modes:
        execution_base = case[mode] if mode in case else case["fixture"]
        if "clean_rerun" in execution_base and "rerun.clean_execution_succeeded" not in assertion_names:
            raise ValueError(f"{expected_path}: {mode}.clean_rerun requires rerun.clean_execution_succeeded")

    return case


def _load_case(case_dir: Path) -> dict[str, Any]:
    expected_path = case_dir / "expected.yaml"
    try:
        with expected_path.open("r", encoding="utf-8") as fh:
            case = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"could not load {expected_path}: {exc}") from exc
    return _validate_case(case, expected_path)


def _prepare_workspace(case_dir: Path, case_def: dict[str, Any], mode: str) -> Path:
    workspace = Path(tempfile.mkdtemp(prefix=f"openmapstack-eval-{case_dir.name}-"))
    project_dirs = {case_def.get("project_dir", "project")}
    if mode in {"fixture", "visual"}:
        project_dirs.update((case_def.get(mode, case_def.get("fixture")).get("extra_generators") or {}).keys())
    for project_dir_name in project_dirs:
        project_src = case_dir / project_dir_name
        if mode in {"fixture", "visual"} and project_src.exists():
            shutil.copytree(project_src, workspace / project_dir_name, dirs_exist_ok=True)
        else:
            (workspace / project_dir_name).mkdir(parents=True, exist_ok=True)
    return workspace


def _prepare_live_fixtures(case_dir: Path, workspace: Path, live_config: dict[str, Any]) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    fixture_root = CASES_DIR.parent.resolve()
    for fixture in live_config.get("fixtures") or []:
        source = (case_dir / fixture["source"]).resolve()
        try:
            source.relative_to(fixture_root)
        except ValueError as exc:
            raise SetupFailure(
                "live_fixture",
                f"fixture source must remain inside {fixture_root}: {fixture['source']!r}",
            ) from exc
        if not source.exists():
            raise SetupFailure("live_fixture", f"fixture source does not exist: {source}")

        destination = workspace / fixture["destination"]
        if source.is_dir():
            shutil.copytree(source, destination, dirs_exist_ok=True)
            kind = "directory"
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            kind = "file"
        prepared.append(
            {
            "source": str(source),
            "destination": str(destination),
            "kind": kind,
            }
        )
    return prepared


def _under_project(path: Path, project_path: Path) -> bool:
    try:
        path.resolve().relative_to(project_path.resolve())
    except ValueError:
        return False
    return True


def _hash_declared_source_baseline(case_dir: Path, source_baseline: list[dict[str, str]]) -> dict[str, str]:
    """Real sha256 of the committed, checked-in fixture files a case declares
    as its immutable ground truth (``fixture.source_baseline``), keyed by the
    project-relative ``destination`` the generator is expected to copy them
    to. Read *before* the generator runs, mirroring live mode's
    pre-execution baseline, so a generator/pipeline that mutates its own
    "immutable" source after copying it in can be caught."""
    import hashlib

    fixture_root = CASES_DIR.parent.resolve()
    hashes: dict[str, str] = {}
    for entry in source_baseline:
        source = (case_dir / entry["source"]).resolve()
        try:
            source.relative_to(fixture_root)
        except ValueError as exc:
            raise SetupFailure(
                "source_baseline",
                f"source_baseline entry must remain inside {fixture_root}: {entry['source']!r}",
            ) from exc
        if not source.is_file():
            raise SetupFailure("source_baseline", f"source_baseline file does not exist: {source}")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        hashes[entry["destination"]] = f"sha256:{digest}"
    return hashes


def _hash_workspace_files(project_path: Path, relative_paths: list[str]) -> dict[str, str]:
    """Real sha256 of specific project-relative files, for use as an
    eval-owned pre-execution baseline (``$SOURCE_HASHES``). Never trusts any
    declared/authored hash — always reads actual bytes on disk."""
    import hashlib

    hashes: dict[str, str] = {}
    for relative in relative_paths:
        target = project_path / relative
        if target.is_file():
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            hashes[relative] = f"sha256:{digest}"
    return hashes


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


def _execute_argv(command: list[str], cwd: Path, timeout_s: int | float, env: dict[str, str]) -> dict[str, Any]:
    """Execute a shell-free canonical project entrypoint."""
    started = time.monotonic()
    try:
        proc = subprocess.run(
            command,
            shell=False,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=env,
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
            "timeout_s": timeout_s,
            "duration_s": time.monotonic() - started,
            "stdout": _output_text(exc.stdout),
            "stderr": _output_text(exc.stderr),
        }
    except OSError as exc:
        return {
            "command": command,
            "cwd": str(cwd),
            "returncode": None,
            "timed_out": False,
            "duration_s": time.monotonic() - started,
            "stdout": "",
            "stderr": f"{type(exc).__name__}: {exc}",
        }


def _safe_project_path(project_root: Path, value: Any, field_name: str) -> tuple[Path, Path]:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty project-relative path")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"{field_name} escapes the project: {value!r}")
    target = (project_root / relative).resolve()
    try:
        normalized = target.relative_to(project_root.resolve())
    except ValueError as exc:
        raise ValueError(f"{field_name} escapes the project: {value!r}") from exc
    return target, normalized


def _hash_immutable_inputs(rerun_root: Path, preserved: set[str]) -> dict[str, str]:
    """Real sha256 of every immutable source/override file actually on disk.

    Only ``data/source/`` and ``data/overrides/`` are covered: these are the
    only paths the spec declares immutable. The canonical entrypoint is
    expected to write/replace files elsewhere (derived outputs, reports,
    run records); it must never touch these two trees.
    """
    import hashlib

    hashes: dict[str, str] = {}
    for relative in sorted(preserved):
        if not (relative == "data/source" or relative == "data/overrides" or relative.startswith("data/source/") or relative.startswith("data/overrides/")):
            continue
        target = rerun_root / relative
        if target.is_dir():
            for file_path in sorted(target.rglob("*")):
                if file_path.is_file():
                    digest = hashlib.sha256(file_path.read_bytes()).hexdigest()
                    hashes[str(file_path.relative_to(rerun_root).as_posix())] = f"sha256:{digest}"
        elif target.is_file():
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            hashes[relative] = f"sha256:{digest}"
    return hashes


def _copy_clean_rerun_path(
    project_root: Path,
    rerun_root: Path,
    value: str,
    field_name: str,
    preserved: set[str],
) -> None:
    source, relative = _safe_project_path(project_root, value, field_name)
    relative_text = relative.as_posix()
    if relative_text in preserved:
        return
    if not source.exists():
        raise ValueError(f"declared clean-rerun dependency does not exist: {value}")
    paths = [source]
    if source.is_dir():
        paths.extend(source.rglob("*"))
    if any(path.is_symlink() for path in paths):
        raise ValueError(f"clean-rerun dependency may not contain symlinks: {value}")

    destination = rerun_root / relative
    if source.is_dir():
        shutil.copytree(source, destination, dirs_exist_ok=True)
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    preserved.add(relative_text)


def _canonical_rerun_command(project_root: Path, project: dict[str, Any]) -> tuple[list[str], list[tuple[str, str]]]:
    runtime = project.get("runtime")
    implementation = runtime.get("implementation") if isinstance(runtime, dict) else None
    if not isinstance(implementation, dict):
        raise ValueError("runtime.implementation is missing")

    preserve: list[tuple[str, str]] = []
    dependencies = implementation.get("dependencies") or []
    if not isinstance(dependencies, list) or not all(isinstance(item, str) and item.strip() for item in dependencies):
        raise ValueError("runtime.implementation.dependencies must be a list of paths")
    preserve.extend((dependency, f"runtime.implementation.dependencies[{index}]") for index, dependency in enumerate(dependencies))

    declared_command = implementation.get("command")
    if declared_command is not None:
        if isinstance(declared_command, str):
            command = shlex.split(declared_command)
        elif isinstance(declared_command, list) and all(isinstance(item, str) and item for item in declared_command):
            command = list(declared_command)
        else:
            raise ValueError("runtime.implementation.command must be a string or list of strings")
        if not command:
            raise ValueError("runtime.implementation.command is empty")

        for index, token in enumerate(command):
            if str(EVALS_DIR.resolve()) in token or "evals/fixtures/reference_pipeline" in token:
                raise ValueError("canonical command depends on the eval reference generator")
            token_path = Path(token)
            if index > 0 and token_path.is_absolute():
                raise ValueError(f"canonical command argument must not use an absolute path: {token!r}")
            if ".." in token_path.parts:
                raise ValueError(f"canonical command must not escape the project: {token!r}")
            if not token.startswith("-") and not token_path.is_absolute():
                candidate = project_root / token_path
                if candidate.exists():
                    preserve.append((token, f"runtime.implementation.command[{index}]"))
        return command, preserve

    pipeline = implementation.get("pipeline")
    pipeline_path, relative = _safe_project_path(project_root, pipeline, "runtime.implementation.pipeline")
    if not pipeline_path.is_file():
        raise ValueError(f"canonical pipeline does not exist: {pipeline!r}")
    preserve.append((relative.as_posix(), "runtime.implementation.pipeline"))
    if pipeline_path.suffix.lower() == ".py":
        return [sys.executable, relative.as_posix()], preserve
    if pipeline_path.stat().st_mode & 0o111:
        executable = relative.as_posix()
        return [executable if executable.startswith("./") else f"./{executable}"], preserve
    raise ValueError("non-Python canonical pipeline is not executable and declares no command")


def _clean_rerun_environment() -> tuple[dict[str, str], list[str]]:
    env = dict(os.environ)
    sensitive_fragments = (
        "ANTHROPIC",
        "CHAT",
        "CLAUDE",
        "CODEX",
        "CONVERSATION",
        "OPENAI",
        "PROMPT",
        "TRANSCRIPT",
    )
    removed = sorted(key for key in env if any(fragment in key.upper() for fragment in sensitive_fragments))
    for key in removed:
        env.pop(key, None)
    env.pop("PYTHONPATH", None)
    env["OPENMAPSTACK_CLEAN_RERUN"] = "1"
    return env, removed


def _write_clean_rerun_evidence(rerun_root: Path, evidence: dict[str, Any]) -> None:
    (rerun_root / CLEAN_RERUN_EVIDENCE).write_text(json.dumps(evidence, indent=2, default=str), encoding="utf-8")


def _perform_clean_rerun(project_root: Path, rerun_root: Path, timeout_s: int | float) -> dict[str, Any]:
    """Rebuild a project from its manifest, local immutable inputs, and declared dependencies."""
    evidence: dict[str, Any] = {
        "schema": "openmapstack-clean-rerun/v1",
        "status": "failed",
        "stage": "preparation",
        "preserved_paths": [],
        "excluded_artifact_classes": [
            "derived_outputs",
            "validation_reports",
            "run_records",
            "caches",
            "presentation_artifacts",
            "conversation_state",
        ],
    }
    preserved: set[str] = set()
    try:
        manifest_path = project_root / "project.yaml"
        if not manifest_path.is_file():
            raise ValueError("project.yaml is missing")
        try:
            project = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise ValueError(f"project.yaml cannot be loaded: {exc}") from exc
        if not isinstance(project, dict):
            raise ValueError("project.yaml must contain a mapping")

        command, declared_paths = _canonical_rerun_command(project_root, project)
        _copy_clean_rerun_path(project_root, rerun_root, "project.yaml", "project manifest", preserved)
        for conventional_path in ("data/source", "data/overrides"):
            if (project_root / conventional_path).exists():
                _copy_clean_rerun_path(
                    project_root,
                    rerun_root,
                    conventional_path,
                    f"clean-rerun input {conventional_path}",
                    preserved,
                )
        for path, field_name in declared_paths:
            _copy_clean_rerun_path(project_root, rerun_root, path, field_name, preserved)

        evidence["preserved_paths"] = sorted(preserved)
        source_hashes_before = _hash_immutable_inputs(rerun_root, preserved)
        evidence["command"] = command
        env, removed_environment = _clean_rerun_environment()
        evidence["removed_environment_keys"] = removed_environment
        execution = _execute_argv(command, rerun_root, timeout_s, env)
        evidence["execution"] = execution
        if execution.get("timed_out"):
            evidence["stage"] = "canonical_execution"
            evidence["error"] = f"canonical entrypoint timed out after {timeout_s}s"
            _write_clean_rerun_evidence(rerun_root, evidence)
            return evidence
        if execution.get("returncode") != 0:
            evidence["stage"] = "canonical_execution"
            evidence["error"] = f"canonical entrypoint exited with status {execution.get('returncode')}"
            _write_clean_rerun_evidence(rerun_root, evidence)
            return evidence

        evidence["stage"] = "source_integrity"
        source_hashes_after = _hash_immutable_inputs(rerun_root, preserved)
        mutated = sorted(relative for relative, digest in source_hashes_before.items() if source_hashes_after.get(relative) != digest)
        evidence["source_hashes"] = source_hashes_after
        if mutated:
            evidence["error"] = f"canonical entrypoint mutated declared-immutable source/override files: {mutated}"
            evidence["mutated_source_files"] = mutated
            _write_clean_rerun_evidence(rerun_root, evidence)
            return evidence

        evidence["stage"] = "artifact_validation"
        validation = validate_project(rerun_root / "project.yaml", artifacts=True)
        evidence["artifact_validation"] = validation.to_dict()
        if not validation.ok():
            evidence["error"] = "post-rerun artifact validation failed"
            _write_clean_rerun_evidence(rerun_root, evidence)
            return evidence

        evidence["status"] = "passed"
        evidence["stage"] = "complete"
        _write_clean_rerun_evidence(rerun_root, evidence)
        return evidence
    except (OSError, ValueError) as exc:
        evidence["preserved_paths"] = sorted(preserved)
        evidence["error"] = f"{type(exc).__name__}: {exc}"
        _write_clean_rerun_evidence(rerun_root, evidence)
        return evidence


def _require_command_success(result: dict[str, Any], stage: str) -> None:
    if result.get("timed_out"):
        raise SetupFailure(stage, f"command timed out after {result.get('timeout_s')}s", result)
    if result.get("returncode") != 0:
        raise SetupFailure(stage, f"command exited with status {result.get('returncode')}", result)


def _format_command(command: str, project_path: Path) -> str:
    """Expand the placeholders a case's generator command may use.

    ``{python}`` is the interpreter running this file, not a bare ``python3`` on
    PATH. A hardcoded ``python3`` silently escapes an active virtualenv (the
    generators then fail on a missing duckdb) and does not exist at all on a
    stock Windows install.
    """
    return command.format(
        python=shlex.quote(sys.executable),
        repo_root=REPO_ROOT,
        evals_dir=EVALS_DIR,
        project_dir=project_path,
    )


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
    if hasattr(agent_result, "normalized"):
        result = agent_result.normalized(include_streams=True)
    else:
        result = {
            "schema": "openmapstack-agent-run/v1",
        "agent": agent_result.agent,
        "model": agent_result.model,
            "version": getattr(agent_result, "version", None),
        "success": agent_result.success,
        "returncode": agent_result.returncode,
        "command": agent_result.command,
        "duration_s": agent_result.duration_s,
            "event_count": len(getattr(agent_result, "events", [])),
            "events": getattr(agent_result, "events", []),
        "stdout": agent_result.stdout,
        "stderr": agent_result.stderr,
            "usage": getattr(agent_result, "usage", {}),
            "cost_usd": getattr(agent_result, "cost_usd", None),
            "final_message": getattr(agent_result, "final_message", None),
            "permissions": getattr(agent_result, "permissions", {}),
        "metadata": agent_result.metadata,
    }
    validation_candidate = {key: value for key, value in result.items() if key not in {"events", "stdout", "stderr"}}
    schema_errors = validation_errors(validation_candidate, _load_eval_schema("agent-run-v1.schema.json"))
    if schema_errors:
        raise SetupFailure(
            "agent_result",
            f"adapter returned an invalid normalized result: {'; '.join(schema_errors)}",
        )
    return result


def _git_revision() -> dict[str, Any]:
    revision: dict[str, Any] = {"commit": None, "dirty": None}
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if commit.returncode == 0:
            revision["commit"] = commit.stdout.strip() or None
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if status.returncode == 0:
            revision["dirty"] = bool(status.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return revision


def _new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _validate_run_id(value: str) -> str:
    if not value or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_." for character in value):
        raise ValueError("run id may contain only letters, digits, '.', '_', and '-'")
    if value in {".", ".."}:
        raise ValueError("run id must name a directory")
    return value


def _evidence_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _prepare_skill_snapshot(workspace: Path) -> tuple[Path, str]:
    """Copy only the distributable skill context, never eval/reference outputs."""
    import hashlib

    destination = workspace / "benchmark-context" / "openmapstack"
    destination.mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "SKILL.md", destination / "SKILL.md")
    for directory in ("references", "templates"):
        shutil.copytree(
            REPO_ROOT / directory,
            destination / directory,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )

    digest = hashlib.sha256()
    for path in sorted(item for item in destination.rglob("*") if item.is_file()):
        relative = path.relative_to(destination).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return destination, f"sha256:{digest.hexdigest()}"


def _skill_augmented_prompt(prompt: str, agent_workdir: Path, skill_dir: Path) -> str:
    relative_skill = Path(os.path.relpath(skill_dir / "SKILL.md", agent_workdir)).as_posix()
    return (
        "Use the controlled OpenMapStack skill snapshot at "
        f"`{relative_skill}` for this task. Read that SKILL.md and its referenced "
        "`references/project-spec.md` before building. The benchmark-context directory "
        "is read-only task guidance, not part of the generated project.\n\n"
        "---\n\n"
        f"{prompt}"
    )


def _agent_artifact_record(
    agent_run: dict[str, Any] | None,
    *,
    agent_name: str | None,
    model: str | None,
    timeout_s: int | float,
    seed: int | None,
) -> dict[str, Any]:
    if agent_run is None:
        return {
            "schema": "openmapstack-agent-run/v1",
            "agent": agent_name or "unresolved",
            "model": model,
            "version": None,
            "success": False,
            "returncode": None,
            "command": [],
            "duration_s": 0.0,
            "event_count": 0,
            "usage": {},
            "cost_usd": None,
            "final_message": None,
            "permissions": {},
            "metadata": {
                "structured_completion": False,
                "timeout_s": timeout_s,
                "requested_seed": seed,
                "adapter_not_started": True,
            },
        }
    return {key: value for key, value in agent_run.items() if key not in {"events", "stdout", "stderr"}}


def _write_trial_bundle(
    bundle_dir: Path,
    *,
    prompt: str,
    project_path: Path,
    result: dict[str, Any],
    agent_name: str | None,
    model: str | None,
    timeout_s: int | float,
    seed: int | None,
) -> None:
    """Persist enough evidence to audit a live trial after its temp workspace is gone."""
    bundle_dir.mkdir(parents=True, exist_ok=False)
    agent_run = result.get("agent_run")
    events = agent_run.get("events", []) if isinstance(agent_run, dict) else []
    stdout = agent_run.get("stdout", "") if isinstance(agent_run, dict) else ""
    stderr = agent_run.get("stderr", "") if isinstance(agent_run, dict) else ""
    agent_record = _agent_artifact_record(
        agent_run,
        agent_name=agent_name,
        model=model,
        timeout_s=timeout_s,
        seed=seed,
    )
    agent_record["metadata"].setdefault("benchmark_context", result.get("benchmark_context", {}))
    schema_errors = validation_errors(agent_record, _load_eval_schema("agent-run-v1.schema.json"))
    if schema_errors:
        raise ValueError(f"invalid persisted agent record: {'; '.join(schema_errors)}")

    (bundle_dir / "prompt.md").write_text(prompt, encoding="utf-8")
    (bundle_dir / "events.ndjson").write_text(
        "".join(json.dumps(event, default=str) + "\n" for event in events),
        encoding="utf-8",
    )
    (bundle_dir / "stdout.txt").write_text(stdout, encoding="utf-8")
    (bundle_dir / "stderr.txt").write_text(stderr, encoding="utf-8")
    (bundle_dir / "agent.json").write_text(json.dumps(agent_record, indent=2, default=str), encoding="utf-8")
    generated_project = bundle_dir / "generated-project"
    if project_path.is_dir():
        shutil.copytree(project_path, generated_project, symlinks=True)
    else:
        generated_project.mkdir()

    grading_record = dict(result)
    grading_record["agent_run"] = agent_record
    (bundle_dir / "grading.json").write_text(json.dumps(grading_record, indent=2, default=str), encoding="utf-8")


def _write_visual_bundle(
    bundle_dir: Path,
    *,
    workspace: Path,
    project_path: Path,
    result: dict[str, Any],
) -> None:
    """Persist enough evidence to audit a visual-mode trial after its temp
    workspace is gone: the graded result, the generated project, and every
    rendered snapshot (PyQGIS renders, dashboard screenshots)."""
    bundle_dir.mkdir(parents=True, exist_ok=False)
    generated_project = bundle_dir / "generated-project"
    if project_path.is_dir():
        shutil.copytree(project_path, generated_project, symlinks=True)
    else:
        generated_project.mkdir()
    visual_dir = workspace / "visual"
    if visual_dir.is_dir():
        shutil.copytree(visual_dir, bundle_dir / "visual", symlinks=True)
    (bundle_dir / "grading.json").write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")


def _assertion_entries(
    case_def: dict[str, Any],
    source_hashes_before: dict[str, str],
    case_mode: str | None = None,
) -> list[dict[str, Any]]:
    entries = list(case_def.get("assertions", []))
    if case_mode is not None:
        # Assertions may be scoped to specific execution modes (e.g. PyQGIS
        # runtime and browser checks that only run in a visual-mode
        # integration environment); unscoped entries apply to every mode.
        entries = [
            entry for entry in entries
            if not entry.get("modes") or case_mode in entry["modes"]
        ]
    has_preexecution_integrity_check = any(
        entry.get("assert") == "overrides.source_files_byte_identical" and (entry.get("args") or {}).get("hashes_before") == "$SOURCE_HASHES"
        for entry in entries
    )
    if source_hashes_before and not has_preexecution_integrity_check:
        entries.insert(
            0,
            {
            "assert": "overrides.source_files_byte_identical",
            "args": {
                "hashes_before": "$SOURCE_HASHES",
                "paths": sorted(source_hashes_before),
            },
            "hard_gate": True,
            },
        )
    return entries


def _evaluate_assertions(
    case_def: dict[str, Any],
    project_path: Path,
    entries: list[dict[str, Any]],
    source_hashes_before: dict[str, str],
    rerun_workspace_path: Path | None,
    *,
    healthy_control: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, int]]]:
    assertion_results: list[dict[str, Any]] = []
    dimension_totals: dict[str, dict[str, int]] = {}

    for entry in entries:
        assert_name = entry["assert"]
        args = dict(entry.get("args", {}) or {})
        if rerun_workspace_path is not None and args.get("rerun_workspace") == "$RERUN":
            args["rerun_workspace"] = str(rerun_workspace_path)
        if args.get("hashes_before") == "$SOURCE_HASHES":
            args["hashes_before"] = source_hashes_before
            args["require_complete_tree"] = True

        declared_expect = entry.get("expect", "passed")
        mutation_role = "target" if declared_expect != "passed" else "guard"
        expect = "passed" if healthy_control else declared_expect
        expect_code = None if healthy_control else entry.get("expect_code")
        module_name, fn = _resolve_assertion(assert_name)

        try:
            result: AssertionResult = fn(project_path, **args)
        except Exception as exc:  # noqa: BLE001
            raise SetupFailure(
                "assertion_execution",
                f"{assert_name} raised {type(exc).__name__}: {exc}",
                {
                    "assert": assert_name,
                    "args": args,
                    "healthy_control": healthy_control,
                },
            ) from exc

        matched = result.status == expect
        if matched and expect_code is not None:
            matched = result.data.get("code") == expect_code
        dim = DIMENSIONS.get(module_name, "other")
        bucket = dimension_totals.setdefault(dim, {"passed": 0, "failed": 0})
        bucket["passed" if matched else "failed"] += 1

        assertion_results.append(
            {
            "assert": assert_name,
            "args": args,
            "expect": expect,
            "expect_code": expect_code,
            "actual_status": result.status,
            "actual_code": result.data.get("code"),
            "detail": result.detail,
            "matched_expectation": matched,
            "hard_gate": entry.get("hard_gate", case_def.get("hard_gate", True)),
            "mutation_role": mutation_role if case_def["case_type"] == "mutation" else None,
            "data": result.data,
            }
        )

    return assertion_results, dimension_totals


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
        return [_portable_result_paths(item, workspace, rerun_workspace, keep_workspace=keep_workspace) for item in value]
    if isinstance(value, dict):
        return {key: _portable_result_paths(item, workspace, rerun_workspace, keep_workspace=keep_workspace) for key, item in value.items()}
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
    artifact_dir: Path | None = None,
    benchmark_context: dict[str, Any] | None = None,
    skill_mode: str = "disabled",
) -> dict[str, Any]:
    """Execute one case/trial, keeping setup failures out of assertion scores."""
    if skill_mode not in {"enabled", "disabled"}:
        raise ValueError("skill_mode must be 'enabled' or 'disabled'")
    case_def = _load_case(case_dir)
    case_id = case_def.get("id", case_dir.name)
    case_type = case_def["case_type"]
    supported_modes = case_def["modes"]

    if mode_filter and mode_filter not in supported_modes:
        return {
            "id": case_id,
            "trial": trial,
            "case_type": case_type,
            "mode": mode_filter,
            "score_type": None,
            "supported_modes": supported_modes,
            "status": "skipped",
            "skipped": True,
            "reason": f"mode={mode_filter} is not supported; supports {supported_modes}",
        }

    case_mode = mode_filter or supported_modes[0]
    score_type = case_def["score_types"][case_mode]
    # Visual mode reuses the fixture execution config unless a dedicated
    # visual block exists: the same generator produces the artifacts, the
    # difference is the richer validation environment (PyQGIS + headless
    # browser) and the separate integration_visual score bucket.
    execution_config = case_def.get(case_mode) or case_def["fixture"]

    started = time.monotonic()
    workspace = _prepare_workspace(case_dir, case_def, case_mode)
    project_dir = case_def.get("project_dir", "project")
    project_path = workspace / project_dir
    keep_workspace = bool(case_def.get("keep_workspace", False))
    rerun_workspace_path: Path | None = None
    generator_result: dict[str, Any] | None = None
    extra_generator_results: list[dict[str, Any]] = []
    rerun_generator_result: dict[str, Any] | None = None
    clean_rerun_result: dict[str, Any] | None = None
    agent_run: dict[str, Any] | None = None
    live_fixtures: list[dict[str, Any]] = []
    assertion_results: list[dict[str, Any]] = []
    dimension_totals: dict[str, dict[str, int]] = {}
    source_hashes_before: dict[str, str] = {}
    control_workspace_path: Path | None = None
    mutation_control: dict[str, Any] | None = None
    prompt = ""
    agent_name = agent_override
    benchmark_context = dict(benchmark_context or {})
    artifact_attempted = False

    def finalize(payload: dict[str, Any]) -> dict[str, Any]:
        nonlocal artifact_attempted
        portable = _portable_result_paths(
            payload,
            workspace,
            rerun_workspace_path,
            keep_workspace=keep_workspace,
        )
        if artifact_dir is not None and case_mode in {"live", "visual"} and not artifact_attempted:
            artifact_attempted = True
            portable["artifact_bundle"] = _evidence_path(artifact_dir)
            try:
                if case_mode == "live":
                    _write_trial_bundle(
                        artifact_dir,
                        prompt=prompt,
                        project_path=project_path,
                        result=portable,
                        agent_name=agent_name,
                        model=model,
                        timeout_s=timeout_s,
                        seed=seed,
                    )
                else:
                    _write_visual_bundle(artifact_dir, workspace=workspace, project_path=project_path, result=portable)
            except (OSError, ValueError) as exc:
                portable["status"] = "setup_failed"
                portable["assertions"] = []
                portable["dimension_totals"] = {}
                portable["hard_failures"] = []
                portable["setup_error"] = {
                    "stage": "artifact_persistence",
                    "message": f"{type(exc).__name__}: {exc}",
                    "data": {"artifact_bundle": _evidence_path(artifact_dir)},
                }
        return portable

    try:
        if case_mode in {"fixture", "visual"}:
            source_hashes_before = _hash_declared_source_baseline(case_dir, execution_config.get("source_baseline") or [])
            generator = execution_config.get("generator")
            if generator:
                command = _format_command(generator, project_path)
                generator_result = _execute_command(command, project_path, timeout_s)
                _require_command_success(generator_result, "generator")

            for extra_dir_name, extra_cmd in (execution_config.get("extra_generators") or {}).items():
                extra_path = workspace / extra_dir_name
                command = _format_command(extra_cmd, extra_path)
                result = _execute_command(command, extra_path, timeout_s)
                result["project_dir"] = extra_dir_name
                extra_generator_results.append(result)
                _require_command_success(result, f"extra_generator:{extra_dir_name}")

            if case_type == "mutation":
                control_workspace_path = _prepare_workspace(case_dir, case_def, case_mode)
                control_project_path = control_workspace_path / project_dir
                control_command = _format_command(case_def["mutation"]["control_generator"], control_project_path)
                control_generator_result = _execute_command(control_command, control_project_path, timeout_s)
                mutation_control = _portable_result_paths(
                    {
                        "generator": control_generator_result,
                        "assertions": [],
                        "dimension_totals": {},
                        "healthy": False,
                        "hard_failures": [],
                    },
                    control_workspace_path,
                    None,
                    keep_workspace=False,
                )
                if control_generator_result.get("timed_out"):
                    raise SetupFailure(
                        "mutation_control_generator",
                        f"command timed out after {control_generator_result.get('timeout_s')}s",
                        mutation_control,
                    )
                if control_generator_result.get("returncode") != 0:
                    raise SetupFailure(
                        "mutation_control_generator",
                        f"command exited with status {control_generator_result.get('returncode')}",
                        mutation_control,
                    )
                control_entries = _assertion_entries(case_def, source_hashes_before, case_mode)
                control_assertions, control_dimensions = _evaluate_assertions(
                    case_def,
                    control_project_path,
                    control_entries,
                    source_hashes_before,
                    None,
                    healthy_control=True,
                )
                control_hard_failures = [assertion for assertion in control_assertions if assertion["hard_gate"] and not assertion["matched_expectation"]]
                mutation_control = _portable_result_paths(
                    {
                        "generator": control_generator_result,
                        "assertions": control_assertions,
                        "dimension_totals": control_dimensions,
                        "healthy": not control_hard_failures,
                        "hard_failures": [assertion["assert"] for assertion in control_hard_failures],
                    },
                    control_workspace_path,
                    None,
                    keep_workspace=False,
                )
                if control_hard_failures:
                    raise SetupFailure(
                        "mutation_control",
                        "healthy control failed assertions; mutation is invalid and ungraded",
                        mutation_control,
                    )

        elif case_mode == "live":
            agent_name = agent_override or execution_config.get("agent", "claude_code")
            prompt_path = case_dir / execution_config.get("prompt_file", "prompt.md")
            if not prompt_path.is_file():
                raise SetupFailure("agent_preflight", f"prompt file does not exist: {prompt_path}")
            prompt = prompt_path.read_text(encoding="utf-8")
            agent_workdir = workspace / execution_config.get("agent_workdir", project_dir)
            agent_workdir.mkdir(parents=True, exist_ok=True)
            if skill_mode == "enabled":
                skill_dir, skill_digest = _prepare_skill_snapshot(workspace)
                prompt = _skill_augmented_prompt(prompt, agent_workdir, skill_dir)
                benchmark_context["skill"] = {
                    "mode": "enabled",
                    "commit": benchmark_context.get("skill_commit"),
                    "content_sha256": skill_digest,
                    "entrypoint": "benchmark-context/openmapstack/SKILL.md",
                }
            else:
                benchmark_context["skill"] = {
                    "mode": "disabled",
                    "commit": benchmark_context.get("skill_commit"),
                    "content_sha256": None,
                    "entrypoint": None,
                }
            adapter = _load_adapter(agent_name)
            if not adapter.is_available():
                raise SetupFailure(
                    "agent_preflight",
                    f"required executable {adapter.executable!r} for agent {agent_name!r} was not found on PATH",
                    {"agent": agent_name, "executable": adapter.executable},
                )
            live_fixtures = _prepare_live_fixtures(case_dir, workspace, execution_config)
            source_hashes_before = _hash_workspace_files(
                project_path,
                [
                    str(Path(fixture["destination"]).resolve().relative_to(project_path.resolve()))
                    for fixture in live_fixtures
                    if fixture["kind"] == "file" and _under_project(Path(fixture["destination"]), project_path)
                ],
            )
            agent_result = adapter.run(
                prompt,
                agent_workdir,
                fixture=None,
                timeout_s=int(timeout_s),
                model=model,
                seed=seed,
            )
            agent_run = _agent_result_dict(agent_result)
            agent_run["metadata"]["benchmark_context"] = benchmark_context
            if not agent_result.success:
                message = f"agent {agent_name!r} failed"
                if agent_result.returncode is not None:
                    message += f" with status {agent_result.returncode}"
                raise SetupFailure("agent_execution", message, agent_run)

        if "clean_rerun" in execution_config:
            rerun_workspace_path = Path(tempfile.mkdtemp(prefix=f"openmapstack-eval-{case_dir.name}-rerun-"))
            clean_rerun_result = _perform_clean_rerun(project_path, rerun_workspace_path, timeout_s)
        else:
            rerun_generator_cmd = execution_config.get("rerun_generator")
            if rerun_generator_cmd:
                rerun_workspace_path = Path(tempfile.mkdtemp(prefix=f"openmapstack-eval-{case_dir.name}-rerun-"))
                command = _format_command(rerun_generator_cmd, rerun_workspace_path)
                rerun_generator_result = _execute_command(command, rerun_workspace_path, timeout_s)
                _require_command_success(rerun_generator_result, "rerun_generator")

        assertion_entries = _assertion_entries(case_def, source_hashes_before, case_mode)
        assertion_results, dimension_totals = _evaluate_assertions(
            case_def,
            project_path,
            assertion_entries,
            source_hashes_before,
            rerun_workspace_path,
        )

        hard_failures = [a for a in assertion_results if a["hard_gate"] and not a["matched_expectation"]]
        status = "assertions_failed" if hard_failures else "passed"
        mutation_analysis = None
        if case_type == "mutation":
            target = next(a for a in assertion_results if a["mutation_role"] == "target")
            guards = [a for a in assertion_results if a["mutation_role"] == "guard"]
            mutation_analysis = {
                "healthy_control_passed": bool(mutation_control and mutation_control["healthy"]),
                "target_assertion": target["assert"],
                "target_expected_code": target["expect_code"],
                "target_detected": target["matched_expectation"],
                "guards_passed": all(guard["matched_expectation"] for guard in guards),
                "isolated": all(guard["matched_expectation"] for guard in guards),
                "control": mutation_control,
            }
        return finalize(
            {
            "id": case_id,
            "trial": trial,
            "case_type": case_type,
            "seed": seed,
            "mode": case_mode,
            "score_type": score_type,
            "supported_modes": supported_modes,
            "status": status,
            "skipped": False,
            "duration_s": time.monotonic() - started,
            "workspace": str(workspace) if keep_workspace else None,
            "workspace_retained": keep_workspace,
            "generator": generator_result,
            "extra_generators": extra_generator_results,
            "rerun_generator": rerun_generator_result,
            "clean_rerun": clean_rerun_result,
            "agent_run": agent_run,
            "live_fixtures": live_fixtures,
                "benchmark_context": benchmark_context,
            "mutation_analysis": mutation_analysis,
            "assertions": assertion_results,
            "dimension_totals": dimension_totals,
            "hard_failures": [a["assert"] for a in hard_failures],
            }
        )
    except SetupFailure as exc:
        return finalize(
            {
            "id": case_id,
            "trial": trial,
            "case_type": case_type,
            "seed": seed,
            "mode": case_mode,
            "score_type": score_type,
            "supported_modes": supported_modes,
            "status": "setup_failed",
            "skipped": False,
            "duration_s": time.monotonic() - started,
            "workspace": str(workspace) if keep_workspace else None,
            "workspace_retained": keep_workspace,
            "generator": generator_result,
            "extra_generators": extra_generator_results,
            "rerun_generator": rerun_generator_result,
            "clean_rerun": clean_rerun_result,
            "agent_run": agent_run,
            "live_fixtures": live_fixtures,
                "benchmark_context": benchmark_context,
            "mutation_analysis": (
                {
                        "healthy_control_passed": bool(mutation_control and mutation_control.get("healthy")),
                    "control": mutation_control,
                }
                if case_type == "mutation"
                else None
            ),
            "assertions": assertion_results,
            "dimension_totals": dimension_totals,
            "hard_failures": [],
                "setup_error": {
                    "stage": exc.stage,
                    "message": exc.message,
                    "data": exc.data,
                },
            }
        )
    except Exception as exc:  # noqa: BLE001
        return finalize(
            {
            "id": case_id,
            "trial": trial,
            "case_type": case_type,
            "seed": seed,
            "mode": case_mode,
            "score_type": score_type,
            "supported_modes": supported_modes,
            "status": "setup_failed",
            "skipped": False,
            "duration_s": time.monotonic() - started,
            "workspace": str(workspace) if keep_workspace else None,
            "workspace_retained": keep_workspace,
            "generator": generator_result,
            "extra_generators": extra_generator_results,
            "rerun_generator": rerun_generator_result,
            "clean_rerun": clean_rerun_result,
            "agent_run": agent_run,
            "live_fixtures": live_fixtures,
                "benchmark_context": benchmark_context,
            "mutation_analysis": (
                {
                        "healthy_control_passed": bool(mutation_control and mutation_control.get("healthy")),
                    "control": mutation_control,
                }
                if case_type == "mutation"
                else None
            ),
            "assertions": assertion_results,
            "dimension_totals": dimension_totals,
            "hard_failures": [],
                "setup_error": {
                    "stage": "runner",
                    "message": f"{type(exc).__name__}: {exc}",
                    "data": {},
                },
            }
        )
    finally:
        if not keep_workspace:
            shutil.rmtree(workspace, ignore_errors=True)
        if rerun_workspace_path is not None:
            shutil.rmtree(rerun_workspace_path, ignore_errors=True)
        if control_workspace_path is not None:
            shutil.rmtree(control_workspace_path, ignore_errors=True)


def discover_cases(only: str | list[str] | None) -> list[Path]:
    dirs = sorted(p for p in CASES_DIR.iterdir() if p.is_dir() and (p / "expected.yaml").exists())
    if not only:
        for case_dir in dirs:
            _load_case(case_dir)
        return dirs

    requested = [only] if isinstance(only, str) else only
    matches: list[Path] = []
    found: set[str] = set()
    for case_dir in dirs:
        case_id = _load_case(case_dir).get("id")
        if case_dir.name in requested or case_id in requested:
            matches.append(case_dir)
            found.update(value for value in requested if value in {case_dir.name, case_id})
    missing = [value for value in requested if value not in found]
    if missing:
        raise ValueError(f"unknown eval case selection(s): {missing}")
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


def _wilson_interval(successes: int, total: int, z: float = 1.96) -> list[float] | None:
    if total <= 0:
        return None
    proportion = successes / total
    denominator = 1 + z * z / total
    centre = (proportion + z * z / (2 * total)) / denominator
    margin = z * math.sqrt(proportion * (1 - proportion) / total + z * z / (4 * total * total)) / denominator
    return [max(0.0, centre - margin), min(1.0, centre + margin)]


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def _agent_benchmark_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    trials = [result for result in results if result.get("mode") == "live" and result.get("status") != "skipped"]
    graded = [result for result in trials if result.get("status") in {"passed", "assertions_failed"}]
    successes = sum(result.get("status") == "passed" for result in graded)
    durations = [float((result.get("agent_run") or {}).get("duration_s", result.get("duration_s", 0))) for result in trials]
    token_counts = [
        float(total)
        for result in trials
        if isinstance(
            total := ((result.get("agent_run") or {}).get("usage") or {}).get("total_tokens"),
            (int, float),
        )
    ]
    costs = [float(cost) for result in trials if isinstance(cost := (result.get("agent_run") or {}).get("cost_usd"), (int, float))]
    return {
        "task_success_rate": successes / len(graded) if graded else None,
        "task_success_rate_95ci": _wilson_interval(successes, len(graded)),
        "hard_safety_gate_rate": successes / len(graded) if graded else None,
        "success_at_1": successes / len(graded) if graded else None,
        "trials": len(trials),
        "graded_trials": len(graded),
        "median_duration_s": median(durations) if durations else None,
        "p95_duration_s": _p95(durations),
        "median_tokens": median(token_counts) if token_counts else None,
        "median_cost_usd": median(costs) if costs else None,
        "setup_failures": sum(result.get("status") == "setup_failed" for result in trials),
        "dimensions": rollup_dimensions(graded),
    }


def build_summary(results: list[dict[str, Any]], run_config: dict[str, Any]) -> dict[str, Any]:
    ran = [r for r in results if r.get("status") != "skipped"]
    passed = [r for r in ran if r.get("status") == "passed"]
    assertion_failures = [r for r in ran if r.get("status") == "assertions_failed"]
    setup_failures = [r for r in ran if r.get("status") == "setup_failed"]
    score_types: dict[str, dict[str, Any]] = {}
    for score_type in sorted(KNOWN_SCORE_TYPES):
        score_results = [r for r in ran if r.get("score_type") == score_type]
        score_passed = sum(r.get("status") == "passed" for r in score_results)
        score_assertion_failures = sum(r.get("status") == "assertions_failed" for r in score_results)
        score_setup_failures = sum(r.get("status") == "setup_failed" for r in score_results)
        graded_trials = score_passed + score_assertion_failures
        score_types[score_type] = {
            "trials_run": len(score_results),
            "graded_trials": graded_trials,
            "passed": score_passed,
            "assertions_failed": score_assertion_failures,
            "setup_failed": score_setup_failures,
            "pass_rate": score_passed / graded_trials if graded_trials else None,
            "dimensions": rollup_dimensions(score_results),
        }
    mutations = [result for result in ran if result.get("case_type") == "mutation"]
    mutation_invalid = sum(result.get("status") == "setup_failed" for result in mutations)
    mutation_detected = sum(result.get("status") == "passed" for result in mutations)
    mutation_survived = sum(result.get("status") == "assertions_failed" for result in mutations)
    valid_mutations = mutation_detected + mutation_survived
    isolated_mutations = sum(bool((result.get("mutation_analysis") or {}).get("isolated")) for result in mutations)
    return {
        "schema": "openmapstack-eval-results/v2",
        "run_config": run_config,
        "environment": _environment(),
        "selection": {
            "result_records": len(results),
            "trials_run": len(ran),
            "case_definitions_skipped": len(results) - len(ran),
        },
        "outcomes": {
            "passed": len(passed),
            "assertions_failed": len(assertion_failures),
            "setup_failed": len(setup_failures),
        },
        "run_setup_failed": False,
        "setup_errors": [],
        "score_types": score_types,
        "agent_benchmark": _agent_benchmark_summary(results),
        "mutation_score": {
            "total": len(mutations),
            "valid": valid_mutations,
            "detected": mutation_detected,
            "survived": mutation_survived,
            "invalid": mutation_invalid,
            "isolated": isolated_mutations,
            "score": mutation_detected / valid_mutations if valid_mutations else None,
        },
        "results": results,
    }


def _write_summary(summary: dict[str, Any], json_path: str | None) -> None:
    schema_errors = validation_errors(summary, _load_eval_schema("results-v2.schema.json"))
    if schema_errors:
        raise ValueError(f"eval result schema validation failed: {'; '.join(schema_errors)}")
    if json_path:
        out_path = Path(json_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
        print(f"\nWrote {out_path}")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / "latest.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--case",
        action="append",
        help="run only this case id/directory name; repeat to select several",
    )
    parser.add_argument(
        "--mode",
        choices=sorted(KNOWN_MODES),
        default="fixture",
        help="execution mode filter (default: fixture)",
    )
    parser.add_argument(
        "--agent",
        choices=sorted(KNOWN_AGENTS),
        help="override the live-case agent adapter",
    )
    parser.add_argument("--model", help="model passed to the selected live agent")
    parser.add_argument(
        "--timeout",
        type=_positive_int,
        default=900,
        help="per generator/agent timeout in seconds",
    )
    parser.add_argument("--repetitions", type=_positive_int, default=1, help="trials per selected case")
    parser.add_argument(
        "--seed",
        type=int,
        help="base seed recorded for the run; incremented per repetition",
    )
    parser.add_argument(
        "--skill-mode",
        choices=("enabled", "disabled"),
        default="enabled",
        help="inject the controlled OpenMapStack skill snapshot in live mode (default: enabled)",
    )
    parser.add_argument("--run-id", help="artifact run id (generated by default for live mode)")
    parser.add_argument(
        "--results-dir",
        type=Path,
        default=RESULTS_DIR,
        help="root directory for retained live benchmark bundles",
    )
    parser.add_argument(
        "--no-retain-artifacts",
        action="store_true",
        help="do not retain per-trial live evidence (intended only for adapter smoke tests)",
    )
    parser.add_argument("--json", help="write full machine-readable results to this path")
    parser.add_argument("--list", action="store_true", help="list discovered cases and exit")
    args = parser.parse_args(argv)

    revision = _git_revision()
    try:
        run_id = _validate_run_id(args.run_id or _new_run_id()) if args.mode in {"live", "visual"} else None
    except ValueError as exc:
        parser.error(str(exc))
    result_root = args.results_dir / run_id if run_id else None
    run_config = {
        "mode": args.mode,
        "agent": args.agent,
        "model": args.model,
        "timeout_s": args.timeout,
        "repetitions": args.repetitions,
        "seed": args.seed,
        "case": args.case,
        "run_id": run_id,
        "artifacts_retained": args.mode in {"live", "visual"} and not args.no_retain_artifacts,
        "artifact_root": _evidence_path(result_root) if result_root else None,
        "skill_commit": revision["commit"],
        "skill_worktree_dirty": revision["dirty"],
        "skill_mode": args.skill_mode if args.mode == "live" else None,
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
            mappings = ", ".join(f"{mode}:{case_def['score_types'][mode]}" for mode in case_def["modes"])
            print(f"{case_def.get('id', case_dir.name):40s} {mappings}")
        return 0

    has_selected_live_case = any("live" in _load_case(case_dir)["modes"] for case_dir in case_dirs)
    if args.mode == "live" and has_selected_live_case and not args.model:
        message = "live benchmarks require --model so the tested model identity is exact"
        print(message, file=sys.stderr)
        summary = build_summary([], run_config)
        summary["run_setup_failed"] = True
        summary["setup_errors"] = [{"stage": "model_identity", "message": message}]
        summary_path = args.json or (str(result_root / "summary.json") if result_root is not None else None)
        _write_summary(summary, summary_path)
        return 2

    if result_root is not None and not args.no_retain_artifacts and result_root.exists():
        message = f"artifact run directory already exists: {result_root}"
        print(message, file=sys.stderr)
        summary = build_summary([], run_config)
        summary["run_setup_failed"] = True
        summary["setup_errors"] = [{"stage": "artifact_preflight", "message": message}]
        _write_summary(summary, args.json)
        return 2

    results: list[dict[str, Any]] = []
    for case_dir in case_dirs:
        case_def = _load_case(case_dir)
        selected_score_type = case_def["score_types"].get(args.mode)
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
                    artifact_dir=(
                        result_root / (args.agent or case_def["live"].get("agent", "claude_code")) / case_def.get("id", case_dir.name) / str(trial)
                        if args.mode == "live" and result_root is not None and not args.no_retain_artifacts and "live" in case_def
                        else (
                            result_root / "visual" / case_def.get("id", case_dir.name) / str(trial)
                            if args.mode == "visual" and result_root is not None and not args.no_retain_artifacts and "visual" in case_def["modes"]
                            else None
                        )
                    ),
                    benchmark_context={
                        "run_id": run_id,
                        "skill_commit": revision["commit"],
                        "skill_worktree_dirty": revision["dirty"],
                        "environment": _environment(),
                    },
                    skill_mode=args.skill_mode,
                )
            except Exception as exc:  # noqa: BLE001
                result = {
                    "id": case_dir.name,
                    "trial": trial,
                    "case_type": case_def["case_type"],
                    "seed": trial_seed,
                    "mode": args.mode,
                    "score_type": selected_score_type,
                    "supported_modes": case_def["modes"],
                    "status": "setup_failed",
                    "skipped": False,
                    "duration_s": time.monotonic() - trial_started,
                    "workspace": None,
                    "workspace_retained": False,
                    "generator": None,
                    "extra_generators": [],
                    "rerun_generator": None,
                    "clean_rerun": None,
                    "agent_run": None,
                    "live_fixtures": [],
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
            marker = {
                "passed": "PASS",
                "assertions_failed": "FAIL",
                "setup_failed": "ERROR",
            }[result["status"]]
            suffix = f" trial={trial}" if args.repetitions > 1 else ""
            print(
                f"{marker:5s}  {result['id']:40s} {result['duration_s']:.2f}s{suffix}",
                end="",
            )
            if result["status"] == "assertions_failed":
                print(f"  -- failed: {', '.join(result['hard_failures'])}")
            elif result["status"] == "setup_failed":
                error = result["setup_error"]
                print(f"  -- {error['stage']}: {error['message']}")
            else:
                print()

    summary = build_summary(results, run_config)
    if summary["selection"]["trials_run"] == 0:
        summary["run_setup_failed"] = True
        summary["setup_errors"] = [
            {
                "stage": "selection",
                "message": f"zero cases executed for mode={args.mode!r}",
            }
        ]
        print(f"ERROR  zero cases executed for mode={args.mode!r}", file=sys.stderr)

    summary_path = args.json or (str(result_root / "summary.json") if result_root is not None else None)
    _write_summary(summary, summary_path)

    print()
    for score_type, score in summary["score_types"].items():
        if not score["trials_run"]:
            continue
        print(
            f"{score_type}: {score['passed']}/{score['graded_trials']} graded trials passed "
            f"({score['assertions_failed']} assertion failures, "
            f"{score['setup_failed']} setup failures)"
        )
    mutation_score = summary["mutation_score"]
    if mutation_score["total"]:
        score_text = f"{mutation_score['score']:.1%}" if mutation_score["score"] is not None else "n/a"
        print(
            "mutation score: "
            f"{mutation_score['detected']}/{mutation_score['valid']} detected "
            f"({score_text}; {mutation_score['isolated']} isolated, "
            f"{mutation_score['invalid']} invalid)"
        )
    skipped = summary["selection"]["case_definitions_skipped"]
    if skipped:
        print(f"Skipped case definitions: {skipped}")

    if summary["run_setup_failed"] or summary["outcomes"]["setup_failed"]:
        return 2
    if summary["outcomes"]["assertions_failed"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
