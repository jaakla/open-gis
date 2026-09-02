"""Connector pilot: read-only discovery, approval-gated snapshots, limits, redaction.

The DuckDB local-file tests run wherever ``openmapstack[geo]`` is installed.
The PostGIS session/discovery/plan contract is tested against a fake DB-API
driver so it needs no server; the end-to-end snapshot path runs only when
``OPENMAPSTACK_TEST_POSTGIS_DSN`` names a reachable PostGIS database.
"""

from __future__ import annotations

import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import yaml

from openmapstack.checks.spatial import connect_spatial
from openmapstack.cli import main
from openmapstack.connectors import (
    ConnectorError,
    ConnectorLimits,
    ConnectorUnavailable,
    apply_snapshot_to_manifest,
    discover_source,
    require_read_only_select,
    resolve_connection_reference,
    snapshot_source,
)
from openmapstack.connectors.postgis import PostGISConnector
from openmapstack.sources import assess_pin
from openmapstack.validation import validate_project
from tests.evals.helpers import make_workspace, minimal_project, write_project

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "evals" / "fixtures" / "mini-tartu"
DUCKDB_AVAILABLE = connect_spatial() is not None
LIVE_DSN = os.environ.get("OPENMAPSTACK_TEST_POSTGIS_DSN", "")


def _duckdb_project():
    workspace = make_workspace()
    project = minimal_project()
    project["sources"]["test_source"]["warehouse"] = {"backend": "duckdb"}
    write_project(workspace, project)
    (workspace / "data/source").mkdir(parents=True)
    for name in ("parcels.geojson", "roads.geojson"):
        (workspace / "data/source" / name).write_bytes((FIXTURES / name).read_bytes())
    return workspace, project


class QueryPolicyTests(unittest.TestCase):
    def test_only_a_single_select_is_accepted(self) -> None:
        self.assertTrue(require_read_only_select("  SELECT 1;  ").startswith("SELECT"))
        self.assertTrue(require_read_only_select("WITH t AS (SELECT 1) SELECT * FROM t"))
        for bad in (
            "DROP TABLE parcels",
            "SELECT 1; SELECT 2",
            "SELECT * FROM read_text('/etc/passwd')",
            "COPY (SELECT 1) TO '/tmp/x'",
            "SELECT * INTO backup FROM parcels",
            "SET enable_external_access = true",
            "INSTALL httpfs",
            "",
        ):
            with self.assertRaises(ConnectorError, msg=bad) as caught:
                require_read_only_select(bad)
            self.assertEqual(caught.exception.code, "query_rejected")

    def test_keywords_inside_string_literals_do_not_trip_the_policy(self) -> None:
        self.assertTrue(require_read_only_select("SELECT 'drop' AS word, 'set;' AS other"))

    def test_connection_references_resolve_without_recording_secrets(self) -> None:
        scheme, secret = resolve_connection_reference("env:OMS_TEST_DSN", environ={"OMS_TEST_DSN": "postgresql://u:pw@h/db"})
        self.assertEqual((scheme, secret), ("env", "postgresql://u:pw@h/db"))
        self.assertEqual(resolve_connection_reference({"ref": "service:geo"}, environ={}), ("service", "service=geo"))
        with self.assertRaises(ConnectorError) as caught:
            resolve_connection_reference("env:OMS_TEST_DSN", environ={})
        self.assertEqual(caught.exception.code, "connection_unresolved")
        with self.assertRaises(ConnectorError):
            resolve_connection_reference("postgresql://u:pw@h/db", environ={})
        with self.assertRaises(ConnectorError):
            resolve_connection_reference("file:relative/path", environ={})

    def test_connector_errors_are_redacted(self) -> None:
        error = ConnectorError("cannot open postgresql://gis:hunter2@db/gis", code="connection_failed")
        self.assertNotIn("hunter2", str(error))
        self.assertIn("gis:***@db", str(error))


@unittest.skipUnless(DUCKDB_AVAILABLE, "DuckDB Spatial is not available")
class DuckDBLocalConnectorTests(unittest.TestCase):
    def test_discovery_describes_files_read_only(self) -> None:
        workspace, project = _duckdb_project()
        discovery = discover_source(project, "test_source", project_root=workspace)
        self.assertEqual(discovery.backend, "duckdb")
        self.assertTrue(discovery.read_only)
        by_name = {table.name: table for table in discovery.tables}
        self.assertEqual(by_name["parcels.geojson"].geometry_column, "geom")
        self.assertEqual(by_name["parcels.geojson"].srid, 3301)
        self.assertEqual(by_name["parcels.geojson"].row_estimate, 5)
        self.assertEqual(discovery.notes, [])

    def test_snapshot_is_a_dry_run_unless_approved(self) -> None:
        workspace, project = _duckdb_project()
        query = 'SELECT cadastral_id, geom FROM "parcels.geojson" WHERE land_use = \'ARIMAA\''
        record = snapshot_source(project, "test_source", query, "data/source/arimaa.parquet", project_root=workspace)
        self.assertFalse(record["materialized"])
        self.assertEqual(record["plan"]["row_count"], 2)
        self.assertFalse((workspace / "data/source/arimaa.parquet").exists())

        record = snapshot_source(project, "test_source", query, "data/source/arimaa.parquet", project_root=workspace, approve=True)
        self.assertTrue(record["materialized"])
        self.assertEqual(record["rows"], 2)
        self.assertEqual(record["pin"]["class"], "local_snapshot")
        self.assertEqual(record["warehouse"]["query_sha256"], record["plan"]["query_sha256"])
        # The pin it hands back is one the pin contract accepts as pinned.
        updated = apply_snapshot_to_manifest(project, "test_source", record)
        self.assertEqual(assess_pin(workspace, updated["sources"]["test_source"]).status, "pinned")
        write_project(workspace, updated)
        (workspace / "pipeline.py").write_text("print('ok')\n", encoding="utf-8")
        checks = {c.id: c.status for c in validate_project(workspace / "project.yaml", artifacts=False).checks if c.id in {"source.pin", "source.credentials"}}
        self.assertEqual(checks, {"source.pin": "passed", "source.credentials": "passed"})

    def test_snapshots_never_overwrite_an_existing_source(self) -> None:
        workspace, project = _duckdb_project()
        with self.assertRaises(ConnectorError) as caught:
            snapshot_source(project, "test_source", 'SELECT * FROM "parcels.geojson"', "data/source/parcels.geojson", project_root=workspace, approve=True)
        self.assertEqual(caught.exception.code, "destination_invalid")
        (workspace / "data/source/taken.parquet").write_bytes(b"x")
        with self.assertRaises(ConnectorError) as caught:
            snapshot_source(project, "test_source", 'SELECT * FROM "parcels.geojson"', "data/source/taken.parquet", project_root=workspace, approve=True)
        self.assertEqual(caught.exception.code, "destination_exists")
        with self.assertRaises(ConnectorError) as caught:
            snapshot_source(project, "test_source", 'SELECT * FROM "parcels.geojson"', "data/derived/out.parquet", project_root=workspace, approve=True)
        self.assertEqual(caught.exception.code, "destination_invalid")

    def test_row_and_byte_limits_refuse_and_leave_no_partial_file(self) -> None:
        workspace, project = _duckdb_project()
        with self.assertRaises(ConnectorError) as caught:
            snapshot_source(project, "test_source", 'SELECT * FROM "parcels.geojson"', "data/source/big.parquet", project_root=workspace, limits=ConnectorLimits(max_rows=2))
        self.assertEqual(caught.exception.code, "row_limit_exceeded")
        with self.assertRaises(ConnectorError) as caught:
            snapshot_source(project, "test_source", 'SELECT * FROM "parcels.geojson"', "data/source/tiny.parquet", project_root=workspace, approve=True, limits=ConnectorLimits(max_bytes=50))
        self.assertEqual(caught.exception.code, "byte_limit_exceeded")
        self.assertEqual(sorted(p.name for p in (workspace / "data/source").iterdir()), ["parcels.geojson", "roads.geojson"])

    def test_file_access_is_confined_to_the_source_root(self) -> None:
        workspace, project = _duckdb_project()
        outside = workspace / "outside.parquet"
        (workspace / "data/derived").mkdir()
        for target in (outside, workspace / "data/derived/secret.parquet"):
            with self.assertRaises(ConnectorError, msg=target) as caught:
                snapshot_source(project, "test_source", f"SELECT * FROM read_parquet('{target.as_posix()}')", "data/source/leak.parquet", project_root=workspace)
            self.assertEqual(caught.exception.code, "query_failed")

    def test_cli_discover_and_snapshot(self) -> None:
        workspace, _ = _duckdb_project()
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(["source", "discover", str(workspace), "--source", "test_source", "--json"]), 0)
        self.assertEqual(json.loads(out.getvalue())["schema"], "openmapstack-source-discovery/v1")
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["source", "snapshot", str(workspace), "--source", "test_source", "--query", 'SELECT * FROM "roads.geojson"', "--destination", "data/source/roads.parquet"])
        self.assertEqual(code, 0)
        self.assertIn("DRY RUN", out.getvalue())
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["source", "snapshot", str(workspace), "--source", "test_source", "--query", 'SELECT * FROM "roads.geojson"', "--destination", "data/source/roads.parquet", "--approve", "--write-manifest", "--json"])
        self.assertEqual(code, 0)
        record = json.loads(out.getvalue())
        self.assertTrue(record["materialized"])
        manifest = yaml.safe_load((workspace / "project.yaml").read_text(encoding="utf-8"))
        self.assertEqual(manifest["sources"]["test_source"]["pin"]["path"], "data/source/roads.parquet")
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            self.assertEqual(main(["source", "snapshot", str(workspace), "--source", "test_source", "--query", "DROP TABLE x", "--destination", "data/source/x.parquet"]), 2)
        self.assertIn("query_rejected", err.getvalue())

    def test_unsupported_backend_is_refused_not_guessed(self) -> None:
        workspace, project = _duckdb_project()
        project["sources"]["test_source"]["warehouse"] = {"backend": "bigquery"}
        project["sources"]["test_source"]["access"]["connection"] = "env:BQ"
        with self.assertRaises(ConnectorError) as caught:
            discover_source(project, "test_source", project_root=workspace, environ={"BQ": "x"})
        self.assertEqual(caught.exception.code, "backend_unsupported")


@unittest.skipUnless(DUCKDB_AVAILABLE, "DuckDB Spatial is not available")
class NumericMaterialisationTests(unittest.TestCase):
    """NUMERIC must survive materialisation exactly; a rounded snapshot that
    is then hashed and pinned would be reproducible and wrong."""

    def _round_trip(self, values):
        from decimal import Decimal

        from openmapstack.connectors.postgis import _write_parquet

        duck = connect_spatial()
        destination = make_workspace() / "numeric.parquet"
        rows = [(f"r{i}", value) for i, value in enumerate(values)]
        try:
            _write_parquet(duck, destination, [{"name": "id", "type": "text"}, {"name": "amount", "type": "numeric"}], [], {}, rows)
            described = duck.execute(f"DESCRIBE SELECT * FROM read_parquet('{destination.as_posix()}')").fetchall()
            read = duck.execute(f"SELECT amount FROM read_parquet('{destination.as_posix()}') ORDER BY id").fetchall()
        finally:
            duck.close()
        return {name: str(type_name) for name, type_name, *_ in described}["amount"], [row[0] for row in read]

    def test_high_precision_values_are_exact(self) -> None:
        from decimal import Decimal

        values = [Decimal("12345678901234567890.123456789"), Decimal("0.000000001"), None]
        type_name, read = self._round_trip(values)
        self.assertTrue(type_name.startswith("DECIMAL"), type_name)
        self.assertEqual(read[0], values[0])
        self.assertEqual(read[1], values[1])
        self.assertIsNone(read[2])
        self.assertNotEqual(float(values[0]), values[0])  # DOUBLE would have rounded it

    def test_beyond_decimal_38_keeps_exact_text(self) -> None:
        from decimal import Decimal

        huge = Decimal("1" * 30 + "." + "2" * 20)
        type_name, read = self._round_trip([huge])
        self.assertEqual(type_name, "VARCHAR")
        self.assertEqual(Decimal(read[0]), huge)


class _FakeCursor:
    """Answers the exact statements the PostGIS connector issues."""

    def __init__(self, log: list[str], *, snapshot_supported: bool = True) -> None:
        self.log = log
        self.description = None
        self._rows: list[tuple] = []
        self._snapshot_supported = snapshot_supported

    def execute(self, statement: str, params=None) -> None:
        self.log.append(statement if params is None else f"{statement} -- {params}")
        text = statement.strip().lower()
        self.description = None
        if text.startswith("select current_database()"):
            self._rows = [("gis", "reader", "PostgreSQL 16.3 (Debian), compiled by gcc")]
        elif "from geometry_columns" in text:
            self._rows = [("cadastre", "parcels", "geom", 3301, "MULTIPOLYGON", 79056, "r")]
        elif text.startswith("show default_transaction_read_only"):
            self._rows = [("on",)]
        elif "limit 0" in text:
            self.description = [("cadastral_id", 25, None, None, None, None, None), ("geom", 17000, None, None, None, None, None)]
            self._rows = []
        elif text.startswith("select oid, typname"):
            self._rows = [(25, "text"), (17000, "geometry")]
        elif text.startswith("select count(*)"):
            self._rows = [(3,)]
        elif "pg_current_snapshot" in text:
            if not self._snapshot_supported:
                raise RuntimeError("function pg_current_snapshot() does not exist")
            self._rows = [("1001:1001:",)]
        else:
            self._rows = []

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _FakeConnection:
    def __init__(self, log: list[str], **kwargs) -> None:
        self.log = log
        self.kwargs = kwargs
        self.closed = False

    def cursor(self):
        return _FakeCursor(self.log, **self.kwargs)

    def rollback(self) -> None:
        self.log.append("ROLLBACK")

    def close(self) -> None:
        self.closed = True


class PostGISContractTests(unittest.TestCase):
    def test_sessions_are_read_only_with_a_statement_timeout(self) -> None:
        log: list[str] = []
        connector = PostGISConnector("postgresql://reader:pw@db/gis", connect=lambda dsn: _FakeConnection(log))
        discovery = connector.discover(ConnectorLimits(timeout_s=12.5))
        self.assertEqual(log[0], "SET default_transaction_read_only = on")
        self.assertIn("SET statement_timeout = 12500", log)
        self.assertTrue(discovery.read_only)
        [table] = discovery.tables
        self.assertEqual((table.schema, table.name, table.geometry_column, table.srid, table.row_estimate), ("cadastre", "parcels", "geom", 3301, 79056))
        self.assertEqual(discovery.identity["database"], "gis")
        self.assertNotIn("pw", json.dumps(discovery.to_dict()))

    def test_plan_records_schema_digest_and_non_durable_backend_snapshot(self) -> None:
        log: list[str] = []
        connector = PostGISConnector("dsn", connect=lambda dsn: _FakeConnection(log))
        plan = connector.plan("SELECT cadastral_id, geom FROM cadastre.parcels", ConnectorLimits())
        self.assertEqual(plan.row_count, 3)
        self.assertEqual([c["type"] for c in plan.columns], ["text", "geometry"])
        self.assertEqual(plan.backend_snapshot["kind"], "pg_current_snapshot")
        self.assertFalse(plan.backend_snapshot["durable"])
        self.assertTrue(plan.schema_sha256.startswith("sha256:"))

    def test_plan_survives_servers_without_pg_current_snapshot(self) -> None:
        log: list[str] = []
        connector = PostGISConnector("dsn", connect=lambda dsn: _FakeConnection(log, snapshot_supported=False))
        plan = connector.plan("SELECT 1", ConnectorLimits())
        self.assertIsNone(plan.backend_snapshot)

    def test_missing_driver_is_reported_not_hidden(self) -> None:
        import openmapstack.connectors.postgis as module

        original = module._default_connect

        def unavailable():
            raise ConnectorUnavailable("no driver")

        module._default_connect = unavailable
        try:
            with self.assertRaises(ConnectorUnavailable):
                PostGISConnector("dsn").discover(ConnectorLimits())
        finally:
            module._default_connect = original

    def test_driver_errors_never_leak_the_dsn(self) -> None:
        def failing(dsn: str):
            raise RuntimeError(f"could not connect to {dsn}")

        connector = PostGISConnector("postgresql://gis:hunter2@db/gis", connect=failing)
        with self.assertRaises(ConnectorError) as caught:
            connector.discover(ConnectorLimits())
        self.assertEqual(caught.exception.code, "connection_failed")
        self.assertNotIn("hunter2", str(caught.exception))


@unittest.skipUnless(LIVE_DSN and DUCKDB_AVAILABLE, "OPENMAPSTACK_TEST_POSTGIS_DSN is not set")
class PostGISLiveTests(unittest.TestCase):
    """End-to-end against a real PostGIS: discovery, dry run, materialised
    GeoParquet snapshot, and a pin the contract accepts."""

    def test_snapshot_round_trip(self) -> None:
        workspace = make_workspace()
        project = minimal_project()
        project["sources"]["test_source"]["warehouse"] = {"backend": "postgis", "schema": "public", "table": "parcels"}
        project["sources"]["test_source"]["access"]["connection"] = "env:OPENMAPSTACK_TEST_POSTGIS_DSN"
        write_project(workspace, project)
        discovery = discover_source(project, "test_source", project_root=workspace)
        self.assertTrue(discovery.read_only)
        self.assertTrue(any(table.name == "parcels" for table in discovery.tables), discovery.to_dict())
        query = "SELECT cadastral_id, land_use, area_m2, geom FROM public.parcels ORDER BY cadastral_id"
        dry = snapshot_source(project, "test_source", query, "data/source/parcels.parquet", project_root=workspace)
        self.assertFalse(dry["materialized"])
        record = snapshot_source(project, "test_source", query, "data/source/parcels.parquet", project_root=workspace, approve=True)
        self.assertTrue(record["materialized"])
        self.assertEqual(record["rows"], dry["plan"]["row_count"])
        self.assertNotIn("oms-test-pw", json.dumps(record))
        updated = apply_snapshot_to_manifest(project, "test_source", record)
        self.assertEqual(assess_pin(workspace, updated["sources"]["test_source"]).status, "pinned")
        connection = connect_spatial()
        try:
            rows = connection.execute(f"SELECT cadastral_id, ST_GeometryType(geom), area_m2 FROM read_parquet('{(workspace / 'data/source/parcels.parquet').as_posix()}') ORDER BY 1").fetchall()
        finally:
            connection.close()
        self.assertEqual([row[0] for row in rows], ["P1", "P2", "P3"])
        self.assertTrue(all("POLYGON" in str(row[1]).upper() for row in rows), rows)
        from decimal import Decimal

        # NUMERIC(24, 9) survives exactly; a DOUBLE mapping would have rounded it.
        self.assertEqual(rows[0][2], Decimal("10000.123456789"))
        # A write attempt through the same reference must be refused by the policy.
        with self.assertRaises(ConnectorError):
            snapshot_source(project, "test_source", "DELETE FROM public.parcels", "data/source/x.parquet", project_root=workspace, approve=True)


if __name__ == "__main__":
    unittest.main()
