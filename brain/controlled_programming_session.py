"""Thin, controlled orchestration for one model-generated programming workflow."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any

from brain.approval_controller import ApprovalController, ApprovalRequiredError
from brain.model_plan_review import (
    ModelPlanReviewError,
    ModelPlanReviewView,
)
from brain.workflow_plan import WorkflowPlan
from brain.workflow_report import WorkflowReport
from brain.workflow_report_renderer import WorkflowReportRenderer
from brain.workflow_runtime import WorkflowRuntimeState


class ProgrammingSessionState(str, Enum):
    IDLE = "idle"
    PENDING_PLAN = "pending_plan"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    AWAITING_CORRECTION = "awaiting_correction"
    COMPLETED = "completed"
    FAILED = "failed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


TERMINAL_SESSION_STATES = frozenset(
    {
        ProgrammingSessionState.COMPLETED,
        ProgrammingSessionState.FAILED,
        ProgrammingSessionState.REJECTED,
        ProgrammingSessionState.CANCELLED,
    }
)

_TRANSITIONS = {
    ProgrammingSessionState.IDLE: {ProgrammingSessionState.PENDING_PLAN},
    ProgrammingSessionState.PENDING_PLAN: {
        ProgrammingSessionState.RUNNING,
        ProgrammingSessionState.REJECTED,
        ProgrammingSessionState.CANCELLED,
    },
    ProgrammingSessionState.RUNNING: {
        ProgrammingSessionState.AWAITING_APPROVAL,
        ProgrammingSessionState.AWAITING_CORRECTION,
        ProgrammingSessionState.COMPLETED,
        ProgrammingSessionState.FAILED,
        ProgrammingSessionState.CANCELLED,
    },
    ProgrammingSessionState.AWAITING_APPROVAL: {
        ProgrammingSessionState.RUNNING,
        ProgrammingSessionState.AWAITING_APPROVAL,
        ProgrammingSessionState.AWAITING_CORRECTION,
        ProgrammingSessionState.COMPLETED,
        ProgrammingSessionState.FAILED,
        ProgrammingSessionState.CANCELLED,
    },
    ProgrammingSessionState.AWAITING_CORRECTION: {
        ProgrammingSessionState.RUNNING,
        ProgrammingSessionState.AWAITING_APPROVAL,
        ProgrammingSessionState.AWAITING_CORRECTION,
        ProgrammingSessionState.COMPLETED,
        ProgrammingSessionState.FAILED,
        ProgrammingSessionState.CANCELLED,
    },
}

_ERROR_MESSAGES = {
    "active_session": "Ya existe una sesión de programación activa.",
    "invalid_state": "La operación no es válida en el estado actual.",
    "invalid_command": "El comando de programación no es válido.",
    "invalid_request": "La solicitud de programación no es válida.",
    "invalid_plan": "El plan indicado no corresponde a la sesión activa.",
    "invalid_approval": "La aprobación no corresponde a esta sesión.",
    "approval_consumed": "La aprobación ya no está disponible.",
    "invalid_correction": "La propuesta de corrección no es válida.",
    "planning_failed": "No fue posible generar un plan válido.",
    "execution_failed": "La ejecución controlada no pudo completarse.",
    "inconsistent_runtime": "El estado de ejecución no es coherente con la sesión.",
}

_SESSION_COMMAND = re.compile(
    r"^(?P<verb>aprobar-plan|rechazar-plan|cancelar-plan|aprobar|rechazar|cancelar)"
    r" (?P<identifier>\S+)$",
    re.IGNORECASE,
)


class ControlledProgrammingSessionError(ValueError):
    __slots__ = ("code",)

    def __init__(self, code: str):
        safe_code = code if code in _ERROR_MESSAGES else "execution_failed"
        self.code = safe_code
        super().__init__(_ERROR_MESSAGES[safe_code])


@dataclass(frozen=True)
class ControlledProgrammingResult:
    """Immutable public projection; operational arguments never enter this object."""

    session_id: str
    state: ProgrammingSessionState
    plan_id: str | None = None
    plan: ModelPlanReviewView | None = None
    runtime_status: str | None = None
    pending_approval_request_id: str | None = None
    pending_correction_step_id: str | None = None
    report: WorkflowReport | None = None
    error_code: str | None = None
    automatic_commit_performed: bool = False
    automatic_push_performed: bool = False

    def __post_init__(self) -> None:
        if type(self.session_id) is not str or not self.session_id:
            raise ValueError("session_id inválido")
        if not isinstance(self.state, ProgrammingSessionState):
            raise TypeError("state debe ser ProgrammingSessionState")
        if self.automatic_commit_performed or self.automatic_push_performed:
            raise ValueError("La sesión controlada no puede hacer commit ni push")
        if self.state == ProgrammingSessionState.COMPLETED:
            if self.runtime_status != "completed" or self.report is None:
                raise ValueError("completed exige evidencia terminal positiva")

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_SESSION_STATES


class ControlledProgrammingSession:
    """Coordinate existing planning, review, execution, approval and report services."""

    def __init__(self, agent):
        self._agent = agent
        self._plan_review_controller = agent.model_plan_review_controller
        self._approval_controller = ApprovalController(agent)
        self._session_id = uuid.uuid4().hex
        self._state = ProgrammingSessionState.IDLE
        self._plan_id: str | None = None
        self._plan_view: ModelPlanReviewView | None = None
        self._workflow_plan: WorkflowPlan | None = None
        self._runtime: WorkflowRuntimeState | None = None
        self._approval_request_id: str | None = None
        self._report: WorkflowReport | None = None
        self._error_code: str | None = None
        self._terminal_logged = False

    @property
    def approval_controller(self) -> ApprovalController:
        return self._approval_controller

    @staticmethod
    def is_controlled_message(message: Any) -> bool:
        if type(message) is not str:
            return False
        text = message.strip()
        lowered = text.lower()
        return (
            lowered.startswith("programar: ")
            or lowered.startswith("corregir: ")
            or lowered in {"estado-programacion", "informe-programacion"}
            or _SESSION_COMMAND.fullmatch(text) is not None
        )

    def should_handle_command(self, message: Any) -> bool:
        """Claim ambiguous approval commands only while this session owns them."""
        if type(message) is not str:
            return False
        command = _SESSION_COMMAND.fullmatch(message.strip())
        if command is None:
            return False
        verb = command.group("verb").lower()
        if verb.endswith("-plan"):
            return self._state == ProgrammingSessionState.PENDING_PLAN
        return self._state == ProgrammingSessionState.AWAITING_APPROVAL

    def handle_message(self, message: str) -> str:
        """Route the closed conversational namespace and render its public result."""
        if type(message) is not str:
            return self._public_error("invalid_command")
        text = message.strip()
        lowered = text.lower()
        try:
            if lowered.startswith("programar: "):
                self.submit(text[len("programar: ") :])
            elif lowered == "estado-programacion" or lowered == "informe-programacion":
                pass
            elif lowered.startswith("corregir: "):
                try:
                    arguments = json.loads(text[len("corregir: ") :])
                except (json.JSONDecodeError, UnicodeError):
                    raise ControlledProgrammingSessionError("invalid_correction") from None
                self.submit_correction(arguments)
            else:
                command = _SESSION_COMMAND.fullmatch(text)
                if command is None:
                    raise ControlledProgrammingSessionError("invalid_command")
                verb = command.group("verb").lower()
                identifier = command.group("identifier")
                if verb == "aprobar-plan":
                    self.approve_plan(identifier)
                elif verb == "rechazar-plan":
                    self.reject_plan(identifier)
                elif verb == "cancelar-plan":
                    self.cancel_plan(identifier)
                else:
                    self.process_operational_command(verb, identifier)
            return self.render_current_report()
        except ControlledProgrammingSessionError as exc:
            return self._public_error(exc.code)
        except ModelPlanReviewError:
            return self._public_error("invalid_plan")
        except Exception:
            if self._state not in {
                ProgrammingSessionState.IDLE,
                *TERMINAL_SESSION_STATES,
            }:
                self._fail("execution_failed")
            return self._public_error("execution_failed")

    def submit(self, user_request: str) -> ControlledProgrammingResult:
        if self._state not in {ProgrammingSessionState.IDLE, *TERMINAL_SESSION_STATES}:
            raise ControlledProgrammingSessionError("active_session")
        if type(user_request) is not str or not user_request.strip():
            raise ControlledProgrammingSessionError("invalid_request")
        if self._agent.model_planning_service is None:
            raise ControlledProgrammingSessionError("planning_failed")
        if self._state in TERMINAL_SESSION_STATES:
            self._begin_new_cycle()

        try:
            planning_result = self._agent.model_planning_service.plan(user_request)
            plan_view = self._plan_review_controller.register(planning_result)
        except (ModelPlanReviewError, TypeError, ValueError):
            raise ControlledProgrammingSessionError("planning_failed") from None
        except Exception:
            raise ControlledProgrammingSessionError("planning_failed") from None

        self._reset_runtime_fields()
        self._plan_id = plan_view.plan_id
        self._plan_view = plan_view
        self._workflow_plan = planning_result.workflow
        self._transition(ProgrammingSessionState.PENDING_PLAN, "plan_registered")
        return self.current_result()

    def approve_plan(self, plan_id: str) -> ControlledProgrammingResult:
        self._require_state(ProgrammingSessionState.PENDING_PLAN)
        self._require_plan_id(plan_id)
        self._transition(ProgrammingSessionState.RUNNING, "plan_approved")
        try:
            runtime = self._plan_review_controller.approve(plan_id)
        except ApprovalRequiredError as error:
            runtime = self._agent.execution_engine.last_workflow_runtime
            self._accept_operational_pause(error, runtime)
            return self.current_result()
        except ModelPlanReviewError:
            self._fail("invalid_plan")
            raise
        except BaseException:
            self._runtime = self._agent.execution_engine.last_workflow_runtime
            self._fail("execution_failed")
            raise

        self._runtime = runtime
        self._synchronize_runtime()
        return self.current_result()

    def reject_plan(self, plan_id: str) -> ControlledProgrammingResult:
        self._require_state(ProgrammingSessionState.PENDING_PLAN)
        self._require_plan_id(plan_id)
        self._plan_review_controller.reject(plan_id)
        self._transition(ProgrammingSessionState.REJECTED, "plan_rejected")
        return self.current_result()

    def cancel_plan(self, plan_id: str) -> ControlledProgrammingResult:
        self._require_state(ProgrammingSessionState.PENDING_PLAN)
        self._require_plan_id(plan_id)
        self._plan_review_controller.cancel(plan_id)
        self._transition(ProgrammingSessionState.CANCELLED, "plan_cancelled")
        return self.current_result()

    def process_operational_command(
        self,
        action: str,
        request_id: str,
    ) -> ControlledProgrammingResult:
        self._require_state(ProgrammingSessionState.AWAITING_APPROVAL)
        self._validate_active_approval(request_id)

        if action == "aprobar":
            self._transition(ProgrammingSessionState.RUNNING, "operation_approved")
            result = self._approval_controller.approve(request_id)
            self._approval_request_id = None
            if result.status == "awaiting_approval":
                self._bind_next_approval(result.request_id)
            elif result.status != "approved":
                self._fail("execution_failed")
            else:
                self._synchronize_runtime()
        elif action in {"rechazar", "cancelar"}:
            operation = (
                self._approval_controller.reject
                if action == "rechazar"
                else self._approval_controller.cancel
            )
            result = operation(request_id)
            self._approval_request_id = None
            if result.status not in {"rejected", "cancelled"}:
                raise ControlledProgrammingSessionError("approval_consumed")
            self._synchronize_runtime(expected_cancel=True)
        else:
            raise ControlledProgrammingSessionError("invalid_command")
        return self.current_result()

    def submit_correction(self, arguments: dict[str, Any]) -> ControlledProgrammingResult:
        self._require_state(ProgrammingSessionState.AWAITING_CORRECTION)
        if type(arguments) is not dict:
            raise ControlledProgrammingSessionError("invalid_correction")
        plan, runtime = self._require_plan_runtime()
        self._transition(ProgrammingSessionState.RUNNING, "correction_submitted")
        try:
            updated = self._agent.execution_engine.submit_workflow_correction(
                plan,
                runtime,
                arguments,
            )
        except ApprovalRequiredError as error:
            self._accept_operational_pause(error, runtime)
            return self.current_result()
        except (TypeError, ValueError):
            if runtime.status == "awaiting_correction":
                self._transition(
                    ProgrammingSessionState.AWAITING_CORRECTION,
                    "correction_rejected",
                )
            else:
                self._fail("invalid_correction")
            raise ControlledProgrammingSessionError("invalid_correction") from None
        except BaseException:
            self._fail("execution_failed")
            raise
        self._runtime = updated
        self._synchronize_runtime()
        return self.current_result()

    def current_result(self) -> ControlledProgrammingResult:
        runtime_status = self._runtime.status if self._runtime is not None else None
        correction_step = (
            self._runtime.awaiting_step_id
            if self._state == ProgrammingSessionState.AWAITING_CORRECTION
            and self._runtime is not None
            else None
        )
        return ControlledProgrammingResult(
            session_id=self._session_id,
            state=self._state,
            plan_id=self._plan_id,
            plan=self._plan_view,
            runtime_status=runtime_status,
            pending_approval_request_id=self._approval_request_id,
            pending_correction_step_id=correction_step,
            report=self._report,
            error_code=self._error_code,
        )

    def get_current_report(self) -> ControlledProgrammingResult:
        return self.current_result()

    def render_current_report(self) -> str:
        result = self.current_result()
        if result.state == ProgrammingSessionState.IDLE:
            return "No hay una sesión de programación activa."
        if result.report is not None:
            return WorkflowReportRenderer.render_markdown(result.report)
        if result.state == ProgrammingSessionState.PENDING_PLAN and result.plan is not None:
            return result.plan.text
        if result.state == ProgrammingSessionState.REJECTED:
            return "El plan fue rechazado. No se inició ninguna ejecución."
        if result.state == ProgrammingSessionState.CANCELLED:
            return "La sesión fue cancelada. No se ejecutarán más operaciones."
        if result.state == ProgrammingSessionState.AWAITING_APPROVAL:
            return (
                "Se requiere aprobación operacional explícita para continuar. "
                f"Solicitud: `{result.pending_approval_request_id}`."
            )
        if result.state == ProgrammingSessionState.AWAITING_CORRECTION:
            return (
                "El workflow espera una propuesta de corrección estructurada para "
                f"el paso `{result.pending_correction_step_id}`."
            )
        if result.state == ProgrammingSessionState.FAILED:
            return self._public_error(result.error_code or "execution_failed")
        return f"Estado de programación: {result.state.value}."

    def get_session_state(self) -> str:
        return self._state.value

    def is_session_active(self) -> bool:
        return self._state not in {ProgrammingSessionState.IDLE, *TERMINAL_SESSION_STATES}

    def close(self) -> None:
        if self.is_session_active():
            raise ControlledProgrammingSessionError("active_session")
        self._plan_id = None
        self._plan_view = None
        self._workflow_plan = None
        self._reset_runtime_fields()
        self._state = ProgrammingSessionState.IDLE
        self._terminal_logged = False

    def _accept_operational_pause(
        self,
        error: ApprovalRequiredError,
        runtime: WorkflowRuntimeState | None,
    ) -> None:
        if (
            not isinstance(runtime, WorkflowRuntimeState)
            or runtime.status != "awaiting_approval"
        ):
            self._runtime = runtime
            self._fail("inconsistent_runtime")
            raise ControlledProgrammingSessionError("inconsistent_runtime")
        plan, _ = self._require_plan_runtime(runtime=runtime)
        runtime.validate_for_plan(plan)
        self._runtime = runtime
        requested = self._approval_controller.request_operation(
            tool_name=error.tool_name,
            action_name=error.action_name,
            important_args=error.important_args,
            execute=error.execute,
            description=f"{error.tool_name}.{error.action_name}",
            force_approval=error.force_approval,
            on_cancel=error.on_cancel,
            on_request=error.on_request,
        )
        if runtime.approval_request_id != requested.request_id:
            self._fail("inconsistent_runtime")
            raise ControlledProgrammingSessionError("inconsistent_runtime")
        self._approval_request_id = requested.request_id
        self._transition(ProgrammingSessionState.AWAITING_APPROVAL, "operation_paused")

    def _bind_next_approval(self, request_id: str) -> None:
        if (
            self._runtime is None
            or self._runtime.status != "awaiting_approval"
            or self._runtime.approval_request_id != request_id
        ):
            self._fail("inconsistent_runtime")
            raise ControlledProgrammingSessionError("inconsistent_runtime")
        self._approval_request_id = request_id
        self._transition(ProgrammingSessionState.AWAITING_APPROVAL, "operation_paused")

    def _synchronize_runtime(self, *, expected_cancel: bool = False) -> None:
        plan, runtime = self._require_plan_runtime()
        runtime.validate_for_plan(plan)
        status = runtime.status
        if status == "completed":
            self._report = self._agent.execution_engine.build_workflow_report(plan, runtime)
            self._transition(ProgrammingSessionState.COMPLETED, "session_completed")
        elif status == "failed":
            self._report = self._agent.execution_engine.build_workflow_report(plan, runtime)
            self._fail("execution_failed")
        elif status == "cancelled":
            self._report = self._agent.execution_engine.build_workflow_report(plan, runtime)
            self._transition(ProgrammingSessionState.CANCELLED, "session_cancelled")
        elif status == "awaiting_correction":
            self._report = self._agent.execution_engine.build_workflow_report(plan, runtime)
            self._transition(
                ProgrammingSessionState.AWAITING_CORRECTION,
                "correction_required",
            )
        elif status == "awaiting_approval":
            request_id = runtime.approval_request_id
            if not request_id:
                self._fail("inconsistent_runtime")
                raise ControlledProgrammingSessionError("inconsistent_runtime")
            self._approval_request_id = request_id
            self._transition(
                ProgrammingSessionState.AWAITING_APPROVAL,
                "operation_paused",
            )
        elif expected_cancel:
            self._fail("inconsistent_runtime")
        else:
            self._fail("inconsistent_runtime")
            raise ControlledProgrammingSessionError("inconsistent_runtime")

    def _validate_active_approval(self, request_id: str) -> None:
        if (
            type(request_id) is not str
            or not request_id
            or self._runtime is None
            or self._runtime.status != "awaiting_approval"
            or self._approval_request_id != request_id
            or self._runtime.approval_request_id != request_id
            or self._runtime.awaiting_step_id is None
            or self._approval_controller.get_pending(request_id) is None
        ):
            raise ControlledProgrammingSessionError("invalid_approval")

    def _require_plan_runtime(
        self,
        *,
        runtime: WorkflowRuntimeState | None = None,
    ) -> tuple[WorkflowPlan, WorkflowRuntimeState]:
        selected_runtime = self._runtime if runtime is None else runtime
        if (
            not isinstance(self._workflow_plan, WorkflowPlan)
            or not isinstance(selected_runtime, WorkflowRuntimeState)
        ):
            raise ControlledProgrammingSessionError("inconsistent_runtime")
        return self._workflow_plan, selected_runtime

    def _require_plan_id(self, plan_id: str) -> None:
        if type(plan_id) is not str or plan_id != self._plan_id:
            raise ControlledProgrammingSessionError("invalid_plan")

    def _require_state(self, state: ProgrammingSessionState) -> None:
        if self._state != state:
            raise ControlledProgrammingSessionError("invalid_state")

    def _transition(self, target: ProgrammingSessionState, event: str) -> None:
        if self._state in TERMINAL_SESSION_STATES:
            raise ControlledProgrammingSessionError("invalid_state")
        if target not in _TRANSITIONS.get(self._state, set()):
            raise ControlledProgrammingSessionError("invalid_state")
        self._state = target
        self._log_transition(event)
        if target in TERMINAL_SESSION_STATES and not self._terminal_logged:
            self._terminal_logged = True
            self._log_transition("session_terminal")

    def _fail(self, code: str) -> None:
        self._error_code = code if code in _ERROR_MESSAGES else "execution_failed"
        if self._state not in TERMINAL_SESSION_STATES:
            self._transition(ProgrammingSessionState.FAILED, "session_failed")

    def _log_transition(self, event: str) -> None:
        self._agent.action_logger.log(
            "controlled_programming_session",
            params={
                "event": event,
                "state": self._state.value,
                "has_plan": self._plan_id is not None,
                "has_runtime": self._runtime is not None,
            },
            result={"status": self._state.value},
        )

    def _reset_runtime_fields(self) -> None:
        self._runtime = None
        self._approval_request_id = None
        self._report = None
        self._error_code = None
        self._terminal_logged = False

    def _begin_new_cycle(self) -> None:
        self._session_id = uuid.uuid4().hex
        self._state = ProgrammingSessionState.IDLE
        self._plan_id = None
        self._plan_view = None
        self._workflow_plan = None
        self._reset_runtime_fields()

    @staticmethod
    def _public_error(code: str) -> str:
        safe_code = code if code in _ERROR_MESSAGES else "execution_failed"
        return f"Error de sesión ({safe_code}): {_ERROR_MESSAGES[safe_code]}"
