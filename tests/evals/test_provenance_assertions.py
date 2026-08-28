from __future__ import annotations

import unittest

from .helpers import make_workspace, minimal_project, write_project

from assertions import provenance  # noqa: E402


class ProviderAndAccessTests(unittest.TestCase):
    def test_complete_source_passes(self) -> None:
        workspace = make_workspace()
        write_project(workspace, minimal_project())
        result = provenance.every_source_has_provider_and_access(workspace)
        self.assertEqual(result.status, "passed")

    def test_missing_provider_fails(self) -> None:
        workspace = make_workspace()
        project = minimal_project()
        del project["sources"]["test_source"]["provider"]
        write_project(workspace, project)
        result = provenance.every_source_has_provider_and_access(workspace)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("code"), "provider_access_missing")

    def test_no_sources_fails(self) -> None:
        workspace = make_workspace()
        project = minimal_project()
        project["sources"] = {}
        write_project(workspace, project)
        result = provenance.every_source_has_provider_and_access(workspace)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("code"), "no_sources")


class EverySourcePinnedTests(unittest.TestCase):
    def test_pinned_source_passes(self) -> None:
        workspace = make_workspace()
        write_project(workspace, minimal_project())
        result = provenance.every_source_pinned(workspace)
        self.assertEqual(result.status, "passed")

    def test_latest_identifier_fails(self) -> None:
        workspace = make_workspace()
        project = minimal_project()
        project["sources"]["test_source"]["version"]["identifier"] = "latest"
        write_project(workspace, project)
        result = provenance.every_source_pinned(workspace)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("code"), "source_unpinned")

    def test_missing_version_fields_fails(self) -> None:
        workspace = make_workspace()
        project = minimal_project()
        project["sources"]["test_source"]["version"] = {}
        write_project(workspace, project)
        result = provenance.every_source_pinned(workspace)
        self.assertEqual(result.status, "failed")


class LicensePresentTests(unittest.TestCase):
    def test_license_present_passes(self) -> None:
        workspace = make_workspace()
        write_project(workspace, minimal_project())
        result = provenance.license_present_where_required(workspace)
        self.assertEqual(result.status, "passed")

    def test_missing_license_fails(self) -> None:
        workspace = make_workspace()
        project = minimal_project()
        project["sources"]["test_source"]["license"] = {}
        write_project(workspace, project)
        result = provenance.license_present_where_required(workspace)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("code"), "license_missing")


class RationalePresentTests(unittest.TestCase):
    def test_rationale_present_passes(self) -> None:
        workspace = make_workspace()
        write_project(workspace, minimal_project())
        result = provenance.rationale_present(workspace)
        self.assertEqual(result.status, "passed")

    def test_missing_rationale_fails(self) -> None:
        workspace = make_workspace()
        project = minimal_project()
        project["sources"]["test_source"]["rationale"] = ""
        write_project(workspace, project)
        result = provenance.rationale_present(workspace)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("code"), "rationale_missing")


class BoundedApiCompletenessTests(unittest.TestCase):
    def test_matched_equals_returned_passes(self) -> None:
        workspace = make_workspace()
        project = minimal_project()
        project["sources"]["test_source"]["selection"]["completeness"] = {"matched": 5, "returned": 5}
        write_project(workspace, project)
        result = provenance.bounded_api_completeness(workspace, source="test_source")
        self.assertEqual(result.status, "passed")

    def test_mismatch_fails(self) -> None:
        workspace = make_workspace()
        project = minimal_project()
        project["sources"]["test_source"]["selection"]["completeness"] = {"matched": 10, "returned": 5}
        write_project(workspace, project)
        result = provenance.bounded_api_completeness(workspace, source="test_source")
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("code"), "completeness_mismatch")

    def test_undeclared_completeness_warns(self) -> None:
        workspace = make_workspace()
        write_project(workspace, minimal_project())
        result = provenance.bounded_api_completeness(workspace, source="test_source")
        self.assertEqual(result.status, "warning")
        self.assertEqual(result.data.get("code"), "completeness_undeclared")

    def test_unknown_source_fails(self) -> None:
        workspace = make_workspace()
        write_project(workspace, minimal_project())
        result = provenance.bounded_api_completeness(workspace, source="nope")
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("code"), "source_not_declared")


class SemanticPredicateDocumentedTests(unittest.TestCase):
    def test_documented_predicate_passes(self) -> None:
        workspace = make_workspace()
        project = minimal_project()
        project["sources"]["test_source"]["selection"]["semantic_predicates"] = [
            {"field": "status", "domain_value": "active"}
        ]
        write_project(workspace, project)
        result = provenance.semantic_predicate_documented(workspace, source="test_source")
        self.assertEqual(result.status, "passed")

    def test_undeclared_predicates_warns(self) -> None:
        workspace = make_workspace()
        write_project(workspace, minimal_project())
        result = provenance.semantic_predicate_documented(workspace, source="test_source")
        self.assertEqual(result.status, "warning")
        self.assertEqual(result.data.get("code"), "predicates_undeclared")

    def test_incomplete_predicate_fields_fail(self) -> None:
        workspace = make_workspace()
        project = minimal_project()
        project["sources"]["test_source"]["selection"]["semantic_predicates"] = [{"field": "status"}]
        write_project(workspace, project)
        result = provenance.semantic_predicate_documented(workspace, source="test_source")
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("code"), "predicate_fields_missing")


if __name__ == "__main__":
    unittest.main()
