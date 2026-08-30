from __future__ import annotations

import unittest

from .helpers import make_workspace, minimal_project, write_project

from openmapstack.checks import presentation  # noqa: E402


def _project_with_layers(layers, groups=None):
    project = minimal_project()
    project["presentation"]["map"]["layers"] = layers
    project["presentation"]["map"]["layer_groups"] = groups or []
    return project


class LayersUseSemanticRolesTests(unittest.TestCase):
    def test_all_roles_declared_passes(self) -> None:
        workspace = make_workspace()
        write_project(workspace, _project_with_layers(
            [{"source": "a", "semantic_role": "primary_result"}]
        ))
        result = presentation.layers_use_semantic_roles(workspace)
        self.assertEqual(result.status, "passed")

    def test_no_layers_warns(self) -> None:
        workspace = make_workspace()
        write_project(workspace, minimal_project())
        result = presentation.layers_use_semantic_roles(workspace)
        self.assertEqual(result.status, "warning")
        self.assertEqual(result.data.get("code"), "no_layers_declared")

    def test_missing_role_fails(self) -> None:
        workspace = make_workspace()
        write_project(workspace, _project_with_layers([{"source": "a"}]))
        result = presentation.layers_use_semantic_roles(workspace)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("code"), "semantic_role_missing")


class RequiredLayerGroupsExistTests(unittest.TestCase):
    def test_present_groups_pass(self) -> None:
        workspace = make_workspace()
        write_project(workspace, _project_with_layers([], groups=[{"id": "analysis"}]))
        result = presentation.required_layer_groups_exist(workspace, groups=["analysis"])
        self.assertEqual(result.status, "passed")

    def test_missing_group_fails(self) -> None:
        workspace = make_workspace()
        write_project(workspace, _project_with_layers([], groups=[]))
        result = presentation.required_layer_groups_exist(workspace, groups=["analysis"])
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("code"), "layer_group_missing")


class ControlsMatchPipelineTests(unittest.TestCase):
    def test_matching_scenario_and_filter_pass(self) -> None:
        workspace = make_workspace()
        project = minimal_project()
        project["overrides"] = [{"id": "OVERRIDE-1"}]
        project["presentation"]["controls"] = {
            "scenarios": [{"id": "s1", "override": "OVERRIDE-1"}],
            "filters": [{"id": "f1", "field": "area_m2", "canonical": 8000}],
        }
        project["processing"]["steps"][0]["expression"] = "area_m2 >= 8000"
        project["processing"]["steps"][0]["output_field"] = "area_m2"
        write_project(workspace, project)
        result = presentation.controls_match_pipeline(workspace)
        self.assertEqual(result.status, "passed")

    def test_multi_select_canonical_list_matches_each_member(self) -> None:
        workspace = make_workspace()
        project = minimal_project()
        project["presentation"]["controls"] = {
            "filters": [
                {"id": "land_use", "field": "land_use", "canonical": ["ARIMAA", "TOOTMISMAA"]}
            ]
        }
        project["processing"]["steps"][0]["expression"] = (
            "land_use IN ('TOOTMISMAA', 'ARIMAA')"
        )
        write_project(workspace, project)
        result = presentation.controls_match_pipeline(workspace)
        self.assertEqual(result.status, "passed")

    def test_multi_select_canonical_missing_one_member_fails(self) -> None:
        workspace = make_workspace()
        project = minimal_project()
        project["presentation"]["controls"] = {
            "filters": [
                {"id": "land_use", "field": "land_use", "canonical": ["ARIMAA", "ELAMUMAA"]}
            ]
        }
        project["processing"]["steps"][0]["expression"] = (
            "land_use IN ('TOOTMISMAA', 'ARIMAA')"
        )
        write_project(workspace, project)
        result = presentation.controls_match_pipeline(workspace)
        self.assertEqual(result.status, "failed")
        self.assertIn("ELAMUMAA", result.detail)

    def test_scenario_referencing_unknown_override_fails(self) -> None:
        workspace = make_workspace()
        project = minimal_project()
        project["presentation"]["controls"] = {"scenarios": [{"id": "s1", "override": "MISSING"}]}
        write_project(workspace, project)
        result = presentation.controls_match_pipeline(workspace)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("code"), "control_pipeline_drift")

    def test_drifted_filter_canonical_fails(self) -> None:
        workspace = make_workspace()
        project = minimal_project()
        project["presentation"]["controls"] = {
            "filters": [{"id": "f1", "field": "area_m2", "canonical": 9999}]
        }
        project["processing"]["steps"][0]["output_field"] = "area_m2"
        project["processing"]["steps"][0]["expression"] = "area_m2 >= 8000"
        write_project(workspace, project)
        result = presentation.controls_match_pipeline(workspace)
        self.assertEqual(result.status, "failed")


class EditTargetsReferenceRealSourcesTests(unittest.TestCase):
    def test_valid_target_passes(self) -> None:
        workspace = make_workspace()
        project = minimal_project()
        project["presentation"]["editing"] = {"targets": {"t1": {"source": "test_source"}}}
        write_project(workspace, project)
        result = presentation.edit_targets_reference_real_sources(workspace)
        self.assertEqual(result.status, "passed")

    def test_unknown_source_fails(self) -> None:
        workspace = make_workspace()
        project = minimal_project()
        project["presentation"]["editing"] = {"targets": {"t1": {"source": "no_such_source"}}}
        write_project(workspace, project)
        result = presentation.edit_targets_reference_real_sources(workspace)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("code"), "edit_target_unknown_source")

    def test_no_targets_vacuously_passes(self) -> None:
        workspace = make_workspace()
        write_project(workspace, minimal_project())
        result = presentation.edit_targets_reference_real_sources(workspace)
        self.assertEqual(result.status, "passed")


class DistinguishableLayerSemanticsTests(unittest.TestCase):
    def test_distinct_roles_pass(self) -> None:
        workspace = make_workspace()
        write_project(workspace, _project_with_layers([
            {"source": "a", "semantic_role": "primary_result"},
            {"source": "b", "semantic_role": "user_override"},
        ]))
        result = presentation.distinguishable_layer_semantics(workspace)
        self.assertEqual(result.status, "passed")

    def test_indistinguishable_roles_fail(self) -> None:
        workspace = make_workspace()
        write_project(workspace, _project_with_layers([
            {"source": "a", "semantic_role": "primary_result"},
            {"source": "b", "semantic_role": "primary_result"},
        ]))
        result = presentation.distinguishable_layer_semantics(workspace)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("code"), "indistinguishable_layers")


class ConsistentWithTests(unittest.TestCase):
    def test_matching_presentation_semantics_pass(self) -> None:
        workspace = make_workspace()
        project_a = _project_with_layers([{"source": "a", "semantic_role": "primary_result"}])
        project_b = _project_with_layers([{"source": "b", "semantic_role": "primary_result"}])
        write_project(workspace, project_a, project_dir="a")
        write_project(workspace, project_b, project_dir="b")
        result = presentation.consistent_with(workspace, other_project_dir="b", project_dir="a")
        self.assertEqual(result.status, "passed")

    def test_differing_layout_fails(self) -> None:
        workspace = make_workspace()
        project_a = _project_with_layers([{"source": "a", "semantic_role": "primary_result"}])
        project_b = _project_with_layers([{"source": "b", "semantic_role": "primary_result"}])
        project_b["presentation"]["layout"]["type"] = "dashboard"
        write_project(workspace, project_a, project_dir="a")
        write_project(workspace, project_b, project_dir="b")
        result = presentation.consistent_with(workspace, other_project_dir="b", project_dir="a")
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data.get("code"), "ux_semantics_drift")


if __name__ == "__main__":
    unittest.main()
