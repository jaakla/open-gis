"""Static artifact validation for ``open-gis-project/v1`` projects."""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

from .integrity import (
    canonical_file_set_hash,
    declared_input_paths,
    declared_output_paths,
    normalize_digest,
    sha256_file,
)
from .project import ProjectError, get_in, load_json, load_project, project_path, step_outputs
from .schema import project_schema_errors

SCHEMA = "open-gis-project/v1"
CHECK_STATUSES = {"passed", "failed", "warning", "not_testable"}
PROJECT_STATUSES = {"draft", "in_progress", "validated", "warning", "failed"}
RUN_STATUSES = {"passed", "warning", "failed"}
OVERRIDE_RESULTS = {"applied", "rejected", "not_testable"}
OVERRIDE_ACTIONS = {
    "add_feature",
    "edit_geometry",
    "replace_geometry",
    "modify_attribute",
    "hide_source_feature",
    "remove_source_feature",
    "merge_features",
    "split_feature",
    "add_annotation",
    "add_aoi",
    "add_scenario",
}
GEOMETRY_ACTIONS = {
    "add_feature",
    "edit_geometry",
    "replace_geometry",
    "add_annotation",
    "add_aoi",
    "add_scenario",
}
TARGET_ACTIONS = {
    "edit_geometry",
    "replace_geometry",
    "modify_attribute",
    "hide_source_feature",
    "remove_source_feature",
    "merge_features",
    "split_feature",
}
PLACEHOLDERS = {"", "todo", "tbd", "n/a", "none", "null", "...", "https://..."}


@dataclass(frozen=True)
class Check:
    id: str
    status: str
    message: str
    path: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.id,
            "status": self.status,
            "message": self.message,
        }
        if self.path:
            result["path"] = self.path
        if self.details:
            result["details"] = self.details
        return result


@dataclass
class ValidationResult:
    project_file: Path
    checks: list[Check]

    @property
    def status(self) -> str:
        statuses = {check.status for check in self.checks}
        if "failed" in statuses:
            return "failed"
        if statuses & {"warning", "not_testable"}:
            return "warning"
        return "passed"

    @property
    def counts(self) -> dict[str, int]:
        counts = Counter(check.status for check in self.checks)
        return {status: counts.get(status, 0) for status in ("passed", "warning", "not_testable", "failed")}

    def ok(self, *, strict: bool = False) -> bool:
        return self.status == "passed" if strict else self.status != "failed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "open-gis-validation-result/v1",
            "project_file": str(self.project_file),
            "status": self.status,
            "counts": self.counts,
            "checks": [check.to_dict() for check in self.checks],
        }


class _Validator:
    def __init__(self, project_file: Path, project: dict[str, Any], *, artifacts: bool) -> None:
        self.project_file = project_file
        self.root = project_file.parent
        self.project = project
        self.artifacts = artifacts
        self.checks: list[Check] = []
        self.report: dict[str, Any] | None = None
        self.report_path: Path | None = None

    def add(
        self,
        check_id: str,
        status: str,
        message: str,
        *,
        path: str | None = None,
        **details: Any,
    ) -> None:
        self.checks.append(Check(check_id, status, message, path, details))

    def require(self, condition: bool, check_id: str, message: str, *, path: str | None = None) -> bool:
        if not condition:
            self.add(check_id, "failed", message, path=path)
            return False
        return True

    def run(self) -> ValidationResult:
        self._schema_and_metadata()
        self._interpretation()
        self._sources()
        self._overrides()
        self._processing_and_outputs()
        self._presentation()
        self._warnings()
        self._runtime()
        self._validation_declaration()
        if self.artifacts:
            self._artifact_files()
            self._validation_report()
            self._run_record()
        self._declared_status_consistency()
        return ValidationResult(self.project_file, self.checks)

    def _schema_and_metadata(self) -> None:
        schema_errors = project_schema_errors(self.project)
        if schema_errors:
            self.add(
                "manifest.json_schema",
                "failed",
                "; ".join(schema_errors),
                path="project.yaml",
                errors=schema_errors,
            )
        else:
            self.add(
                "manifest.json_schema",
                "passed",
                "project.yaml conforms to the packaged Open-GIS v1 JSON Schema",
                path="project.yaml",
            )
        schema = self.project.get("schema")
        if schema == SCHEMA:
            self.add("manifest.schema", "passed", f"schema is {SCHEMA}", path="schema")
        else:
            self.add("manifest.schema", "failed", f"expected {SCHEMA!r}, got {schema!r}", path="schema")

        metadata = self.project.get("project")
        if not isinstance(metadata, dict):
            self.add("manifest.project", "failed", "project must be a mapping", path="project")
            return
        missing = [key for key in ("id", "title", "question", "created_at", "updated_at", "status") if not _present(metadata.get(key))]
        if missing:
            self.add("manifest.project", "failed", f"project metadata is missing: {', '.join(missing)}", path="project")
        elif metadata.get("status") not in PROJECT_STATUSES:
            self.add("manifest.project", "failed", f"unknown project.status {metadata.get('status')!r}", path="project.status")
        else:
            self.add("manifest.project", "passed", "project identity, timestamps, question, and status are declared", path="project")

    def _interpretation(self) -> None:
        interpretation = self.project.get("interpretation")
        if not isinstance(interpretation, dict) or not _present(interpretation.get("objective")):
            self.add("interpretation.objective", "failed", "interpretation.objective is required", path="interpretation.objective")
            return
        assumptions = interpretation.get("assumptions")
        if not isinstance(assumptions, list) or not assumptions:
            self.add("interpretation.assumptions", "warning", "no assumptions are documented", path="interpretation.assumptions")
            return
        bad: list[str] = []
        ids: list[str] = []
        for index, assumption in enumerate(assumptions):
            if not isinstance(assumption, dict):
                bad.append(str(index))
                continue
            aid = str(assumption.get("id", index))
            ids.append(aid)
            if not _present(assumption.get("id")) or not _present(assumption.get("statement")) or not _present(assumption.get("rationale")):
                bad.append(aid)
        duplicates = _duplicates(ids)
        if bad or duplicates:
            message = []
            if bad:
                message.append(f"missing id/statement/rationale: {bad}")
            if duplicates:
                message.append(f"duplicate ids: {duplicates}")
            self.add("interpretation.assumptions", "failed", "; ".join(message), path="interpretation.assumptions")
        else:
            self.add("interpretation.assumptions", "passed", f"{len(assumptions)} assumptions have rationale", path="interpretation.assumptions")

    def _sources(self) -> None:
        sources = self.project.get("sources")
        if not isinstance(sources, dict) or not sources:
            self.add("sources.provenance", "failed", "at least one source is required", path="sources")
            return
        for key, source in sources.items():
            base = f"sources.{key}"
            if not isinstance(source, dict):
                self.add("source.provenance", "failed", "source must be a mapping", path=base)
                continue
            missing: list[str] = []
            required_values = {
                "provider": source.get("provider"),
                "dataset": source.get("dataset"),
                "source_url": source.get("source_url"),
                "access.retrieved_at": get_in(source, "access", "retrieved_at") or get_in(source, "access", "downloaded_at"),
                "version.identifier/published_at": get_in(source, "version", "identifier") or get_in(source, "version", "published_at"),
                "selection": source.get("selection"),
                "rationale": source.get("rationale"),
            }
            for name, value in required_values.items():
                if not _present(value):
                    missing.append(name)
            if missing:
                self.add("source.provenance", "failed", f"missing reproducibility fields: {', '.join(missing)}", path=base)
            else:
                self.add("source.provenance", "passed", "source URL, retrieval, version, selection, and rationale are pinned", path=base)

            license_block = source.get("license")
            if not isinstance(license_block, dict) or not _present(license_block.get("name")) or not _present(license_block.get("url")):
                self.add("source.license", "failed", "license.name and license.url are required", path=f"{base}.license")
            elif _unknown(license_block.get("name")):
                self.add("source.license", "warning", f"license is unresolved: {license_block.get('name')}", path=f"{base}.license")
            else:
                self.add("source.license", "passed", f"license recorded as {license_block.get('name')}", path=f"{base}.license")

            completeness = source.get("completeness") or get_in(source, "selection", "completeness")
            method = str(get_in(source, "access", "method", default="")).lower()
            bounded = any(token in method for token in ("wfs", "api", "feature", "arcgis"))
            if bounded:
                matched = completeness.get("matched") if isinstance(completeness, dict) else None
                returned = completeness.get("returned") if isinstance(completeness, dict) else None
                if matched is None or returned is None:
                    self.add("source.completeness", "warning", "bounded API does not record matched and returned counts", path=base)
                elif matched != returned:
                    self.add("source.completeness", "failed", f"bounded API is incomplete: matched={matched}, returned={returned}", path=base)
                else:
                    self.add("source.completeness", "passed", f"bounded API completeness proved ({returned}/{matched})", path=base)

    def _overrides(self) -> None:
        overrides = self.project.get("overrides", [])
        if overrides is None:
            overrides = []
        if not isinstance(overrides, list):
            self.add("overrides.declaration", "failed", "overrides must be a list", path="overrides")
            return
        ids: list[str] = []
        geometry_files: set[Path] = set()
        for index, override in enumerate(overrides):
            base = f"overrides[{index}]"
            if not isinstance(override, dict):
                self.add("override.declaration", "failed", "override must be a mapping", path=base)
                continue
            oid = str(override.get("id", index))
            ids.append(oid)
            missing = [key for key in ("id", "action", "rationale", "created_at", "created_by") if not _present(override.get(key))]
            action = override.get("action")
            if action not in OVERRIDE_ACTIONS:
                missing.append("supported action")
            evidence = override.get("evidence")
            if not isinstance(evidence, list) or not evidence or any(_placeholder_evidence(item) for item in evidence):
                missing.append("non-placeholder evidence")
            if action in TARGET_ACTIONS:
                target = override.get("target")
                if not isinstance(target, dict) or not _present(target.get("source")):
                    missing.append("target.source")
                elif str(target.get("source")) not in set((self.project.get("sources") or {}).keys()):
                    missing.append("target.source referencing sources.*")
                if action not in {"merge_features", "split_feature"} and (not isinstance(target, dict) or not _present(target.get("feature_id"))):
                    missing.append("target.feature_id")
            if action == "modify_attribute":
                change = override.get("change")
                if not isinstance(change, dict) or not _present(change.get("field")) or "from" not in change or "to" not in change:
                    missing.append("change.field/from/to")
            if action in GEOMETRY_ACTIONS:
                geometry_rel = get_in(override, "geometry_file", "path")
                target = project_path(self.root, geometry_rel)
                if target is None:
                    missing.append("safe geometry_file.path")
                else:
                    geometry_files.add(target)
                    if not target.is_file():
                        self.add("override.geometry_file", "failed", f"geometry file does not exist: {geometry_rel}", path=base)
            if missing:
                self.add("override.declaration", "failed", f"{oid} is missing/invalid: {', '.join(missing)}", path=base)
            else:
                self.add("override.declaration", "passed", f"{oid} has action, provenance, rationale, and evidence", path=base)

        duplicates = _duplicates(ids)
        if duplicates:
            self.add("overrides.ids", "failed", f"duplicate override ids: {duplicates}", path="overrides")
        elif overrides:
            self.add("overrides.ids", "passed", f"{len(ids)} override ids are unique", path="overrides")

        if self.artifacts:
            override_dir = self.root / "data" / "overrides"
            if override_dir.is_dir():
                actual = {path.resolve() for path in override_dir.rglob("*") if path.is_file() and path.name != ".gitkeep"}
                unreferenced = sorted(str(path.relative_to(self.root)) for path in actual - geometry_files)
                if unreferenced:
                    self.add("overrides.undocumented_files", "warning", f"override files are not referenced by project.yaml: {unreferenced}", path="data/overrides")
                else:
                    self.add("overrides.undocumented_files", "passed", "all override geodata files are declared", path="data/overrides")

    def _processing_and_outputs(self) -> None:
        processing = self.project.get("processing")
        if not isinstance(processing, dict):
            self.add("processing.declaration", "failed", "processing must be a mapping", path="processing")
            return
        analysis_crs = processing.get("analysis_crs")
        storage_crs = processing.get("storage_crs")
        if not _present(analysis_crs) or not _present(storage_crs):
            self.add("processing.crs", "failed", "analysis_crs and storage_crs are required", path="processing")
        elif str(analysis_crs).upper() in {"EPSG:4326", "EPSG:3857"}:
            self.add("processing.crs", "failed", f"{analysis_crs} is not valid for metric analysis", path="processing.analysis_crs")
        else:
            self.add("processing.crs", "passed", f"analysis={analysis_crs}; storage={storage_crs}", path="processing")

        steps = processing.get("steps")
        if not isinstance(steps, list) or not steps:
            self.add("processing.graph", "failed", "processing.steps must be a non-empty ordered list", path="processing.steps")
            steps = []
        source_symbols = set((self.project.get("sources") or {}).keys())
        override_ids = {
            str(item.get("id"))
            for item in (self.project.get("overrides") or [])
            if isinstance(item, dict) and _present(item.get("id"))
        }
        produced: set[str] = set()
        step_ids: list[str] = []
        graph_errors: list[str] = []
        for index, step in enumerate(steps):
            if not isinstance(step, dict):
                graph_errors.append(f"step {index} is not a mapping")
                continue
            step_id = step.get("id")
            operation = step.get("operation")
            if not _present(step_id) or not _present(operation):
                graph_errors.append(f"step {index} needs id and operation")
                continue
            step_ids.append(str(step_id))
            references: list[tuple[str, object]] = []
            for key in ("input", "source", "target"):
                if key in step:
                    references.append((key, step.get(key)))
            inputs = step.get("inputs")
            if inputs is not None:
                if not isinstance(inputs, list):
                    graph_errors.append(f"step {step_id} inputs must be a list")
                else:
                    references.extend(("inputs", item) for item in inputs)
            for key, reference in references:
                if not isinstance(reference, str) or reference not in source_symbols | produced:
                    graph_errors.append(f"step {step_id} {key}={reference!r} is not a source or prior output")
            if "override" in step and str(step.get("override")) not in override_ids:
                graph_errors.append(f"step {step_id} override={step.get('override')!r} is not declared")
            for symbol in step_outputs(step):
                if symbol in produced or symbol in source_symbols:
                    graph_errors.append(f"step {step_id} produces duplicate symbol {symbol!r}")
                produced.add(symbol)
        duplicate_steps = _duplicates(step_ids)
        if duplicate_steps:
            graph_errors.append(f"duplicate step ids: {duplicate_steps}")

        outputs = self.project.get("outputs")
        if not isinstance(outputs, dict) or not outputs:
            self.add("outputs.declaration", "failed", "outputs must be a non-empty mapping", path="outputs")
            outputs = {}
        output_errors: list[str] = []
        for key, output in outputs.items():
            if not isinstance(output, dict):
                output_errors.append(f"{key} is not a mapping")
                continue
            for field_name in ("path", "format", "generated_by"):
                if not _present(output.get(field_name)):
                    output_errors.append(f"{key}.{field_name} is missing")
            generated_by = output.get("generated_by")
            if _present(generated_by) and str(generated_by) not in step_ids:
                output_errors.append(f"{key}.generated_by={generated_by!r} is not a step id")
            if _present(output.get("path")) and project_path(self.root, output.get("path")) is None:
                output_errors.append(f"{key}.path escapes the project directory")
        if graph_errors:
            self.add("processing.graph", "failed", "; ".join(graph_errors), path="processing.steps", errors=graph_errors)
        elif steps:
            self.add("processing.graph", "passed", f"{len(steps)} ordered steps and {len(produced)} symbols resolve", path="processing.steps")
        if output_errors:
            self.add("outputs.declaration", "failed", "; ".join(output_errors), path="outputs", errors=output_errors)
        elif outputs:
            self.add("outputs.declaration", "passed", f"{len(outputs)} outputs trace to real steps", path="outputs")

    def _presentation(self) -> None:
        presentation = self.project.get("presentation")
        if not isinstance(presentation, dict):
            self.add("presentation.declaration", "failed", "presentation must be a mapping", path="presentation")
            return
        missing = [key for key in ("intent", "primary_view", "layout", "map", "provenance_ui") if not _present(presentation.get(key))]
        layers = get_in(presentation, "map", "layers", default=[])
        groups = get_in(presentation, "map", "layer_groups", default=[])
        group_ids = {str(group.get("id")) for group in groups if isinstance(group, dict) and _present(group.get("id"))} if isinstance(groups, list) else set()
        layer_errors: list[str] = []
        if isinstance(layers, list):
            for index, layer in enumerate(layers):
                if not isinstance(layer, dict):
                    layer_errors.append(f"layer {index} is not a mapping")
                    continue
                if not _present(layer.get("source")) or not _present(layer.get("semantic_role")):
                    layer_errors.append(f"layer {index} needs source and semantic_role")
                if _present(layer.get("group")) and str(layer.get("group")) not in group_ids:
                    layer_errors.append(f"layer {index} references unknown group {layer.get('group')!r}")
        else:
            layer_errors.append("map.layers must be a list")
        if missing or layer_errors:
            self.add("presentation.declaration", "failed", "; ".join(([f"missing {missing}"] if missing else []) + layer_errors), path="presentation")
        else:
            self.add("presentation.declaration", "passed", f"semantic presentation declares {len(layers)} layers", path="presentation")

    def _runtime(self) -> None:
        implementation = get_in(self.project, "runtime", "implementation")
        if not isinstance(implementation, dict):
            self.add("runtime.pipeline", "failed", "runtime.implementation is required", path="runtime.implementation")
            return
        command = implementation.get("command")
        pipeline = implementation.get("pipeline")
        if command is None and not _present(pipeline):
            self.add("runtime.pipeline", "failed", "runtime.implementation.pipeline or command is required", path="runtime.implementation")
            return
        if _present(pipeline):
            target = project_path(self.root, pipeline)
            if target is None:
                self.add("runtime.pipeline", "failed", "pipeline path escapes the project directory", path="runtime.implementation.pipeline")
            elif not target.is_file():
                self.add("runtime.pipeline", "failed", f"pipeline does not exist: {pipeline}", path="runtime.implementation.pipeline")
            else:
                self.add("runtime.pipeline", "passed", f"canonical pipeline exists: {pipeline}", path="runtime.implementation.pipeline")
        else:
            self.add("runtime.pipeline", "passed", "explicit shell-free runtime command is declared", path="runtime.implementation.command")
        dependencies = implementation.get("dependencies")
        if dependencies is not None:
            dependency_errors: list[str] = []
            if not isinstance(dependencies, list):
                dependency_errors.append("dependencies must be a list")
                dependencies = []
            elif not dependencies:
                dependency_errors.append("dependencies must not be empty when declared")
            for index, dependency in enumerate(dependencies):
                target = project_path(self.root, dependency)
                if target is None:
                    dependency_errors.append(f"dependency {index} is not a safe project-relative path")
                elif not target.exists():
                    dependency_errors.append(f"dependency does not exist: {dependency}")
            if dependency_errors:
                self.add(
                    "runtime.dependencies",
                    "failed",
                    "; ".join(dependency_errors),
                    path="runtime.implementation.dependencies",
                    errors=dependency_errors,
                )
            else:
                self.add(
                    "runtime.dependencies",
                    "passed",
                    f"{len(dependencies)} clean-run dependencies resolve inside the project",
                    path="runtime.implementation.dependencies",
                )
        environment = get_in(self.project, "runtime", "environment")
        if not isinstance(environment, dict) or not environment:
            self.add("runtime.environment", "warning", "runtime.environment does not pin tool versions", path="runtime.environment")
        elif any(not _present(value) for value in environment.values()):
            self.add("runtime.environment", "warning", "runtime.environment contains unpinned values", path="runtime.environment")
        else:
            self.add("runtime.environment", "passed", f"{len(environment)} runtime versions are recorded", path="runtime.environment")

    def _warnings(self) -> None:
        warnings = self.project.get("warnings", [])
        if warnings is None:
            return
        if not isinstance(warnings, list):
            self.add("warnings.declaration", "failed", "warnings must be a list", path="warnings")
            return
        bad: list[str] = []
        ids: list[str] = []
        for index, warning in enumerate(warnings):
            if not isinstance(warning, dict):
                bad.append(str(index))
                continue
            warning_id = str(warning.get("id", index))
            ids.append(warning_id)
            if any(not _present(warning.get(key)) for key in ("id", "severity", "issue", "statement", "mitigation")):
                bad.append(warning_id)
        duplicates = _duplicates(ids)
        if bad or duplicates:
            self.add(
                "warnings.declaration",
                "failed",
                f"warnings missing required fields={bad}; duplicate ids={duplicates}",
                path="warnings",
            )
        elif warnings:
            self.add("warnings.declaration", "passed", f"{len(warnings)} unresolved warnings are documented", path="warnings")

    def _validation_declaration(self) -> None:
        validation = self.project.get("validation")
        if not isinstance(validation, dict):
            self.add("validation.declaration", "failed", "validation must be a mapping", path="validation")
            return
        required = validation.get("required")
        domains = validation.get("domain_checks", [])
        errors: list[str] = []
        if not isinstance(required, list) or not required or any(not isinstance(item, str) or not item.strip() for item in required):
            errors.append("required must be a non-empty list of flat ids")
            required = []
        if not isinstance(domains, list):
            errors.append("domain_checks must be a list")
            domains = []
        domain_names = [item.get("name") for item in domains if isinstance(item, dict)]
        if len(domain_names) != len(domains) or any(not _present(item) for item in domain_names):
            errors.append("each domain check needs a name")
        declared = [str(item) for item in required] + [str(item) for item in domain_names]
        duplicates = _duplicates(declared)
        if duplicates:
            errors.append(f"duplicate check ids: {duplicates}")
        if errors:
            self.add("validation.declaration", "failed", "; ".join(errors), path="validation")
        else:
            self.add("validation.declaration", "passed", f"{len(declared)} validation checks are declared", path="validation")

    def _artifact_files(self) -> None:
        outputs = self.project.get("outputs") or {}
        missing: list[str] = []
        declared_targets: set[Path] = set()
        for output in outputs.values() if isinstance(outputs, dict) else []:
            if not isinstance(output, dict):
                continue
            relative = output.get("path")
            target = project_path(self.root, relative)
            if target is not None:
                declared_targets.add(target)
                if not target.exists():
                    missing.append(str(relative))
        if missing:
            self.add("outputs.files", "failed", f"declared outputs do not exist: {missing}", path="outputs", missing=missing)
        elif outputs:
            self.add("outputs.files", "passed", f"all {len(outputs)} declared outputs exist", path="outputs")

        derived_dir = self.root / "data" / "derived"
        if derived_dir.is_dir():
            actual_derived = {
                path.resolve()
                for path in derived_dir.rglob("*")
                if path.is_file() and path.name != ".gitkeep"
            }
            undeclared = sorted(
                str(path.relative_to(self.root)) for path in actual_derived - declared_targets
            )
            if undeclared:
                self.add(
                    "outputs.undeclared_derived_files",
                    "warning",
                    f"derived files are not declared in outputs: {undeclared}",
                    path="data/derived",
                    undeclared=undeclared,
                )
            else:
                self.add(
                    "outputs.undeclared_derived_files",
                    "passed",
                    "every file under data/derived is a declared output",
                    path="data/derived",
                )

        if not (self.root / "README.md").is_file():
            self.add("project.readme", "warning", "README.md is missing", path="README.md")
        else:
            self.add("project.readme", "passed", "README.md exists", path="README.md")
        if get_in(self.project, "presentation", "primary_view") == "map":
            if not (self.root / "project.qgz").is_file():
                self.add("qgis.project", "warning", "map project has no project.qgz companion", path="project.qgz")
            else:
                self.add("qgis.project", "passed", "project.qgz exists", path="project.qgz")

    def _validation_report(self) -> None:
        relative = get_in(self.project, "runs", "latest", "validation_report", "path") or "validation/latest-report.json"
        target = project_path(self.root, relative)
        if target is None:
            self.add("validation.report", "failed", "validation report path escapes the project directory", path="runs.latest.validation_report.path")
            return
        self.report_path = target
        if not target.is_file():
            self.add("validation.report", "failed", f"validation report does not exist: {relative}", path=str(relative))
            return
        try:
            report = load_json(target)
        except ProjectError as exc:
            self.add("validation.report", "failed", str(exc), path=str(relative))
            return
        self.report = report
        checks = report.get("checks")
        if not isinstance(checks, list):
            self.add("validation.report", "failed", "report.checks must be a list", path=str(relative))
            return
        bad_status = [str(item.get("id", "?")) for item in checks if not isinstance(item, dict) or item.get("status") not in CHECK_STATUSES]
        ids = [str(item.get("id")) for item in checks if isinstance(item, dict) and _present(item.get("id"))]
        duplicate_ids = _duplicates(ids)
        expected = set(get_in(self.project, "validation", "required", default=[]) or [])
        expected.update(
            item.get("name")
            for item in (get_in(self.project, "validation", "domain_checks", default=[]) or [])
            if isinstance(item, dict) and _present(item.get("name"))
        )
        missing = sorted(str(item) for item in expected - set(ids))
        errors: list[str] = []
        if bad_status:
            errors.append(f"checks with invalid/missing status: {bad_status}")
        if duplicate_ids:
            errors.append(f"duplicate check ids: {duplicate_ids}")
        if missing:
            errors.append(f"declared checks missing from report: {missing}")
        actual_statuses = [item.get("status") for item in checks if isinstance(item, dict)]
        expected_status = _rollup_report_status(actual_statuses)
        report_status = report.get("status")
        if report_status != expected_status:
            errors.append(f"report.status={report_status!r}, expected {expected_status!r} from check statuses")
        project_status = get_in(self.project, "project", "status")
        expected_project_status = {"passed": "validated", "warning": "warning", "failed": "failed"}.get(report_status)
        if project_status in {"validated", "warning", "failed"} and project_status != expected_project_status:
            errors.append(f"project.status={project_status!r}, expected {expected_project_status!r} from report")
        if report_status == "failed" and not errors:
            errors.append("report contains failed checks")
        if errors:
            self.add("validation.report", "failed", "; ".join(errors), path=str(relative), errors=errors)
        else:
            status = "warning" if report_status == "warning" else "passed"
            self.add("validation.report", status, f"report has {len(checks)} explicit checks; status={report_status}", path=str(relative))
        self._override_application_results(report)

    def _override_application_results(self, report: dict[str, Any]) -> None:
        overrides = self.project.get("overrides") or []
        if not overrides:
            return
        declared = [str(item.get("id")) for item in overrides if isinstance(item, dict) and _present(item.get("id"))]
        checks = report.get("checks") or []
        override_check = next((item for item in checks if isinstance(item, dict) and item.get("id") == "overrides_applied"), None)
        results = (override_check or {}).get("results") or report.get("overrides") or []
        indexed: dict[str, list[dict[str, Any]]] = {}
        for result in results if isinstance(results, list) else []:
            if isinstance(result, dict) and _present(result.get("id")):
                indexed.setdefault(str(result.get("id")), []).append(result)
        missing = [oid for oid in declared if len(indexed.get(oid, [])) != 1]
        invalid = [
            oid
            for oid in declared
            if len(indexed.get(oid, [])) == 1 and indexed[oid][0].get("status") not in OVERRIDE_RESULTS
        ]
        rejected = [oid for oid in declared if indexed.get(oid) and indexed[oid][0].get("status") == "rejected"]
        not_testable = [oid for oid in declared if indexed.get(oid) and indexed[oid][0].get("status") == "not_testable"]
        if missing or invalid or rejected:
            self.add(
                "overrides.application",
                "failed",
                f"override application results invalid; missing/duplicate={missing}, invalid={invalid}, rejected={rejected}",
                path=str(self.report_path.relative_to(self.root)) if self.report_path else None,
            )
        elif not_testable:
            self.add("overrides.application", "warning", f"overrides not testable: {not_testable}", path="validation/latest-report.json")
        else:
            self.add("overrides.application", "passed", f"all {len(declared)} overrides have one applied result", path="validation/latest-report.json")

    def _run_record(self) -> None:
        latest = get_in(self.project, "runs", "latest")
        if not isinstance(latest, dict) or not _present(latest.get("id")):
            self.add("runs.latest", "failed", "runs.latest.id is required", path="runs.latest")
            return
        run_id = str(latest.get("id"))
        run_path = self.root / "runs" / f"{run_id}.json"
        if not run_path.is_file():
            self.add("runs.latest", "failed", f"run record does not exist: runs/{run_id}.json", path="runs.latest.id")
            return
        try:
            run = load_json(run_path)
        except ProjectError as exc:
            self.add("runs.latest", "failed", str(exc), path=str(run_path.relative_to(self.root)))
            return
        errors: list[str] = []
        if str(run.get("run_id")) != run_id:
            errors.append(f"run record id {run.get('run_id')!r} != manifest {run_id!r}")
        if self.report is not None and str(self.report.get("run_id")) != run_id:
            errors.append(f"report run_id {self.report.get('run_id')!r} != manifest {run_id!r}")
        report_status = self.report.get("status") if self.report else None
        if report_status and run.get("status") != report_status:
            errors.append(f"run status {run.get('status')!r} != report status {report_status!r}")
        if report_status and latest.get("status") != report_status:
            errors.append(f"manifest run status {latest.get('status')!r} != report status {report_status!r}")
        if run.get("status") not in RUN_STATUSES:
            errors.append(f"invalid run status {run.get('status')!r}")
        for timestamp in ("started_at", "completed_at"):
            if not _present(run.get(timestamp)):
                errors.append(f"run record {timestamp} is missing")
            if not _present(latest.get(timestamp)):
                errors.append(f"manifest runs.latest.{timestamp} is missing")
        environment = run.get("environment")
        if not isinstance(environment, dict) or not environment:
            errors.append("run record environment is missing")
        input_paths = self._verify_run_inventory(
            run.get("inputs"), "input", set(declared_input_paths(self.root, self.project)), errors
        )
        output_paths = self._verify_run_inventory(
            run.get("outputs"), "output", set(declared_output_paths(self.project)), errors
        )
        for hash_name, inventory_paths in (
            ("inputs_hash", input_paths),
            ("outputs_hash", output_paths),
        ):
            labelled_values = {
                "manifest": normalize_digest(latest.get(hash_name)),
                "report": normalize_digest(self.report.get(hash_name)) if self.report else None,
                "run": normalize_digest(run.get(hash_name)),
            }
            invalid_from = [
                label
                for label, value in labelled_values.items()
                if value is None
            ]
            if invalid_from:
                errors.append(f"{hash_name} is missing or invalid in {invalid_from}")
                continue
            if not inventory_paths:
                continue
            try:
                actual = canonical_file_set_hash(self.root, inventory_paths)
            except ValueError as exc:
                errors.append(f"cannot recompute {hash_name}: {exc}")
                continue
            wrong = [label for label, value in labelled_values.items() if value != actual]
            if wrong:
                errors.append(
                    f"{hash_name} does not match the real canonical file-set hash in {wrong}; "
                    f"actual={actual}"
                )
        if errors:
            self.add("runs.latest", "failed", "; ".join(errors), path=str(run_path.relative_to(self.root)), errors=errors)
        else:
            self.add("runs.latest", "passed", f"{run_id} resolves and matches report status/hashes", path=str(run_path.relative_to(self.root)))

    def _verify_run_inventory(
        self,
        inventory: object,
        kind: str,
        required_paths: set[str],
        errors: list[str],
    ) -> list[str]:
        if not isinstance(inventory, list) or not inventory:
            errors.append(f"run record {kind} inventory is missing")
            return []
        verified: list[str] = []
        seen: set[str] = set()
        for index, item in enumerate(inventory):
            if not isinstance(item, dict):
                errors.append(f"run {kind} {index} is not a mapping")
                continue
            relative = item.get("path")
            target = project_path(self.root, relative)
            expected_hash = normalize_digest(item.get("sha256"))
            if target is None:
                errors.append(f"run {kind} {index} has an unsafe path")
                continue
            normalized_path = target.relative_to(self.root).as_posix()
            if normalized_path in seen:
                errors.append(f"run {kind} inventory has duplicate path: {normalized_path}")
                continue
            seen.add(normalized_path)
            if not target.is_file():
                errors.append(f"run {kind} does not exist: {normalized_path}")
            elif expected_hash is None:
                errors.append(f"run {kind} has missing or invalid sha256: {normalized_path}")
            elif sha256_file(target) != expected_hash:
                errors.append(f"run {kind} hash mismatch: {normalized_path}")
            else:
                verified.append(normalized_path)
        omitted = sorted(required_paths - seen)
        if omitted:
            errors.append(
                f"declared {kind}s do not participate in run {kind} hashing: {omitted}"
            )
        return verified

    def _declared_status_consistency(self) -> None:
        project_status = get_in(self.project, "project", "status")
        non_passed = [check for check in self.checks if check.status in {"warning", "not_testable"}]
        if project_status == "validated" and non_passed:
            self.add(
                "project.status_consistency",
                "failed",
                "project.status is validated but the artifact has warnings or not-testable checks",
                path="project.status",
                checks=[check.id for check in non_passed],
            )
        elif self.artifacts and project_status in {"draft", "in_progress"}:
            self.add(
                "project.status_consistency",
                "warning",
                f"project.status is {project_status!r}; the artifact is not finalized",
                path="project.status",
            )


def validate_project(value: str | Path, *, artifacts: bool = True) -> ValidationResult:
    """Validate a project manifest and, by default, its generated artifacts."""
    try:
        project_file, project = load_project(value)
    except ProjectError as exc:
        project_file = Path(value).expanduser().resolve()
        return ValidationResult(project_file, [Check("manifest.parse", "failed", str(exc))])
    return _Validator(project_file, project, artifacts=artifacts).run()


def _present(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        normalized = value.strip().lower()
        return normalized not in PLACEHOLDERS and not normalized.startswith("todo-") and not normalized.startswith("todo ")
    if isinstance(value, (list, dict, tuple, set)):
        return bool(value)
    if isinstance(value, (date, datetime)):
        return True
    return True


def _unknown(value: object) -> bool:
    text = str(value).strip().lower()
    return any(marker in text for marker in ("unknown", "not stated", "unresolved", "tbd", "todo"))


def _placeholder_evidence(value: object) -> bool:
    if isinstance(value, dict):
        candidate = value.get("value") or value.get("source") or value.get("title")
    else:
        candidate = value
    return not _present(candidate)


def _duplicates(values: Iterable[str]) -> list[str]:
    counts = Counter(values)
    return sorted(value for value, count in counts.items() if count > 1)


def _rollup_report_status(statuses: Iterable[object]) -> str:
    values = set(statuses)
    if "failed" in values:
        return "failed"
    if values & {"warning", "not_testable"}:
        return "warning"
    return "passed"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"
