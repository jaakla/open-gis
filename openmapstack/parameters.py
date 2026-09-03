"""Versioned parameter addressing for a project's canonical entrypoint.

A metamorphic relation that varies a threshold, and a benchmark that wants to
run the same pipeline at a different setting, both need one thing the
manifest did not previously give them: a way to *address* a parameter of the
pipeline without editing the pipeline. ``runtime.implementation.parameters``
is that contract.

.. code-block:: yaml

    runtime:
      implementation:
        pipeline: pipeline.py
        parameters:
          - id: road_distance_m
            type: number
            canonical: 2000
            binding: {argument: "--road-distance-m"}   # or {environment: OMS_ROAD_DISTANCE_M}
            step: road_distance          # optional: the processing step that
            field: max_distance_m        # consumes it, so drift is checkable

Rules:

- ``id`` is a simple identifier, unique within the manifest;
- ``type`` is ``integer``, ``number``, or ``string`` and ``canonical`` has that
  type (booleans are not numbers);
- exactly one binding: ``argument`` (a ``--long-flag``, passed as
  ``--flag value``) or ``environment`` (an ``UPPER_SNAKE`` variable);
- ``step``/``field`` are optional but come together; when present the named
  processing step must exist and its field must equal ``canonical``, so a
  manifest cannot advertise one threshold while the step declares another.

A parameter may additionally declare a sampling ``role``, which is how
``openmapstack run --sample*`` addresses the knob that shrinks the work:

.. code-block:: yaml

        parameters:
          - id: sample_area
            type: string
            canonical: ""                 # the canonical run samples nothing
            role: sample_area             # sample_area | sample_rows | sample_fraction
            sample: "26.68,58.35,26.76,58.39"   # what bare --sample uses
            binding: {argument: "--sample-area"}

Sampling rules on top of the above:

- at most one parameter may claim each role;
- ``canonical`` must be the role's *no sampling* value (``""`` for a string,
  ``0`` for a number), because a canonical run passes nothing and must
  therefore process the full inputs;
- ``sample`` is optional, must differ from ``canonical``, and is what bare
  ``--sample`` binds; without it the role needs an explicit value on the
  command line;
- a sampling parameter must not pair ``step``/``field``: it selects *input*,
  not a processing threshold, so there is no step value to agree with.

The canonical run passes nothing: a pipeline must produce the accepted result
with no arguments and no variables set. Bindings exist so a *variant* run can
say "same pipeline, this one knob turned".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .project import get_in

PARAMETERS_SCHEMA = "openmapstack-parameters/v1"
PARAMETER_TYPES = ("integer", "number", "string")
#: Sampling knobs ``openmapstack run`` can address by role rather than by id.
SAMPLE_ROLES = ("sample_area", "sample_rows", "sample_fraction")

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ARGUMENT = re.compile(r"^--[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_ENVIRONMENT = re.compile(r"^[A-Z][A-Z0-9_]*$")

#: The value each type carries when a sampling role is switched off.
_NO_SAMPLING: dict[str, Any] = {"integer": 0, "number": 0, "string": ""}


class ParameterError(ValueError):
    """The parameters block is malformed or drifts from the processing steps."""


@dataclass(frozen=True)
class Parameter:
    id: str
    type: str
    canonical: Any
    argument: str | None = None
    environment: str | None = None
    step: str | None = None
    field: str | None = None
    role: str | None = None
    sample: Any = None

    def bind(self, value: Any) -> tuple[list[str], dict[str, str]]:
        """Return the argv suffix and environment additions for ``value``."""
        rendered = _render(value, self.type)
        if self.argument is not None:
            return [self.argument, rendered], {}
        assert self.environment is not None
        return [], {self.environment: rendered}


def _render(value: Any, type_name: str) -> str:
    if type_name == "integer":
        return str(int(value))
    if type_name == "number":
        number = float(value)
        return str(int(number)) if number.is_integer() else repr(number)
    return str(value)


def value_has_type(value: Any, type_name: str) -> bool:
    if isinstance(value, bool):
        return False
    if type_name == "integer":
        return isinstance(value, int)
    if type_name == "number":
        return isinstance(value, (int, float))
    return isinstance(value, str)


def declared_parameters(manifest: dict[str, Any]) -> list[Parameter]:
    """Parse and validate ``runtime.implementation.parameters``.

    Returns an empty list when nothing is declared. Raises ``ParameterError``
    describing every problem found, so a caller can report one failure that
    names all of them.
    """
    raw = get_in(manifest, "runtime", "implementation", "parameters")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ParameterError("runtime.implementation.parameters must be a list")
    steps = get_in(manifest, "processing", "steps", default=[]) or []
    steps_by_id = {
        str(step.get("id")): step for step in steps if isinstance(step, dict) and step.get("id") is not None
    }
    errors: list[str] = []
    seen: set[str] = set()
    seen_roles: dict[str, str] = {}
    parameters: list[Parameter] = []
    for index, entry in enumerate(raw):
        where = f"parameters[{index}]"
        if not isinstance(entry, dict):
            errors.append(f"{where} must be a mapping")
            continue
        unknown = set(entry) - {"id", "type", "canonical", "binding", "step", "field", "description", "role", "sample"}
        if unknown:
            errors.append(f"{where} has unknown keys {sorted(unknown)}")
        parameter_id = entry.get("id")
        if not isinstance(parameter_id, str) or _IDENTIFIER.fullmatch(parameter_id) is None:
            errors.append(f"{where}.id must be a simple identifier")
            continue
        where = f"parameters[{parameter_id}]"
        if parameter_id in seen:
            errors.append(f"{where} is declared more than once")
        seen.add(parameter_id)
        type_name = entry.get("type")
        if type_name not in PARAMETER_TYPES:
            errors.append(f"{where}.type must be one of {list(PARAMETER_TYPES)}")
            continue
        if "canonical" not in entry or not value_has_type(entry["canonical"], type_name):
            errors.append(f"{where}.canonical must be a {type_name}")
            continue
        binding = entry.get("binding")
        argument = environment = None
        if not isinstance(binding, dict) or len(binding) != 1:
            errors.append(f"{where}.binding must declare exactly one of argument/environment")
        elif "argument" in binding:
            argument = binding["argument"]
            if not isinstance(argument, str) or _ARGUMENT.fullmatch(argument) is None:
                errors.append(f"{where}.binding.argument must be a --long-flag")
                argument = None
        elif "environment" in binding:
            environment = binding["environment"]
            if not isinstance(environment, str) or _ENVIRONMENT.fullmatch(environment) is None:
                errors.append(f"{where}.binding.environment must be an UPPER_SNAKE variable name")
                environment = None
        else:
            errors.append(f"{where}.binding must declare exactly one of argument/environment")
        role = entry.get("role")
        sample = entry.get("sample")
        if role is not None:
            if role not in SAMPLE_ROLES:
                errors.append(f"{where}.role must be one of {list(SAMPLE_ROLES)}, got {role!r}")
                role = None
            elif role in seen_roles:
                errors.append(f"{where}.role {role!r} is already claimed by parameter {seen_roles[role]!r}")
            else:
                seen_roles[role] = parameter_id
                if entry["canonical"] != _NO_SAMPLING[type_name]:
                    errors.append(
                        f"{where}.canonical must be {_NO_SAMPLING[type_name]!r} for a sampling role: "
                        f"the canonical run samples nothing"
                    )
        if "sample" in entry:
            if role is None:
                errors.append(f"{where}.sample needs a sampling role; a sample value alone is unaddressable")
            elif not value_has_type(sample, type_name):
                errors.append(f"{where}.sample must be a {type_name}")
            elif sample == entry["canonical"]:
                errors.append(f"{where}.sample equals canonical, so it would not sample anything")
        step = entry.get("step")
        field = entry.get("field")
        if role is not None and (step is not None or field is not None):
            errors.append(
                f"{where} is a sampling parameter and must not pair step/field: "
                f"it selects input, not a processing threshold"
            )
        elif (step is None) != (field is None):
            errors.append(f"{where} must declare step and field together")
        elif step is not None:
            if not isinstance(step, str) or step not in steps_by_id:
                errors.append(f"{where}.step {step!r} is not a processing step")
            elif not isinstance(field, str) or field not in steps_by_id[step]:
                errors.append(f"{where}.field {field!r} is not declared on step {step!r}")
            elif steps_by_id[step][field] != entry["canonical"]:
                errors.append(
                    f"{where}.canonical {entry['canonical']!r} != processing step "
                    f"{step!r}.{field} = {steps_by_id[step][field]!r}"
                )
        if argument is None and environment is None:
            continue
        parameters.append(
            Parameter(
                id=parameter_id,
                type=type_name,
                canonical=entry["canonical"],
                argument=argument,
                environment=environment,
                step=step if isinstance(step, str) else None,
                field=field if isinstance(field, str) else None,
                role=role if isinstance(role, str) else None,
                sample=sample if "sample" in entry else None,
            )
        )
    if errors:
        raise ParameterError("; ".join(errors))
    return parameters


def find_parameter(manifest: dict[str, Any], parameter_id: str) -> Parameter | None:
    for parameter in declared_parameters(manifest):
        if parameter.id == parameter_id:
            return parameter
    return None
