"""Clean-rerun protocol for an ``openmapstack-project/v1`` artifact.

Rebuild a project in an empty workspace from only its manifest, its declared
immutable inputs, and its declared dependencies -- then execute the one
canonical entrypoint and revalidate what it produced.

This is the strongest correctness signal available on data nobody has a known
answer for. It needs no oracle: a pipeline that cannot reproduce itself from
source plus manifest is untrustworthy whatever its numbers say, and one that
mutates its own declared-immutable inputs is not reproducible at all.

Two callers share this: ``evals/run.py`` (case ``clean_rerun: {}``) and
``openmapstack verify --rerun``.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Sequence

import yaml

from .validation import validate_project

CLEAN_RERUN_EVIDENCE = ".openmapstack-clean-rerun.json"


def _output_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


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

def canonical_rerun_command(
    project_root: Path,
    project: dict[str, Any],
    *,
    forbidden_fragments: Sequence[str] = (),
) -> tuple[list[str], list[tuple[str, str]]]:
    """Resolve the one canonical entrypoint plus the paths a rerun must keep.

    ``forbidden_fragments`` lets a caller reject commands that reach back into
    machinery a clean rerun must not depend on. The eval runner passes its own
    generator paths; nothing in the package knows about ``evals/``.
    """
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
            for fragment in forbidden_fragments:
                if fragment and fragment in token:
                    raise ValueError(
                        f"canonical command depends on excluded machinery: {fragment!r}"
                    )
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

def prepare_clean_workspace(
    project_root: Path,
    rerun_root: Path,
    *,
    forbidden_fragments: Sequence[str] = (),
) -> tuple[list[str], set[str], dict[str, Any]]:
    """Copy only the clean-rerun inputs into ``rerun_root``.

    Returns the resolved canonical command, the set of preserved
    project-relative paths, and the loaded manifest. Raises ``ValueError``
    for a manifest that cannot be rerun safely. Shared by the clean rerun
    and by metamorphic variant runs, which need the same isolation but then
    perturb one input or parameter before executing.
    """
    manifest_path = project_root / "project.yaml"
    if not manifest_path.is_file():
        raise ValueError("project.yaml is missing")
    try:
        project = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError(f"project.yaml cannot be loaded: {exc}") from exc
    if not isinstance(project, dict):
        raise ValueError("project.yaml must contain a mapping")

    preserved: set[str] = set()
    command, declared_paths = canonical_rerun_command(
        project_root, project, forbidden_fragments=forbidden_fragments
    )
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
    return command, preserved, project


def execute_canonical(
    command: list[str],
    rerun_root: Path,
    timeout_s: int | float,
    *,
    extra_argv: Sequence[str] = (),
    extra_env: dict[str, str] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Run the canonical entrypoint in a sanitized environment.

    Returns the execution record and the list of environment keys removed.
    ``extra_argv``/``extra_env`` carry parameter bindings for variant runs.
    """
    env, removed_environment = _clean_rerun_environment()
    if extra_env:
        env.update(extra_env)
    execution = _execute_argv([*command, *extra_argv], rerun_root, timeout_s, env)
    return execution, removed_environment


def perform_clean_rerun(
    project_root: Path,
    rerun_root: Path,
    timeout_s: int | float,
    *,
    forbidden_fragments: Sequence[str] = (),
) -> dict[str, Any]:
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
        command, preserved, _project = prepare_clean_workspace(
            project_root, rerun_root, forbidden_fragments=forbidden_fragments
        )

        evidence["preserved_paths"] = sorted(preserved)
        source_hashes_before = _hash_immutable_inputs(rerun_root, preserved)
        evidence["command"] = command
        execution, removed_environment = execute_canonical(command, rerun_root, timeout_s)
        evidence["removed_environment_keys"] = removed_environment
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
