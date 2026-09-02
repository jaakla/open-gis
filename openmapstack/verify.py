"""Verify a produced project without requiring a golden answer.

`validate` audits the manifest and its bookkeeping. `verify` runs the check
library in `openmapstack.checks` against what the pipeline actually produced:
geometry read back through DuckDB Spatial, dataset CRS read from real
coordinates rather than the manifest's claim, validation evidence recomputed
from the geodata it summarises, the QGIS project loaded, and -- with
`--rerun` -- the whole project rebuilt from source in an empty workspace.

The checks planned here do not require a repository-owned golden answer, so
they transfer to data this package has never seen. They establish bounded
structural, provenance, artifact, and reproducibility predicates; they do not
claim to prove every project-specific analytical answer.

The plan is derived from the manifest rather than configured, so a project
cannot quietly opt out of a check by omitting it: an output declared in
`outputs` is an output that gets checked.
"""

from __future__ import annotations

import re
import shutil
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .checks import AssertionResult, not_testable
from .checks import geodata as geodata_checks
from .checks import overrides as overrides_checks
from .checks import presentation as presentation_checks
from .checks import project as project_checks
from .checks import provenance as provenance_checks
from .checks import qgis as qgis_checks
from .checks import rerun as rerun_checks
from .checks import validation as validation_checks
from .expectations import evaluate_expectation
from .project import load_project
from .rerun import perform_clean_rerun

SCHEMA = "openmapstack-verify-result/v1"

# Formats DuckDB Spatial can read back. A declared output in some other
# format is reported as unchecked rather than silently skipped.
GEODATA_SUFFIXES = {".parquet", ".gpkg", ".geojson", ".json", ".fgb", ".shp"}

_EPSG = re.compile(r"\bEPSG:\s*(\d{4,6})\b", re.IGNORECASE)


@dataclass
class CheckRun:
    """One executed check, named the way an eval case would name it."""

    name: str
    result: AssertionResult
    args: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "check": self.name,
            "status": self.result.status,
            "message": self.result.detail,
        }
        if self.args:
            payload["args"] = self.args
        if self.evidence:
            payload["evidence"] = self.evidence
        code = (self.result.data or {}).get("code")
        if code:
            payload["code"] = code
        return payload


@dataclass
class VerifyResult:
    project_file: Path
    checks: list[CheckRun] = field(default_factory=list)
    rerun_evidence: dict[str, Any] | None = None

    @property
    def counts(self) -> dict[str, int]:
        totals = {"passed": 0, "warning": 0, "not_testable": 0, "failed": 0}
        for run in self.checks:
            totals[run.result.status] = totals.get(run.result.status, 0) + 1
        return totals

    @property
    def coverage(self) -> dict[str, int | float | None]:
        """Describe how much of the applicable plan actually executed.

        ``verify_project`` only adds checks that apply to the manifest: QGIS
        checks, for example, are absent when no QGIS project is declared.
        Every added check is therefore applicable. A ``not_testable`` result
        means that applicable check could not execute its predicate because
        an environmental dependency, supported artifact, or required
        addressing information was unavailable.
        """
        applicable = len(self.checks)
        not_testable_count = self.counts["not_testable"]
        executed = applicable - not_testable_count
        return {
            "applicable": applicable,
            "executed": executed,
            "not_testable": not_testable_count,
            "execution_rate": executed / applicable if applicable else None,
        }

    @property
    def status(self) -> str:
        counts = self.counts
        if counts["failed"]:
            return "failed"
        if counts["warning"]:
            return "warning"
        if counts["not_testable"]:
            # A completely unavailable plan is not testable. A partially
            # executed plan is a warning: the successful checks remain useful,
            # but the report must not present incomplete evidence as passed.
            return "not_testable" if self.coverage["executed"] == 0 else "warning"
        if not self.checks:
            return "not_testable"
        return "passed"

    def ok(self, *, strict: bool = False) -> bool:
        return self.status == "passed" if strict else self.status != "failed"

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema": SCHEMA,
            "project_file": str(self.project_file),
            "status": self.status,
            "counts": self.counts,
            "coverage": self.coverage,
            "checks": [run.to_dict() for run in self.checks],
        }
        if self.rerun_evidence is not None:
            payload["clean_rerun"] = self.rerun_evidence
        return payload


def _declared_output_paths(project: dict[str, Any]) -> list[tuple[str, str, str | None]]:
    """Return (output id, project-relative path, declared EPSG or None)."""
    outputs = project.get("outputs")
    if not isinstance(outputs, dict):
        return []
    found: list[tuple[str, str, str | None]] = []
    for name, spec in outputs.items():
        if not isinstance(spec, dict):
            continue
        path = spec.get("path")
        if not isinstance(path, str) or not path.strip():
            continue
        match = _EPSG.search(str(spec.get("format", "")))
        found.append((str(name), path, f"EPSG:{match.group(1)}" if match else None))
    return found


def _immutable_input_paths(root: Path) -> list[str]:
    paths: list[str] = []
    for directory in ("data/source", "data/overrides"):
        base = root / directory
        if not base.is_dir():
            continue
        for item in sorted(base.rglob("*")):
            if item.is_file():
                paths.append(item.relative_to(root).as_posix())
    return paths


def _run(
    runs: list[CheckRun],
    name: str,
    fn: Callable[..., AssertionResult],
    root: Path,
    **kwargs: Any,
) -> None:
    try:
        result = fn(root, **kwargs)
    except Exception as exc:  # noqa: BLE001 - a check must never take the command down
        result = not_testable(f"{type(exc).__name__}: {exc}", code="check_error")
    runs.append(CheckRun(name, result, dict(kwargs)))


def verify_project(
    project: str | Path,
    *,
    rerun: bool = False,
    rerun_timeout_s: float = 1800,
    forbidden_fragments: Sequence[str] = (),
) -> VerifyResult:
    """Run every applicable no-golden-answer check the environment supports."""
    project_file, manifest = load_project(project)
    root = project_file.parent
    result = VerifyResult(project_file=project_file)
    runs = result.checks

    # -- contract: the manifest describes a resolvable, single-entrypoint project
    _run(runs, "project.parses", project_checks.parses, root)
    _run(runs, "project.conforms_to_schema", project_checks.conforms_to_schema, root)
    _run(runs, "project.graph_resolves", project_checks.graph_resolves, root)
    _run(runs, "project.one_canonical_pipeline", project_checks.one_canonical_pipeline, root)
    _run(runs, "project.assumptions_have_rationale", project_checks.assumptions_have_rationale, root)
    _run(
        runs,
        "project.status_agrees_with_validation_report",
        project_checks.status_agrees_with_validation_report,
        root,
    )
    declared = [path for _, path, _ in _declared_output_paths(manifest)]
    if declared:
        _run(runs, "project.declared_files_exist", project_checks.declared_files_exist, root, files=declared)

    # -- provenance: sources are attributed, pinned, and licensed
    for name, fn in (
        ("every_source_has_provider_and_access", provenance_checks.every_source_has_provider_and_access),
        ("every_source_pinned", provenance_checks.every_source_pinned),
        ("license_present_where_required", provenance_checks.license_present_where_required),
        ("rationale_present", provenance_checks.rationale_present),
    ):
        _run(runs, f"provenance.{name}", fn, root)

    # -- overrides: declared, evidenced, and verified against the real source
    _run(runs, "overrides.every_override_has_provenance", overrides_checks.every_override_has_provenance, root)
    _run(runs, "overrides.evidence_not_placeholder", overrides_checks.evidence_not_placeholder, root)

    # -- validation: the report is complete, explicit, and matches the run record
    for name, fn in (
        ("required_all_present", validation_checks.required_all_present),
        ("no_implicit_pass", validation_checks.no_implicit_pass),
        ("warning_or_failed_propagates_to_status", validation_checks.warning_or_failed_propagates_to_status),
        ("run_record_matches", validation_checks.run_record_matches),
    ):
        _run(runs, f"validation.{name}", fn, root)

    # -- project-specific answers: execute only when independently attested
    expectations = (manifest.get("validation") or {}).get("expectations", [])
    if isinstance(expectations, list):
        seen_expectation_ids: set[str] = set()
        for index, expectation in enumerate(expectations):
            expectation_id = expectation.get("id") if isinstance(expectation, dict) else index
            if isinstance(expectation_id, str) and expectation_id in seen_expectation_ids:
                result_value = AssertionResult(
                    "failed",
                    f"expectation id {expectation_id!r} is duplicated",
                    {"code": "expectation_id_duplicate"},
                )
                evidence = {"class": "invalid"}
            else:
                result_value, evidence = evaluate_expectation(root, manifest, expectation)
            if isinstance(expectation_id, str):
                seen_expectation_ids.add(expectation_id)
            check = expectation.get("check") if isinstance(expectation, dict) else None
            args = expectation.get("args") if isinstance(expectation, dict) else None
            report_args = {"check": check, **args} if isinstance(args, dict) else {"check": check}
            runs.append(
                CheckRun(
                    f"expectation.{expectation_id}",
                    result_value,
                    report_args,
                    evidence,
                )
            )

    # -- geodata: read the produced files, do not trust what the manifest says
    _run(runs, "geodata.crs_not_used_for_metrics", geodata_checks.crs_not_used_for_metrics, root)
    for name, path, epsg in _declared_output_paths(manifest):
        if Path(path).suffix.lower() not in GEODATA_SUFFIXES:
            runs.append(
                CheckRun(
                    "geodata.geometry_all_valid",
                    not_testable(
                        f"output {name!r} is {Path(path).suffix or 'extensionless'}, "
                        "which DuckDB Spatial does not read back",
                        code="unsupported_format",
                    ),
                    {"path": path},
                )
            )
            continue
        _run(runs, "geodata.geometry_all_valid", geodata_checks.geometry_all_valid, root, path=path)
        if epsg:
            _run(runs, "geodata.dataset_crs_is", geodata_checks.dataset_crs_is, root, path=path, expected=epsg)
        else:
            runs.append(
                CheckRun(
                    "geodata.dataset_crs_is",
                    not_testable(
                        f"output {name!r} declares no EPSG code in its format string, "
                        "so its real CRS cannot be cross-checked",
                        code="crs_undeclared",
                    ),
                    {"path": path},
                )
            )

    # -- presentation and QGIS: the product matches what the manifest claims
    for name, fn in (
        ("layers_use_semantic_roles", presentation_checks.layers_use_semantic_roles),
        ("controls_match_pipeline", presentation_checks.controls_match_pipeline),
        ("edit_targets_reference_real_sources", presentation_checks.edit_targets_reference_real_sources),
    ):
        _run(runs, f"presentation.{name}", fn, root)
    if (root / "project.qgz").is_file():
        for name, fn in (
            ("static_valid", qgis_checks.static_valid),
            ("styles_declared", qgis_checks.styles_declared),
            ("groups_match_manifest", qgis_checks.groups_match_manifest),
            ("every_layer_declares_crs", qgis_checks.every_layer_declares_crs),
            ("runtime_load", qgis_checks.runtime_load),
            ("layers_match_manifest", qgis_checks.layers_match_manifest),
            ("every_declared_layer_renders", qgis_checks.every_declared_layer_renders),
        ):
            _run(runs, f"qgis.{name}", fn, root)

    # -- reproducibility
    _run(runs, "rerun.no_chat_dependency", rerun_checks.no_chat_dependency, root)
    if rerun:
        _verify_clean_rerun(
            result,
            root,
            manifest,
            timeout_s=rerun_timeout_s,
            forbidden_fragments=forbidden_fragments,
        )

    return result


def _verify_clean_rerun(
    result: VerifyResult,
    root: Path,
    manifest: dict[str, Any],
    *,
    timeout_s: float,
    forbidden_fragments: Sequence[str],
) -> None:
    """Rebuild the project from source and compare, then discard the copy."""
    rerun_root = Path(tempfile.mkdtemp(prefix="openmapstack-verify-rerun-"))
    try:
        result.rerun_evidence = perform_clean_rerun(
            root, rerun_root, timeout_s, forbidden_fragments=forbidden_fragments
        )
        runs = result.checks
        _run(
            runs,
            "rerun.clean_execution_succeeded",
            rerun_checks.clean_execution_succeeded,
            root,
            rerun_workspace=str(rerun_root),
        )
        outputs = [
            path
            for _, path, _ in _declared_output_paths(manifest)
            if Path(path).suffix.lower() in GEODATA_SUFFIXES
        ]
        if outputs:
            _run(
                runs,
                "rerun.outputs_semantically_equal",
                rerun_checks.outputs_semantically_equal,
                root,
                rerun_workspace=str(rerun_root),
                paths=outputs,
            )
        _run(
            runs,
            "rerun.validation_report_reproducible",
            rerun_checks.validation_report_reproducible,
            root,
            rerun_workspace=str(rerun_root),
        )
        immutable = _immutable_input_paths(root)
        if immutable:
            _run(
                runs,
                "overrides.source_files_byte_identical",
                overrides_checks.source_files_byte_identical,
                root,
                rerun_workspace=str(rerun_root),
                paths=immutable,
            )
    finally:
        shutil.rmtree(rerun_root, ignore_errors=True)
