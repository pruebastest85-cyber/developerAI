from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from brain.workflow_plan import StepSpec, WorkflowPlan
from tools.tool_result import ToolResult


STEP_STATUSES = frozenset(
    {
        "pending",
        "running",
        "ok",
        "failed",
        "partial",
        "skipped",
        "awaiting_approval",
        "awaiting_correction",
    }
)
WORKFLOW_STATUSES = frozenset(
    {
        "running",
        "awaiting_approval",
        "awaiting_correction",
        "completed",
        "failed",
        "cancelled",
    }
)


class WorkflowRuntimeTransitionError(RuntimeError):
    """A workflow runtime transition is not valid from its current state."""


class WorkflowRuntimeMismatchError(ValueError):
    """A runtime belongs to a different declarative plan."""


@dataclass
class RuntimeStepState:
    step_id: str
    step_identity: tuple
    status: str = "pending"
    result: ToolResult | None = None
    resolved_args: dict[str, Any] | None = None
    reason: str | None = None
    attempts: int = 0
    correction_controller: Any = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.status not in STEP_STATUSES:
            raise ValueError(f"Estado de paso runtime no válido: {self.status}")

    def mark_running(self, resolved_args: dict[str, Any]) -> None:
        if self.status not in {"pending", "awaiting_approval"}:
            raise WorkflowRuntimeTransitionError(
                f"No se puede iniciar {self.step_id} desde {self.status}"
            )
        self.status = "running"
        self.resolved_args = resolved_args
        self.reason = None

    def mark_awaiting_approval(self, resolved_args: dict[str, Any]) -> None:
        if self.status not in {"pending", "running", "awaiting_correction"}:
            raise WorkflowRuntimeTransitionError(
                f"No se puede pausar {self.step_id} desde {self.status}"
            )
        self.status = "awaiting_approval"
        self.resolved_args = resolved_args
        self.reason = "approval_required"

    def mark_awaiting_correction(self, reason: str | None = None) -> None:
        if self.status not in {"running", "awaiting_approval"}:
            raise WorkflowRuntimeTransitionError(
                f"No se puede esperar corrección en {self.step_id} desde {self.status}"
            )
        self.status = "awaiting_correction"
        self.reason = reason or "correction_required"

    def clear_correction_controller(self) -> None:
        self.correction_controller = None

    def record_result(self, result: ToolResult) -> None:
        if self.status not in {"running", "awaiting_approval", "awaiting_correction"}:
            raise WorkflowRuntimeTransitionError(
                f"No se puede registrar resultado de {self.step_id} desde {self.status}"
            )
        if result.status not in {"ok", "failed", "partial"}:
            raise WorkflowRuntimeTransitionError(
                f"ToolResult no admite el estado {result.status}"
            )
        self.result = result
        self.status = result.status
        self.reason = result.error or result.message or None
        self.attempts += 1

    def mark_skipped(self, reason: str) -> None:
        if self.status != "pending":
            raise WorkflowRuntimeTransitionError(
                f"No se puede omitir {self.step_id} desde {self.status}"
            )
        self.status = "skipped"
        self.reason = reason

    def reset_for_explicit_repeat(self) -> None:
        if self.status != "ok":
            raise WorkflowRuntimeTransitionError(
                f"No se puede repetir {self.step_id} desde {self.status}"
            )
        self.status = "pending"
        self.result = None
        self.resolved_args = None
        self.reason = None


@dataclass
class WorkflowRuntimeState:
    plan_identity: tuple
    goal: str
    execution_order: tuple[str, ...]
    steps: dict[str, RuntimeStepState]
    results: dict[str, ToolResult] = field(default_factory=dict)
    current_step_id: str | None = None
    awaiting_step_id: str | None = None
    approval_request_id: str | None = None
    status: str = "running"
    inspected_files: set[str] = field(default_factory=set)
    modified_files: set[str] = field(default_factory=set)
    total_change_bytes: int = 0
    changed_lines: int = 0

    def __post_init__(self) -> None:
        if self.status not in WORKFLOW_STATUSES:
            raise ValueError(f"Estado de workflow no válido: {self.status}")

    @classmethod
    def create(cls, plan: WorkflowPlan, goal: str = "") -> "WorkflowRuntimeState":
        order = plan.execution_order()
        return cls(
            plan_identity=plan.identity(),
            goal=goal,
            execution_order=tuple(step.id for step in order),
            steps={
                step.id: RuntimeStepState(
                    step_id=step.id,
                    step_identity=step.identity(),
                )
                for step in plan.steps
            },
        )

    def validate_for_plan(self, plan: WorkflowPlan) -> None:
        if self.plan_identity != plan.identity():
            raise WorkflowRuntimeMismatchError(
                "El runtime pertenece a otro WorkflowPlan"
            )
        expected_ids = {step.id for step in plan.steps}
        if set(self.steps) != expected_ids:
            raise WorkflowRuntimeMismatchError(
                "Los pasos del runtime no coinciden con el plan"
            )
        for step in plan.steps:
            runtime_step = self.steps[step.id]
            if runtime_step.step_identity != step.identity():
                raise WorkflowRuntimeMismatchError(
                    f"La identidad runtime de {step.id} no coincide con el plan"
                )

    def prepare_resume(self, plan: WorkflowPlan) -> None:
        self.validate_for_plan(plan)
        if self.status == "cancelled":
            raise WorkflowRuntimeTransitionError(
                "Un workflow cancelado no puede reanudarse"
            )
        if self.status == "awaiting_correction":
            raise WorkflowRuntimeTransitionError(
                "Un workflow pendiente requiere submit_workflow_correction"
            )
        self.status = "running"
        self.current_step_id = None
        self.awaiting_step_id = None
        self.approval_request_id = None
        for step in plan.steps:
            runtime_step = self.steps[step.id]
            if runtime_step.status == "ok" and step.repeat_completed:
                runtime_step.reset_for_explicit_repeat()
                self.results.pop(step.id, None)

    def mark_awaiting_approval(self, step_id: str) -> None:
        if self.status != "running":
            raise WorkflowRuntimeTransitionError(
                f"No se puede pausar el workflow desde {self.status}"
            )
        self.status = "awaiting_approval"
        self.current_step_id = step_id
        self.awaiting_step_id = step_id

    def mark_awaiting_correction(self, step_id: str, reason: str | None = None) -> None:
        if self.status not in {"running", "awaiting_approval"}:
            raise WorkflowRuntimeTransitionError(
                f"No se puede esperar corrección desde {self.status}"
            )
        self.steps[step_id].mark_awaiting_correction(reason)
        self.status = "awaiting_correction"
        self.current_step_id = step_id
        self.awaiting_step_id = step_id
        self.approval_request_id = None

    def begin_correction_submission(self, step_id: str) -> None:
        if self.status != "awaiting_correction" or self.awaiting_step_id != step_id:
            raise WorkflowRuntimeTransitionError(
                f"El paso {step_id} no espera una corrección"
            )
        step = self.steps[step_id]
        if step.correction_controller is None:
            raise WorkflowRuntimeTransitionError(
                f"El paso {step_id} no conserva su controlador"
            )
        self.status = "running"
        self.current_step_id = step_id
        self.awaiting_step_id = None
        self.approval_request_id = None
        step.status = "running"
        step.reason = None

    def begin_approved_step(self, step_id: str) -> None:
        if self.status != "awaiting_approval" or self.awaiting_step_id != step_id:
            raise WorkflowRuntimeTransitionError(
                f"El paso {step_id} no es la aprobación pendiente"
            )
        self.status = "running"
        self.current_step_id = step_id
        self.awaiting_step_id = None
        self.approval_request_id = None
        resolved_args = self.steps[step_id].resolved_args
        if resolved_args is None:
            raise WorkflowRuntimeTransitionError(
                f"El paso {step_id} no conserva argumentos resueltos"
            )
        self.steps[step_id].mark_running(resolved_args)

    def record_result(self, step_id: str, result: ToolResult) -> None:
        self.steps[step_id].record_result(result)
        self.results[step_id] = result
        self.current_step_id = step_id
        self.awaiting_step_id = None
        self.approval_request_id = None
        self.status = "running"

    def mark_cancelled(self, step_id: str, reason: str) -> None:
        step = self.steps[step_id]
        if step.status != "awaiting_approval":
            raise WorkflowRuntimeTransitionError(
                f"No se puede cancelar {step_id} desde {step.status}"
            )
        step.status = "skipped"
        step.reason = reason
        step.clear_correction_controller()
        self.status = "cancelled"
        self.current_step_id = step_id
        self.awaiting_step_id = None
        self.approval_request_id = None

    def finish(self, plan: WorkflowPlan) -> None:
        if self.status != "running":
            raise WorkflowRuntimeTransitionError(
                f"No se puede finalizar el workflow desde {self.status}"
            )
        required_ok = all(
            not step.required or self.steps[step.id].status == "ok"
            for step in plan.steps
        )
        self.status = "completed" if required_ok else "failed"
        self.current_step_id = None
