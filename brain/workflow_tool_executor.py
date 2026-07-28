from __future__ import annotations

import copy
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable, Mapping

from brain.workflow_limits import WorkflowLimits
from brain.workflow_plan import StepSpec, WorkflowPlan
from brain.workflow_runtime import WorkflowRuntimeState
from tools.tool_result import ToolResult


class WorkflowExecutorConfigurationError(ValueError):
    """A declarative tool/action pair is unavailable or not explicitly allowed."""


class InvalidWorkflowToolResultError(TypeError):
    """A declarative tool returned an invalid or mismatched ToolResult."""


class WorkflowLimitExceededError(ValueError):
    """A workflow operation would exceed an enforceable pre-effect limit."""


@dataclass(frozen=True)
class _ActionContract:
    agent_attribute: str
    required_args: frozenset[str]
    optional_args: frozenset[str] = frozenset()
    payload_builder: Callable[[Mapping[str, Any]], dict[str, Any]] = dict

    def validate_argument_names(self, supplied: set[str]) -> None:
        missing = self.required_args.difference(supplied)
        unexpected = supplied.difference(self.required_args | self.optional_args)
        if missing:
            raise WorkflowExecutorConfigurationError(
                f"Faltan argumentos requeridos: {sorted(missing)}"
            )
        if unexpected:
            raise WorkflowExecutorConfigurationError(
                f"Argumentos no permitidos: {sorted(unexpected)}"
            )

    def build_payload(self, args: Mapping[str, Any]) -> dict[str, Any]:
        self.validate_argument_names(set(args))
        return self.payload_builder(args)


def _copy_payload(args: Mapping[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(dict(args))


def _git_status_payload(args: Mapping[str, Any]) -> dict[str, Any]:
    if args:
        raise WorkflowExecutorConfigurationError(
            "git_tools.status no acepta argumentos"
        )
    return {"action": "status"}


ACTION_CONTRACTS = {
    ("code_reader", "read_file"): _ActionContract(
        "code_reader",
        frozenset({"path"}),
        frozenset({"max_lines"}),
        _copy_payload,
    ),
    ("code_analyzer", "summarize"): _ActionContract(
        "code_analyzer",
        frozenset({"path"}),
        payload_builder=_copy_payload,
    ),
    ("patch_generator", "generate_patch"): _ActionContract(
        "patch_generator",
        frozenset({"path", "new_content"}),
        payload_builder=_copy_payload,
    ),
    ("patch_applier", "apply_patch"): _ActionContract(
        "patch_applier",
        frozenset({"path", "old_content", "new_content"}),
        payload_builder=_copy_payload,
    ),
    ("file_creator", "create_file"): _ActionContract(
        "file_creator",
        frozenset({"path", "content"}),
        payload_builder=_copy_payload,
    ),
    ("test_runner", "run_tests"): _ActionContract(
        "test_runner",
        frozenset(),
        frozenset({"command"}),
        _copy_payload,
    ),
    ("git_tools", "status"): _ActionContract(
        "git_tools",
        frozenset(),
        payload_builder=_git_status_payload,
    ),
}


class WorkflowToolExecutor:
    """Validate and execute only explicitly declared current tool contracts."""

    def __init__(self, agent, limits: WorkflowLimits | None = None):
        self.agent = agent
        self.limits = limits or WorkflowLimits()

    @property
    def allowed_tools(self) -> frozenset[str]:
        return frozenset(tool for tool, _ in ACTION_CONTRACTS)

    def validate_plan(self, plan: WorkflowPlan) -> None:
        registry_names = set(self.agent.registry.names())
        for step in plan.steps:
            contract = ACTION_CONTRACTS.get((step.tool, step.action))
            if contract is None:
                raise WorkflowExecutorConfigurationError(
                    f"Pareja tool/action no permitida: {step.tool}/{step.action}"
                )
            if step.tool not in registry_names:
                raise WorkflowExecutorConfigurationError(
                    f"Herramienta no registrada: {step.tool}"
                )
            tool = vars(self.agent).get(contract.agent_attribute)
            execute = vars(type(tool)).get("execute") if tool is not None else None
            if tool is None or not callable(execute):
                raise WorkflowExecutorConfigurationError(
                    f"Herramienta no disponible: {step.tool}"
                )
            contract.validate_argument_names(set(step.args) | set(step.bindings))

    def build_payload(
        self,
        step: StepSpec,
        resolved_args: Mapping[str, Any],
    ) -> dict[str, Any]:
        contract = ACTION_CONTRACTS[(step.tool, step.action)]
        return contract.build_payload(resolved_args)

    def execute(
        self,
        step: StepSpec,
        resolved_args: Mapping[str, Any],
        runtime: WorkflowRuntimeState,
        *,
        approval_token: str | None = None,
    ) -> ToolResult:
        contract = ACTION_CONTRACTS[(step.tool, step.action)]
        payload = contract.build_payload(resolved_args)
        usage = self._validate_limits(step, payload, runtime)
        tool = vars(self.agent)[contract.agent_attribute]

        def execute_structured_tool():
            raw_result = tool.execute(payload, structured=True)
            self._validate_result(step, raw_result)
            return raw_result

        result = self.agent.execute_tool(
            step.tool,
            execute_structured_tool,
            action_name=step.action,
            important_args=payload,
            approval_token=approval_token,
            structured=True,
            require_approval=step.approval == "required",
        )
        self._validate_result(step, result)
        self._record_usage(runtime, usage)
        return result

    def complete_approved(
        self,
        step: StepSpec,
        resolved_args: Mapping[str, Any],
        runtime: WorkflowRuntimeState,
        approved_action: Callable[[], Any],
    ) -> ToolResult:
        payload = self.build_payload(step, resolved_args)
        usage = self._validate_limits(step, payload, runtime)
        result = approved_action()
        self._validate_result(step, result)
        self._record_usage(runtime, usage)
        return result

    @staticmethod
    def _validate_result(step: StepSpec, result: Any) -> None:
        if not isinstance(result, ToolResult):
            raise InvalidWorkflowToolResultError(
                f"{step.tool}/{step.action} no devolvió ToolResult"
            )
        if result.tool_name != step.tool:
            raise InvalidWorkflowToolResultError(
                f"ToolResult pertenece a {result.tool_name}, no a {step.tool}"
            )

    def _validate_limits(
        self,
        step: StepSpec,
        payload: Mapping[str, Any],
        runtime: WorkflowRuntimeState,
    ) -> dict[str, Any]:
        usage = {
            "inspected": None,
            "modified": None,
            "change_bytes": 0,
            "changed_lines": 0,
        }
        path = payload.get("path")
        normalized_path = self._normalize_path(path) if isinstance(path, str) else None

        if step.tool in {"code_reader", "code_analyzer"}:
            inspected = set(runtime.inspected_files)
            inspected.add(normalized_path)
            if len(inspected) > self.limits.max_inspected_files:
                raise WorkflowLimitExceededError("max_inspected_files excedido")
            usage["inspected"] = normalized_path

        if step.tool in {"patch_applier", "file_creator"}:
            modified = set(runtime.modified_files)
            modified.add(normalized_path)
            if len(modified) > self.limits.max_modified_files:
                raise WorkflowLimitExceededError("max_modified_files excedido")
            usage["modified"] = normalized_path

            new_content = (
                payload["content"]
                if step.tool == "file_creator"
                else payload["new_content"]
            )
            encoded_size = len(new_content.encode("utf-8"))
            if (
                step.tool == "file_creator"
                and encoded_size > self.limits.max_new_file_bytes
            ):
                raise WorkflowLimitExceededError("max_new_file_bytes excedido")
            if (
                runtime.total_change_bytes + encoded_size
                > self.limits.max_total_change_bytes
            ):
                raise WorkflowLimitExceededError("max_total_change_bytes excedido")
            usage["change_bytes"] = encoded_size

            if step.tool == "file_creator":
                changed_lines = len(new_content.splitlines())
            else:
                changed_lines = self._changed_lines(
                    payload["old_content"], payload["new_content"]
                )
            if runtime.changed_lines + changed_lines > self.limits.max_changed_lines:
                raise WorkflowLimitExceededError("max_changed_lines excedido")
            usage["changed_lines"] = changed_lines

        return usage

    @staticmethod
    def _record_usage(
        runtime: WorkflowRuntimeState,
        usage: Mapping[str, Any],
    ) -> None:
        if usage["inspected"] is not None:
            runtime.inspected_files.add(usage["inspected"])
        if usage["modified"] is not None:
            runtime.modified_files.add(usage["modified"])
        runtime.total_change_bytes += usage["change_bytes"]
        runtime.changed_lines += usage["changed_lines"]

    @staticmethod
    def _normalize_path(value: str) -> str:
        return Path(value).as_posix().casefold()

    @staticmethod
    def _changed_lines(old_content: str, new_content: str) -> int:
        matcher = SequenceMatcher(
            None,
            old_content.splitlines(),
            new_content.splitlines(),
            autojunk=False,
        )
        return sum(
            max(old_end - old_start, new_end - new_start)
            for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes()
            if tag != "equal"
        )
