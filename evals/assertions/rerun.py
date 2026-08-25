"""Clean-rerun / reproducibility assertions.

See references/project-spec.md sections 4, 6, 7 and issue acceptance
criterion: "a produced project is rerun from a clean workspace with no
chat transcript". These assertions compare a first-run workspace against
a second, independently generated workspace of the same project.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from . import AssertionResult, failed, not_testable, passed, project_root


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def outputs_hash_stable(
    workspace: Path, rerun_workspace: str, paths: list[str], project_dir: str = ".",
) -> AssertionResult:
    """Compare declared output files between this workspace and a second,
    independently generated workspace (e.g. produced by rerunning the
    pipeline from immutable source + overrides only). `rerun_workspace` is
    an absolute path string to the second project root (not nested under
    the eval's temp workspace, since it is generated out-of-band by the
    case's `rerun_generator`)."""
    root = project_root(workspace, project_dir)
    rerun_root = Path(rerun_workspace)
    if not rerun_root.exists():
        return not_testable(f"rerun workspace {rerun_workspace} does not exist")

    mismatches: list[str] = []
    missing: list[str] = []
    for rel in paths:
        a = root / rel
        b = rerun_root / rel
        if not a.exists() or not b.exists():
            missing.append(rel)
            continue
        if _sha256(a) != _sha256(b):
            mismatches.append(rel)

    if missing:
        return not_testable(f"outputs missing in one of the two runs: {missing}")
    if mismatches:
        return failed(f"outputs not hash-stable across clean rerun: {mismatches}")
    return passed(f"all {len(paths)} declared outputs byte-identical across independent reruns")


def validation_report_reproducible(
    workspace: Path, rerun_workspace: str, project_dir: str = ".",
    report_path: str = "validation/latest-report.json",
) -> AssertionResult:
    from . import load_json

    root = project_root(workspace, project_dir)
    rerun_root = Path(rerun_workspace)
    report_a = load_json(root / report_path)
    report_b = load_json(rerun_root / report_path)
    if report_a is None or report_b is None:
        return not_testable("validation report missing in one of the two runs")

    status_a, status_b = report_a.get("status"), report_b.get("status")
    if status_a != status_b:
        return failed(f"validation status differs across rerun: {status_a!r} vs {status_b!r}")

    checks_a = {c["id"]: c.get("status") for c in report_a.get("checks", [])}
    checks_b = {c["id"]: c.get("status") for c in report_b.get("checks", [])}
    if checks_a != checks_b:
        return failed(f"per-check statuses differ across rerun: {checks_a} vs {checks_b}")

    return passed(f"validation report reproduces with status {status_a!r} across independent reruns")


def no_chat_dependency(workspace: Path, project_dir: str = ".") -> AssertionResult:
    """Static sanity check: the project must be runnable from files alone —
    i.e. pipeline.py must not reference a chat/transcript/conversation file
    that would not exist in a clean checkout."""
    root = project_root(workspace, project_dir)
    pipeline = root / "pipeline.py"
    if not pipeline.exists():
        return failed("pipeline.py does not exist")
    text = pipeline.read_text(encoding="utf-8", errors="ignore")
    forbidden = ["chat_history", "conversation.json", "transcript.txt", "chat_log"]
    hits = [f for f in forbidden if f in text]
    if hits:
        return failed(f"pipeline.py references transcript-like inputs: {hits}")
    return passed("pipeline.py contains no reference to chat/transcript state")
