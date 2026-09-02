"""Source provenance assertions.

See references/project-spec.md section 2.2.
"""

from __future__ import annotations

from pathlib import Path

from . import AssertionResult, failed, get_in, load_project_yaml, passed, project_root, warning


def every_source_has_provider_and_access(workspace: Path, project_dir: str = ".") -> AssertionResult:
    proj = load_project_yaml(workspace, project_dir)
    if proj is None:
        return failed("project.yaml missing", code="manifest_missing")
    sources = proj.get("sources") or {}
    if not sources:
        return failed("no sources declared", code="no_sources")
    missing: list[str] = []
    for key, src in sources.items():
        if not src.get("provider"):
            missing.append(f"{key}.provider")
        if not get_in(src, "access.method"):
            missing.append(f"{key}.access.method")
        if not (get_in(src, "access.retrieved_at") or get_in(src, "access.downloaded_at")):
            missing.append(f"{key}.access.retrieved_at")
    if missing:
        return failed(f"sources missing provider/access fields: {missing}", code="provider_access_missing")
    return passed(f"all {len(sources)} sources declare provider + access method + retrieval timestamp")


def every_source_pinned(workspace: Path, project_dir: str = ".") -> AssertionResult:
    """Every source is reproducibly pinned.

    A source without a ``pin`` block must carry a ``version.identifier`` or
    ``published_at`` that is not a mutable alias such as ``latest``. A source
    with a pin block is held to its pin class (``openmapstack.sources``): a
    local snapshot must exist and match its hash, and a backend snapshot must
    be identified, unexpired, and not known to be inaccessible. A pin that
    cannot deliver the bytes again is ``not_reproducible`` -- a timestamp
    string alone does not make a warehouse table pinned.
    """
    from ..sources import source_pin_summary

    proj = load_project_yaml(workspace, project_dir)
    if proj is None:
        return failed("project.yaml missing", code="manifest_missing")
    sources = proj.get("sources") or {}
    if not sources:
        return failed("no sources declared", code="no_sources")
    assessments = source_pin_summary(project_root(workspace, project_dir), sources)
    by_status: dict[str, list[str]] = {}
    for key, assessment in assessments.items():
        by_status.setdefault(assessment.status, []).append(f"{key}: {assessment.reason}")
    if by_status.get("invalid"):
        return failed(f"sources with malformed pins: {by_status['invalid']}", code="pin_invalid")
    if by_status.get("not_reproducible"):
        return failed(
            f"sources whose pinned snapshot cannot be obtained again: {by_status['not_reproducible']}",
            code="not_reproducible",
            causes={key: assessment.details.get("cause") for key, assessment in assessments.items() if assessment.status == "not_reproducible"},
        )
    if by_status.get("unpinned"):
        return failed(f"sources not pinned to a version/identifier: {by_status['unpinned']}", code="source_unpinned")
    classes = sorted({assessment.pin_class for assessment in assessments.values()})
    return passed(f"all {len(sources)} sources are pinned ({', '.join(classes)})", pin_classes=classes)


def no_inline_credentials(workspace: Path, project_dir: str = ".") -> AssertionResult:
    """No source embeds a secret, and warehouse connections are by reference."""
    from ..sources import connection_reference_error, find_inline_credentials

    proj = load_project_yaml(workspace, project_dir)
    if proj is None:
        return failed("project.yaml missing", code="manifest_missing")
    sources = proj.get("sources") or {}
    if not sources:
        return failed("no sources declared", code="no_sources")
    root = project_root(workspace, project_dir)
    problems: list[str] = []
    for key, src in sources.items():
        if not isinstance(src, dict):
            continue
        for finding in find_inline_credentials(src, f"sources.{key}"):
            problems.append(f"{finding['path']} ({finding['pattern']})")
        error = connection_reference_error(root, get_in(src, "access.connection"))
        if error:
            problems.append(f"sources.{key}.access.connection: {error}")
    if problems:
        return failed(f"credentials or connection strings embedded in the manifest: {problems}", code="inline_credentials")
    return passed(f"no inline credentials in {len(sources)} sources; connections are by reference")


def license_present_where_required(
    workspace: Path, required_for: list[str] | None = None, project_dir: str = "."
) -> AssertionResult:
    proj = load_project_yaml(workspace, project_dir)
    if proj is None:
        return failed("project.yaml missing", code="manifest_missing")
    sources = proj.get("sources") or {}
    required_for = required_for or list(sources.keys())
    missing = [k for k in required_for if k in sources and not get_in(sources[k], "license.name")]
    if missing:
        return failed(f"sources missing license.name: {missing}", code="license_missing")
    return passed("license metadata present for required sources")


def rationale_present(workspace: Path, project_dir: str = ".") -> AssertionResult:
    proj = load_project_yaml(workspace, project_dir)
    if proj is None:
        return failed("project.yaml missing", code="manifest_missing")
    sources = proj.get("sources") or {}
    missing = [k for k, s in sources.items() if not s.get("rationale")]
    if missing:
        return failed(f"sources missing selection rationale: {missing}", code="rationale_missing")
    return passed(f"all {len(sources)} sources document selection rationale")


def bounded_api_completeness(
    workspace: Path, source: str, project_dir: str = "."
) -> AssertionResult:
    """For bounded/paginated APIs, matched == returned must be recorded, or
    an explicit reason for incompleteness must be present."""
    proj = load_project_yaml(workspace, project_dir)
    if proj is None:
        return failed("project.yaml missing", code="manifest_missing")
    src = get_in(proj, f"sources.{source}")
    if src is None:
        return failed(f"source {source!r} not declared", code="source_not_declared")
    completeness = get_in(src, "selection.completeness") or get_in(src, "completeness")
    if completeness is None:
        return warning(
            f"source {source!r} does not record completeness (matched/returned)",
            code="completeness_undeclared",
        )
    matched = completeness.get("matched")
    returned = completeness.get("returned")
    if matched is None or returned is None:
        return warning(
            f"source {source!r} completeness block missing matched/returned",
            code="completeness_incomplete_fields",
        )
    if matched != returned:
        return failed(
            f"source {source!r} incomplete: matched={matched} returned={returned} "
            "(a response filled to the page limit is not proof of completeness)",
            code="completeness_mismatch",
        )
    return passed(f"source {source!r} complete: matched == returned == {matched}")


def semantic_predicate_documented(
    workspace: Path, source: str, project_dir: str = "."
) -> AssertionResult:
    """If a source's selection depends on a semantic predicate (ownership,
    active status, etc.), the authoritative field/domain must be documented."""
    proj = load_project_yaml(workspace, project_dir)
    if proj is None:
        return failed("project.yaml missing", code="manifest_missing")
    src = get_in(proj, f"sources.{source}")
    if src is None:
        return failed(f"source {source!r} not declared", code="source_not_declared")
    predicates = get_in(src, "selection.semantic_predicates")
    if not predicates:
        return warning(f"source {source!r} declares no semantic_predicates block", code="predicates_undeclared")
    missing = [p for p in predicates if not p.get("field") or p.get("domain_value") in (None, "")]
    if missing:
        return failed(
            f"source {source!r} has semantic_predicates missing field/domain_value",
            code="predicate_fields_missing",
        )
    return passed(f"source {source!r} documents {len(predicates)} semantic predicate(s)")
