from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from tests.evals.helpers import EVALS_DIR  # noqa: F401 - ensures evals/ is importable

import spatial


class FakeConnection:
    def __init__(self, *, load_fails: bool = False) -> None:
        self.load_fails = load_fails
        self.commands: list[str] = []
        self.closed = False

    def execute(self, command: str):
        self.commands.append(command)
        if command == "LOAD spatial" and self.load_fails:
            raise RuntimeError("extension absent")
        if command == "INSTALL spatial":
            self.load_fails = False
        return self

    def close(self) -> None:
        self.closed = True


class FakeDuckdb:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.config: dict[str, str] | None = None

    def connect(self, *, config):
        self.config = config
        return self.connection


class ControlledSpatialTests(unittest.TestCase):
    def test_grading_loads_preinstalled_extension_without_install(self) -> None:
        connection = FakeConnection()
        duckdb = FakeDuckdb(connection)
        with patch.dict("sys.modules", {"duckdb": duckdb}):
            result = spatial.connect_spatial()
        self.assertIs(result, connection)
        self.assertEqual(connection.commands, ["LOAD spatial"])

    def test_missing_extension_never_installs_during_grading(self) -> None:
        connection = FakeConnection(load_fails=True)
        duckdb = FakeDuckdb(connection)
        with patch.dict("sys.modules", {"duckdb": duckdb}):
            result = spatial.connect_spatial()
        self.assertIsNone(result)
        self.assertEqual(connection.commands, ["LOAD spatial"])
        self.assertTrue(connection.closed)

    def test_preparation_is_the_only_explicit_install_path(self) -> None:
        connection = FakeConnection(load_fails=True)
        duckdb = FakeDuckdb(connection)
        with patch.dict("sys.modules", {"duckdb": duckdb}):
            result = spatial.connect_spatial(install=True)
        self.assertIs(result, connection)
        self.assertEqual(
            connection.commands,
            ["LOAD spatial", "INSTALL spatial", "LOAD spatial"],
        )

    def test_configured_extension_directory_is_passed_to_duckdb(self) -> None:
        connection = FakeConnection()
        duckdb = FakeDuckdb(connection)
        with (
            patch.dict("sys.modules", {"duckdb": duckdb}),
            patch.dict(os.environ, {spatial.EXTENSION_DIR_ENV: "/tmp/openmapstack-spatial-test"}),
        ):
            spatial.connect_spatial()
        self.assertEqual(
            duckdb.config,
            {"extension_directory": "/tmp/openmapstack-spatial-test"},
        )


if __name__ == "__main__":
    unittest.main()
