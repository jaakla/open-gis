from __future__ import annotations

import json
import tomllib
import unittest
from pathlib import Path

from openmapstack import __version__


REPO_ROOT = Path(__file__).resolve().parents[1]


class ReleaseVersionTests(unittest.TestCase):
    def test_python_and_plugin_versions_match(self) -> None:
        package = tomllib.loads(
            (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )["project"]["version"]
        plugin = json.loads(
            (REPO_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
        )["version"]

        self.assertEqual(__version__, package)
        self.assertEqual(plugin, package)


if __name__ == "__main__":
    unittest.main()
