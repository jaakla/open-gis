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
import os
import platform
import shlex
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
CLEAN_RERUN_EVIDENCE = ".open-gis-clean-rerun.json"
KNOWN_MODES = {"fixture", "live"}
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
from open_gis.validation import validate_project  # noqa: E402

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
        raise ValueError(
            f"{expected_path}: {mode}.clean_rerun has unknown options: "
            f"{sorted(config['clean_rerun'])}"
        )
    if "clean_rerun" in config and config.get("rerun_generator") is not None:
        raise ValueError(
            f"{expected_path}: {mode} cannot declare both clean_rerun and rerun_generator"
        )


def _validate_case(case: Any, expected_path: Path) -> dict[str, Any]:
    if not isinstance(case, dict):
        raise ValueError(f"{expected_path}: expected a YAML mapping")

    case_id = case.get("id")
    if not isinstance(case_id, str) or not case_id.strip():
        raise ValueError(f"{expected_path}: id must be a non-empty string")

    case_type = case.get("case_type")
    if case_type not in KNOWN_CASE_TYPES:
        raise ValueError(
            f"{expected_path}: case_type must be one of {sorted(KNOWN_CASE_TYPES)}, "
            f"got {case_type!r}"
        )

    if "mode" in case:
        raise ValueError(f"{expected_path}: legacy field 'mode' is not supported; use 'modes'")

    modes = case.get("modes")
    if not isinstance(modes, list) or not modes:
        raise ValueError(f"{expected_path}: modes must be a non-empty list")
    if any(not isinstance(mode, str) or mode not in KNOWN_MODES for mode in modes):
        raise ValueError(
            f"{expected_path}: modes must contain only {sorted(KNOWN_MODES)}, got {modes!r}"
        )
    if len(modes) != len(set(modes)):
        raise ValueError(f"{expected_path}: modes must not contain duplicates")

    score_types = case.get("score_types")
    if not isinstance(score_types, dict):
        raise ValueError(f"{expected_path}: score_types must map each mode to a score type")
    if set(score_types) != set(modes):
        raise ValueError(
            f"{expected_path}: score_types keys must exactly match modes; "
            f"expected {sorted(modes)}, got {sorted(score_types)}"
        )
    for mode, score_type in score_types.items():
        if score_type not in KNOWN_SCORE_TYPES:
            raise ValueError(
                f"{expected_path}: unknown score type {score_type!r} for mode {mode!r}; "
                f"expected one of {sorted(KNOWN_SCORE_TYPES)}"
            )
        if score_type == "contract_ci" and mode != "fixture":
            raise ValueError(f"{expected_path}: contract_ci is only valid for fixture mode")
        if score_type == "agent_benchmark" and mode != "live":
            raise ValueError(f"{expected_path}: agent_benchmark is only valid for live mode")

    if case_type == "mutation":
        if modes != ["fixture"] or score_types["fixture"] != "mutation_tests":
            raise ValueError(
                f"{expected_path}: mutation cases must be fixture-only mutation_tests"
            )
    elif "mutation_tests" in score_types.values():
        raise ValueError(f"{expected_path}: positive cases cannot contribute to mutation_tests")

    _validate_relative_dir(case.get("project_dir", "project"), "project_dir")

    for mode in modes:
        config = case.get(mode)
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
            _validate_command(
                command, f"fixture.extra_generators[{project_dir!r}]", expected_path
            )
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
            raise ValueError(
                f"{expected_path}: unknown agent {agent!r}; expected one of {sorted(KNOWN_AGENTS)}"
            )
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
        hard_gate = entry.get("hard_gate", case.get("hard_gate", True))
        if not isinstance(hard_gate, bool):
            raise ValueError(f"{location}.hard_gate must be true or false")

    assertion_names = {entry["assert"] for entry in assertions}
    for mode in modes:
        if (
            "clean_rerun" in case[mode]
            and "rerun.clean_execution_succeeded" not in assertion_names
        ):
            raise ValueError(
                f"{expected_path}: {mode}.clean_rerun requires "
                "rerun.clean_execution_succeeded"
            )

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
    workspace = Path(tempfile.mkdtemp(prefix=f"open-gis-eval-{case_dir.name}-"))
    project_dirs = {case_def.get("project_dir", "project")}
    if mode == "fixture":
        project_dirs.update((case_def["fixture"].get("extra_generators") or {}).keys())
    for project_dir_name in project_dirs:
        project_src = case_dir / project_dir_name
        if mode == "fixture" and project_src.exists():
            shutil.copytree(project_src, workspace / project_dir_name, dirs_exist_ok=True)
        else:
            (workspace / project_dir_name).mkdir(parents=True, exist_ok=True)
    return workspace


def _prepare_live_fixtures(
    case_dir: Path, workspace: Path, live_config: dict[str, Any]
) -> list[dict[str, Any]]:
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
        prepared.append({
            "source": str(source),
            "destination": str(destination),
            "kind": kind,
        })
    return prepared


def _under_project(path: Path, project_path: Path) -> bool:
    try:
        path.resolve().relative_to(project_path.resolve())
    except ValueError:
        return False
    return True


def _hash_declared_source_baseline(
    case_dir: Path, source_baseline: list[dict[str, str]]
) -> dict[str, str]:
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


def _execute_argv(
    command: list[str], cwd: Path, timeout_s: int | float, env: dict[str, str]
) -> dict[str, Any]:
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
        if not (relative == "data/source" or relative == "data/overrides" or
                relative.startswith("data/source/") or relative.startswith("data/overrides/")):
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


def _canonical_rerun_command(
    project_root: Path, project: dict[str, Any]
) -> tuple[list[str], list[tuple[str, str]]]:
    runtime = project.get("runtime")
    implementation = runtime.get("implementation") if isinstance(runtime, dict) else None
    if not isinstance(implementation, dict):
        raise ValueError("runtime.implementation is missing")

    preserve: list[tuple[str, str]] = []
    dependencies = implementation.get("dependencies") or []
    if not isinstance(dependencies, list) or not all(
        isinstance(item, str) and item.strip() for item in dependencies
    ):
        raise ValueError("runtime.implementation.dependencies must be a list of paths")
    preserve.extend(
        (dependency, f"runtime.implementation.dependencies[{index}]")
        for index, dependency in enumerate(dependencies)
    )

    declared_command = implementation.get("command")
    if declared_command is not None:
        if isinstance(declared_command, str):
            command = shlex.split(declared_command)
        elif isinstance(declared_command, list) and all(
            isinstance(item, str) and item for item in declared_command
        ):
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
    pipeline_path, relative = _safe_project_path(
        project_root, pipeline, "runtime.implementation.pipeline"
    )
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
    removed = sorted(
        key for key in env if any(fragment in key.upper() for fragment in sensitive_fragments)
    )
    for key in removed:
        env.pop(key, None)
    env.pop("PYTHONPATH", None)
    env["OPEN_GIS_CLEAN_RERUN"] = "1"
    return env, removed


def _write_clean_rerun_evidence(rerun_root: Path, evidence: dict[str, Any]) -> None:
    (rerun_root / CLEAN_RERUN_EVIDENCE).write_text(
        json.dumps(evidence, indent=2, default=str), encoding="utf-8"
    )


def _perform_clean_rerun(
    project_root: Path, rerun_root: Path, timeout_s: int | float
) -> dict[str, Any]:
    """Rebuild a project from its manifest, local immutable inputs, and declared dependencies."""
    evidence: dict[str, Any] = {
        "schema": "open-gis-clean-rerun/v1",
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
        _copy_clean_rerun_path(
            project_root, rerun_root, "project.yaml", "project manifest", preserved
        )
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
            evidence["error"] = (
                f"canonical entrypoint exited with status {execution.get('returncode')}"
            )
            _write_clean_rerun_evidence(rerun_root, evidence)
            return evidence

        evidence["stage"] = "source_integrity"
        source_hashes_after = _hash_immutable_inputs(rerun_root, preserved)
        mutated = sorted(
            relative
            for relative, digest in source_hashes_before.items()
            if source_hashes_after.get(relative) != digest
        )
        evidence["source_hashes"] = source_hashes_after
        if mutated:
            evidence["error"] = (
                f"canonical entrypoint mutated declared-immutable source/override files: {mutated}"
            )
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
    execution_config = case_def[case_mode]

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

    try:
        if case_mode == "fixture":
            source_hashes_before = _hash_declared_source_baseline(
                case_dir, execution_config.get("source_baseline") or []
            )
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

        elif case_mode == "live":
            agent_name = agent_override or execution_config.get("agent", "claude_code")
            adapter = _load_adapter(agent_name)
            if not adapter.is_available():
                raise SetupFailure(
                    "agent_preflight",
                    f"required executable {adapter.executable!r} for agent {agent_name!r} was not found on PATH",
                    {"agent": agent_name, "executable": adapter.executable},
                )
            prompt_path = case_dir / execution_config.get("prompt_file", "prompt.md")
            if not prompt_path.is_file():
                raise SetupFailure("agent_preflight", f"prompt file does not exist: {prompt_path}")
            prompt = prompt_path.read_text(encoding="utf-8")
            live_fixtures = _prepare_live_fixtures(case_dir, workspace, execution_config)
            source_hashes_before = _hash_workspace_files(
                project_path,
                [
                    str(Path(fixture["destination"]).resolve().relative_to(project_path.resolve()))
                    for fixture in live_fixtures
                    if fixture["kind"] == "file"
                    and _under_project(Path(fixture["destination"]), project_path)
                ],
            )
            agent_workdir = workspace / execution_config.get("agent_workdir", project_dir)
            agent_workdir.mkdir(parents=True, exist_ok=True)
            agent_result = adapter.run(
                prompt,
                agent_workdir,
                fixture=None,
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

        if "clean_rerun" in execution_config:
            rerun_workspace_path = Path(tempfile.mkdtemp(prefix=f"open-gis-eval-{case_dir.name}-rerun-"))
            clean_rerun_result = _perform_clean_rerun(
                project_path, rerun_workspace_path, timeout_s
            )
        else:
            rerun_generator_cmd = execution_config.get("rerun_generator")
            if rerun_generator_cmd:
                rerun_workspace_path = Path(
                    tempfile.mkdtemp(prefix=f"open-gis-eval-{case_dir.name}-rerun-")
                )
                command = _format_command(rerun_generator_cmd, rerun_workspace_path)
                rerun_generator_result = _execute_command(
                    command, rerun_workspace_path, timeout_s
                )
                _require_command_success(rerun_generator_result, "rerun_generator")

        for entry in case_def.get("assertions", []):
            assert_name = entry["assert"]
            args = dict(entry.get("args", {}) or {})
            if rerun_workspace_path is not None and args.get("rerun_workspace") == "$RERUN":
                args["rerun_workspace"] = str(rerun_workspace_path)
            if args.get("hashes_before") == "$SOURCE_HASHES":
                args["hashes_before"] = source_hashes_before
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
            "assertions": assertion_results,
            "dimension_totals": dimension_totals,
            "hard_failures": [a["assert"] for a in hard_failures],
        }, workspace, rerun_workspace_path, keep_workspace=keep_workspace)
    except SetupFailure as exc:
        return _portable_result_paths({
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
            "assertions": assertion_results,
            "dimension_totals": dimension_totals,
            "hard_failures": [],
            "setup_error": {"stage": exc.stage, "message": exc.message, "data": exc.data},
        }, workspace, rerun_workspace_path, keep_workspace=keep_workspace)
    except Exception as exc:  # noqa: BLE001
        return _portable_result_paths({
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
    score_types: dict[str, dict[str, Any]] = {}
    for score_type in sorted(KNOWN_SCORE_TYPES):
        score_results = [r for r in ran if r.get("score_type") == score_type]
        score_passed = sum(r.get("status") == "passed" for r in score_results)
        score_assertion_failures = sum(
            r.get("status") == "assertions_failed" for r in score_results
        )
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
    return {
        "schema": "open-gis-eval-results/v2",
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
            mappings = ", ".join(
                f"{mode}:{case_def['score_types'][mode]}" for mode in case_def["modes"]
            )
            print(f"{case_def.get('id', case_dir.name):40s} {mappings}")
        return 0

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
    if summary["selection"]["trials_run"] == 0:
        summary["run_setup_failed"] = True
        summary["setup_errors"] = [{"stage": "selection", "message": f"zero cases executed for mode={args.mode!r}"}]
        print(f"ERROR  zero cases executed for mode={args.mode!r}", file=sys.stderr)

    _write_summary(summary, args.json)

    print()
    for score_type, score in summary["score_types"].items():
        if not score["trials_run"]:
            continue
        print(
            f"{score_type}: {score['passed']}/{score['graded_trials']} graded trials passed "
            f"({score['assertions_failed']} assertion failures, "
            f"{score['setup_failed']} setup failures)"
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
