"""Metamorphic-relation assertions.

Thin check-library entry points over ``openmapstack.metamorphic`` so an eval
case (``assert: metamorphic.relation_holds``) and ``openmapstack verify
--metamorphic`` grade the same thing. See that module for the relations,
their preconditions, and the result vocabulary.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import AssertionResult, failed, load_project_yaml, not_testable, passed, project_root


def declarations_valid(workspace: Path, project_dir: str = ".") -> AssertionResult:
    """Every ``validation.metamorphic[]`` entry parses, names a known relation,
    and addresses declared outputs. Structural only: nothing is executed."""
    from ..metamorphic import DeclarationError, declared_relations, parse_declaration

    proj = load_project_yaml(workspace, project_dir)
    if proj is None:
        return failed("project.yaml missing", code="manifest_missing")
    try:
        raw_declarations = declared_relations(proj)
    except DeclarationError as exc:
        return failed(str(exc), code="metamorphic_declaration_invalid")
    if not raw_declarations:
        return not_testable("no metamorphic relations are declared", code="metamorphic_undeclared")
    errors: list[str] = []
    seen: set[str] = set()
    outputs = proj.get("outputs") if isinstance(proj.get("outputs"), dict) else {}
    for raw in raw_declarations:
        try:
            declaration = parse_declaration(raw)
        except DeclarationError as exc:
            errors.append(str(exc))
            continue
        if declaration.id in seen:
            errors.append(f"{declaration.id}: duplicate relation id")
        seen.add(declaration.id)
        missing = [key for key in declaration.outputs if key not in outputs]
        if missing:
            errors.append(f"{declaration.id}: outputs {missing} are not declared outputs")
    if errors:
        return failed("; ".join(errors), code="metamorphic_declaration_invalid")
    return passed(f"{len(raw_declarations)} metamorphic relation(s) are well-formed")


def relation_holds(
    workspace: Path,
    id: str,
    project_dir: str = ".",
    forbidden_fragments: list[str] | None = None,
) -> AssertionResult:
    """Execute the declared relation ``id`` in an isolated variant workspace."""
    from ..metamorphic import DeclarationError, declared_relations, run_relation

    root = project_root(workspace, project_dir)
    proj = load_project_yaml(workspace, project_dir)
    if proj is None:
        return failed("project.yaml missing", code="manifest_missing")
    try:
        raw_declarations = declared_relations(proj)
    except DeclarationError as exc:
        return failed(str(exc), code="metamorphic_declaration_invalid")
    matches = [raw for raw in raw_declarations if isinstance(raw, dict) and raw.get("id") == id]
    if not matches:
        return failed(f"no metamorphic relation with id {id!r} is declared", code="metamorphic_relation_undeclared")
    result, evidence = run_relation(root, proj, matches[0], forbidden_fragments=tuple(forbidden_fragments or ()))
    data: dict[str, Any] = dict(result.data)
    data["evidence"] = evidence
    return AssertionResult(result.status, result.detail, data)
