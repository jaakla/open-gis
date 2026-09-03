"""Sampled ("nail it before you scale it") runs, and why they can never be canonical.

A wide-area, high-resolution analysis can run for hours before a late step
fails. A sampled run executes the *same* pipeline over a deliberately smaller
slice so that failure arrives in minutes instead.

What a sampled run proves and does not prove
--------------------------------------------
It proves the pipeline *executes*: the manifest graph resolves, the sources are
reachable, CRS handling holds, schemas line up, pagination works within the
slice. It does **not** prove the numbers. Clipping to a test AOI breaks every
neighbourhood operation at the cut, row sampling destroys the spatial coherence
a join depends on, and raster downsampling changes areas and slopes
non-linearly. So a sampled run's outputs are never an answer.

How that is enforced
--------------------
Structurally, not by convention:

- a sampled run legitimately produces a different ``inputs_hash`` -- clipped
  inputs are different bytes -- so it cannot share the canonical hash chain;
- its run record carries ``mode: sampled`` plus the **realized** sample, not
  merely the requested one (``TABLESAMPLE`` and friends only approximate);
- ``runs.latest`` may never point at a sampled record, so ``verify``, the
  clean-rerun protocol, and ``validation.expectations`` attestations -- which
  bind to ``runs.latest.inputs_hash`` -- cannot inherit a sampled baseline;
- ``openmapstack run --sample*`` re-reads the manifest afterwards and fails
  loudly if the pipeline promoted itself into ``runs.latest``.

If a sampled run overwrites the declared outputs in place, the existing
``outputs_hash`` machinery already refuses to call the project validated: the
files no longer hash to what the canonical run recorded. The CLI reports that
clobber explicitly so the later failure is not a surprise.

Choosing a *representative* sample is judgment, not arithmetic -- a naive
sub-bbox of a global dataset lands in open ocean -- so this module never picks
one. It binds the value the manifest or the operator supplied.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .parameters import SAMPLE_ROLES, Parameter, ParameterError, declared_parameters, value_has_type

#: Run kinds a run record may declare. Absent means ``canonical``.
RUN_MODES = ("canonical", "sampled")

#: Sentinel for "use the value the manifest declared for this role".
USE_DECLARED = object()

_ROLE_FLAGS = {
    "sample_area": "--sample-area",
    "sample_rows": "--sample-rows",
    "sample_fraction": "--sample-fraction",
}


class SamplingError(ValueError):
    """A sampled run was asked for that the manifest cannot express."""


@dataclass(frozen=True)
class Sample:
    """A resolved sampled run: what to pass, and what was asked for."""

    argv: list[str] = field(default_factory=list)
    environment: dict[str, str] = field(default_factory=dict)
    requested: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"requested": dict(self.requested), "argv": list(self.argv), "environment": dict(self.environment)}


def sampling_parameters(manifest: dict[str, Any]) -> dict[str, Parameter]:
    """The declared sampling knobs, keyed by role.

    Raises ``ParameterError`` if the parameter block is malformed; role
    uniqueness is enforced there, so the mapping is unambiguous.
    """
    return {
        parameter.role: parameter
        for parameter in declared_parameters(manifest)
        if parameter.role is not None
    }


def resolve_sample(manifest: dict[str, Any], requested: Mapping[str, Any]) -> Sample:
    """Bind ``requested`` (role -> value or ``USE_DECLARED``) to pipeline arguments.

    Fails rather than guessing: an undeclared role, or a bare ``--sample``
    against a manifest that declares no default, is an error naming exactly
    what the manifest is missing.
    """
    try:
        available = sampling_parameters(manifest)
    except ParameterError as exc:
        raise SamplingError(str(exc)) from exc

    unknown = sorted(role for role in requested if role not in SAMPLE_ROLES)
    if unknown:
        raise SamplingError(f"unknown sampling role(s): {unknown}")
    if not requested:
        raise SamplingError("no sampling was requested")

    argv: list[str] = []
    environment: dict[str, str] = {}
    resolved: dict[str, Any] = {}
    for role in SAMPLE_ROLES:
        if role not in requested:
            continue
        parameter = available.get(role)
        if parameter is None:
            raise SamplingError(
                f"{_ROLE_FLAGS[role]} needs a runtime.implementation.parameters entry with "
                f"role: {role}; the manifest declares "
                f"{sorted(available) or 'no sampling parameters'}"
            )
        value = requested[role]
        if value is USE_DECLARED:
            if parameter.sample is None:
                raise SamplingError(
                    f"parameter {parameter.id!r} declares role {role!r} but no sample value; "
                    f"pass {_ROLE_FLAGS[role]} explicitly or add `sample:` to the manifest"
                )
            value = parameter.sample
        elif not value_has_type(value, parameter.type):
            raise SamplingError(f"{_ROLE_FLAGS[role]} must be a {parameter.type} for parameter {parameter.id!r}")
        extra_argv, extra_environment = parameter.bind(value)
        argv.extend(extra_argv)
        environment.update(extra_environment)
        resolved[role] = value
    return Sample(argv=argv, environment=environment, requested=resolved)


def declared_sample(manifest: dict[str, Any]) -> dict[str, Any]:
    """The roles bare ``--sample`` would bind, i.e. those with a ``sample`` value."""
    try:
        available = sampling_parameters(manifest)
    except ParameterError as exc:
        raise SamplingError(str(exc)) from exc
    return {role: USE_DECLARED for role, parameter in available.items() if parameter.sample is not None}


def run_mode(run_record: Mapping[str, Any]) -> str:
    """The run kind a record declares. A record with no ``mode`` is canonical."""
    mode = run_record.get("mode")
    if mode is None:
        return "canonical"
    return str(mode)


def run_record_errors(run_record: Mapping[str, Any]) -> list[str]:
    """Everything wrong with a run record's sampling declaration.

    A canonical record must not carry a sample descriptor, and a sampled one
    must record what it *realized* -- the actual rows, AOI, or resolution --
    because a requested fraction is a request, not a measurement.
    """
    mode = run_mode(run_record)
    errors: list[str] = []
    if mode not in RUN_MODES:
        return [f"invalid run mode {mode!r}, expected one of {list(RUN_MODES)}"]
    sample = run_record.get("sample")
    if mode == "canonical":
        if sample is not None:
            errors.append("a canonical run record must not carry a sample descriptor")
        return errors
    if not isinstance(sample, dict):
        errors.append("a sampled run record must carry a sample object")
        return errors
    requested = sample.get("requested")
    if not isinstance(requested, dict) or not requested:
        errors.append("sample.requested must be a non-empty object")
    else:
        unknown = sorted(role for role in requested if role not in SAMPLE_ROLES)
        if unknown:
            errors.append(f"sample.requested has unknown role(s): {unknown}")
    realized = sample.get("realized")
    if not isinstance(realized, dict) or not realized:
        errors.append(
            "sample.realized must be a non-empty object: record what the run actually "
            "sampled, not only what was asked for"
        )
    scale_factor = sample.get("scale_factor")
    if scale_factor is not None:
        if isinstance(scale_factor, bool) or not isinstance(scale_factor, (int, float)):
            errors.append("sample.scale_factor must be a number")
        elif not 0 < scale_factor <= 1:
            errors.append(f"sample.scale_factor must be within (0, 1], got {scale_factor!r}")
    return errors
