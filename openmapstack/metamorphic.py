"""Conditional metamorphic relations for projects with no golden answer.

A metamorphic relation asks: if I perturb an input or a parameter in a
controlled way, does the output change the way the analysis semantics say it
must? It needs no frozen answer, which is what makes it usable on a user's
own data -- and it is *conditional*: each relation holds only under declared
preconditions, and a relation that cannot be addressed safely reports
``not_testable`` with the reason, never a pass.

Relations are declared in ``validation.metamorphic[]``:

.. code-block:: yaml

    validation:
      metamorphic:
        - id: parcel-order
          relation: input_permutation_invariance
          source: {path: data/source/parcels.geojson}
          outputs: [candidate_parcels]
          key: cadastral_id
          preconditions:
            tie_break: "candidates are keyed by cadastral_id; no order-dependent selection"
        - id: parcel-duplicates
          relation: duplicate_resistance
          source: {path: data/source/parcels.geojson}
          outputs: [candidate_parcels]
          key: cadastral_id
          preconditions: {dedup_key: cadastral_id, measure: set}
        - id: road-distance-monotonic
          relation: positive_buffer_monotonicity
          parameter: road_distance_m
          variant: {multiply: 1.5}
          outputs: [candidate_parcels]
          key: cadastral_id
          preconditions: {predicate: within_distance, expected: superset}

Every relation runs the canonical entrypoint in an isolated copy prepared
exactly like a clean rerun (``openmapstack.rerun.prepare_clean_workspace``),
perturbs only that copy, compares the copy's outputs with the project's
produced outputs, and removes the copy. The project's own ``data/source`` and
``data/overrides`` are hashed before and after; a variant that reaches back
and mutates them fails outright.

Implemented relations and their machine-checked preconditions:

``input_permutation_invariance``
    Source features are shuffled (deterministically, from ``seed``). Every
    listed output must be semantically equal. Precondition: a declared
    ``tie_break`` rule and a ``key`` that is unique in the baseline output.
``duplicate_resistance``
    Every source feature is appended once more. Outputs must be equal.
    Precondition: ``dedup_key`` is unique in the source, and ``measure`` is
    ``set``. This relation is invalid for counts and sums, which legitimately
    change when rows are duplicated; declaring another measure is rejected.
``positive_buffer_monotonicity``
    A declared numeric parameter is increased through its binding. Every
    baseline output key must survive in the variant (``superset``).
    Precondition: the parameter is declared, positive, and the variant is a
    strict increase; the ``predicate`` names an inclusion predicate.

Candidate relations that are *not* implemented here (CRS round trip, subset
additivity, area-scale consistency) must not be declared; the verifier
rejects unknown relation names rather than silently skipping them.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import shutil
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .checks import AssertionResult, failed, not_testable, passed
from .checks.rerun import _semantic_snapshot
from .checks.spatial import connect_spatial
from .parameters import ParameterError, declared_parameters
from .project import get_in, project_path
from .rerun import execute_canonical, prepare_clean_workspace

METAMORPHIC_SCHEMA = "openmapstack-metamorphic/v1"
RELATIONS = (
    "input_permutation_invariance",
    "duplicate_resistance",
    "positive_buffer_monotonicity",
)
INCLUSION_PREDICATES = ("within_distance", "intersects_buffer", "within_buffer")
DEFAULT_TIMEOUT_S = 600.0
DEFAULT_MAX_SOURCE_BYTES = 64 * 1024 * 1024
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DECLARATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_TRANSFORMABLE_SUFFIXES = {".geojson", ".json", ".parquet"}
_READABLE_OUTPUT_SUFFIXES = {".geojson", ".json", ".parquet"}


class DeclarationError(ValueError):
    """A metamorphic declaration is structurally invalid."""


@dataclass
class Declaration:
    id: str
    relation: str
    outputs: list[str]
    key: str
    source_path: str | None = None
    parameter: str | None = None
    variant: dict[str, Any] = field(default_factory=dict)
    preconditions: dict[str, Any] = field(default_factory=dict)
    tolerance: dict[str, Any] = field(default_factory=dict)
    timeout_s: float = DEFAULT_TIMEOUT_S
    max_source_bytes: int = DEFAULT_MAX_SOURCE_BYTES
    seed: int = 7


def parse_declaration(raw: object) -> Declaration:
    """Validate one ``validation.metamorphic[]`` entry structurally."""
    if not isinstance(raw, dict):
        raise DeclarationError("declaration must be a mapping")
    errors: list[str] = []
    declaration_id = raw.get("id")
    if not isinstance(declaration_id, str) or _DECLARATION_ID.fullmatch(declaration_id) is None:
        raise DeclarationError("declaration id is required")
    relation = raw.get("relation")
    if relation not in RELATIONS:
        raise DeclarationError(f"{declaration_id}: relation must be one of {list(RELATIONS)}, got {relation!r}")
    allowed = {
        "id", "relation", "outputs", "key", "source", "parameter", "variant",
        "preconditions", "tolerance", "limits", "seed", "description",
    }
    unknown = set(raw) - allowed
    if unknown:
        errors.append(f"unknown keys {sorted(unknown)}")
    outputs = raw.get("outputs")
    if not isinstance(outputs, list) or not outputs or any(not isinstance(item, str) or not item for item in outputs):
        errors.append("outputs must be a non-empty list of output keys")
        outputs = []
    key = raw.get("key")
    if not isinstance(key, str) or _IDENTIFIER.fullmatch(key) is None:
        errors.append("key must be a simple field identifier")
        key = ""
    preconditions = raw.get("preconditions") or {}
    if not isinstance(preconditions, dict):
        errors.append("preconditions must be a mapping")
        preconditions = {}
    tolerance = raw.get("tolerance") or {}
    if not isinstance(tolerance, dict):
        errors.append("tolerance must be a mapping")
        tolerance = {}
    limits = raw.get("limits") or {}
    if not isinstance(limits, dict):
        errors.append("limits must be a mapping")
        limits = {}
    timeout_s = limits.get("timeout_s", DEFAULT_TIMEOUT_S)
    max_source_bytes = limits.get("max_source_bytes", DEFAULT_MAX_SOURCE_BYTES)
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or timeout_s <= 0:
        errors.append("limits.timeout_s must be a positive number")
        timeout_s = DEFAULT_TIMEOUT_S
    if isinstance(max_source_bytes, bool) or not isinstance(max_source_bytes, int) or max_source_bytes <= 0:
        errors.append("limits.max_source_bytes must be a positive integer")
        max_source_bytes = DEFAULT_MAX_SOURCE_BYTES
    seed = raw.get("seed", 7)
    if isinstance(seed, bool) or not isinstance(seed, int):
        errors.append("seed must be an integer")
        seed = 7

    source_path: str | None = None
    parameter: str | None = None
    variant: dict[str, Any] = {}
    if relation in {"input_permutation_invariance", "duplicate_resistance"}:
        source = raw.get("source")
        source_path = source.get("path") if isinstance(source, dict) else None
        if not isinstance(source_path, str) or not source_path.strip():
            errors.append("source.path is required for this relation")
            source_path = None
        elif not (source_path.startswith("data/source/") or source_path.startswith("data/overrides/")):
            errors.append("source.path must name a file under data/source/ or data/overrides/")
        if "parameter" in raw or "variant" in raw:
            errors.append("parameter/variant do not apply to this relation")
    if relation == "input_permutation_invariance":
        tie_break = preconditions.get("tie_break")
        if not isinstance(tie_break, str) or not tie_break.strip():
            errors.append("preconditions.tie_break must state the deterministic ordering rule")
    if relation == "duplicate_resistance":
        dedup_key = preconditions.get("dedup_key")
        if not isinstance(dedup_key, str) or _IDENTIFIER.fullmatch(dedup_key) is None:
            errors.append("preconditions.dedup_key must be a simple field identifier")
        measure = preconditions.get("measure", "set")
        if measure != "set":
            errors.append(
                f"duplicate_resistance is valid only for set semantics; measure {measure!r} "
                "(counts and sums change legitimately when rows are duplicated)"
            )
    if relation == "positive_buffer_monotonicity":
        parameter = raw.get("parameter")
        if not isinstance(parameter, str) or _IDENTIFIER.fullmatch(parameter) is None:
            errors.append("parameter must name a declared runtime parameter")
            parameter = None
        variant = raw.get("variant") or {}
        if not isinstance(variant, dict) or len(variant) != 1 or not ({"multiply", "add"} & set(variant)):
            errors.append("variant must declare exactly one of multiply/add")
            variant = {}
        else:
            operation, amount = next(iter(variant.items()))
            if isinstance(amount, bool) or not isinstance(amount, (int, float)):
                errors.append(f"variant.{operation} must be a number")
                variant = {}
            elif operation == "multiply" and amount <= 1:
                errors.append("variant.multiply must be > 1 so the buffer strictly grows")
                variant = {}
            elif operation == "add" and amount <= 0:
                errors.append("variant.add must be > 0 so the buffer strictly grows")
                variant = {}
        predicate = preconditions.get("predicate")
        if predicate not in INCLUSION_PREDICATES:
            errors.append(f"preconditions.predicate must be one of {list(INCLUSION_PREDICATES)}")
        if preconditions.get("expected", "superset") != "superset":
            errors.append("positive_buffer_monotonicity establishes only expected: superset")
        if "source" in raw:
            errors.append("source does not apply to a parameter relation")
    if errors:
        raise DeclarationError(f"{declaration_id}: " + "; ".join(errors))
    return Declaration(
        id=declaration_id,
        relation=relation,
        outputs=list(outputs),
        key=key,
        source_path=source_path,
        parameter=parameter,
        variant=dict(variant),
        preconditions=dict(preconditions),
        tolerance=dict(tolerance),
        timeout_s=float(timeout_s),
        max_source_bytes=int(max_source_bytes),
        seed=int(seed),
    )


def declared_relations(manifest: dict[str, Any]) -> list[object]:
    raw = get_in(manifest, "validation", "metamorphic")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise DeclarationError("validation.metamorphic must be a list")
    return list(raw)


# --- data access ------------------------------------------------------------


def _read_features(path: Path) -> tuple[dict[str, Any], list[Any]]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("type") != "FeatureCollection":
        raise ValueError(f"{path.name} is not a GeoJSON FeatureCollection")
    features = document.get("features")
    if not isinstance(features, list):
        raise ValueError(f"{path.name} has no features list")
    return document, features


def _write_features(path: Path, document: dict[str, Any], features: list[Any]) -> None:
    replaced = dict(document)
    replaced["features"] = features
    path.write_text(json.dumps(replaced, ensure_ascii=False), encoding="utf-8")


def _duckdb_or_none():
    return connect_spatial()


def _key_values(path: Path, key: str) -> list[Any]:
    """Every value of ``key`` in an output artifact, in file order."""
    suffix = path.suffix.lower()
    if suffix in {".geojson", ".json"}:
        _, features = _read_features(path)
        values = []
        for feature in features:
            properties = feature.get("properties") if isinstance(feature, dict) else None
            if not isinstance(properties, dict) or key not in properties:
                raise KeyError(key)
            values.append(properties[key])
        return values
    if suffix == ".parquet":
        connection = _duckdb_or_none()
        if connection is None:
            raise RuntimeError("duckdb_unavailable")
        try:
            escaped = path.as_posix().replace("'", "''")
            columns = {row[0] for row in connection.execute(f"DESCRIBE SELECT * FROM read_parquet('{escaped}')").fetchall()}
            if key not in columns:
                raise KeyError(key)
            rows = connection.execute(f'SELECT "{key}" FROM read_parquet(\'{escaped}\')').fetchall()
        finally:
            connection.close()
        return [row[0] for row in rows]
    raise ValueError("unsupported_format")


def _source_key_values(path: Path, key: str) -> list[Any]:
    return _key_values(path, key)


def _hash_tree(root: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for directory in ("data/source", "data/overrides"):
        base = root / directory
        if not base.is_dir():
            continue
        for item in sorted(base.rglob("*")):
            if item.is_file():
                hashes[item.relative_to(root).as_posix()] = hashlib.sha256(item.read_bytes()).hexdigest()
    return hashes


# --- transformations ----------------------------------------------------------


def _permute_source(path: Path, seed: int) -> dict[str, Any]:
    suffix = path.suffix.lower()
    if suffix in {".geojson", ".json"}:
        document, features = _read_features(path)
        order = list(range(len(features)))
        random.Random(seed).shuffle(order)
        if len(features) > 1 and order == list(range(len(features))):
            order.reverse()
        _write_features(path, document, [features[index] for index in order])
        return {"transformation": "permute_features", "features": len(features), "seed": seed}
    if suffix == ".parquet":
        connection = _duckdb_or_none()
        if connection is None:
            raise RuntimeError("duckdb_unavailable")
        try:
            escaped = path.as_posix().replace("'", "''")
            count = connection.execute(f"SELECT COUNT(*) FROM read_parquet('{escaped}')").fetchone()[0]
            connection.execute(f"SELECT setseed({(seed % 1000) / 1000.0})")
            temp = path.with_suffix(".permuted.tmp.parquet")
            connection.execute(
                f"COPY (SELECT * FROM read_parquet('{escaped}') ORDER BY random()) "
                f"TO '{temp.as_posix().replace(chr(39), chr(39) * 2)}' (FORMAT PARQUET)"
            )
        finally:
            connection.close()
        temp.replace(path)
        return {"transformation": "permute_rows", "rows": count, "seed": seed}
    raise ValueError("unsupported_format")


def _duplicate_source(path: Path) -> dict[str, Any]:
    suffix = path.suffix.lower()
    if suffix in {".geojson", ".json"}:
        document, features = _read_features(path)
        _write_features(path, document, features + [json.loads(json.dumps(item)) for item in features])
        return {"transformation": "duplicate_features", "features": len(features), "duplicated": len(features)}
    if suffix == ".parquet":
        connection = _duckdb_or_none()
        if connection is None:
            raise RuntimeError("duckdb_unavailable")
        try:
            escaped = path.as_posix().replace("'", "''")
            count = connection.execute(f"SELECT COUNT(*) FROM read_parquet('{escaped}')").fetchone()[0]
            temp = path.with_suffix(".duplicated.tmp.parquet")
            connection.execute(
                f"COPY (SELECT * FROM read_parquet('{escaped}') UNION ALL SELECT * FROM read_parquet('{escaped}')) "
                f"TO '{temp.as_posix().replace(chr(39), chr(39) * 2)}' (FORMAT PARQUET)"
            )
        finally:
            connection.close()
        temp.replace(path)
        return {"transformation": "duplicate_rows", "rows": count, "duplicated": count}
    raise ValueError("unsupported_format")


# --- execution ----------------------------------------------------------------


def _declared_output_files(manifest: dict[str, Any], keys: Sequence[str]) -> dict[str, str]:
    outputs = manifest.get("outputs") if isinstance(manifest.get("outputs"), dict) else {}
    resolved: dict[str, str] = {}
    for key in keys:
        spec = outputs.get(key)
        path = spec.get("path") if isinstance(spec, dict) else None
        if not isinstance(path, str) or not path.strip():
            raise DeclarationError(f"outputs[{key!r}] is not a declared output with a path")
        resolved[key] = path
    return resolved


def run_relation(
    project_root: Path,
    manifest: dict[str, Any],
    raw_declaration: object,
    *,
    forbidden_fragments: Sequence[str] = (),
) -> tuple[AssertionResult, dict[str, Any]]:
    """Execute one declared relation and return (result, evidence).

    The result vocabulary:

    - ``failed``: the relation was executed and does not hold, or the
      declaration is invalid, or the variant reached back into the project's
      immutable inputs;
    - ``not_testable``: a precondition does not hold on this data or the
      environment cannot run the relation (unsupported format, DuckDB absent,
      timeout, oversize source);
    - ``passed``: the variant ran and the declared relation holds.
    """
    evidence: dict[str, Any] = {"schema": METAMORPHIC_SCHEMA}
    try:
        declaration = parse_declaration(raw_declaration)
    except DeclarationError as exc:
        evidence["class"] = "invalid"
        return failed(str(exc), code="metamorphic_declaration_invalid"), evidence
    evidence.update({"id": declaration.id, "relation": declaration.relation})
    root = project_root.resolve()

    try:
        output_files = _declared_output_files(manifest, declaration.outputs)
    except DeclarationError as exc:
        evidence["class"] = "invalid"
        return failed(f"{declaration.id}: {exc}", code="metamorphic_declaration_invalid"), evidence
    for output_key, relative in output_files.items():
        target = project_path(root, relative)
        if target is None:
            evidence["class"] = "invalid"
            return failed(f"{declaration.id}: output {output_key!r} path is unsafe", code="metamorphic_declaration_invalid"), evidence
        if not target.is_file():
            return not_testable(
                f"{declaration.id}: baseline output {relative} does not exist; run the pipeline first",
                code="baseline_missing",
            ), evidence
        if target.suffix.lower() not in _READABLE_OUTPUT_SUFFIXES:
            return not_testable(
                f"{declaration.id}: output {relative} is {target.suffix or 'extensionless'}, which the relation cannot compare",
                code="unsupported_format",
            ), evidence

    # Baseline keys: every listed output must expose a unique key.
    baseline_keys: dict[str, list[Any]] = {}
    for output_key, relative in output_files.items():
        try:
            values = _key_values(root / relative, declaration.key)
        except KeyError:
            return not_testable(
                f"{declaration.id}: output {relative} has no field {declaration.key!r}",
                code="precondition_unmet",
            ), evidence
        except RuntimeError as exc:
            return not_testable(f"{declaration.id}: {exc}", code="duckdb_unavailable"), evidence
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            return not_testable(f"{declaration.id}: cannot read {relative}: {exc}", code="unsupported_format"), evidence
        if len(set(map(_hashable, values))) != len(values):
            return not_testable(
                f"{declaration.id}: key {declaration.key!r} is not unique in {relative}, so set semantics cannot be asserted",
                code="precondition_unmet",
            ), evidence
        baseline_keys[output_key] = values

    # Relation-specific preconditions and the variant plan.
    extra_argv: list[str] = []
    extra_env: dict[str, str] = {}
    source_relative: str | None = None
    if declaration.relation in {"input_permutation_invariance", "duplicate_resistance"}:
        assert declaration.source_path is not None
        source_relative = declaration.source_path
        source_target = project_path(root, source_relative)
        if source_target is None or not source_target.is_file():
            return not_testable(f"{declaration.id}: source {source_relative} does not exist", code="precondition_unmet"), evidence
        if source_target.suffix.lower() not in _TRANSFORMABLE_SUFFIXES:
            return not_testable(
                f"{declaration.id}: source {source_relative} is {source_target.suffix or 'extensionless'}, which cannot be transformed",
                code="unsupported_format",
            ), evidence
        size = source_target.stat().st_size
        if size > declaration.max_source_bytes:
            return not_testable(
                f"{declaration.id}: source {source_relative} is {size} bytes, above limits.max_source_bytes={declaration.max_source_bytes}",
                code="resource_limit",
            ), evidence
        if declaration.relation == "duplicate_resistance":
            dedup_key = declaration.preconditions["dedup_key"]
            try:
                source_values = _source_key_values(source_target, dedup_key)
            except KeyError:
                return not_testable(
                    f"{declaration.id}: source {source_relative} has no field {dedup_key!r}",
                    code="precondition_unmet",
                ), evidence
            except RuntimeError as exc:
                return not_testable(f"{declaration.id}: {exc}", code="duckdb_unavailable"), evidence
            except (ValueError, OSError, json.JSONDecodeError) as exc:
                return not_testable(f"{declaration.id}: cannot read {source_relative}: {exc}", code="unsupported_format"), evidence
            if len(set(map(_hashable, source_values))) != len(source_values):
                return not_testable(
                    f"{declaration.id}: source {source_relative} already has duplicate {dedup_key!r} values; "
                    "the analysis cannot be deduplicating on that key",
                    code="precondition_unmet",
                ), evidence
            evidence["dedup_key"] = dedup_key
    else:
        assert declaration.parameter is not None
        try:
            parameters = {parameter.id: parameter for parameter in declared_parameters(manifest)}
        except ParameterError as exc:
            return failed(f"{declaration.id}: {exc}", code="parameters_invalid"), evidence
        parameter = parameters.get(declaration.parameter)
        if parameter is None:
            return failed(
                f"{declaration.id}: parameter {declaration.parameter!r} is not declared under runtime.implementation.parameters",
                code="metamorphic_declaration_invalid",
            ), evidence
        if parameter.type not in {"integer", "number"}:
            return not_testable(
                f"{declaration.id}: parameter {parameter.id!r} is {parameter.type}, not numeric",
                code="precondition_unmet",
            ), evidence
        if parameter.canonical <= 0:
            return not_testable(
                f"{declaration.id}: parameter {parameter.id!r} canonical value {parameter.canonical!r} is not positive",
                code="precondition_unmet",
            ), evidence
        operation, amount = next(iter(declaration.variant.items()))
        variant_value = parameter.canonical * amount if operation == "multiply" else parameter.canonical + amount
        if parameter.type == "integer":
            variant_value = int(round(variant_value))
            if variant_value <= parameter.canonical:
                return not_testable(
                    f"{declaration.id}: variant does not strictly increase the integer parameter",
                    code="precondition_unmet",
                ), evidence
        extra_argv, extra_env = parameter.bind(variant_value)
        evidence["parameter"] = {"id": parameter.id, "canonical": parameter.canonical, "variant": variant_value}

    # Run the variant in an isolated copy; the project itself is read-only.
    original_hashes = _hash_tree(root)
    variant_root = Path(tempfile.mkdtemp(prefix=f"openmapstack-metamorphic-{declaration.id}-"))
    started = time.monotonic()
    try:
        try:
            command, preserved, _ = prepare_clean_workspace(root, variant_root, forbidden_fragments=forbidden_fragments)
        except ValueError as exc:
            return failed(f"{declaration.id}: cannot prepare variant workspace: {exc}", code="variant_preparation_failed"), evidence
        evidence["preserved_paths"] = sorted(preserved)
        if source_relative is not None:
            variant_source = variant_root / source_relative
            try:
                if declaration.relation == "input_permutation_invariance":
                    evidence["variant"] = _permute_source(variant_source, declaration.seed)
                else:
                    evidence["variant"] = _duplicate_source(variant_source)
            except RuntimeError as exc:
                return not_testable(f"{declaration.id}: {exc}", code="duckdb_unavailable"), evidence
            except (ValueError, OSError, json.JSONDecodeError) as exc:
                return not_testable(f"{declaration.id}: cannot transform {source_relative}: {exc}", code="unsupported_format"), evidence
        else:
            evidence["variant"] = {"transformation": "parameter", "argv": extra_argv, "environment": sorted(extra_env)}

        execution, removed = execute_canonical(
            command, variant_root, declaration.timeout_s, extra_argv=extra_argv, extra_env=extra_env
        )
        evidence["command"] = [*command, *extra_argv]
        evidence["removed_environment_keys"] = removed
        evidence["duration_s"] = time.monotonic() - started
        if execution.get("timed_out"):
            return not_testable(
                f"{declaration.id}: variant run exceeded limits.timeout_s={declaration.timeout_s}",
                code="variant_timeout",
            ), evidence
        if execution.get("returncode") != 0:
            evidence["stderr_tail"] = (execution.get("stderr") or "")[-2000:]
            return failed(
                f"{declaration.id}: variant run exited with status {execution.get('returncode')}",
                code="variant_execution_failed",
            ), evidence

        if _hash_tree(root) != original_hashes:
            return failed(
                f"{declaration.id}: the variant run mutated the project's declared-immutable inputs",
                code="original_source_mutated",
            ), evidence

        # Compare.
        differences: list[str] = []
        for output_key, relative in output_files.items():
            baseline_file = root / relative
            variant_file = variant_root / relative
            if not variant_file.is_file():
                return failed(
                    f"{declaration.id}: variant run did not produce output {relative}",
                    code="variant_output_missing",
                ), evidence
            if declaration.relation == "positive_buffer_monotonicity":
                try:
                    variant_keys = set(map(_hashable, _key_values(variant_file, declaration.key)))
                except RuntimeError as exc:
                    return not_testable(f"{declaration.id}: {exc}", code="duckdb_unavailable"), evidence
                except (KeyError, ValueError, OSError, json.JSONDecodeError) as exc:
                    return failed(
                        f"{declaration.id}: variant output {relative} lost field {declaration.key!r}: {exc}",
                        code="monotonicity_violated",
                    ), evidence
                lost = sorted(str(value) for value in set(map(_hashable, baseline_keys[output_key])) - variant_keys)
                evidence.setdefault("counts", {})[output_key] = {
                    "baseline": len(baseline_keys[output_key]),
                    "variant": len(variant_keys),
                }
                if lost:
                    differences.append(f"{relative} lost {len(lost)} baseline feature(s) when the buffer grew: {lost[:10]}")
            else:
                ignored = set(declaration.tolerance.get("ignored_fields") or [])
                try:
                    equal = _snapshot(baseline_file, ignored, declaration.tolerance) == _snapshot(
                        variant_file, ignored, declaration.tolerance
                    )
                except RuntimeError as exc:
                    return not_testable(f"{declaration.id}: {exc}", code="duckdb_unavailable"), evidence
                except Exception as exc:  # noqa: BLE001 - normalization is best effort
                    return not_testable(f"{declaration.id}: cannot normalize {relative}: {exc}", code="normalize_error"), evidence
                if not equal:
                    differences.append(f"{relative} changed")
        if differences:
            code = {
                "input_permutation_invariance": "permutation_changed_output",
                "duplicate_resistance": "duplicates_changed_output",
                "positive_buffer_monotonicity": "monotonicity_violated",
            }[declaration.relation]
            return failed(f"{declaration.id}: {'; '.join(differences)}", code=code), evidence
        return passed(
            f"{declaration.id}: {declaration.relation} holds across {len(output_files)} output(s)"
        ), evidence
    finally:
        shutil.rmtree(variant_root, ignore_errors=True)


def _snapshot(path: Path, ignored: set[str], tolerance: dict[str, Any]) -> Any:
    snapshot = _semantic_snapshot(path, ignored)
    digits = tolerance.get("round_numbers")
    if isinstance(digits, int) and not isinstance(digits, bool):
        snapshot = _round(snapshot, digits)
    return snapshot


def _round(value: Any, digits: int) -> Any:
    if isinstance(value, float):
        return round(value, digits)
    if isinstance(value, list):
        return [_round(item, digits) for item in value]
    if isinstance(value, dict):
        return {key: _round(item, digits) for key, item in value.items()}
    return value


def _hashable(value: Any) -> Any:
    if isinstance(value, (list, dict)):
        return json.dumps(value, sort_keys=True, default=str)
    return value
