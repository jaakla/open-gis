from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from .helpers import make_workspace, minimal_project, write_project

from assertions import geodata  # noqa: E402


def _write_geojson(path, features):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"type": "FeatureCollection", "features": features}), encoding="utf-8"
    )


def _point_feature(properties, coords=(0.0, 0.0)):
    return {
        "type": "Feature",
        "properties": properties,
        "geometry": {"type": "Point", "coordinates": list(coords)},
    }


class DuckdbUnavailableTests(unittest.TestCase):
    """Every geodata assertion must return not_testable, never an implicit
    pass, when DuckDB Spatial cannot be loaded."""

    def test_row_count_not_testable_without_duckdb(self) -> None:
        workspace = make_workspace()
        target = workspace / "data.geojson"
        _write_geojson(target, [_point_feature({"id": 1})])
        with patch.object(geodata, "_connect", return_value=None):
            result = geodata.row_count(workspace, path="data.geojson", equals=1)
        self.assertEqual(result.status, "not_testable")
        self.assertEqual(result.data.get("code"), "duckdb_unavailable")

    def test_geometry_all_valid_not_testable_without_duckdb(self) -> None:
        workspace = make_workspace()
        target = workspace / "data.geojson"
        _write_geojson(target, [_point_feature({"id": 1})])
        with patch.object(geodata, "_connect", return_value=None):
            result = geodata.geometry_all_valid(workspace, path="data.geojson")
        self.assertEqual(result.status, "not_testable")


class RowCountTests(unittest.TestCase):
    def test_missing_file_fails(self) -> None:
        workspace = make_workspace()
        result = geodata.row_count(workspace, path="missing.geojson", equals=1)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("code"), "file_missing")

    def test_equals_matches(self) -> None:
        workspace = make_workspace()
        target = workspace / "data.geojson"
        _write_geojson(target, [_point_feature({"id": 1}), _point_feature({"id": 2})])
        result = geodata.row_count(workspace, path="data.geojson", equals=2)
        self.assertEqual(result.status, "passed")

    def test_equals_mismatch_fails(self) -> None:
        workspace = make_workspace()
        target = workspace / "data.geojson"
        _write_geojson(target, [_point_feature({"id": 1})])
        result = geodata.row_count(workspace, path="data.geojson", equals=5)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("code"), "row_count_equals")

    def test_at_least_and_at_most(self) -> None:
        workspace = make_workspace()
        target = workspace / "data.geojson"
        _write_geojson(target, [_point_feature({"id": 1})])
        self.assertEqual(
            geodata.row_count(workspace, path="data.geojson", at_least=2).data.get("code"),
            "row_count_at_least",
        )
        self.assertEqual(
            geodata.row_count(workspace, path="data.geojson", at_most=0).data.get("code"),
            "row_count_at_most",
        )


class GeometryAllValidTests(unittest.TestCase):
    def test_valid_geometry_passes(self) -> None:
        workspace = make_workspace()
        target = workspace / "data.geojson"
        _write_geojson(target, [_point_feature({"id": 1})])
        result = geodata.geometry_all_valid(workspace, path="data.geojson")
        self.assertEqual(result.status, "passed")

    def test_invalid_polygon_fails(self) -> None:
        workspace = make_workspace()
        target = workspace / "bad.geojson"
        bowtie = {
            "type": "Feature",
            "properties": {"id": 1},
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[0, 0], [2, 2], [2, 0], [0, 2], [0, 0]]],
            },
        }
        _write_geojson(target, [bowtie])
        result = geodata.geometry_all_valid(workspace, path="bad.geojson")
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("code"), "invalid_geometry")


class DuplicateAndNullIdsTests(unittest.TestCase):
    def test_no_duplicates_passes(self) -> None:
        workspace = make_workspace()
        target = workspace / "data.geojson"
        _write_geojson(target, [_point_feature({"id": 1}), _point_feature({"id": 2})])
        result = geodata.no_duplicate_ids(workspace, path="data.geojson", id_field="id")
        self.assertEqual(result.status, "passed")

    def test_duplicates_fail(self) -> None:
        workspace = make_workspace()
        target = workspace / "data.geojson"
        _write_geojson(target, [_point_feature({"id": 1}), _point_feature({"id": 1})])
        result = geodata.no_duplicate_ids(workspace, path="data.geojson", id_field="id")
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("code"), "duplicate_ids")

    def test_null_ids_fail(self) -> None:
        workspace = make_workspace()
        target = workspace / "data.geojson"
        _write_geojson(target, [_point_feature({"id": None}), _point_feature({"id": 2})])
        result = geodata.no_null_ids(workspace, path="data.geojson", id_field="id")
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("code"), "null_ids")


class FeaturePresenceTests(unittest.TestCase):
    def test_feature_present_passes_when_found(self) -> None:
        workspace = make_workspace()
        target = workspace / "data.geojson"
        _write_geojson(target, [_point_feature({"id": "a"})])
        result = geodata.feature_present(workspace, path="data.geojson", id_field="id", id="a")
        self.assertEqual(result.status, "passed")

    def test_feature_present_fails_when_missing(self) -> None:
        workspace = make_workspace()
        target = workspace / "data.geojson"
        _write_geojson(target, [_point_feature({"id": "a"})])
        result = geodata.feature_present(workspace, path="data.geojson", id_field="id", id="b")
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("code"), "feature_missing")

    def test_feature_absent_passes_when_missing(self) -> None:
        workspace = make_workspace()
        target = workspace / "data.geojson"
        _write_geojson(target, [_point_feature({"id": "a"})])
        result = geodata.feature_absent(workspace, path="data.geojson", id_field="id", id="b")
        self.assertEqual(result.status, "passed")

    def test_feature_absent_fails_when_present(self) -> None:
        workspace = make_workspace()
        target = workspace / "data.geojson"
        _write_geojson(target, [_point_feature({"id": "a"})])
        result = geodata.feature_absent(workspace, path="data.geojson", id_field="id", id="a")
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("code"), "feature_present")

    def test_feature_absent_not_testable_when_file_missing(self) -> None:
        workspace = make_workspace()
        result = geodata.feature_absent(workspace, path="missing.geojson", id_field="id", id="a")
        self.assertEqual(result.status, "not_testable")


class FeatureFieldEqualsTests(unittest.TestCase):
    def test_matching_field_passes(self) -> None:
        workspace = make_workspace()
        target = workspace / "data.geojson"
        _write_geojson(target, [_point_feature({"id": "a", "status": "closed"})])
        result = geodata.feature_field_equals(
            workspace, path="data.geojson", id_field="id", id="a", field="status", equals="closed"
        )
        self.assertEqual(result.status, "passed")

    def test_mismatched_field_fails(self) -> None:
        workspace = make_workspace()
        target = workspace / "data.geojson"
        _write_geojson(target, [_point_feature({"id": "a", "status": "active"})])
        result = geodata.feature_field_equals(
            workspace, path="data.geojson", id_field="id", id="a", field="status", equals="closed"
        )
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("code"), "field_value_mismatch")

    def test_missing_feature_fails(self) -> None:
        workspace = make_workspace()
        target = workspace / "data.geojson"
        _write_geojson(target, [_point_feature({"id": "a", "status": "active"})])
        result = geodata.feature_field_equals(
            workspace, path="data.geojson", id_field="id", id="zzz", field="status", equals="closed"
        )
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("code"), "feature_not_found")


class FieldRangeTests(unittest.TestCase):
    def test_within_range_passes(self) -> None:
        workspace = make_workspace()
        target = workspace / "data.geojson"
        _write_geojson(target, [_point_feature({"area": 500})])
        result = geodata.field_range(workspace, path="data.geojson", field="area", min=0, max=1000)
        self.assertEqual(result.status, "passed")

    def test_out_of_range_fails(self) -> None:
        workspace = make_workspace()
        target = workspace / "data.geojson"
        _write_geojson(target, [_point_feature({"area": 5000})])
        result = geodata.field_range(workspace, path="data.geojson", field="area", min=0, max=1000)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("code"), "field_out_of_range")


class CrsNotUsedForMetricsTests(unittest.TestCase):
    def test_projected_crs_passes(self) -> None:
        workspace = make_workspace()
        write_project(workspace, minimal_project())
        result = geodata.crs_not_used_for_metrics(workspace)
        self.assertEqual(result.status, "passed")

    def test_geographic_analysis_crs_fails(self) -> None:
        workspace = make_workspace()
        project = minimal_project()
        project["processing"]["analysis_crs"] = "EPSG:4326"
        write_project(workspace, project)
        result = geodata.crs_not_used_for_metrics(workspace)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("code"), "forbidden_analysis_crs")

    def test_step_level_forbidden_crs_fails(self) -> None:
        workspace = make_workspace()
        project = minimal_project()
        project["processing"]["steps"][1]["crs"] = "EPSG:3857"
        write_project(workspace, project)
        result = geodata.crs_not_used_for_metrics(workspace)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("code"), "forbidden_step_crs")


if __name__ == "__main__":
    unittest.main()
