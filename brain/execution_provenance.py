from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

from brain.workflow_limits import WorkflowLimits
from brain.workflow_plan import StepSpec, WorkflowPlan
from brain.workflow_runtime import WorkflowRuntimeState
from tools.tool_result import ToolResult


class ExecutionProvenanceError(ValueError):
    """A correction trigger does not belong to an authorized execution."""


_ISSUER = object()


@dataclass(frozen=True, eq=False)
class _ControlledExecutionCapability:
    session: Any
    plan_identity: tuple
    session_id: str
    session_epoch: int
    plan_id: str
    approved_plan: WorkflowPlan

    def __init__(
        self,
        issuer,
        session,
        plan_identity,
        session_id,
        session_epoch,
        plan_id,
        approved_plan,
    ):
        if issuer is not _ISSUER:
            raise ExecutionProvenanceError("Capacidad de ejecución no válida")
        object.__setattr__(self, "session", session)
        object.__setattr__(self, "plan_identity", plan_identity)
        object.__setattr__(self, "session_id", session_id)
        object.__setattr__(self, "session_epoch", session_epoch)
        object.__setattr__(self, "plan_id", plan_id)
        object.__setattr__(self, "approved_plan", approved_plan)


@dataclass(frozen=True, eq=False)
class _ControlledExecutionBinding:
    capability: _ControlledExecutionCapability
    plan: WorkflowPlan
    runtime: WorkflowRuntimeState

    def __init__(self, issuer, capability, plan, runtime):
        if issuer is not _ISSUER:
            raise ExecutionProvenanceError("Vinculación de ejecución no válida")
        object.__setattr__(self, "capability", capability)
        object.__setattr__(self, "plan", plan)
        object.__setattr__(self, "runtime", runtime)


@dataclass(frozen=True, eq=False)
class RedTestExecutionEvent:
    """Opaque identity-bound evidence of one authorized test execution."""

    binding: _ControlledExecutionBinding
    step: StepSpec
    result: ToolResult
    test_id: str
    attempt: int
    approval_request_id: str
    budget_snapshot: tuple

    def __init__(
        self,
        issuer,
        binding,
        step,
        result,
        test_id,
        attempt,
        approval_request_id,
        budget_snapshot,
    ):
        if issuer is not _ISSUER:
            raise ExecutionProvenanceError("Evento de prueba no válido")
        object.__setattr__(self, "binding", binding)
        object.__setattr__(self, "step", step)
        object.__setattr__(self, "result", result)
        object.__setattr__(self, "test_id", test_id)
        object.__setattr__(self, "attempt", attempt)
        object.__setattr__(self, "approval_request_id", approval_request_id)
        object.__setattr__(self, "budget_snapshot", budget_snapshot)


@dataclass(frozen=True, eq=False)
class _ConsumedRedTestAuthority:
    event: RedTestExecutionEvent
    carried_budget: tuple

    def __init__(self, issuer, event, carried_budget):
        if issuer is not _ISSUER:
            raise ExecutionProvenanceError("Autoridad correctiva no válida")
        object.__setattr__(self, "event", event)
        object.__setattr__(self, "carried_budget", carried_budget)


class ExecutionProvenanceRegistry:
    """Issue and consume opaque, identity-based workflow authority."""

    def __init__(self, limits: WorkflowLimits, *, session_owner):
        if not callable(session_owner):
            raise TypeError("session_owner debe ser invocable")
        self._limits = limits
        self._session_owner = session_owner
        self._lock = threading.RLock()
        self._pending: _ControlledExecutionCapability | None = None
        self._bindings: dict[int, _ControlledExecutionBinding] = {}
        self._events: dict[int, RedTestExecutionEvent] = {}
        self._consumed_events: set[int] = set()
        self._authorities: dict[int, _ConsumedRedTestAuthority] = {}
        self._attempts: dict[tuple[int, str], int] = {}
        self._correction_activations: dict[int, int] = {}

    def authorize(self, session: Any, plan: WorkflowPlan) -> object:
        if not isinstance(plan, WorkflowPlan):
            raise ExecutionProvenanceError("Plan controlado no válido")
        context = self._session_context(session)
        if session is not self._session_owner() or context is None:
            raise ExecutionProvenanceError("La sesión controlada no está ejecutándose")
        capability = _ControlledExecutionCapability(
            _ISSUER,
            session,
            plan.identity(),
            context[0],
            context[1],
            context[2],
            context[3],
        )
        with self._lock:
            self._pending = capability
        return capability

    def discard(self, capability: object) -> None:
        with self._lock:
            if self._pending is capability:
                self._pending = None

    def bind_pending(
        self,
        plan: WorkflowPlan,
        runtime: WorkflowRuntimeState,
    ) -> object | None:
        with self._lock:
            capability = self._pending
            if (
                capability is None
                or capability.plan_identity != plan.identity()
                or not self._valid_session_context(capability, runtime=None)
            ):
                return None
            self._pending = None
            binding = _ControlledExecutionBinding(
                _ISSUER,
                capability,
                plan,
                runtime,
            )
            self._bindings[id(runtime)] = binding
            return binding

    def issue_test_event(
        self,
        binding: object | None,
        plan: WorkflowPlan,
        runtime: WorkflowRuntimeState,
        step: StepSpec,
        result: ToolResult,
    ) -> RedTestExecutionEvent | None:
        with self._lock:
            if not self._valid_binding(binding, plan, runtime):
                return None
            step_runtime = runtime.steps.get(step.id)
            test_id = step.args.get("test_id")
            if (
                step.tool != "test_runner"
                or step.action != "run_tests"
                or type(test_id) is not str
                or not test_id
                or step_runtime is None
                or step_runtime.status != "running"
                or step_runtime.approval_status != "approved"
                or type(step_runtime.approval_request_id) is not str
                or not step_runtime.approval_request_id
                or not isinstance(result, ToolResult)
                or not self._is_focal_red_result(result)
            ):
                return None
            key = (id(runtime), step.id)
            attempt = self._attempts.get(key, 0) + 1
            self._attempts[key] = attempt
            event = RedTestExecutionEvent(
                _ISSUER,
                binding,
                step,
                result,
                test_id,
                attempt,
                step_runtime.approval_request_id,
                self._budget_snapshot(runtime),
            )
            self._events[id(event)] = event
            return event

    def consume_red_test_event(
        self,
        event: object,
        *,
        plan: WorkflowPlan,
        runtime: WorkflowRuntimeState,
        step: StepSpec,
        result: ToolResult,
    ) -> object:
        with self._lock:
            event_id = id(event)
            registered = self._events.get(event_id)
            if registered is not event or event_id in self._consumed_events:
                raise ExecutionProvenanceError(
                    "La evidencia de prueba no es auténtica o ya fue consumida"
                )
            binding = event.binding
            if (
                not self._valid_binding(binding, plan, runtime)
                or event.step is not step
                or event.result is not result
                or runtime.current_step_id != step.id
                or runtime.steps.get(step.id) is None
                or runtime.steps[step.id].status != "running"
                or runtime.steps[step.id].approval_status != "approved"
                or runtime.steps[step.id].approval_request_id
                != event.approval_request_id
                or event.budget_snapshot != self._budget_snapshot(runtime)
            ):
                raise ExecutionProvenanceError(
                    "La evidencia no pertenece a la ejecución activa"
                )
            self._validate_available_budget(runtime)
            activations = self._correction_activations.get(id(runtime), 0)
            if activations >= self._limits.max_correction_iterations:
                raise ExecutionProvenanceError(
                    "Se agotó max_correction_iterations"
                )

            # Consumption and budget reservation occur under the same lock.
            self._consumed_events.add(event_id)
            self._events.pop(event_id, None)
            self._correction_activations[id(runtime)] = activations + 1
            carried_budget = (
                frozenset(runtime.modified_files),
                runtime.total_change_bytes,
                runtime.changed_lines,
                activations,
            )
            authority = _ConsumedRedTestAuthority(
                _ISSUER,
                event,
                carried_budget,
            )
            self._authorities[id(authority)] = authority
            return authority

    def claim_consumed_authority(
        self,
        authority: object,
        *,
        event: object,
        plan: WorkflowPlan,
        runtime: WorkflowRuntimeState,
        step: StepSpec,
        result: ToolResult,
    ) -> dict[str, Any]:
        with self._lock:
            registered = self._authorities.pop(id(authority), None)
            if (
                registered is not authority
                or authority.event is not event
                or not self._valid_binding(event.binding, plan, runtime)
                or event.binding.plan is not plan
                or event.binding.runtime is not runtime
                or event.step is not step
                or event.result is not result
                or runtime.current_step_id != step.id
                or runtime.steps.get(step.id) is None
                or runtime.steps[step.id].status != "running"
                or runtime.steps[step.id].approval_status != "approved"
                or runtime.steps[step.id].approval_request_id
                != event.approval_request_id
            ):
                raise ExecutionProvenanceError(
                    "La autoridad correctiva no es auténtica"
                )
            modified, write_bytes, changed_lines, iterations = (
                authority.carried_budget
            )
            return {
                "modified_files": modified,
                "total_write_bytes": write_bytes,
                "total_changed_lines": changed_lines,
                "correction_iterations": iterations,
            }

    def _valid_binding(self, binding, plan, runtime) -> bool:
        return bool(
            isinstance(binding, _ControlledExecutionBinding)
            and self._bindings.get(id(runtime)) is binding
            and binding.plan is plan
            and binding.runtime is runtime
            and runtime.plan_identity == plan.identity()
            and self._valid_session_context(binding.capability, runtime=runtime)
        )

    def _validate_available_budget(self, runtime: WorkflowRuntimeState) -> None:
        if runtime.total_change_bytes >= self._limits.max_total_change_bytes:
            raise ExecutionProvenanceError("Se agotó max_total_change_bytes")
        if runtime.changed_lines >= self._limits.max_changed_lines:
            raise ExecutionProvenanceError("Se agotó max_changed_lines")

    @staticmethod
    def _session_context(session: Any):
        getter = getattr(session, "_execution_authority_context", None)
        try:
            context = getter() if callable(getter) else None
        except BaseException:
            return None
        if (
            not isinstance(context, tuple)
            or len(context) != 6
            or context[5] != "running"
        ):
            return None
        return context

    def _valid_session_context(self, capability, *, runtime) -> bool:
        context = self._session_context(capability.session)
        return bool(
            context is not None
            and context[0] == capability.session_id
            and context[1] == capability.session_epoch
            and context[2] == capability.plan_id
            and context[3] is capability.approved_plan
            and context[4] is runtime
        )

    @staticmethod
    def _budget_snapshot(runtime: WorkflowRuntimeState) -> tuple:
        return (
            tuple(sorted(runtime.modified_files)),
            runtime.total_change_bytes,
            runtime.changed_lines,
        )

    @staticmethod
    def _is_focal_red_result(result: ToolResult) -> bool:
        data = result.data
        return bool(
            result.tool_name == "test_runner"
            and result.status == "failed"
            and isinstance(data, dict)
            and type(data.get("tests_run")) is int
            and data["tests_run"] > 0
            and type(data.get("failures")) is int
            and type(data.get("errors")) is int
            and data["failures"] + data["errors"] > 0
            and data.get("returncode") != 0
            and data.get("timed_out") is not True
        )
