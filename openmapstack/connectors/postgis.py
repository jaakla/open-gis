"""PostGIS connector (the warehouse half of the reference pair).

Every session is opened with ``default_transaction_read_only = on`` and a
``statement_timeout``, discovery reads ``geometry_columns`` and planner
estimates, and a snapshot is materialised as GeoParquet through DuckDB from
rows fetched with geometry as WKB.

PostgreSQL has no durable time travel: ``pg_export_snapshot()`` lives only as
long as its transaction. The pin for a PostGIS source is therefore always a
``local_snapshot``; the transaction snapshot identity and the schema digest
are recorded beside it as *retrieval* metadata, not as a pin.

The DB-API driver (``psycopg`` 3, or ``psycopg2``) is imported lazily and
its absence is reported as ``driver_unavailable``. A ``connect`` callable
may be injected for tests.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

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

_GEOMETRY_TYPES = {"geometry", "geography"}


def _default_connect() -> Callable[[str], Any]:
    try:
        import psycopg  # type: ignore[import-not-found]

        return lambda dsn: psycopg.connect(dsn)
    except ImportError:
        pass
    try:
        import psycopg2  # type: ignore[import-not-found]

        return lambda dsn: psycopg2.connect(dsn)
    except ImportError as exc:
        raise ConnectorUnavailable("a PostgreSQL driver (psycopg or psycopg2) is required for the postgis connector") from exc


class PostGISConnector:
    backend = "postgis"

    def __init__(self, dsn: str, *, connect: Callable[[str], Any] | None = None) -> None:
        self._dsn = dsn
        self._connect = connect
        self._identity: dict[str, Any] = {}

    # -- sessions ---------------------------------------------------------------

    def _session(self, limits: ConnectorLimits):
        connect = self._connect or _default_connect()
        try:
            connection = connect(self._dsn)
        except ConnectorError:
            raise
        except Exception as exc:  # noqa: BLE001 - driver errors may carry the DSN
            raise ConnectorError(f"cannot connect to PostGIS: {type(exc).__name__}", code="connection_failed") from exc
        cursor = connection.cursor()
        cursor.execute("SET default_transaction_read_only = on")
        cursor.execute("SET transaction_read_only = on")
        # SET cannot take a bound parameter; the value is an int we computed.
        cursor.execute(f"SET statement_timeout = {int(limits.timeout_s * 1000)}")
        return connection, cursor

    def identity_for_manifest(self) -> dict[str, Any]:
        identity = {"backend": "postgis"}
        for key in ("database", "server_version"):
            if self._identity.get(key):
                identity[key] = self._identity[key]
        return identity

    # -- discovery --------------------------------------------------------------

    def discover(self, limits: ConnectorLimits) -> Discovery:
        connection, cursor = self._session(limits)
        try:
            cursor.execute("SELECT current_database(), current_user, version()")
            database, user, version = cursor.fetchone()
            self._identity = {"database": database, "user": user, "server_version": str(version).split(",")[0]}
            cursor.execute(
                "SELECT g.f_table_schema, g.f_table_name, g.f_geometry_column, g.srid, g.type, "
                "c.reltuples::bigint, c.relkind "
                "FROM geometry_columns g "
                "LEFT JOIN pg_namespace n ON n.nspname = g.f_table_schema "
                "LEFT JOIN pg_class c ON c.relname = g.f_table_name AND c.relnamespace = n.oid "
                "ORDER BY 1, 2, 3"
            )
            tables = [
                TableInfo(
                    str(schema), str(name), str(column), int(srid) if srid is not None else None,
                    str(geometry_type) if geometry_type else None,
                    int(estimate) if estimate is not None and estimate >= 0 else None,
                    kind="view" if relkind in ("v", "m") else "table",
                )
                for schema, name, column, srid, geometry_type, estimate, relkind in cursor.fetchall()
            ]
            cursor.execute("SHOW default_transaction_read_only")
            read_only = str(cursor.fetchone()[0]).lower() in {"on", "true", "1"}
        except ConnectorError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ConnectorError(f"discovery failed: {type(exc).__name__}", code="discovery_failed") from exc
        finally:
            _close(connection)
        notes = ["row_estimate comes from the planner (pg_class.reltuples); -1 means never analysed"]
        return Discovery("postgis", dict(self._identity), tables, read_only=read_only, notes=notes)

    # -- queries ----------------------------------------------------------------

    def _columns(self, cursor, query: str) -> list[dict[str, str]]:
        cursor.execute(f"SELECT * FROM ({query}) AS q LIMIT 0")
        description = cursor.description or []
        oids = sorted({int(column[1]) for column in description if column[1] is not None})
        names: dict[int, str] = {}
        if oids:
            cursor.execute("SELECT oid, typname FROM pg_type WHERE oid = ANY(%s)", (oids,))
            names = {int(oid): str(typname) for oid, typname in cursor.fetchall()}
        return [{"name": str(column[0]), "type": names.get(int(column[1]), str(column[1]))} for column in description]

    def plan(self, query: str, limits: ConnectorLimits) -> QueryPlan:
        connection, cursor = self._session(limits)
        try:
            columns = self._columns(cursor, query)
            cursor.execute(f"SELECT count(*) FROM ({query}) AS q")
            count = int(cursor.fetchone()[0])
            backend_snapshot: dict[str, Any] | None = None
            try:
                cursor.execute("SELECT pg_current_snapshot()::text")
                backend_snapshot = {
                    "kind": "pg_current_snapshot",
                    "value": str(cursor.fetchone()[0]),
                    "durable": False,
                    "note": "PostgreSQL keeps no durable time travel; the pin is the local snapshot",
                }
            except Exception:  # noqa: BLE001 - PostgreSQL < 13
                try:
                    connection.rollback()
                except Exception:  # noqa: BLE001
                    pass
        except ConnectorError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ConnectorError(f"query failed: {type(exc).__name__}", code="query_failed") from exc
        finally:
            _close(connection)
        return QueryPlan(columns, count, query_digest(query), schema_digest(columns), backend_snapshot)

    def materialize(self, query: str, destination: Path, limits: ConnectorLimits) -> int:
        from ..checks.spatial import connect_spatial

        duck = connect_spatial()
        if duck is None:
            raise ConnectorUnavailable("materialising GeoParquet requires duckdb with Spatial (pip install 'openmapstack[geo]')")
        connection, cursor = self._session(limits)
        try:
            columns = self._columns(cursor, query)
            geometry_columns = [column["name"] for column in columns if column["type"] in _GEOMETRY_TYPES]
            selected = ", ".join(
                f'ST_AsBinary("{column["name"]}") AS "{column["name"]}"' if column["name"] in geometry_columns else f'"{column["name"]}"'
                for column in columns
            )
            cursor.execute(f"SELECT {selected} FROM ({query}) AS q LIMIT %s", (int(limits.max_rows),))
            rows = cursor.fetchall()
            srids: dict[str, int | None] = {}
            for name in geometry_columns:
                cursor.execute(f'SELECT ST_SRID("{name}") FROM ({query}) AS q WHERE "{name}" IS NOT NULL LIMIT 1')
                row = cursor.fetchone()
                srids[name] = int(row[0]) if row and row[0] else None
        except ConnectorError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ConnectorError(f"snapshot failed: {type(exc).__name__}", code="query_failed") from exc
        finally:
            _close(connection)
        try:
            _write_parquet(duck, destination, columns, geometry_columns, srids, rows)
        finally:
            duck.close()
        return len(rows)


def _decimal_type(values) -> str:
    """A DuckDB type that holds every NUMERIC value exactly.

    ``numeric`` has arbitrary precision in PostgreSQL. Mapping it to DOUBLE
    would round identifiers and high-precision measurements while the file
    is hashed and pinned as the immutable source -- the snapshot would then
    be reproducible and wrong. Infer the widest scale and precision present
    and use DECIMAL; beyond DECIMAL(38) keep the exact text instead.
    """
    from decimal import Decimal

    max_scale = 0
    max_integer_digits = 1
    for value in values:
        if value is None:
            continue
        if not isinstance(value, Decimal):
            value = Decimal(str(value))
        if not value.is_finite():
            return "VARCHAR"
        sign, digits, exponent = value.as_tuple()
        scale = max(0, -exponent)
        integer_digits = max(1, len(digits) + exponent)
        max_scale = max(max_scale, scale)
        max_integer_digits = max(max_integer_digits, integer_digits)
    precision = max_integer_digits + max_scale
    if precision > 38:
        return "VARCHAR"
    return f"DECIMAL({precision}, {max_scale})"


def _write_parquet(duck, destination: Path, columns, geometry_columns, srids, rows) -> None:
    duck_types = {
        "int2": "SMALLINT", "int4": "INTEGER", "int8": "BIGINT", "float4": "FLOAT", "float8": "DOUBLE",
        "bool": "BOOLEAN", "date": "DATE", "timestamp": "TIMESTAMP", "timestamptz": "TIMESTAMPTZ",
        "json": "JSON", "jsonb": "JSON", "uuid": "UUID",
    }
    definitions = []
    exact_text_columns: set[int] = set()
    for index, column in enumerate(columns):
        if column["name"] in geometry_columns:
            definitions.append(f'"{column["name"]}" BLOB')
        elif column["type"] == "numeric":
            decimal_type = _decimal_type(row[index] for row in rows)
            if decimal_type == "VARCHAR":
                exact_text_columns.add(index)
            definitions.append(f'"{column["name"]}" {decimal_type}')
        else:
            definitions.append(f'"{column["name"]}" {duck_types.get(column["type"], "VARCHAR")}')
    duck.execute(f"CREATE TABLE staging ({', '.join(definitions)})")
    placeholders = ", ".join("?" for _ in columns)
    if rows:
        duck.executemany(
            f"INSERT INTO staging VALUES ({placeholders})",
            [tuple(_plain(value, exact_text=index in exact_text_columns) for index, value in enumerate(row)) for row in rows],
        )
    selected = []
    for column in columns:
        name = column["name"]
        if name in geometry_columns:
            srid = srids.get(name)
            geometry_expression = f'ST_GeomFromWKB("{name}")'
            if srid:
                try:
                    duck.execute(f"SELECT ST_SetSRID(ST_GeomFromWKB(NULL::BLOB), {int(srid)})")
                    geometry_expression = f'ST_SetSRID(ST_GeomFromWKB("{name}"), {int(srid)})'
                except Exception:  # noqa: BLE001 - older Spatial without CRS support
                    pass
            selected.append(f'{geometry_expression} AS "{name}"')
        else:
            selected.append(f'"{name}"')
    duck.execute(f"COPY (SELECT {', '.join(selected)} FROM staging) TO '{destination.as_posix().replace(chr(39), chr(39) * 2)}' (FORMAT PARQUET)")


def _plain(value: Any, *, exact_text: bool = False) -> Any:
    from decimal import Decimal

    if isinstance(value, memoryview):
        return bytes(value)
    if isinstance(value, Decimal):
        # DuckDB binds Decimal exactly for DECIMAL columns. For the VARCHAR
        # fallback the text must be rendered here: bound as a Decimal it would
        # be cast through DOUBLE and lose the digits the fallback exists for.
        if exact_text or not value.is_finite():
            return format(value, "f") if value.is_finite() else str(value)
        return value
    if isinstance(value, (dict, list)):
        import json

        return json.dumps(value)
    return value


def _close(connection) -> None:
    try:
        connection.rollback()
    except Exception:  # noqa: BLE001
        pass
    try:
        connection.close()
    except Exception:  # noqa: BLE001
        pass
