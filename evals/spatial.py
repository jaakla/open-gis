"""Controlled DuckDB Spatial loading for deterministic evals.

Grading code never downloads extensions.  A preparation step may explicitly
install Spatial into a pinned directory, after which tests and evals only use
``LOAD spatial``.  This separation lets CI prove the suite inside a container
whose network is disabled at runtime.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

EXTENSION_DIR_ENV = "OPEN_GIS_SPATIAL_EXTENSION_DIR"


def _connection_config() -> dict[str, str]:
    configured = os.environ.get(EXTENSION_DIR_ENV)
    if not configured:
        return {}
    extension_dir = Path(configured).expanduser().resolve()
    extension_dir.mkdir(parents=True, exist_ok=True)
    return {"extension_directory": str(extension_dir)}


def connect_spatial(*, install: bool = False) -> Any | None:
    """Return a Spatial-enabled DuckDB connection, or ``None`` if unavailable.

    ``install=False`` is intentional and is used by every assertion and
    generated pipeline.  Only ``evals/prepare_spatial.py`` passes
    ``install=True`` while image/dependency preparation still has network.
    """
    try:
        import duckdb
    except ImportError:
        return None

    connection = duckdb.connect(config=_connection_config())
    try:
        connection.execute("LOAD spatial")
        return connection
    except Exception:
        if not install:
            connection.close()
            return None

    try:
        connection.execute("INSTALL spatial")
        connection.execute("LOAD spatial")
        return connection
    except Exception:
        connection.close()
        return None


def require_spatial() -> Any:
    connection = connect_spatial()
    if connection is None:
        directory = os.environ.get(EXTENSION_DIR_ENV, "DuckDB's default extension directory")
        raise RuntimeError(
            "DuckDB Spatial is not preinstalled in "
            f"{directory}; run evals/prepare_spatial.py before disabling network access"
        )
    return connection
