"""Source pin classes and credential hygiene (issue #13, B5a)."""

from __future__ import annotations

import hashlib
import unittest
from copy import deepcopy
from datetime import datetime, timezone

from openmapstack.checks.provenance import every_source_pinned, no_inline_credentials
from openmapstack.schema import project_schema_errors
from openmapstack.sources import (
    assess_pin,
    connection_reference_error,
    find_inline_credentials,
    redact,
)
from openmapstack.validation import validate_project
from tests.evals.helpers import make_workspace, minimal_project, write_project
from tests.test_cli import valid_manifest

NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)


def _snapshot(workspace, name="parcels.parquet", content=b"parquet bytes"):
    target = workspace / "data/source" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return {
        "class": "local_snapshot",
        "path": f"data/source/{name}",
        "sha256": "sha256:" + hashlib.sha256(content).hexdigest(),
        "captured_at": "2026-08-30T10:00:00Z",
    }


def _backend(**extra):
    pin = {
        "class": "backend_snapshot",
        "identifier": "pg_export_snapshot:00000003-000001A8-1",
        "captured_at": "2026-08-30T10:00:00Z",
        "retention_until": "2026-12-31T00:00:00Z",
    }
    pin.update(extra)
    return pin


def _source(**extra):
    source = deepcopy(minimal_project()["sources"]["test_source"])
    source.update(extra)
    return source


class PinAssessmentTests(unittest.TestCase):
    def test_version_identity_without_pin_block_is_still_accepted(self) -> None:
        assessment = assess_pin(make_workspace(), _source(), now=NOW)
        self.assertEqual((assessment.status, assessment.pin_class), ("pinned", "version_identity"))

    def test_mutable_alias_is_unpinned_even_with_a_valid_snapshot(self) -> None:
        workspace = make_workspace()
        source = _source(version={"identifier": "latest"}, pin=_snapshot(workspace))
        self.assertEqual(assess_pin(workspace, source, now=NOW).status, "unpinned")

    def test_local_snapshot_pins_when_bytes_match(self) -> None:
        workspace = make_workspace()
        assessment = assess_pin(workspace, _source(pin=_snapshot(workspace)), now=NOW)
        self.assertEqual((assessment.status, assessment.pin_class), ("pinned", "local_snapshot"))

    def test_local_snapshot_missing_or_changed_is_not_reproducible(self) -> None:
        workspace = make_workspace()
        pin = _snapshot(workspace)
        (workspace / pin["path"]).write_bytes(b"edited")
        changed = assess_pin(workspace, _source(pin=pin), now=NOW)
        self.assertEqual((changed.status, changed.details["cause"]), ("not_reproducible", "snapshot_hash_mismatch"))
        (workspace / pin["path"]).unlink()
        missing = assess_pin(workspace, _source(pin=pin), now=NOW)
        self.assertEqual((missing.status, missing.details["cause"]), ("not_reproducible", "snapshot_missing"))

    def test_local_snapshot_must_live_under_data_source(self) -> None:
        workspace = make_workspace()
        pin = _snapshot(workspace)
        pin["path"] = "data/derived/parcels.parquet"
        self.assertEqual(assess_pin(workspace, _source(pin=pin), now=NOW).status, "invalid")
        pin["path"] = "../outside.parquet"
        self.assertEqual(assess_pin(workspace, _source(pin=pin), now=NOW).status, "invalid")

    def test_backend_snapshot_pins_until_retention_lapses(self) -> None:
        workspace = make_workspace()
        self.assertEqual(assess_pin(workspace, _source(pin=_backend()), now=NOW).status, "pinned")
        expired = assess_pin(workspace, _source(pin=_backend()), now=datetime(2027, 1, 1, tzinfo=timezone.utc))
        self.assertEqual((expired.status, expired.details["cause"]), ("not_reproducible", "snapshot_expired"))

    def test_backend_snapshot_verified_inaccessible_is_not_reproducible(self) -> None:
        pin = _backend(verification={"at": "2026-08-31T00:00:00Z", "status": "inaccessible"})
        assessment = assess_pin(make_workspace(), _source(pin=pin), now=NOW)
        self.assertEqual((assessment.status, assessment.details["cause"]), ("not_reproducible", "snapshot_inaccessible"))

    def test_backend_snapshot_needs_identifier_and_retention(self) -> None:
        workspace = make_workspace()
        for broken in (
            _backend(identifier="latest"),
            {k: v for k, v in _backend().items() if k != "retention_until"},
            _backend(retention_until="whenever"),
            _backend(captured_at=None),
            {"class": "time_travel", "captured_at": "2026-08-30T10:00:00Z"},
            "not a mapping",
        ):
            assessment = assess_pin(workspace, _source(pin=broken), now=NOW)
            self.assertIn(assessment.status, {"invalid", "unpinned"}, broken)

    def test_provenance_check_reports_each_class_with_its_own_code(self) -> None:
        workspace = make_workspace()
        project = minimal_project()
        project["sources"]["test_source"]["pin"] = _snapshot(workspace)
        write_project(workspace, project)
        self.assertEqual(every_source_pinned(workspace).status, "passed")

        project["sources"]["test_source"]["pin"] = _backend(retention_until="2020-01-01T00:00:00Z")
        write_project(workspace, project)
        result = every_source_pinned(workspace)
        self.assertEqual((result.status, result.data["code"]), ("failed", "not_reproducible"))
        self.assertEqual(result.data["causes"], {"test_source": "snapshot_expired"})

        project["sources"]["test_source"]["pin"] = {"class": "backend_snapshot"}
        write_project(workspace, project)
        self.assertEqual(every_source_pinned(workspace).data["code"], "pin_invalid")

        del project["sources"]["test_source"]["pin"]
        project["sources"]["test_source"]["version"] = {"identifier": "latest"}
        write_project(workspace, project)
        self.assertEqual(every_source_pinned(workspace).data["code"], "source_unpinned")


class CredentialHygieneTests(unittest.TestCase):
    def test_embedded_secrets_are_found_without_being_echoed(self) -> None:
        source = _source(
            access={"method": "postgis", "retrieved_at": "x", "connection": "postgresql://gis:hunter2@db/gis"},
            notes="password=opensesame",
        )
        findings = find_inline_credentials(source, "sources.s")
        paths = {item["path"] for item in findings}
        self.assertIn("sources.s.access.connection", paths)
        self.assertIn("sources.s.notes", paths)
        self.assertNotIn("hunter2", str(findings))
        self.assertNotIn("opensesame", str(findings))

    def test_ordinary_urls_and_prose_are_not_flagged(self) -> None:
        source = _source(
            source_url="https://geoportaal.maaruum.ee/eng/spatial-data/cadastral-data-p310.html",
            rationale="The token field in the source schema is a land-use token, not a credential.",
        )
        self.assertEqual(find_inline_credentials(source, "sources.s"), [])

    def test_connection_must_be_a_reference(self) -> None:
        workspace = make_workspace()
        self.assertIsNone(connection_reference_error(workspace, "env:PARCELS_DSN"))
        self.assertIsNone(connection_reference_error(workspace, {"ref": "service:geo-prod"}))
        self.assertIsNone(connection_reference_error(workspace, None))
        self.assertIsNotNone(connection_reference_error(workspace, "host=db dbname=gis"))
        self.assertIsNotNone(connection_reference_error(workspace, "postgresql://db/gis"))
        self.assertIsNotNone(connection_reference_error(workspace, {"ref": "env:"}))
        # A secrets file inside the project directory would be committed, and
        # a relative path is refused here exactly as the connector refuses it.
        self.assertIsNotNone(connection_reference_error(workspace, "file:secrets/dsn.txt"))
        self.assertIsNotNone(connection_reference_error(workspace, "file:../secrets/postgis.dsn"))
        self.assertIsNotNone(connection_reference_error(workspace, f"file:{workspace}/dsn.txt"))
        self.assertIsNone(connection_reference_error(workspace, "file:/etc/openmapstack/dsn"))

    def test_provenance_check_and_validate_reject_inline_credentials(self) -> None:
        workspace = make_workspace()
        project = minimal_project()
        project["sources"]["test_source"]["access"]["connection"] = "postgresql://gis:hunter2@db/gis"
        write_project(workspace, project)
        result = no_inline_credentials(workspace)
        self.assertEqual((result.status, result.data["code"]), ("failed", "inline_credentials"))

        manifest = valid_manifest()
        key = next(iter(manifest["sources"]))
        manifest["sources"][key]["access"]["connection"] = {"ref": "env:GIS_DSN"}
        write_project(workspace, manifest)
        (workspace / "pipeline.py").write_text("print('ok')\n", encoding="utf-8")
        checks = {c.id: c for c in validate_project(workspace / "project.yaml", artifacts=False).checks}
        self.assertEqual(checks["source.credentials"].status, "passed")
        manifest["sources"][key]["access"]["connection"] = "postgresql://gis:hunter2@db/gis"
        write_project(workspace, manifest)
        checks = {c.id: c for c in validate_project(workspace / "project.yaml", artifacts=False).checks}
        self.assertEqual(checks["source.credentials"].status, "failed")
        self.assertNotIn("hunter2", checks["source.credentials"].message)

    def test_redaction_masks_secrets_in_recorded_text(self) -> None:
        text = "postgresql://gis:hunter2@db/gis password=abc token: xyz AKIAABCDEFGHIJKLMNOP"
        masked = redact(text)
        for secret in ("hunter2", "abc", "xyz", "AKIAABCDEFGHIJKLMNOP"):
            self.assertNotIn(secret, masked)
        self.assertIn("postgresql://gis:***@db/gis", masked)


class SchemaTests(unittest.TestCase):
    def test_pin_and_warehouse_blocks_validate(self) -> None:
        manifest = valid_manifest()
        key = next(iter(manifest["sources"]))
        manifest["sources"][key]["pin"] = _backend()
        manifest["sources"][key]["warehouse"] = {"backend": "postgis", "database": "gis", "schema": "cadastre", "table": "parcels"}
        manifest["sources"][key]["access"]["connection"] = {"ref": "env:GIS_DSN"}
        self.assertEqual(project_schema_errors(manifest), [])
        manifest["sources"][key]["pin"] = {"class": "local_snapshot", "captured_at": "x"}
        self.assertTrue(project_schema_errors(manifest))
        manifest["sources"][key]["pin"] = {"class": "elsewhere", "captured_at": "x"}
        self.assertTrue(project_schema_errors(manifest))

    def test_validate_reports_pin_class_per_source(self) -> None:
        workspace = make_workspace()
        manifest = valid_manifest()
        key = next(iter(manifest["sources"]))
        manifest["sources"][key]["pin"] = _snapshot(workspace)
        write_project(workspace, manifest)
        (workspace / "pipeline.py").write_text("print('ok')\n", encoding="utf-8")
        checks = [c for c in validate_project(workspace / "project.yaml", artifacts=False).checks if c.id == "source.pin"]
        self.assertEqual([c.status for c in checks], ["passed"])
        self.assertEqual(checks[0].details["pin_class"], "local_snapshot")


if __name__ == "__main__":
    unittest.main()
