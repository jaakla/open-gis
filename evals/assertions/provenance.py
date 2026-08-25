"""Source provenance assertions.

See references/project-spec.md section 2.2.
"""

from __future__ import annotations

from pathlib import Path

from . import AssertionResult, failed, get_in, load_project_yaml, passed, warning


def every_source_has_provider_and_access(workspace: Path, project_dir: str = ".") -> AssertionResult:
    proj = load_project_yaml(workspace, project_dir)
    if proj is None:
        return failed("project.yaml missing")
    sources = proj.get("sources") or {}
    if not sources:
        return failed("no sources declared")
    missing: list[str] = []
    for key, src in sources.items():
        if not src.get("provider"):
            missing.append(f"{key}.provider")
        if not get_in(src, "access.method"):
            missing.append(f"{key}.access.method")
        if not (get_in(src, "access.retrieved_at") or get_in(src, "access.downloaded_at")):
            missing.append(f"{key}.access.retrieved_at")
    if missing:
        return failed(f"sources missing provider/access fields: {missing}")
    return passed(f"all {len(sources)} sources declare provider + access method + retrieval timestamp")


def every_source_pinned(workspace: Path, project_dir: str = ".") -> AssertionResult:
    """Pinning to 'latest' is not reproducible — version.identifier/published_at
    must be present and not equal to the literal string 'latest'."""
    proj = load_project_yaml(workspace, project_dir)
    if proj is None:
        return failed("project.yaml missing")
    sources = proj.get("sources") or {}
    if not sources:
        return failed("no sources declared")
    unpinned: list[str] = []
    for key, src in sources.items():
        identifier = get_in(src, "version.identifier")
        published_at = get_in(src, "version.published_at")
        if not identifier and not published_at:
            unpinned.append(key)
        elif str(identifier).strip().lower() == "latest":
            unpinned.append(key)
    if unpinned:
        return failed(f"sources not pinned to a version/identifier: {unpinned}")
    return passed(f"all {len(sources)} sources are pinned")


def license_present_where_required(
    workspace: Path, required_for: list[str] | None = None, project_dir: str = "."
) -> AssertionResult:
    proj = load_project_yaml(workspace, project_dir)
    if proj is None:
        return failed("project.yaml missing")
    sources = proj.get("sources") or {}
    required_for = required_for or list(sources.keys())
    missing = [k for k in required_for if k in sources and not get_in(sources[k], "license.name")]
    if missing:
        return failed(f"sources missing license.name: {missing}")
    return passed("license metadata present for required sources")


def rationale_present(workspace: Path, project_dir: str = ".") -> AssertionResult:
    proj = load_project_yaml(workspace, project_dir)
    if proj is None:
        return failed("project.yaml missing")
    sources = proj.get("sources") or {}
    missing = [k for k, s in sources.items() if not s.get("rationale")]
    if missing:
        return failed(f"sources missing selection rationale: {missing}")
    return passed(f"all {len(sources)} sources document selection rationale")


def bounded_api_completeness(
    workspace: Path, source: str, project_dir: str = "."
) -> AssertionResult:
    """For bounded/paginated APIs, matched == returned must be recorded, or
    an explicit reason for incompleteness must be present."""
    proj = load_project_yaml(workspace, project_dir)
    if proj is None:
        return failed("project.yaml missing")
    src = get_in(proj, f"sources.{source}")
    if src is None:
        return failed(f"source {source!r} not declared")
    completeness = get_in(src, "selection.completeness") or get_in(src, "completeness")
    if completeness is None:
        return warning(f"source {source!r} does not record completeness (matched/returned)")
    matched = completeness.get("matched")
    returned = completeness.get("returned")
    if matched is None or returned is None:
        return warning(f"source {source!r} completeness block missing matched/returned")
    if matched != returned:
        return failed(
            f"source {source!r} incomplete: matched={matched} returned={returned} "
            "(a response filled to the page limit is not proof of completeness)"
        )
    return passed(f"source {source!r} complete: matched == returned == {matched}")


def semantic_predicate_documented(
    workspace: Path, source: str, project_dir: str = "."
) -> AssertionResult:
    """If a source's selection depends on a semantic predicate (ownership,
    active status, etc.), the authoritative field/domain must be documented."""
    proj = load_project_yaml(workspace, project_dir)
    if proj is None:
        return failed("project.yaml missing")
    src = get_in(proj, f"sources.{source}")
    if src is None:
        return failed(f"source {source!r} not declared")
    predicates = get_in(src, "selection.semantic_predicates")
    if not predicates:
        return warning(f"source {source!r} declares no semantic_predicates block")
    missing = [p for p in predicates if not p.get("field") or p.get("domain_value") in (None, "")]
    if missing:
        return failed(f"source {source!r} has semantic_predicates missing field/domain_value")
    return passed(f"source {source!r} documents {len(predicates)} semantic predicate(s)")
