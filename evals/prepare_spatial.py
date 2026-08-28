#!/usr/bin/env python3
"""Explicitly preinstall DuckDB Spatial for the later offline eval phase."""

from __future__ import annotations

from spatial import EXTENSION_DIR_ENV, connect_spatial


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
