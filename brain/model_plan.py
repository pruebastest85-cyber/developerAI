from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from brain.structured_json import StructuredOutputSchema
from brain.workflow_plan import StepSpec, WorkflowPlan, WorkflowValidationError
from brain.change_proposal import TEST_ID_PATTERN


ROOT_FIELDS = frozenset(
    {"schema_version", "goal", "completed", "steps", "message"}
)
STEP_FIELDS = frozenset(
    {"id", "tool", "action", "args", "goal", "depends_on", "justification"}
)
AUTHORITY_FIELDS = frozenset(
    {
        "result",
        "results",
        "status",
        "tool_result",
        "output",
        "approval",
        "approved",
        "approval_token",
        "required",
        "repeat_completed",
        "runtime_metadata",
        "bindings",
    }
)
STEP_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$", re.ASCII)

ERROR_MESSAGES = MappingProxyType(
    {
        "unsupported_plan_version": "La versión del plan del modelo no está soportada.",
        "invalid_model_plan": "El plan propuesto por el modelo no es válido.",
        "unknown_tool": "El plan propone una herramienta no permitida.",
        "unknown_action": "El plan propone una acción no permitida.",
        "invalid_arguments": "Los argumentos de una acción no son válidos.",
        "step_limit_exceeded": "El plan excede el límite permitido de pasos.",
        "invalid_dependency": "Las dependencias del plan no son válidas.",
        "cyclic_dependencies": "El plan contiene dependencias cíclicas.",
    }
)


class ModelPlanAdaptationError(ValueError):
    """Public, non-sensitive rejection of an untrusted model plan."""

    __slots__ = ("code",)

    def __init__(self, code: str):
        safe_code = code if code in ERROR_MESSAGES else "invalid_model_plan"
        self.code = safe_code
        super().__init__(ERROR_MESSAGES[safe_code])


@dataclass(frozen=True)
class ModelPlanLimits:
    max_steps: int = 8
    max_dependencies: int = 8
    max_goal_length: int = 1000
    max_step_text_length: int = 500
    max_message_length: int = 2000

    def __post_init__(self) -> None:
        for value in vars(self).values():
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ModelPlanAdaptationError("invalid_model_plan")


@dataclass(frozen=True)
class ModelArgumentContract:
    """Closed validation rule for one JSON argument."""

    kind: str
    required: bool = False
    min_value: int | float | None = None
    max_value: int | float | None = None
    min_length: int | None = None
    max_length: int | None = None

    def __post_init__(self) -> None:
        invalid = False
        try:
            invalid = (
                type(self.kind) is not str
                or self.kind not in {"string", "integer", "number", "boolean"}
                or type(self.required) is not bool
            )
            for value in (self.min_length, self.max_length):
                if value is not None and (
                    type(value) is not int
                    or value < 0
                    or self.kind != "string"
                ):
                    invalid = True
            if (
                self.min_length is not None
                and self.max_length is not None
                and self.min_length > self.max_length
            ):
                invalid = True
            for value in (self.min_value, self.max_value):
                if value is not None and (
                    type(value) not in {int, float} or not math.isfinite(value)
                ):
                    invalid = True
            if self.kind not in {"integer", "number"} and (
                self.min_value is not None or self.max_value is not None
            ):
                invalid = True
            if (
                self.min_value is not None
                and self.max_value is not None
                and self.min_value > self.max_value
            ):
                invalid = True
        except BaseException:
            invalid = True
        if invalid:
            raise ModelPlanAdaptationError("invalid_arguments") from None

    def accepts(self, value: Any) -> bool:
        if self.kind == "string":
            valid = isinstance(value, str)
            if valid and self.min_length is not None:
                valid = len(value) >= self.min_length
            if valid and self.max_length is not None:
                valid = len(value) <= self.max_length
            if valid:
                valid = _text_is_safe(value)
            return valid
        if self.kind == "integer":
            valid = isinstance(value, int) and not isinstance(value, bool)
        elif self.kind == "number":
            valid = (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and (not isinstance(value, float) or math.isfinite(value))
            )
        else:
            return isinstance(value, bool)
        if not valid:
            return False
        if self.min_value is not None and value < self.min_value:
            return False
        if self.max_value is not None and value > self.max_value:
            return False
        return True


@dataclass(frozen=True)
class ModelOperationContract:
    """Immutable contract for one exact tool/action pair."""

    arguments: Mapping[str, ModelArgumentContract] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Constructor inputs cross a public trust boundary.  A mappingproxy is
        # not proof that its wrapped mapping is a plain dict, so only an exact
        # dict is traversed here; the validated copy is made read-only below.
        if type(self.arguments) is not dict:
            raise ModelPlanAdaptationError("invalid_arguments")
        copied = {}
        invalid = False
        try:
            for name, contract in self.arguments.items():
                if (
                    type(name) is not str
                    or not name
                    or type(contract) is not ModelArgumentContract
                ):
                    invalid = True
                    break
                copied[name] = contract
        except BaseException:
            invalid = True
        if invalid:
            raise ModelPlanAdaptationError("invalid_arguments") from None
        object.__setattr__(self, "arguments", MappingProxyType(copied))


# This catalog contains proposal-safe operations only. It deliberately excludes
# file mutation, approvals, arbitrary shell commands, and every destructive Git
# operation. Callers may inject a smaller catalog; forbidden operations remain
# forbidden even if a caller attempts to include them.
SAFE_MODEL_OPERATION_CATALOG = MappingProxyType(
    {
        ("code_reader", "read_file"): ModelOperationContract(
            {
                "path": ModelArgumentContract(
                    "string", required=True, min_length=1, max_length=512
                ),
                "max_lines": ModelArgumentContract(
                    "integer", min_value=1, max_value=10_000
                ),
            }
        ),
        ("code_analyzer", "summarize"): ModelOperationContract(
            {
                "path": ModelArgumentContract(
                    "string", required=True, min_length=1, max_length=512
                ),
            }
        ),
        ("patch_generator", "generate_patch"): ModelOperationContract(
            {
                "path": ModelArgumentContract(
                    "string", required=True, min_length=1, max_length=512
                ),
                "new_content": ModelArgumentContract(
                    "string", required=True, max_length=256 * 1024
                ),
            }
        ),
        ("test_runner", "run_tests"): ModelOperationContract(
            {
                "test_id": ModelArgumentContract(
                    "string", min_length=3, max_length=512
                ),
            }
        ),
        ("git_tools", "status"): ModelOperationContract(),
    }
)

MODEL_PLAN_OUTPUT_SCHEMA = StructuredOutputSchema(
    "developer_ai_model_plan",
    {
        "type": "object",
        "properties": {
            "schema_version": {"type": "string", "enum": ["1"]},
            "goal": {"type": "string", "maxLength": 1000},
            "completed": {"type": "boolean"},
            "steps": {
                "type": "array",
                "minItems": 0,
                "maxItems": 8,
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 64,
                        },
                        "tool": {
                            "type": "string",
                            "enum": [
                                "code_reader",
                                "code_analyzer",
                                "patch_generator",
                                "test_runner",
                                "git_tools",
                            ],
                        },
                        "action": {
                            "type": "string",
                            "enum": [
                                "read_file",
                                "summarize",
                                "generate_patch",
                                "run_tests",
                                "status",
                            ],
                        },
                        "args": {
                            "type": "object",
                            "properties": {
                                "path": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 512,
                                },
                                "max_lines": {"type": "integer"},
                                "new_content": {
                                    "type": "string",
                                    "maxLength": 256 * 1024,
                                },
                                "test_id": {
                                    "type": "string",
                                    "minLength": 3,
                                    "maxLength": 512,
                                },
                            },
                            "required": [],
                            "additionalProperties": False,
                        },
                        "goal": {"type": "string", "maxLength": 500},
                        "depends_on": {
                            "type": "array",
                            "items": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 64,
                            },
                            "minItems": 0,
                            "maxItems": 8,
                        },
                        "justification": {
                            "type": "string",
                            "maxLength": 500,
                        },
                    },
                    "required": [
                        "id",
                        "tool",
                        "action",
                        "args",
                        "goal",
                        "depends_on",
                        "justification",
                    ],
                    "additionalProperties": False,
                },
            },
            "message": {"type": "string", "maxLength": 2000},
        },
        "required": [
            "schema_version",
            "goal",
            "completed",
            "steps",
            "message",
        ],
        "additionalProperties": False,
    },
)


@dataclass(frozen=True)
class ModelPlanStep:
    """Immutable declarative step with no runtime state or authority fields."""

    id: str
    tool: str
    action: str
    args: Mapping[str, Any]
    goal: str
    depends_on: tuple[str, ...]
    justification: str

    def __post_init__(self) -> None:
        code = _validate_step_values(self)
        if code:
            raise ModelPlanAdaptationError(code)
        frozen_args, failed = _freeze_json_mapping(self.args)
        if failed:
            raise ModelPlanAdaptationError("invalid_arguments")
        object.__setattr__(self, "args", frozen_args)
        object.__setattr__(self, "depends_on", tuple(self.depends_on))

    @classmethod
    def from_mapping(
        cls,
        payload: Mapping[str, Any],
        *,
        limits: ModelPlanLimits | None = None,
    ) -> "ModelPlanStep":
        limits = limits or ModelPlanLimits()
        if type(payload) is not dict or set(payload) != STEP_FIELDS:
            raise ModelPlanAdaptationError("invalid_model_plan")
        try:
            step = cls(
                id=payload["id"],
                tool=payload["tool"],
                action=payload["action"],
                args=payload["args"],
                goal=payload["goal"],
                depends_on=payload["depends_on"],
                justification=payload["justification"],
            )
        except ModelPlanAdaptationError:
            raise
        except BaseException:
            step = None
        if step is None:
            raise ModelPlanAdaptationError("invalid_model_plan") from None
        if (
            len(step.goal) > limits.max_step_text_length
            or len(step.justification) > limits.max_step_text_length
            or len(step.depends_on) > limits.max_dependencies
        ):
            raise ModelPlanAdaptationError("invalid_model_plan")
        return step


@dataclass(frozen=True)
class ModelPlanDecision:
    """Validated model decision, still lacking permission to execute."""

    schema_version: str
    goal: str
    completed: bool
    steps: tuple[ModelPlanStep, ...]
    message: str

    def __post_init__(self) -> None:
        code = _validate_decision_values(self, ModelPlanLimits())
        if code:
            raise ModelPlanAdaptationError(code)
        object.__setattr__(self, "steps", tuple(self.steps))

    @classmethod
    def from_mapping(
        cls,
        payload: Mapping[str, Any],
        *,
        limits: ModelPlanLimits | None = None,
    ) -> "ModelPlanDecision":
        limits = limits or ModelPlanLimits()
        if type(payload) is not dict or set(payload) != ROOT_FIELDS:
            raise ModelPlanAdaptationError("invalid_model_plan")
        if payload.get("schema_version") != "1":
            raise ModelPlanAdaptationError("unsupported_plan_version")
        raw_steps = payload.get("steps")
        if type(raw_steps) is not list:
            raise ModelPlanAdaptationError("invalid_model_plan")
        if len(raw_steps) > limits.max_steps:
            raise ModelPlanAdaptationError("step_limit_exceeded")

        steps = []
        for raw_step in raw_steps:
            steps.append(ModelPlanStep.from_mapping(raw_step, limits=limits))
        decision = cls(
            schema_version=payload["schema_version"],
            goal=payload["goal"],
            completed=payload["completed"],
            steps=tuple(steps),
            message=payload["message"],
        )
        code = _validate_decision_values(decision, limits)
        if code:
            raise ModelPlanAdaptationError(code)
        return decision


class ModelPlanAdapter:
    """Pure semantic adapter from a model decision to the existing WorkflowPlan."""

    def __init__(
        self,
        catalog: Mapping[tuple[str, str], ModelOperationContract],
        *,
        limits: ModelPlanLimits | None = None,
    ):
        self._limits = limits or ModelPlanLimits()
        self._catalog = _copy_catalog(catalog)

    @property
    def catalog(self) -> Mapping[tuple[str, str], ModelOperationContract]:
        return self._catalog

    def adapt(self, decision: ModelPlanDecision) -> WorkflowPlan:
        if not isinstance(decision, ModelPlanDecision):
            raise ModelPlanAdaptationError("invalid_model_plan")
        code = _validate_decision_values(decision, self._limits)
        if code:
            raise ModelPlanAdaptationError(code)

        known_tools = frozenset(tool for tool, _ in self._catalog)
        steps = []
        for model_step in decision.steps:
            contract = self._catalog.get((model_step.tool, model_step.action))
            if model_step.tool not in known_tools:
                raise ModelPlanAdaptationError("unknown_tool")
            if contract is None:
                raise ModelPlanAdaptationError("unknown_action")
            if not _arguments_match(model_step.args, contract):
                raise ModelPlanAdaptationError("invalid_arguments")
            if (
                (model_step.tool, model_step.action)
                == ("test_runner", "run_tests")
                and "test_id" in model_step.args
                and not TEST_ID_PATTERN.fullmatch(model_step.args["test_id"])
            ):
                raise ModelPlanAdaptationError("invalid_arguments")
            steps.append(
                StepSpec(
                    id=model_step.id,
                    action=model_step.action,
                    tool=model_step.tool,
                    args=dict(model_step.args),
                    bindings={},
                    goal=model_step.goal,
                    depends_on=model_step.depends_on,
                    approval="policy",
                    required=True,
                    repeat_completed=False,
                )
            )

        workflow = None
        workflow_failed = False
        try:
            workflow = WorkflowPlan(tuple(steps), allowed_tools=known_tools)
        except WorkflowValidationError:
            workflow_failed = True
        if workflow_failed:
            raise ModelPlanAdaptationError("invalid_model_plan") from None
        return workflow


def _copy_catalog(
    catalog: Mapping[tuple[str, str], ModelOperationContract],
) -> Mapping[tuple[str, str], ModelOperationContract]:
    # SAFE_MODEL_OPERATION_CATALOG is the sole trusted proxy.  Reject every
    # external mappingproxy before traversal because it may wrap a hostile
    # dict subclass whose methods still execute through the proxy.
    if catalog is not SAFE_MODEL_OPERATION_CATALOG and type(catalog) is not dict:
        raise ModelPlanAdaptationError("invalid_model_plan")
    items = ()
    invalid = False
    try:
        items = tuple(catalog.items())
    except BaseException:
        invalid = True
    if invalid:
        raise ModelPlanAdaptationError("invalid_model_plan") from None

    copied = {}
    for key, contract in items:
        if (
            type(key) is not tuple
            or len(key) != 2
            or any(type(item) is not str or not item for item in key)
            or type(contract) is not ModelOperationContract
        ):
            raise ModelPlanAdaptationError("invalid_model_plan")
        base_contract = SAFE_MODEL_OPERATION_CATALOG.get(key)
        if base_contract is None or not _contract_is_restriction(
            contract, base_contract
        ):
            raise ModelPlanAdaptationError("invalid_model_plan")
        copied[key] = ModelOperationContract(dict(contract.arguments))
    return MappingProxyType(copied)


def _contract_is_restriction(
    candidate: ModelOperationContract,
    base: ModelOperationContract,
) -> bool:
    candidate_names = set(candidate.arguments)
    base_names = set(base.arguments)
    base_required = {
        name for name, rule in base.arguments.items() if rule.required
    }
    if candidate_names.difference(base_names) or base_required.difference(
        candidate_names
    ):
        return False

    for name, candidate_rule in candidate.arguments.items():
        base_rule = base.arguments[name]
        if candidate_rule.kind != base_rule.kind:
            return False
        if base_rule.required and not candidate_rule.required:
            return False
        if not _lower_bound_is_narrower(
            candidate_rule.min_value, base_rule.min_value
        ):
            return False
        if not _upper_bound_is_narrower(
            candidate_rule.max_value, base_rule.max_value
        ):
            return False
        if not _lower_bound_is_narrower(
            candidate_rule.min_length, base_rule.min_length
        ):
            return False
        if not _upper_bound_is_narrower(
            candidate_rule.max_length, base_rule.max_length
        ):
            return False
    return True


def _lower_bound_is_narrower(candidate, base) -> bool:
    if base is None:
        return True
    return candidate is not None and candidate >= base


def _upper_bound_is_narrower(candidate, base) -> bool:
    if base is None:
        return True
    return candidate is not None and candidate <= base


def _arguments_match(
    args: Mapping[str, Any],
    contract: ModelOperationContract,
) -> bool:
    supplied = set(args)
    allowed = set(contract.arguments)
    required = {
        name for name, argument in contract.arguments.items() if argument.required
    }
    if supplied.difference(allowed) or required.difference(supplied):
        return False
    return all(contract.arguments[name].accepts(value) for name, value in args.items())


def _validate_step_values(step: ModelPlanStep) -> str | None:
    if not isinstance(step.id, str) or STEP_ID_PATTERN.fullmatch(step.id) is None:
        return "invalid_model_plan"
    for value in (step.tool, step.action):
        if not isinstance(value, str) or not value or not _text_is_safe(value):
            return "invalid_model_plan"
    if not isinstance(step.goal, str) or not isinstance(step.justification, str):
        return "invalid_model_plan"
    if (
        len(step.goal) > 500
        or len(step.justification) > 500
        or not _text_is_safe(step.goal)
        or not _text_is_safe(step.justification)
    ):
        return "invalid_model_plan"
    if not isinstance(step.args, Mapping):
        return "invalid_arguments"
    if not isinstance(step.depends_on, (list, tuple)):
        return "invalid_dependency"
    if any(
        not isinstance(item, str) or STEP_ID_PATTERN.fullmatch(item) is None
        for item in step.depends_on
    ):
        return "invalid_dependency"
    if len(step.depends_on) != len(set(step.depends_on)):
        return "invalid_dependency"
    if len(step.depends_on) > 8:
        return "invalid_dependency"
    if step.id in step.depends_on:
        return "invalid_dependency"
    return None


def _validate_decision_values(
    decision: ModelPlanDecision,
    limits: ModelPlanLimits,
) -> str | None:
    if decision.schema_version != "1":
        return "unsupported_plan_version"
    if (
        not isinstance(decision.goal, str)
        or len(decision.goal) > limits.max_goal_length
        or not _text_is_safe(decision.goal)
        or not isinstance(decision.message, str)
        or len(decision.message) > limits.max_message_length
        or not _text_is_safe(decision.message)
        or not isinstance(decision.completed, bool)
        or not isinstance(decision.steps, (list, tuple))
        or any(not isinstance(step, ModelPlanStep) for step in decision.steps)
    ):
        return "invalid_model_plan"
    if len(decision.steps) > limits.max_steps:
        return "step_limit_exceeded"
    if decision.completed:
        if decision.steps or not decision.message:
            return "invalid_model_plan"
    elif not decision.steps:
        return "invalid_model_plan"

    ids = [step.id for step in decision.steps]
    if len(ids) != len(set(ids)):
        return "invalid_dependency"
    known = set(ids)
    for step in decision.steps:
        if (
            len(step.goal) > limits.max_step_text_length
            or len(step.justification) > limits.max_step_text_length
            or len(step.depends_on) > limits.max_dependencies
            or any(dependency not in known for dependency in step.depends_on)
        ):
            return "invalid_dependency"
    if _has_dependency_cycle(decision.steps):
        return "cyclic_dependencies"
    return None


def _has_dependency_cycle(steps: tuple[ModelPlanStep, ...]) -> bool:
    remaining = {step.id: set(step.depends_on) for step in steps}
    while remaining:
        ready = [step_id for step_id, deps in remaining.items() if not deps]
        if not ready:
            return True
        for step_id in ready:
            del remaining[step_id]
            for dependencies in remaining.values():
                dependencies.discard(step_id)
    return False


def _text_is_safe(value: str) -> bool:
    return all(
        unicodedata.category(character) != "Cc" or character in "\n\r\t"
        for character in value
    )


def _freeze_json_mapping(value: Mapping[str, Any]):
    if type(value) is not dict:
        return None, True
    active = set()

    def freeze(item):
        if item is None or isinstance(item, (str, bool, int)):
            return item
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError
            return item
        if type(item) is dict:
            item_id = id(item)
            if item_id in active:
                raise ValueError
            active.add(item_id)
            try:
                converted = {}
                for key, nested in item.items():
                    if not isinstance(key, str):
                        raise ValueError
                    converted[key] = freeze(nested)
                return MappingProxyType(converted)
            finally:
                active.remove(item_id)
        if type(item) is list:
            item_id = id(item)
            if item_id in active:
                raise ValueError
            active.add(item_id)
            try:
                return tuple(freeze(nested) for nested in item)
            finally:
                active.remove(item_id)
        raise ValueError

    frozen = None
    failed = False
    try:
        frozen = freeze(value)
    except BaseException:
        failed = True
    return frozen, failed
