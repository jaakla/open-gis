"""Skill snapshots: hashed, inspectable, symlink- and escape-safe (issue #13, C2)."""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from openmapstack.cli import main
from openmapstack.snapshot import (
    SnapshotError,
    create_skill_snapshot,
    find_skill_root,
    hash_skill_root,
    inspect_skill_snapshot,
)
from tests.evals.helpers import make_workspace

REPO_ROOT = Path(__file__).resolve().parents[1]


def _skill_root() -> Path:
    root = make_workspace() / "skill"
    (root / "references").mkdir(parents=True)
    (root / "templates").mkdir()
    (root / "SKILL.md").write_text("# skill\n", encoding="utf-8")
    (root / "references/project-spec.md").write_text("spec\n", encoding="utf-8")
    (root / "templates/project.yaml").write_text("schema: openmapstack-project/v1\n", encoding="utf-8")
    (root / "evals").mkdir()
    (root / "evals/secret-case.yaml").write_text("must not be copied\n", encoding="utf-8")
    return root


class SnapshotTests(unittest.TestCase):
    def test_snapshot_copies_only_the_distributable_skill(self) -> None:
        root = _skill_root()
        out = make_workspace() / "snap"
        manifest = create_skill_snapshot(root, out)
        self.assertEqual(manifest["schema"], "openmapstack-skill-snapshot/v1")
        self.assertEqual({entry["path"] for entry in manifest["files"]}, {"SKILL.md", "references/project-spec.md", "templates/project.yaml"})
        self.assertFalse((out / "evals").exists())
        self.assertEqual(manifest["content_sha256"], hash_skill_root(root))
        self.assertTrue(inspect_skill_snapshot(out)["intact"])

    def test_content_hash_covers_paths_and_bytes(self) -> None:
        root = _skill_root()
        before = hash_skill_root(root)
        (root / "references/project-spec.md").write_text("spec v2\n", encoding="utf-8")
        self.assertNotEqual(before, hash_skill_root(root))
        (root / "references/project-spec.md").write_text("spec\n", encoding="utf-8")
        (root / "references/project-spec.md").rename(root / "references/renamed.md")
        self.assertNotEqual(before, hash_skill_root(root))

    def test_symlinks_are_refused(self) -> None:
        root = _skill_root()
        (root / "references/escape.md").symlink_to("/etc/hostname")
        with self.assertRaises(SnapshotError):
            create_skill_snapshot(root, make_workspace() / "snap")

    def test_inspection_detects_tampering_and_escapes(self) -> None:
        root = _skill_root()
        out = make_workspace() / "snap"
        create_skill_snapshot(root, out)
        (out / "SKILL.md").write_text("# edited\n", encoding="utf-8")
        (out / "references/extra.md").write_text("added\n", encoding="utf-8")
        report = inspect_skill_snapshot(out)
        self.assertFalse(report["intact"])
        self.assertIn("changed: SKILL.md", report["problems"])
        self.assertIn("extra: references/extra.md", report["problems"])
        manifest = json.loads((out / "snapshot.json").read_text())
        manifest["files"].append({"path": "../outside.md", "sha256": "sha256:" + "0" * 64, "bytes": 1})
        (out / "snapshot.json").write_text(json.dumps(manifest))
        self.assertTrue(any("escapes" in problem for problem in inspect_skill_snapshot(out)["problems"]))

    def test_destination_must_be_empty_and_outside_the_root(self) -> None:
        root = _skill_root()
        with self.assertRaises(SnapshotError):
            create_skill_snapshot(root, root / "snap")
        out = make_workspace() / "snap"
        out.mkdir()
        (out / "leftover").write_text("x")
        with self.assertRaises(SnapshotError):
            create_skill_snapshot(root, out)

    def test_repository_skill_root_is_discoverable_and_snapshots(self) -> None:
        self.assertEqual(find_skill_root(REPO_ROOT / "openmapstack"), REPO_ROOT)
        out = make_workspace() / "snap"
        manifest = create_skill_snapshot(REPO_ROOT, out)
        self.assertIn("references/project-spec.md", {entry["path"] for entry in manifest["files"]})
        self.assertNotIn("evals/README.md", {entry["path"] for entry in manifest["files"]})

    def test_cli_creates_and_inspects(self) -> None:
        root = _skill_root()
        out = make_workspace() / "snap"
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.assertEqual(main(["skill-snapshot", "--out", str(out), "--source", str(root), "--json"]), 0)
        manifest = json.loads(buffer.getvalue())
        self.assertEqual(manifest["file_count"], 3)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["skill-snapshot", "--inspect", str(out)]), 0)
        (out / "SKILL.md").write_text("# edited\n", encoding="utf-8")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["skill-snapshot", "--inspect", str(out)]), 1)
        with redirect_stderr(io.StringIO()):
            self.assertEqual(main(["skill-snapshot", "--out", str(make_workspace() / "x"), "--source", str(make_workspace())]), 2)


if __name__ == "__main__":
    unittest.main()
