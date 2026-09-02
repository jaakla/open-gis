"""DuckDB connector over local files (the local half of the reference pair).

The "connection" is a directory of geodata files, or a ``.duckdb`` database
opened read-only. Discovery uses DuckDB Spatial's metadata readers and never
loads whole files. Queries run in a fresh connection whose file access is
confined to that directory when the installed DuckDB supports
``allowed_directories``; otherwise the confinement gap is recorded in the
discovery notes rather than hidden.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from ..checks.spatial import connect_spatial
from . import (
    ConnectorError,
    ConnectorLimits,
    ConnectorUnavailable,
    Discovery,
    QueryPlan,
    TableInfo,
    query_digest,
    schema_digest,
)

GEO_SUFFIXES = {".parquet": "GeoParquet", ".geojson": "GeoJSON", ".json": "GeoJSON", ".gpkg": "GeoPackage", ".fgb": "FlatGeobuf", ".shp": "Shapefile"}
_MAX_DISCOVERED_FILES = 500


def _escape(value: str) -> str:
    return value.replace("'", "''")


class _Timeout:
    """Interrupt a DuckDB connection after ``seconds``; DuckDB has no statement timeout."""

    def __init__(self, connection: Any, seconds: float) -> None:
        self._connection = connection
        self._timer = threading.Timer(seconds, self._interrupt)
        self.fired = False

    def _interrupt(self) -> None:
        self.fired = True
        try:
            self._connection.interrupt()
        except Exception:  # noqa: BLE001 - best effort
            pass

    def __enter__(self) -> "_Timeout":
        self._timer.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._timer.cancel()


class DuckDBLocalConnector:
    backend = "duckdb"

    def __init__(self, connection: str, *, project_root: Path) -> None:
        root = Path(connection).expanduser() if connection else project_root / "data" / "source"
        if not root.is_absolute():
            root = (project_root / root)
        self.root = root.resolve()
        self.database: Path | None = None
        if self.root.is_file() and self.root.suffix.lower() == ".duckdb":
            self.database = self.root
            self.root = self.root.parent
        if not self.root.is_dir():
            raise ConnectorError(f"duckdb connector root is not a directory: {self.root.name}", code="connection_unresolved")
        self.notes: list[str] = []

    # -- sessions ---------------------------------------------------------------

    def _connect(self, *, allow: tuple[Path, ...] = ()):
        connection = connect_spatial()
        if connection is None:
            raise ConnectorUnavailable("duckdb with the Spatial extension is required (pip install 'openmapstack[geo]')")
        allowed = ", ".join(f"'{_escape(str(path))}'" for path in (self.root, *allow))
        try:
            # Both statements must be literal SETs after the database has
            # started; once external access is off it cannot be re-enabled
            # for this connection, which is the point.
            connection.execute(f"SET allowed_directories = [{allowed}]")
            connection.execute("SET enable_external_access = false")
        except Exception:  # noqa: BLE001 - older DuckDB
            note = "installed DuckDB does not support allowed_directories; file access is not confined to the source root"
            if note not in self.notes:
                self.notes.append(note)
        if self.database is not None:
            connection.execute(f"ATTACH '{_escape(str(self.database))}' AS warehouse (READ_ONLY)")
            connection.execute("USE warehouse")
        self._register_file_views(connection)
        return connection

    def _candidate_files(self) -> list[Path]:
        candidates = sorted(path for path in self.root.rglob("*") if path.is_file() and path.suffix.lower() in GEO_SUFFIXES)
        if len(candidates) > _MAX_DISCOVERED_FILES:
            note = f"only the first {_MAX_DISCOVERED_FILES} of {len(candidates)} geodata files were registered"
            if note not in self.notes:
                self.notes.append(note)
            candidates = candidates[:_MAX_DISCOVERED_FILES]
        return candidates

    def _register_file_views(self, connection) -> None:
        """Expose every geodata file under the root as a view named by its
        root-relative path, so a query says ``FROM "parcels.geojson"`` and
        never spells a filesystem path of its own."""
        for path in self._candidate_files():
            relative = path.relative_to(self.root).as_posix()
            escaped = _escape(path.as_posix())
            reader = f"read_parquet('{escaped}')" if path.suffix.lower() == ".parquet" else f"ST_Read('{escaped}')"
            try:
                connection.execute(f'CREATE VIEW "{relative.replace(chr(34), chr(34) * 2)}" AS SELECT * FROM {reader}')
            except Exception as exc:  # noqa: BLE001 - describe what can be described
                note = f"{relative}: not readable ({type(exc).__name__})"
                if note not in self.notes:
                    self.notes.append(note)

    def identity_for_manifest(self) -> dict[str, Any]:
        identity: dict[str, Any] = {"backend": "duckdb"}
        if self.database is not None:
            identity["database"] = self.database.name
        return identity

    # -- discovery --------------------------------------------------------------

    def discover(self, limits: ConnectorLimits) -> Discovery:
        connection = self._connect()
        tables: list[TableInfo] = []
        try:
            with _Timeout(connection, limits.timeout_s):
                if self.database is not None:
                    tables.extend(self._discover_database(connection))
                tables.extend(self._discover_files(connection))
        finally:
            connection.close()
        identity = {"root": self.root.name, "database": self.database.name if self.database else None}
        return Discovery("duckdb", identity, tables, read_only=True, notes=list(self.notes))

    def _discover_database(self, connection) -> list[TableInfo]:
        rows = connection.execute(
            "SELECT table_schema, table_name, table_type FROM information_schema.tables "
            "WHERE table_catalog = 'warehouse' ORDER BY 1, 2"
        ).fetchall()
        found: list[TableInfo] = []
        for schema, name, table_type in rows:
            columns = connection.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_catalog = 'warehouse' AND table_schema = ? AND table_name = ?",
                [schema, name],
            ).fetchall()
            geometry = next((column for column, type_name in columns if str(type_name).upper() == "GEOMETRY"), None)
            estimate = connection.execute(f'SELECT COUNT(*) FROM "{schema}"."{name}"').fetchone()[0]
            found.append(TableInfo(schema, name, geometry, None, None, int(estimate), kind="view" if "VIEW" in str(table_type).upper() else "table"))
        return found

    def _discover_files(self, connection) -> list[TableInfo]:
        found: list[TableInfo] = []
        for path in self._candidate_files():
            relative = path.relative_to(self.root).as_posix()
            try:
                found.append(self._describe_file(connection, path, relative))
            except Exception as exc:  # noqa: BLE001 - describe what can be described
                note = f"{relative}: not readable ({type(exc).__name__})"
                if note not in self.notes:
                    self.notes.append(note)
        return found

    def _describe_file(self, connection, path: Path, relative: str) -> TableInfo:
        escaped = _escape(path.as_posix())
        if path.suffix.lower() == ".parquet":
            columns = connection.execute(f"DESCRIBE SELECT * FROM read_parquet('{escaped}')").fetchall()
            geometry = next((str(name) for name, type_name, *_ in columns if str(type_name).upper() == "GEOMETRY"), None)
            estimate = connection.execute(f"SELECT COUNT(*) FROM read_parquet('{escaped}')").fetchone()[0]
            srid = None
            if geometry:
                crs_row = connection.execute(f'SELECT ST_CRS("{geometry}") FROM read_parquet(\'{escaped}\') LIMIT 1').fetchone()
                srid = _srid_from(crs_row[0] if crs_row else None)
            return TableInfo(None, relative, geometry, srid, None, int(estimate), kind="file")
        meta = connection.execute(f"SELECT layers FROM ST_Read_Meta('{escaped}')").fetchone()
        layer = (meta[0] or [None])[0] if meta else None
        geometry = srid = geometry_type = None
        estimate = None
        if isinstance(layer, dict):
            fields = layer.get("geometry_fields") or []
            if fields:
                geometry = fields[0].get("name") or "geom"
                geometry_type = fields[0].get("type")
                srid = _srid_from(((fields[0].get("crs") or {}).get("auth_code")))
            estimate = layer.get("feature_count")
        return TableInfo(None, relative, geometry, srid, geometry_type, int(estimate) if estimate is not None else None, kind="file")

    # -- queries ----------------------------------------------------------------

    def plan(self, query: str, limits: ConnectorLimits) -> QueryPlan:
        connection = self._connect()
        try:
            with _Timeout(connection, limits.timeout_s) as timeout:
                try:
                    described = connection.execute(f"DESCRIBE SELECT * FROM ({query}) AS q").fetchall()
                    count = connection.execute(f"SELECT COUNT(*) FROM ({query}) AS q").fetchone()[0]
                except Exception as exc:  # noqa: BLE001
                    if timeout.fired:
                        raise ConnectorError(f"query exceeded timeout_s={limits.timeout_s}", code="timeout") from exc
                    raise ConnectorError(f"query failed: {type(exc).__name__}: {exc}", code="query_failed") from exc
        finally:
            connection.close()
        columns = [{"name": str(name), "type": str(type_name)} for name, type_name, *_ in described]
        return QueryPlan(columns, int(count), query_digest(query), schema_digest(columns))

    def materialize(self, query: str, destination: Path, limits: ConnectorLimits) -> int:
        connection = self._connect(allow=(destination.resolve().parent,))
        try:
            with _Timeout(connection, limits.timeout_s) as timeout:
                try:
                    connection.execute(
                        f"COPY (SELECT * FROM ({query}) AS q LIMIT {int(limits.max_rows)}) "
                        f"TO '{_escape(destination.as_posix())}' (FORMAT PARQUET)"
                    )
                    rows = connection.execute(f"SELECT COUNT(*) FROM read_parquet('{_escape(destination.as_posix())}')").fetchone()[0]
                except Exception as exc:  # noqa: BLE001
                    if timeout.fired:
                        raise ConnectorError(f"query exceeded timeout_s={limits.timeout_s}", code="timeout") from exc
                    raise ConnectorError(f"snapshot failed: {type(exc).__name__}: {exc}", code="query_failed") from exc
        finally:
            connection.close()
        return int(rows)


def _srid_from(value: object) -> int | None:
    if value is None:
        return None
    text = str(value).strip().upper()
    if text.startswith("EPSG:"):
        text = text[5:]
    return int(text) if text.isdigit() else None
