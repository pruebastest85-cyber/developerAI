from __future__ import annotations

import copy
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable

from tools.tool_result import ToolResult


APPROVAL_POLICIES = frozenset({"none", "policy", "required"})
TOOL_RESULT_FIELDS = frozenset(
    {"status", "tool_name", "data", "message", "error", "metadata", "retryable"}
)


class WorkflowValidationError(ValueError):
    """A declarative workflow is internally inconsistent or unsafe to resolve."""


class ResultResolutionError(ValueError):
    """A ResultRef cannot be resolved from successful prior step results."""


@dataclass(frozen=True)
class ResultRef:
    """Path into the seven-field dictionary representation of a ToolResult."""

    step_id: str
    path: tuple[str | int, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.step_id, str) or not self.step_id.strip():
            raise WorkflowValidationError("ResultRef.step_id debe ser una cadena no vacía")
        if not isinstance(self.path, tuple):
            if isinstance(self.path, (str, bytes)):
                raise WorkflowValidationError(
                    "ResultRef.path debe ser una secuencia de segmentos"
                )
            object.__setattr__(self, "path", tuple(self.path))
        for segment in self.path:
            if isinstance(segment, bool) or not isinstance(segment, (str, int)):
                raise WorkflowValidationError(
                    "ResultRef.path solo admite segmentos str o int"
                )
            if isinstance(segment, int) and segment < 0:
                raise WorkflowValidationError(
                    "ResultRef.path no admite índices negativos"
                )


@dataclass(frozen=True)
class StepSpec:
    id: str
    action: str
    tool: str
    args: Mapping[str, Any] = field(default_factory=dict)
    bindings: Mapping[str, ResultRef] = field(default_factory=dict)
    goal: str = ""
    depends_on: tuple[str, ...] = ()
    approval: str = "policy"
    required: bool = True
    repeat_completed: bool = False

    def __post_init__(self) -> None:
        for field_name in ("id", "action", "tool"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise WorkflowValidationError(
                    f"StepSpec.{field_name} debe ser una cadena no vacía"
                )
        if not isinstance(self.goal, str):
            raise WorkflowValidationError("StepSpec.goal debe ser una cadena")
        if self.approval not in APPROVAL_POLICIES:
            raise WorkflowValidationError(
                f"approval debe ser uno de {sorted(APPROVAL_POLICIES)}"
            )
        if not isinstance(self.required, bool) or not isinstance(
            self.repeat_completed, bool
        ):
            raise WorkflowValidationError(
                "required y repeat_completed deben ser booleanos"
            )
        if not isinstance(self.args, Mapping) or not isinstance(self.bindings, Mapping):
            raise WorkflowValidationError("args y bindings deben ser mappings")

        args = copy.deepcopy(dict(self.args))
        bindings = dict(self.bindings)
        self._validate_argument_names(args, "args")
        self._validate_argument_names(bindings, "bindings")
        if set(args).intersection(bindings):
            raise WorkflowValidationError(
                "Un argumento no puede aparecer a la vez en args y bindings"
            )
        if any(not isinstance(ref, ResultRef) for ref in bindings.values()):
            raise WorkflowValidationError("Todos los bindings deben ser ResultRef")

        depends_on = tuple(self.depends_on)
        if any(not isinstance(item, str) or not item.strip() for item in depends_on):
            raise WorkflowValidationError(
                "depends_on solo admite IDs de paso no vacíos"
            )
        if len(set(depends_on)) != len(depends_on):
            raise WorkflowValidationError("depends_on no admite IDs duplicados")

        object.__setattr__(self, "args", MappingProxyType(args))
        object.__setattr__(self, "bindings", MappingProxyType(bindings))
        object.__setattr__(self, "depends_on", depends_on)

    @staticmethod
    def _validate_argument_names(values: Mapping[str, Any], label: str) -> None:
        for name in values:
            if not isinstance(name, str) or not name.strip():
                raise WorkflowValidationError(
                    f"Los nombres de {label} deben ser cadenas no vacías"
                )

    def identity(self) -> tuple:
        return (
            ("id", self.id),
            ("action", self.action),
            ("tool", self.tool),
            ("args", _canonical_identity(self.args)),
            ("bindings", _canonical_identity(self.bindings)),
            ("goal", self.goal),
            ("depends_on", _canonical_identity(self.depends_on)),
            ("approval", self.approval),
            ("required", self.required),
            ("repeat_completed", self.repeat_completed),
        )


@dataclass(frozen=True)
class WorkflowPlan:
    steps: tuple[StepSpec, ...]
    allowed_tools: frozenset[str] | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        steps = tuple(self.steps)
        if any(not isinstance(step, StepSpec) for step in steps):
            raise WorkflowValidationError("WorkflowPlan.steps solo admite StepSpec")
        object.__setattr__(self, "steps", steps)

        allowed = self.allowed_tools
        if allowed is not None:
            allowed = frozenset(allowed)
            if any(not isinstance(name, str) or not name for name in allowed):
                raise WorkflowValidationError(
                    "allowed_tools solo admite nombres no vacíos"
                )
            object.__setattr__(self, "allowed_tools", allowed)

        self.validate()

    def validate(self) -> None:
        ids = [step.id for step in self.steps]
        if len(ids) != len(set(ids)):
            raise WorkflowValidationError("Los IDs de los pasos deben ser únicos")
        known_ids = set(ids)

        for step in self.steps:
            step.identity()
            if self.allowed_tools is not None and step.tool not in self.allowed_tools:
                raise WorkflowValidationError(
                    f"Herramienta no registrada en el plan: {step.tool}"
                )
            if step.id in step.depends_on:
                raise WorkflowValidationError(
                    f"El paso {step.id} no puede depender de sí mismo"
                )
            missing_dependencies = set(step.depends_on).difference(known_ids)
            if missing_dependencies:
                raise WorkflowValidationError(
                    f"Dependencias inexistentes en {step.id}: "
                    f"{sorted(missing_dependencies)}"
                )
            for ref in step.bindings.values():
                if ref.step_id not in known_ids:
                    raise WorkflowValidationError(
                        f"ResultRef hacia paso inexistente: {ref.step_id}"
                    )
                if ref.step_id == step.id:
                    raise WorkflowValidationError(
                        f"El paso {step.id} no puede referenciar su propio resultado"
                    )

        self.execution_order()

    def execution_order(self) -> tuple[StepSpec, ...]:
        by_id = {step.id: step for step in self.steps}
        remaining = {
            step.id: set(step.depends_on).union(
                ref.step_id for ref in step.bindings.values()
            )
            for step in self.steps
        }
        ordered: list[StepSpec] = []

        while remaining:
            ready = [
                step.id
                for step in self.steps
                if step.id in remaining and not remaining[step.id]
            ]
            if not ready:
                raise WorkflowValidationError("El plan contiene un ciclo de dependencias")
            for step_id in ready:
                ordered.append(by_id[step_id])
                del remaining[step_id]
                for dependencies in remaining.values():
                    dependencies.discard(step_id)

        return tuple(ordered)

    def identity(self) -> tuple:
        return tuple(step.identity() for step in self.steps)


class ArgumentResolver:
    """Resolve bindings from successful ToolResults without mutating StepSpec."""

    def resolve(
        self,
        step: StepSpec,
        prior_results: Mapping[str, ToolResult],
    ) -> dict[str, Any]:
        if not isinstance(step, StepSpec):
            raise TypeError("step debe ser StepSpec")
        if not isinstance(prior_results, Mapping):
            raise TypeError("prior_results debe ser un mapping")

        resolved = copy.deepcopy(dict(step.args))
        for argument_name, reference in step.bindings.items():
            result = prior_results.get(reference.step_id)
            if result is None:
                raise ResultResolutionError(
                    f"El paso {reference.step_id} todavía no tiene resultado"
                )
            if not isinstance(result, ToolResult):
                raise ResultResolutionError(
                    f"El resultado de {reference.step_id} no es ToolResult"
                )
            if result.status != "ok":
                raise ResultResolutionError(
                    f"El paso {reference.step_id} terminó con status={result.status}"
                )
            resolved[argument_name] = copy.deepcopy(
                self._walk(self._tool_result_view(result), reference.path)
            )
        return resolved

    @staticmethod
    def _tool_result_view(result: ToolResult) -> dict[str, Any]:
        return {
            "status": result.status,
            "tool_name": result.tool_name,
            "data": result.data,
            "message": result.message,
            "error": result.error,
            "metadata": result.metadata,
            "retryable": result.retryable,
        }

    @staticmethod
    def _walk(root: Any, path: Iterable[str | int]) -> Any:
        current = root
        for segment in path:
            if isinstance(current, Mapping):
                if not isinstance(segment, str) or segment not in current:
                    raise ResultResolutionError(
                        f"Clave inexistente al resolver ResultRef: {segment!r}"
                    )
                current = current[segment]
            elif isinstance(current, (list, tuple)):
                if isinstance(segment, bool) or not isinstance(segment, int):
                    raise ResultResolutionError(
                        "Las secuencias requieren índices enteros"
                    )
                try:
                    current = current[segment]
                except IndexError as exc:
                    raise ResultResolutionError(
                        f"Índice fuera de rango al resolver ResultRef: {segment}"
                    ) from exc
            else:
                raise ResultResolutionError(
                    "ResultRef solo puede recorrer mappings, listas y tuplas"
                )
        return current


def _canonical_identity(value: Any, active_ids: set[int] | None = None) -> tuple:
    if active_ids is None:
        active_ids = set()
    if value is None:
        return ("none",)
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, int):
        return ("int", value)
    if isinstance(value, float):
        if math.isnan(value):
            return ("float", "nan")
        if math.isinf(value):
            return ("float", "inf" if value > 0 else "-inf")
        return ("float", value.hex())
    if isinstance(value, str):
        return ("str", value)
    if isinstance(value, bytes):
        return ("bytes", value.hex())
    if isinstance(value, Path):
        return ("path", value.as_posix())
    if isinstance(value, Enum):
        return (
            "enum",
            value.__class__.__module__,
            value.__class__.__qualname__,
            _canonical_identity(value.value, active_ids),
        )
    if isinstance(value, ResultRef):
        return (
            "result-ref",
            value.step_id,
            _canonical_identity(value.path, active_ids),
        )

    tracked = isinstance(value, (Mapping, list, tuple, set, frozenset))
    value_id = id(value)
    if tracked:
        if value_id in active_ids:
            raise WorkflowValidationError(
                "La configuración declarativa no admite referencias circulares"
            )
        active_ids.add(value_id)
    try:
        if isinstance(value, Mapping):
            items = [
                (
                    _canonical_identity(key, active_ids),
                    _canonical_identity(nested, active_ids),
                )
                for key, nested in value.items()
            ]
            return ("mapping", tuple(sorted(items, key=lambda item: item[0])))
        if isinstance(value, list):
            return (
                "list",
                tuple(_canonical_identity(item, active_ids) for item in value),
            )
        if isinstance(value, tuple):
            return (
                "tuple",
                tuple(_canonical_identity(item, active_ids) for item in value),
            )
        if isinstance(value, (set, frozenset)):
            return (
                "set",
                tuple(sorted(_canonical_identity(item, active_ids) for item in value)),
            )
        raise WorkflowValidationError(
            "Tipo no admitido en configuración declarativa: "
            f"{value.__class__.__module__}.{value.__class__.__qualname__}"
        )
    finally:
        if tracked:
            active_ids.remove(value_id)
