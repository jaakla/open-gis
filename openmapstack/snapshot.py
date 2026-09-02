"""Controlled snapshots of the shipped skill (``openmapstack-skill-snapshot/v1``).

A benchmark arm that injects the skill must say *which* skill: not a tag
that can move, not a commit whose working tree may have been dirty, but the
bytes the agent actually read. ``create_skill_snapshot`` copies exactly
``SKILL.md``, ``references/``, and ``templates/`` from a skill root, records a
per-file inventory and a content hash, and writes the manifest beside the
copy. ``inspect_skill_snapshot`` re-verifies one later.

Safety: symlinks anywhere in the source tree are refused (a snapshot must
not quietly include files from outside the skill), and every inventory path
is confined to the snapshot root. The content hash covers the relative path
and the bytes of every file, so a renamed reference changes it.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SNAPSHOT_SCHEMA = "openmapstack-skill-snapshot/v1"
SNAPSHOT_MANIFEST = "snapshot.json"
SKILL_ENTRYPOINT = "SKILL.md"
SKILL_DIRECTORIES = ("references", "templates")
_IGNORED = {"__pycache__", ".DS_Store"}


class SnapshotError(ValueError):
    """The skill root or an existing snapshot is unusable."""


def find_skill_root(start: Path | None = None) -> Path | None:
    """Walk upwards from ``start`` to the nearest directory holding SKILL.md."""
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / SKILL_ENTRYPOINT).is_file() and all((candidate / name).is_dir() for name in SKILL_DIRECTORIES):
            return candidate
    return None


def _iter_skill_files(root: Path) -> list[Path]:
    entrypoint = root / SKILL_ENTRYPOINT
    if not entrypoint.is_file():
        raise SnapshotError(f"{SKILL_ENTRYPOINT} is missing from {root}")
    files = [entrypoint]
    for name in SKILL_DIRECTORIES:
        directory = root / name
        if not directory.is_dir():
            raise SnapshotError(f"{name}/ is missing from {root}")
        for path in sorted(directory.rglob("*")):
            if any(part in _IGNORED or part.endswith(".pyc") for part in path.relative_to(root).parts):
                continue
            if path.is_symlink():
                raise SnapshotError(f"skill tree contains a symlink, refusing to snapshot: {path.relative_to(root)}")
            if path.is_file():
                files.append(path)
    if entrypoint.is_symlink():
        raise SnapshotError(f"{SKILL_ENTRYPOINT} is a symlink, refusing to snapshot")
    return files


def _content_hash(files: list[tuple[str, bytes]]) -> str:
    """Hash over sorted (relative path, raw bytes) pairs, each terminated by
    a NUL: exactly the algorithm the eval runner used before this module
    existed, so an unchanged skill keeps the content hash recorded in
    historical benchmark arms."""
    digest = hashlib.sha256()
    for relative, data in sorted(files, key=lambda item: item[0]):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(data)
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def _git(root: Path) -> dict[str, Any]:
    revision: dict[str, Any] = {"commit": None, "dirty": None}
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, timeout=10, check=False)
        if commit.returncode == 0:
            revision["commit"] = commit.stdout.strip() or None
        status = subprocess.run(["git", "status", "--porcelain", "--", SKILL_ENTRYPOINT, *SKILL_DIRECTORIES], cwd=root, capture_output=True, text=True, timeout=10, check=False)
        if status.returncode == 0:
            revision["dirty"] = bool(status.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return revision


def create_skill_snapshot(source_root: str | Path, destination: str | Path, *, now: datetime | None = None) -> dict[str, Any]:
    """Copy the distributable skill into ``destination`` and return its manifest."""
    root = Path(source_root).resolve()
    target = Path(destination).resolve()
    if target.exists() and any(target.iterdir()):
        raise SnapshotError(f"snapshot destination is not empty: {target}")
    if target == root or root in target.parents:
        raise SnapshotError("snapshot destination must not be inside the skill root")
    files = _iter_skill_files(root)
    target.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, Any]] = []
    contents: list[tuple[str, bytes]] = []
    for path in files:
        relative = path.relative_to(root).as_posix()
        copy_to = target / relative
        copy_to.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, copy_to)
        data = copy_to.read_bytes()
        contents.append((relative, data))
        entries.append({"path": relative, "sha256": "sha256:" + hashlib.sha256(data).hexdigest(), "bytes": len(data)})
    manifest = {
        "schema": SNAPSHOT_SCHEMA,
        "created_at": (now or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z"),
        "source_root": root.name,
        "source_git": _git(root),
        "entrypoint": SKILL_ENTRYPOINT,
        "files": entries,
        "file_count": len(entries),
        "content_sha256": _content_hash(contents),
    }
    (target / SNAPSHOT_MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def hash_skill_root(source_root: str | Path) -> str:
    """Content hash of a skill root without copying it."""
    root = Path(source_root).resolve()
    return _content_hash([(path.relative_to(root).as_posix(), path.read_bytes()) for path in _iter_skill_files(root)])


def inspect_skill_snapshot(snapshot_dir: str | Path) -> dict[str, Any]:
    """Re-verify a snapshot against its own manifest.

    Reports missing, changed, and extra files plus any symlink or escaping
    inventory path; ``intact`` is true only when the copy still matches.
    """
    root = Path(snapshot_dir).resolve()
    manifest_path = root / SNAPSHOT_MANIFEST
    if not manifest_path.is_file():
        raise SnapshotError(f"{SNAPSHOT_MANIFEST} is missing from {root}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SnapshotError(f"cannot read {SNAPSHOT_MANIFEST}: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("schema") != SNAPSHOT_SCHEMA:
        raise SnapshotError(f"{SNAPSHOT_MANIFEST} is not an {SNAPSHOT_SCHEMA} document")
    problems: list[str] = []
    seen: set[str] = set()
    recomputed: list[tuple[str, bytes]] = []
    for entry in manifest.get("files") or []:
        relative = entry.get("path") if isinstance(entry, dict) else None
        if not isinstance(relative, str) or not relative:
            problems.append("inventory entry without a path")
            continue
        candidate = (root / relative)
        try:
            candidate.resolve().relative_to(root)
        except ValueError:
            problems.append(f"inventory path escapes the snapshot: {relative}")
            continue
        if Path(relative).is_absolute() or ".." in Path(relative).parts:
            problems.append(f"inventory path escapes the snapshot: {relative}")
            continue
        seen.add(relative)
        if candidate.is_symlink():
            problems.append(f"symlink in snapshot: {relative}")
            continue
        if not candidate.is_file():
            problems.append(f"missing: {relative}")
            continue
        data = candidate.read_bytes()
        actual = "sha256:" + hashlib.sha256(data).hexdigest()
        recomputed.append((relative, data))
        if actual != entry.get("sha256"):
            problems.append(f"changed: {relative}")
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if relative == SNAPSHOT_MANIFEST or any(part in _IGNORED for part in path.relative_to(root).parts):
            continue
        if path.is_symlink():
            problems.append(f"symlink in snapshot: {relative}")
        elif path.is_file() and relative not in seen:
            problems.append(f"extra: {relative}")
    content_hash = _content_hash(recomputed) if recomputed else None
    if not problems and content_hash != manifest.get("content_sha256"):
        problems.append("content_sha256 does not match the inventory")
    return {
        "schema": "openmapstack-skill-snapshot-inspection/v1",
        "snapshot": str(root),
        "intact": not problems,
        "content_sha256": manifest.get("content_sha256"),
        "recomputed_sha256": content_hash,
        "file_count": len(seen),
        "problems": problems,
        "manifest": manifest,
    }
