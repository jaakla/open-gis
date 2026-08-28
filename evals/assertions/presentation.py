"""Presentation-contract assertions: semantic roles, layer groups, controls parity.

See references/project-spec.md sections 2.7 and 3.
"""

from __future__ import annotations

from pathlib import Path

from . import AssertionResult, failed, get_in, load_project_yaml, passed, warning

SEMANTIC_ROLES = {
    "primary_result", "secondary_result", "source", "context", "constraint",
    "excluded_area", "warning", "user_override", "planned", "hypothetical",
    "selected_feature",
}


def layers_use_semantic_roles(workspace: Path, project_dir: str = ".") -> AssertionResult:
    proj = load_project_yaml(workspace, project_dir)
    if proj is None:
        return failed("project.yaml missing", code="manifest_missing")
    layers = get_in(proj, "presentation.map.layers", []) or []
    if not layers:
        return warning("no layers declared under presentation.map.layers", code="no_layers_declared")
    missing = [layer.get("source", "?") for layer in layers if not layer.get("semantic_role")]
    if missing:
        return failed(f"layers missing semantic_role: {missing}", code="semantic_role_missing")
    return passed(f"all {len(layers)} layers declare a semantic_role")


def required_layer_groups_exist(workspace: Path, groups: list[str], project_dir: str = ".") -> AssertionResult:
    proj = load_project_yaml(workspace, project_dir)
    if proj is None:
        return failed("project.yaml missing", code="manifest_missing")
    declared = {g.get("id") for g in get_in(proj, "presentation.map.layer_groups", []) or []}
    missing = [g for g in groups if g not in declared]
    if missing:
        return failed(f"missing required layer groups: {missing}", code="layer_group_missing")
    return passed(f"all required layer groups present: {groups}")


def controls_match_pipeline(workspace: Path, project_dir: str = ".") -> AssertionResult:
    """presentation.controls.filters[].canonical must equal the threshold the
    pipeline actually used for the equivalent processing.steps parameter, and
    presentation.controls.scenarios[].override must reference a real
    override id. A drift here means the view can misrepresent the run."""
    proj = load_project_yaml(workspace, project_dir)
    if proj is None:
        return failed("project.yaml missing", code="manifest_missing")

    override_ids = {o.get("id") for o in (proj.get("overrides") or [])}
    scenarios = get_in(proj, "presentation.controls.scenarios", []) or []
    errors: list[str] = []
    for s in scenarios:
        if s.get("override") not in override_ids:
            errors.append(f"scenario {s.get('id')} references unknown override {s.get('override')!r}")

    steps = get_in(proj, "processing.steps", []) or []
    filters = get_in(proj, "presentation.controls.filters", []) or []
    for f in filters:
        field = f.get("field")
        canonical = f.get("canonical")
        if field is None or canonical is None:
            continue
        # Look for a step expression mentioning this field and the canonical value.
        matching_steps = [
            s for s in steps
            if field in str(s.get("expression", "")) or field == s.get("output_field")
        ]
        if not matching_steps:
            continue  # not every control necessarily maps 1:1 to a single step; skip silently
        found = any(str(canonical) in str(s.get("expression", "")) for s in matching_steps)
        if not found:
            errors.append(
                f"control {f.get('id')} canonical={canonical!r} not found in matching step expression(s)"
            )

    if errors:
        return failed("; ".join(errors), errors=errors, code="control_pipeline_drift")
    return passed(f"{len(filters)} filter control(s) and {len(scenarios)} scenario control(s) consistent")


def edit_targets_reference_real_sources(workspace: Path, project_dir: str = ".") -> AssertionResult:
    proj = load_project_yaml(workspace, project_dir)
    if proj is None:
        return failed("project.yaml missing", code="manifest_missing")
    sources = set((proj.get("sources") or {}).keys())
    targets = get_in(proj, "presentation.editing.targets", {}) or {}
    if not targets:
        return passed("no edit targets declared (vacuously true)")
    bad = [k for k, t in targets.items() if t.get("source") not in sources]
    if bad:
        return failed(f"edit targets referencing unknown sources: {bad}", code="edit_target_unknown_source")
    return passed(f"all {len(targets)} edit targets reference real project sources")


def consistent_with(
    workspace: Path, other_project_dir: str, project_dir: str = ".",
    required_shared_keys: list[str] | None = None,
) -> AssertionResult:
    """Two different analyses over the same skill must share stable UX
    semantics (layout type, sidebar organization, semantic-role vocabulary,
    provenance_ui shape, editing capability keys) even though their actual
    layers/results differ. Do not require byte-identical output."""
    proj_a = load_project_yaml(workspace, project_dir)
    proj_b = load_project_yaml(workspace, other_project_dir)
    if proj_a is None or proj_b is None:
        return failed("one of the two projects is missing project.yaml", code="manifest_missing")

    errors: list[str] = []

    layout_a = get_in(proj_a, "presentation.layout.type")
    layout_b = get_in(proj_b, "presentation.layout.type")
    if layout_a != layout_b:
        errors.append(f"layout.type differs: {layout_a!r} vs {layout_b!r}")

    org_a = get_in(proj_a, "presentation.layout.sidebar.organization")
    org_b = get_in(proj_b, "presentation.layout.sidebar.organization")
    if org_a != org_b:
        errors.append(f"sidebar.organization differs: {org_a!r} vs {org_b!r}")

    prov_a = set(get_in(proj_a, "presentation.provenance_ui", {}) or {})
    prov_b = set(get_in(proj_b, "presentation.provenance_ui", {}) or {})
    if prov_a != prov_b:
        errors.append(f"provenance_ui keys differ: {sorted(prov_a)} vs {sorted(prov_b)}")

    edit_a = set(get_in(proj_a, "presentation.editing", {}) or {})
    edit_b = set(get_in(proj_b, "presentation.editing", {}) or {})
    if edit_a != edit_b:
        errors.append(f"editing capability keys differ: {sorted(edit_a)} vs {sorted(edit_b)}")

    roles_a = {l.get("semantic_role") for l in get_in(proj_a, "presentation.map.layers", []) or []}
    roles_b = {l.get("semantic_role") for l in get_in(proj_b, "presentation.map.layers", []) or []}
    if not (roles_a & roles_b):
        errors.append(f"no shared semantic roles between projects: {roles_a} vs {roles_b}")

    for key in required_shared_keys or []:
        va = get_in(proj_a, key)
        vb = get_in(proj_b, key)
        if va != vb:
            errors.append(f"{key} differs: {va!r} vs {vb!r}")

    if errors:
        return failed("; ".join(errors), errors=errors, code="ux_semantics_drift")
    return passed("presentation semantics stable across both analyses")


def distinguishable_layer_semantics(workspace: Path, project_dir: str = ".") -> AssertionResult:
    """Source, result, override, and hypothetical data must remain visually
    distinguishable — i.e. use different semantic_role values, not all the
    same role."""
    proj = load_project_yaml(workspace, project_dir)
    if proj is None:
        return failed("project.yaml missing", code="manifest_missing")
    layers = get_in(proj, "presentation.map.layers", []) or []
    roles = {layer.get("semantic_role") for layer in layers if layer.get("semantic_role")}
    if len(layers) > 1 and len(roles) <= 1:
        return failed(
            "all layers share a single semantic_role; source/result/override/hypothetical indistinguishable",
            code="indistinguishable_layers",
        )
    return passed(f"layers use {len(roles)} distinct semantic role(s)")
