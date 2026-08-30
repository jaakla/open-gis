#!/usr/bin/env python3
"""Explicitly preinstall DuckDB Spatial for the later offline eval phase."""

from __future__ import annotations

import sys
from pathlib import Path

# This script runs before `pip install .` in some workflows, so it cannot
# assume the package is importable from site-packages. Same bootstrap as
# evals/run.py.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openmapstack.checks.spatial import EXTENSION_DIR_ENV, connect_spatial  # noqa: E402


def main() -> int:
    connection = connect_spatial(install=True)
    if connection is None:
        raise SystemExit("could not install/load DuckDB Spatial")
    version = connection.execute("SELECT extension_version FROM duckdb_extensions() WHERE extension_name='spatial'").fetchone()
    connection.close()
    print(f"DuckDB Spatial prepared ({EXTENSION_DIR_ENV}; version={version[0] if version else 'unknown'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
