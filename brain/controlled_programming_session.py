"""Thin, controlled orchestration for one model-generated programming workflow."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any

from brain.approval_controller import ApprovalController, ApprovalRequiredError
from brain.model_plan_review import (
    ModelPlanReviewError,
    ModelPlanReviewView,
)
from brain.change_proposal import ChangeProposal, TestSpec
from brain.model_correction import (
    AdaptedModelCorrection,
    ModelCorrectionContext,
    ModelCorrectionError,
    ModelCorrectionGenerationPolicy,
    ModelCorrectionGenerationResult,
    ModelCorrectionSource,
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
    ProgrammingSessionState.IDLE: {
        ProgrammingSessionState.PENDING_PLAN,
        ProgrammingSessionState.FAILED,
    },
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
    "correction_budget_exhausted": "Se agotó el presupuesto de correcciones del modelo.",
    "invalid_model_correction": "El modelo no produjo una corrección válida.",
    "invalid_correction_approval": "La aprobación de corrección ya no es válida.",
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
    workflow_runtime_id: str | None = None
    correction_applications: int = 0
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


@dataclass(frozen=True)
class _PendingModelCorrection:
    session_id: str
    workflow_runtime_id: str
    correction_runtime_id: str
    step_id: str
    draft_id: str
    proposal_id: str
    public_summary: str
    files: tuple[tuple[str, str], ...]
    budget: tuple[tuple[str, int], ...]
    tests: tuple[str, ...]
    plan_identity: str
    runtime_snapshot: tuple[Any, ...] | None = None

class ControlledProgrammingSession:
    """Coordinate existing planning, review, execution, approval and report services."""

    def __init__(self, agent):
        self._agent = agent
        self._plan_review_controller = agent.model_plan_review_controller
        self._approval_controller = ApprovalController(agent)
        self._session_id = uuid.uuid4().hex
        self._execution_epoch = 0
        self._state = ProgrammingSessionState.IDLE
        self._plan_id: str | None = None
        self._plan_view: ModelPlanReviewView | None = None
        self._workflow_plan: WorkflowPlan | None = None
        self._runtime: WorkflowRuntimeState | None = None
        self._approval_request_id: str | None = None
        self._report: WorkflowReport | None = None
        self._error_code: str | None = None
        self._terminal_logged = False
        self._generation_policy = ModelCorrectionGenerationPolicy()
        self._generation_attempts = 0
        self._invalid_drafts = 0
        self._rejected_proposals = 0
        self._seen_draft_ids: set[str] = set()
        self._seen_proposal_ids: set[str] = set()
        self._pending_model_correction: _PendingModelCorrection | None = None

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
            self._fail("planning_failed")
            raise ControlledProgrammingSessionError("planning_failed")
        if self._state in TERMINAL_SESSION_STATES:
            self._begin_new_cycle()
        self._execution_epoch += 1

        try:
            planning_result = self._agent.model_planning_service.plan(user_request)
            plan_view = self._plan_review_controller.register(planning_result)
        except (ModelPlanReviewError, TypeError, ValueError):
            self._fail("planning_failed")
            raise ControlledProgrammingSessionError("planning_failed") from None
        except Exception:
            self._fail("planning_failed")
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
        capability = self._agent.execution_engine._authorize_controlled_execution(
            self,
            self._workflow_plan,
        )
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
        finally:
            self._agent.execution_engine._discard_controlled_execution(
                capability
            )

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
            self._validate_pending_model_correction()
            self._transition(ProgrammingSessionState.RUNNING, "operation_approved")
            result = self._approval_controller.approve(request_id)
            self._approval_request_id = None
            if result.status == "awaiting_approval":
                self._bind_next_approval(result.request_id)
            elif result.status != "approved":
                code = self._operational_approval_failure_code()
                self._pending_model_correction = None
                self._fail(code)
            else:
                self._pending_model_correction = None
                self._synchronize_runtime()
        elif action in {"rechazar", "cancelar"}:
            rejected_model_correction = self._pending_model_correction is not None
            operation = (
                self._approval_controller.reject
                if action == "rechazar"
                else self._approval_controller.cancel
            )
            result = operation(request_id)
            if (
                action == "rechazar"
                and self._pending_model_correction is not None
            ):
                self._rejected_proposals += 1
            self._pending_model_correction = None
            self._approval_request_id = None
            if result.status not in {"rejected", "cancelled"}:
                raise ControlledProgrammingSessionError("approval_consumed")
            if (
                rejected_model_correction
                and self._runtime is not None
                and self._runtime.status == "awaiting_correction"
            ):
                plan, runtime = self._require_plan_runtime()
                self._agent.execution_engine.abort_workflow_correction(
                    plan,
                    runtime,
                    action,
                )
            self._synchronize_runtime(expected_cancel=True)
        else:
            raise ControlledProgrammingSessionError("invalid_command")
        return self.current_result()

    def _operational_approval_failure_code(self) -> str:
        binding = self._pending_model_correction
        if binding is None or self._runtime is None:
            return "execution_failed"
        step_runtime = self._runtime.steps.get(binding.step_id)
        correction_runtime = (
            step_runtime.correction_runtime
            if step_runtime is not None
            else None
        )
        reason = (
            correction_runtime.terminal_reason
            if correction_runtime is not None
            else None
        )
        if reason in {
            "invalid_correction_approval",
            "correction_budget_exhausted",
        }:
            return reason
        return "execution_failed"

    def submit_correction(
        self,
        arguments: dict[str, Any] | ChangeProposal,
    ) -> ControlledProgrammingResult:
        self._require_state(ProgrammingSessionState.AWAITING_CORRECTION)
        if type(arguments) is not dict and type(arguments) is not ChangeProposal:
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
        correction_applications = 0
        if self._runtime is not None:
            correction_applications = sum(
                len(step.correction_runtime.applied_proposal_ids)
                for step in self._runtime.steps.values()
                if step.correction_runtime is not None
            )
        return ControlledProgrammingResult(
            session_id=self._session_id,
            state=self._state,
            plan_id=self._plan_id,
            plan=self._plan_view,
            runtime_status=runtime_status,
            workflow_runtime_id=(
                self._runtime.runtime_id if self._runtime is not None else None
            ),
            correction_applications=correction_applications,
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

    def _execution_authority_context(self) -> tuple:
        """Return identity-only state consumed by the internal execution registry."""
        return (
            self._session_id,
            self._execution_epoch,
            self._plan_id,
            self._workflow_plan,
            self._runtime,
            self._state.value,
        )

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
        important_args = error.important_args
        binding = self._pending_model_correction
        if binding is not None:
            step_runtime = runtime.steps.get(binding.step_id)
            correction_runtime = (
                step_runtime.correction_runtime
                if step_runtime is not None
                else None
            )
            if correction_runtime is None:
                self._fail("inconsistent_runtime")
                raise ControlledProgrammingSessionError("inconsistent_runtime")
            binding = replace(
                binding,
                runtime_snapshot=self._correction_authority_snapshot(
                    correction_runtime
                ),
            )
            self._pending_model_correction = binding
            important_args = {
                "session_id": binding.session_id,
                "workflow_runtime_id": binding.workflow_runtime_id,
                "correction_runtime_id": binding.correction_runtime_id,
                "step_id": binding.step_id,
                "draft_id": binding.draft_id,
                "proposal_id": binding.proposal_id,
                "summary": binding.public_summary,
                "files": [list(item) for item in binding.files],
                "budget": dict(binding.budget),
                "tests": list(binding.tests),
            }
        requested = self._approval_controller.request_operation(
            tool_name=error.tool_name,
            action_name=error.action_name,
            important_args=important_args,
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
            if self._agent.model_correction_service is not None:
                self._generate_model_correction()
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

    def _generate_model_correction(self) -> None:
        plan, runtime = self._require_plan_runtime()
        if (
            self._state != ProgrammingSessionState.AWAITING_CORRECTION
            or runtime.status != "awaiting_correction"
            or runtime.awaiting_step_id is None
        ):
            raise ControlledProgrammingSessionError("invalid_correction")
        step_id = runtime.awaiting_step_id
        step = next((item for item in plan.steps if item.id == step_id), None)
        step_runtime = runtime.steps.get(step_id)
        correction_runtime = (
            step_runtime.correction_runtime if step_runtime is not None else None
        )
        if (
            step is None
            or correction_runtime is None
            or correction_runtime.status != "awaiting_correction"
            or not (
                step.tool == "correction_workflow"
                or (
                    step.tool == "test_runner"
                    and step.action == "run_tests"
                    and type(step.args.get("test_id")) is str
                    and correction_runtime.initial_plan_identity
                    == runtime.plan_identity
                )
            )
        ):
            raise ControlledProgrammingSessionError("inconsistent_runtime")

        while self._state == ProgrammingSessionState.AWAITING_CORRECTION:
            if (
                self._generation_attempts
                >= self._generation_policy.max_generations
                or self._invalid_drafts
                >= self._generation_policy.max_invalid_drafts
                or self._rejected_proposals
                >= self._generation_policy.max_rejected_proposals
            ):
                self._agent.execution_engine.abort_workflow_correction(
                    plan,
                    runtime,
                    "correction_budget_exhausted",
                )
                self._runtime = runtime
                self._synchronize_runtime()
                return
            self._generation_attempts += 1
            context = self._model_correction_context(
                runtime,
                step_id,
                step.goal,
                correction_runtime,
            )
            try:
                generated = self._agent.model_correction_service.propose(context)
                if type(generated) is not ModelCorrectionGenerationResult:
                    raise ModelCorrectionError("invalid_model_correction")
                tests = self._trusted_correction_tests(correction_runtime)
                if (
                    len(tests)
                    > self._generation_policy.max_test_executions_per_application
                ):
                    raise ModelCorrectionError("correction_limit_exceeded")
                adapted = self._agent.model_correction_adapter.adapt(
                    generated.draft,
                    tests=tests,
                )
                self._accept_model_correction(
                    runtime,
                    step_id,
                    correction_runtime.runtime_id,
                    generated,
                    adapted,
                )
                return
            except ModelCorrectionError as exc:
                self._invalid_drafts += 1
                self._log_model_correction(
                    "model_correction_rejected",
                    exc.code,
                )

    def _accept_model_correction(
        self,
        runtime: WorkflowRuntimeState,
        step_id: str,
        correction_runtime_id: str,
        generated: ModelCorrectionGenerationResult,
        adapted: AdaptedModelCorrection,
    ) -> None:
        draft_id = generated.draft.draft_id
        proposal_id = adapted.proposal.proposal_id
        if draft_id in self._seen_draft_ids or proposal_id in self._seen_proposal_ids:
            raise ModelCorrectionError("duplicate_correction")
        self._seen_draft_ids.add(draft_id)
        self._seen_proposal_ids.add(proposal_id)
        budget = adapted.validated.calculated_budget.canonical_dict()
        self._pending_model_correction = _PendingModelCorrection(
            session_id=self._session_id,
            workflow_runtime_id=runtime.runtime_id,
            correction_runtime_id=correction_runtime_id,
            step_id=step_id,
            draft_id=draft_id,
            proposal_id=proposal_id,
            public_summary=self._public_correction_summary(
                adapted.validated,
                adapted.proposal.tests,
            ),
            files=tuple(
                (item.relative_path, item.operation)
                for item in adapted.validated.resolved_changes
            ),
            budget=tuple(sorted(budget.items())),
            tests=tuple(
                (
                    "full"
                    if test.scope == "full"
                    else f"focused:{','.join(test.targets)}"
                )
                for test in adapted.proposal.tests
            ),
            plan_identity=runtime.plan_identity,
        )
        self._log_model_correction("model_correction_generated", "ok")
        self.submit_correction(adapted.proposal)

    def _model_correction_context(
        self,
        runtime: WorkflowRuntimeState,
        step_id: str,
        goal: str,
        correction_runtime,
    ) -> ModelCorrectionContext:
        limits = correction_runtime.limits
        current = correction_runtime.current_proposal
        sources = ()
        if current is not None:
            sources = tuple(
                ModelCorrectionSource(
                    path=change.path,
                    current_content=change.new_content,
                    current_sha256=change.content_sha256,
                )
                for change in current.changes
            )
        else:
            sources = self._trusted_runtime_sources(runtime)
        failed_test_ids = ()
        if correction_runtime.test_runs:
            data = correction_runtime.test_runs[-1].result.data
            if isinstance(data, dict):
                candidates = (
                    data.get("failed_test_ids", []),
                    data.get("error_test_ids", []),
                )
                failed_test_ids = tuple(
                    item
                    for group in candidates
                    if isinstance(group, list)
                    for item in group
                    if type(item) is str and 0 < len(item) <= 512
                )[:32]
        return ModelCorrectionContext(
            session_id=self._session_id,
            runtime_id=runtime.runtime_id,
            step_id=step_id,
            goal=goal or runtime.goal or "correction",
            failure_code="tests_failed",
            remaining_files=max(
                0,
                limits.max_modified_files - len(correction_runtime.modified_files),
            ),
            remaining_bytes=max(
                0,
                limits.max_total_change_bytes
                - correction_runtime.total_write_bytes,
            ),
            remaining_lines=max(
                0,
                limits.max_changed_lines
                - correction_runtime.total_changed_lines,
            ),
            sources=sources,
            failed_test_ids=failed_test_ids,
        )

    @staticmethod
    def _trusted_correction_tests(correction_runtime) -> tuple[TestSpec, ...]:
        proposal = correction_runtime.current_proposal
        if proposal is None:
            focused = tuple(
                run.test_spec
                for run in correction_runtime.test_runs
                if isinstance(run.test_spec, TestSpec)
                and run.test_spec.scope == "focused"
            )
            return (*focused, TestSpec("full"))
        focused = tuple(test for test in proposal.tests if test.scope == "focused")
        return (*focused, TestSpec("full"))

    def _trusted_runtime_sources(
        self,
        runtime: WorkflowRuntimeState,
    ) -> tuple[ModelCorrectionSource, ...]:
        plan = self._workflow_plan
        adapter = self._agent.model_correction_adapter
        if plan is None or adapter is None:
            return ()
        sources = []
        for step in plan.steps:
            if step.tool != "code_reader" or step.action != "read_file":
                continue
            result = runtime.results.get(step.id)
            path = step.args.get("path")
            if (
                result is None
                or result.status != "ok"
                or type(result.data) is not str
                or type(path) is not str
            ):
                continue
            try:
                resolved = adapter.path_policy.resolve_for_read(path)
                payload = resolved.absolute.read_bytes()
                current = payload.decode("utf-8")
            except (OSError, UnicodeError, ValueError):
                continue
            if current != result.data:
                continue
            sources.append(
                ModelCorrectionSource(
                    path=resolved.relative.as_posix(),
                    current_content=current,
                    current_sha256=hashlib.sha256(payload).hexdigest(),
                )
            )
            if len(sources) >= 5:
                break
        return tuple(sources)

    @staticmethod
    def _public_correction_summary(validated, tests) -> str:
        """Build an allowlisted summary without model-authored prose or content."""
        files = ", ".join(
            f"{item.operation}:{item.relative_path}"
            for item in validated.resolved_changes
        )
        budget = validated.calculated_budget
        scopes = ", ".join(test.scope for test in tests)
        return (
            f"{len(validated.resolved_changes)} cambio(s) [{files}]; "
            f"{budget.write_bytes} bytes; {budget.changed_lines} lÃ­nea(s); "
            f"pruebas [{scopes}]"
        )

    def _validate_pending_model_correction(self) -> None:
        binding = self._pending_model_correction
        if binding is None:
            return
        plan, runtime = self._require_plan_runtime()
        step_runtime = runtime.steps.get(binding.step_id)
        correction_runtime = (
            step_runtime.correction_runtime if step_runtime is not None else None
        )
        pending = self._approval_controller.get_pending(
            self._approval_request_id
        )
        expected_args = {
            "session_id": binding.session_id,
            "workflow_runtime_id": binding.workflow_runtime_id,
            "correction_runtime_id": binding.correction_runtime_id,
            "step_id": binding.step_id,
            "draft_id": binding.draft_id,
            "proposal_id": binding.proposal_id,
            "summary": binding.public_summary,
            "files": [list(item) for item in binding.files],
            "budget": dict(binding.budget),
            "tests": list(binding.tests),
        }
        if (
            binding.session_id != self._session_id
            or binding.workflow_runtime_id != runtime.runtime_id
            or binding.plan_identity != runtime.plan_identity
            or runtime.plan_identity != plan.identity()
            or runtime.awaiting_step_id != binding.step_id
            or correction_runtime is None
            or correction_runtime.runtime_id != binding.correction_runtime_id
            or correction_runtime.validated_proposal is None
            or correction_runtime.validated_proposal.proposal_id
            != binding.proposal_id
            or not self._correction_file_preconditions_hold(
                correction_runtime.validated_proposal
            )
            or binding.runtime_snapshot is None
            or binding.runtime_snapshot
            != self._correction_authority_snapshot(correction_runtime)
            or pending is None
            or pending.important_args != expected_args
        ):
            self._invalidate_stale_correction_approval(plan, runtime)
            raise ControlledProgrammingSessionError(
                "invalid_correction_approval"
            )

    @staticmethod
    def _correction_file_preconditions_hold(validated) -> bool:
        """Recheck trusted file preconditions before approval can run callbacks."""
        try:
            for change in validated.resolved_changes:
                path = change.absolute_path
                if change.operation == "replace":
                    if not path.exists() or path.is_symlink() or not path.is_file():
                        return False
                    if hashlib.sha256(path.read_bytes()).hexdigest() != change.current_sha256:
                        return False
                elif change.operation == "create":
                    if path.exists() or path.is_symlink():
                        return False
                else:
                    return False
        except (OSError, TypeError, ValueError):
            return False
        return True

    def _invalidate_stale_correction_approval(
        self,
        plan: WorkflowPlan,
        runtime: WorkflowRuntimeState,
    ) -> None:
        request_id = self._approval_request_id
        if request_id is not None:
            self._approval_controller._invalidate_without_callback(request_id)
        self._agent.execution_engine.invalidate_workflow_correction_approval(
            plan,
            runtime,
            self._pending_model_correction.step_id,
            "invalid_correction_approval",
        )
        self._approval_request_id = None
        self._pending_model_correction = None
        self._error_code = "invalid_correction_approval"
        self._synchronize_runtime(expected_cancel=True)

    @staticmethod
    def _correction_authority_snapshot(runtime) -> tuple[Any, ...]:
        """Capture only trusted runtime authority and remaining-budget state."""
        validated = runtime.validated_proposal
        return (
            runtime.runtime_id,
            runtime.status,
            runtime.current_proposal.proposal_id
            if runtime.current_proposal is not None
            else None,
            validated.proposal_id if validated is not None else None,
            tuple(sorted(vars(runtime.limits).items())),
            runtime.correction_iterations,
            tuple(sorted(runtime.modified_files)),
            tuple(sorted(runtime.new_files)),
            runtime.total_write_bytes,
            runtime.total_changed_lines,
            tuple(sorted(runtime.applied_proposal_ids)),
            len(runtime.test_runs),
            len(runtime.modified_files),
            len(validated.resolved_changes) if validated is not None else 0,
            len(runtime.applied_proposal_ids),
            max(
                0,
                runtime.limits.max_modified_files - len(runtime.modified_files),
            ),
            max(
                0,
                runtime.limits.max_total_change_bytes
                - runtime.total_write_bytes,
            ),
            max(
                0,
                runtime.limits.max_changed_lines
                - runtime.total_changed_lines,
            ),
            max(
                0,
                runtime.limits.max_correction_iterations
                - runtime.correction_iterations,
            ),
        )

    def _log_model_correction(self, event: str, status: str) -> None:
        self._agent.action_logger.log(
            "model_correction",
            params={
                "event": event,
                "status": status,
                "generation": self._generation_attempts,
                "has_binding": self._pending_model_correction is not None,
            },
            result={"status": status},
        )

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
        self._generation_attempts = 0
        self._invalid_drafts = 0
        self._rejected_proposals = 0
        self._seen_draft_ids = set()
        self._seen_proposal_ids = set()
        self._pending_model_correction = None

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
