"""Canonical, path-aware hashing for OpenMapStack run evidence."""

from __future__ import annotations

import hashlib
import shlex
from pathlib import Path
from typing import Any, Iterable

from .project import get_in, project_path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def normalize_digest(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip().lower()
    if not normalized.startswith("sha256:"):
        normalized = f"sha256:{normalized}"
    payload = normalized.removeprefix("sha256:")
    if len(payload) != 64 or any(char not in "0123456789abcdef" for char in payload):
        return None
    return normalized


def canonical_file_set_hash(root: Path, paths: Iterable[str | Path]) -> str:
    """Hash a sorted set of project-relative path names and file bytes.

    Including the relative name prevents two differently named inventories
    with identical concatenated contents from sharing a digest. Duplicate
    paths are collapsed because an inventory represents a set of files.
    """
    digest = hashlib.sha256()
    normalized = sorted({Path(path).as_posix() for path in paths})
    for relative in normalized:
        target = project_path(root, relative)
        if target is None or not target.is_file():
            raise ValueError(f"cannot hash missing or unsafe project file: {relative}")
        encoded = relative.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        with target.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def file_inventory(root: Path, paths: Iterable[str | Path]) -> list[dict[str, str]]:
    normalized = sorted({Path(path).as_posix() for path in paths})
    inventory: list[dict[str, str]] = []
    for relative in normalized:
        target = project_path(root, relative)
        if target is None or not target.is_file():
            raise ValueError(f"cannot inventory missing or unsafe project file: {relative}")
        inventory.append({"path": relative, "sha256": sha256_file(target)})
    return inventory


def declared_output_paths(project: dict[str, Any]) -> list[str]:
    outputs = project.get("outputs") or {}
    if not isinstance(outputs, dict):
        return []
    return sorted(
        {
            Path(output["path"]).as_posix()
            for output in outputs.values()
            if isinstance(output, dict)
            and isinstance(output.get("path"), str)
            and output["path"].strip()
        }
    )


def declared_input_paths(root: Path, project: dict[str, Any]) -> list[str]:
    """Return the immutable inputs and canonical implementation files.

    The manifest itself is deliberately excluded because it contains the
    resulting digest. Sources, overrides, pipeline/command-local files, and
    declared dependencies are the clean-rerun inputs defined by the spec.
    """
    # project_path() returns resolved paths, so every path compared below must be
    # measured against a resolved root. On macOS the temp and /var trees are
    # symlinks, so an unresolved root differs from a resolved child by a
    # /private prefix and relative_to() raises.
    resolved_root = root.resolve()
    paths: set[str] = set()
    for relative_dir in ("data/source", "data/overrides"):
        directory = project_path(root, relative_dir)
        if directory is not None and directory.is_dir():
            paths.update(
                path.relative_to(resolved_root).as_posix()
                for path in directory.rglob("*")
                if path.is_file()
            )

    implementation = get_in(project, "runtime", "implementation", default={})
    if not isinstance(implementation, dict):
        return sorted(paths)
    pipeline = implementation.get("pipeline")
    if isinstance(pipeline, str) and pipeline.strip():
        paths.add(Path(pipeline).as_posix())
    command = implementation.get("command")
    if isinstance(command, str):
        try:
            tokens = shlex.split(command)
        except ValueError:
            tokens = []
    else:
        tokens = command if isinstance(command, list) else []
    for token in tokens:
        if not isinstance(token, str) or token.startswith("-"):
            continue
        target = project_path(root, token)
        if target is not None and target.is_file():
            paths.add(Path(token).as_posix())
    for dependency in implementation.get("dependencies") or []:
        if not isinstance(dependency, str):
            continue
        target = project_path(root, dependency)
        if target is None:
            continue
        if target.is_file():
            paths.add(Path(dependency).as_posix())
        elif target.is_dir():
            paths.update(
                path.relative_to(resolved_root).as_posix()
                for path in target.rglob("*")
                if path.is_file()
            )
    return sorted(paths)
