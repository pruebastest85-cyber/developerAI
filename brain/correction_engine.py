from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from brain.change_proposal import ChangeProposal, TestSpec
from brain.change_transaction import (
    ChangeTransaction,
    ChangeTransactionResult,
    DuplicateProposalApplicationError,
    TransactionApplyError,
    TransactionPreconditionError,
    TransactionRollbackError,
)
from brain.change_validator import (
    ChangeProposalValidator,
    ChangeValidationError,
    ValidatedChangeProposal,
)
from brain.correction_runtime import (
    CorrectionRuntimeState,
    CorrectionRuntimeTransitionError,
    TERMINAL_CORRECTION_STATUSES,
)
from brain.test_failure import failure_category, failure_fingerprint
from brain.workflow_limits import WorkflowLimits
from tools.test_runner import TestRunner
from tools.tool_result import ToolResult


class CorrectionEngineError(RuntimeError):
    pass


class CorrectionApprovalError(CorrectionEngineError):
    pass


class CorrectionBudgetExceededError(CorrectionEngineError):
    pass


class CorrectionTestResultError(CorrectionEngineError):
    pass


class CorrectionProposalError(CorrectionEngineError):
    pass


@dataclass(frozen=True)
class CorrectionApprovalRequest:
    request_id: str
    runtime_id: str
    proposal_id: str
    goal: str
    changes: tuple[tuple[str, str], ...]
    budget: Mapping[str, int]
    risks: tuple[str, ...]

    def __post_init__(self):
        for name in ("request_id", "runtime_id", "proposal_id", "goal"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise CorrectionApprovalError(
                    f"{name} debe ser una cadena no vacía"
                )
        try:
            changes = tuple(tuple(item) for item in self.changes)
            risks = tuple(self.risks)
            budget = dict(self.budget)
        except (TypeError, ValueError) as exc:
            raise CorrectionApprovalError(
                "Resumen de aprobación inválido"
            ) from exc
        if any(
            len(item) != 2
            or any(not isinstance(value, str) or not value for value in item)
            for item in changes
        ):
            raise CorrectionApprovalError("changes no es un resumen válido")
        if any(not isinstance(risk, str) for risk in risks):
            raise CorrectionApprovalError("risks solo admite cadenas")
        for name, value in budget.items():
            if (
                not isinstance(name, str)
                or isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
            ):
                raise CorrectionApprovalError("budget no es válido")
        object.__setattr__(self, "changes", changes)
        object.__setattr__(self, "risks", risks)
        object.__setattr__(
            self,
            "budget",
            MappingProxyType(budget),
        )


class InMemoryCorrectionApprovalService:
    """Explicit one-use approval boundary suitable for UI adapters and tests."""

    def __init__(self, id_factory=None):
        self.id_factory = id_factory or (lambda: str(uuid.uuid4()))
        self._pending: dict[str, CorrectionApprovalRequest] = {}

    def request(
        self,
        runtime: CorrectionRuntimeState,
        validated: ValidatedChangeProposal,
    ) -> CorrectionApprovalRequest:
        for request in self._pending.values():
            if (
                request.runtime_id == runtime.runtime_id
                and request.proposal_id == validated.proposal_id
            ):
                return request
        request = CorrectionApprovalRequest(
            request_id=self.id_factory(),
            runtime_id=runtime.runtime_id,
            proposal_id=validated.proposal_id,
            goal=runtime.goal,
            changes=tuple(
                (change.relative_path, change.operation)
                for change in validated.resolved_changes
            ),
            budget=validated.calculated_budget.canonical_dict(),
            risks=tuple(validated.proposal.risks),
        )
        self._pending[request.request_id] = request
        return request

    def decide(
        self,
        request_id: str,
        *,
        runtime_id: str,
        proposal_id: str,
        approved: bool,
    ) -> bool:
        if not isinstance(approved, bool):
            raise CorrectionApprovalError("approved debe ser bool")
        request = self._pending.get(request_id)
        if request is None:
            raise CorrectionApprovalError("La solicitud no existe o ya fue consumida")
        if (
            request.runtime_id != runtime_id
            or request.proposal_id != proposal_id
        ):
            raise CorrectionApprovalError(
                "La aprobación no corresponde al runtime y propuesta pendientes"
            )
        del self._pending[request_id]
        return approved

    def cancel(self, request_id: str) -> bool:
        return self._pending.pop(request_id, None) is not None


class PermissionManagerCorrectionApprovalAdapter:
    """Bind correction approvals to the existing one-use PermissionManager."""

    tool_name = "patch_applier"
    action_name = "apply_change_proposal"

    def __init__(self, permission_manager):
        self.permission_manager = permission_manager
        self._pending: dict[str, CorrectionApprovalRequest] = {}

    def request(
        self,
        runtime: CorrectionRuntimeState,
        validated: ValidatedChangeProposal,
    ) -> CorrectionApprovalRequest:
        for request in self._pending.values():
            if (
                request.runtime_id == runtime.runtime_id
                and request.proposal_id == validated.proposal_id
            ):
                return request
        important_args = self._important_args(runtime, validated)
        raw = self.permission_manager.create_approval_request(
            self.tool_name,
            self.action_name,
            important_args=important_args,
            force=True,
        )
        if not raw or not raw.get("request_id"):
            raise CorrectionApprovalError(
                "PermissionManager no pudo crear la solicitud"
            )
        request = CorrectionApprovalRequest(
            request_id=raw["request_id"],
            runtime_id=runtime.runtime_id,
            proposal_id=validated.proposal_id,
            goal=runtime.goal,
            changes=tuple(
                (change.relative_path, change.operation)
                for change in validated.resolved_changes
            ),
            budget=validated.calculated_budget.canonical_dict(),
            risks=tuple(validated.proposal.risks),
        )
        self._pending[request.request_id] = request
        return request

    def decide(
        self,
        request_id: str,
        *,
        runtime_id: str,
        proposal_id: str,
        approved: bool,
    ) -> bool:
        if not isinstance(approved, bool):
            raise CorrectionApprovalError("approved debe ser bool")
        request = self._pending.get(request_id)
        if request is None:
            raise CorrectionApprovalError("La solicitud no existe o ya fue consumida")
        if (
            request.runtime_id != runtime_id
            or request.proposal_id != proposal_id
        ):
            raise CorrectionApprovalError(
                "La aprobación no corresponde al runtime y propuesta pendientes"
            )
        important_args = {
            "runtime_id": runtime_id,
            "proposal_id": proposal_id,
            "goal": request.goal,
            "changes": [list(item) for item in request.changes],
            "budget": dict(request.budget),
            "risks": list(request.risks),
        }
        if not approved:
            self.permission_manager.cancel_approval_request(request_id)
            del self._pending[request_id]
            return False
        token = self.permission_manager.grant_approval(request_id)
        allowed = bool(
            token
            and self.permission_manager.can_execute(
                self.tool_name,
                self.action_name,
                important_args=important_args,
                approval_token=token,
                require_confirmation=True,
            )
        )
        if not allowed:
            raise CorrectionApprovalError("La aprobación no pudo consumirse")
        del self._pending[request_id]
        return True

    def cancel(self, request_id: str) -> bool:
        self._pending.pop(request_id, None)
        return self.permission_manager.cancel_approval_request(request_id)

    @staticmethod
    def _important_args(runtime, validated):
        return {
            "runtime_id": runtime.runtime_id,
            "proposal_id": validated.proposal_id,
            "goal": runtime.goal,
            "changes": [
                [change.relative_path, change.operation]
                for change in validated.resolved_changes
            ],
            "budget": validated.calculated_budget.canonical_dict(),
            "risks": list(validated.proposal.risks),
        }


class CorrectionEngine:
    """Coordinate externally supplied proposals through approval and tests."""

    def __init__(
        self,
        workspace=None,
        *,
        limits: WorkflowLimits | None = None,
        validator=None,
        transaction=None,
        test_runner=None,
        approval_service=None,
        runtime_id_factory=None,
    ):
        if limits is not None and not isinstance(limits, WorkflowLimits):
            raise CorrectionEngineError("limits debe ser WorkflowLimits")
        self.workspace = Path(workspace or ".").resolve()
        self.limits = limits or WorkflowLimits()
        self.validator = validator or ChangeProposalValidator(
            self.workspace,
            self.limits,
        )
        self.transaction = transaction or ChangeTransaction(self.workspace)
        self.test_runner = test_runner or TestRunner(self.workspace)
        self.approval_service = (
            approval_service or InMemoryCorrectionApprovalService()
        )
        self.runtime_id_factory = runtime_id_factory or (
            lambda: str(uuid.uuid4())
        )
        self.runtime: CorrectionRuntimeState | None = None
        self._validate_dependencies()

    def start(
        self,
        goal: str,
        proposal: ChangeProposal,
    ) -> CorrectionRuntimeState:
        if self.runtime is not None and self.runtime.status not in TERMINAL_CORRECTION_STATUSES:
            raise CorrectionEngineError("Ya existe un runtime activo")
        runtime = CorrectionRuntimeState(
            goal,
            limits=self.limits,
            runtime_id=self.runtime_id_factory(),
        )
        self.runtime = runtime
        runtime.accept_proposal(proposal)
        self._validate_and_request(runtime, proposal)
        return runtime

    def submit_correction(
        self,
        proposal: ChangeProposal,
    ) -> CorrectionRuntimeState:
        runtime = self._require_runtime()
        if runtime.status != "awaiting_correction":
            raise CorrectionRuntimeTransitionError(
                "Solo se aceptan correcciones desde awaiting_correction"
            )
        if not isinstance(proposal, ChangeProposal):
            raise CorrectionProposalError("proposal debe ser ChangeProposal")
        if proposal.proposal_id in runtime.applied_proposal_ids:
            raise CorrectionProposalError("La propuesta ya fue aplicada")
        try:
            validated = self.validator.validate(proposal)
            self._validate_test_contract(validated)
            self._check_accumulated_limits(runtime, validated)
        except ChangeValidationError as exc:
            runtime.terminal_reason = str(exc)
            return runtime
        except CorrectionBudgetExceededError as exc:
            runtime.terminal_reason = str(exc)
            return runtime
        runtime.accept_proposal(
            proposal,
            correction=True,
            consume_iteration=False,
        )
        runtime.record_validation(validated)
        self._request_approval(runtime)
        return runtime

    def resume(
        self,
        request_id: str,
        *,
        runtime_id: str,
        proposal_id: str,
        approved: bool,
    ) -> CorrectionRuntimeState:
        runtime = self._require_runtime()
        if not isinstance(approved, bool):
            raise CorrectionApprovalError("approved debe ser bool")
        if runtime.status != "awaiting_approval":
            raise CorrectionApprovalError("El runtime no espera aprobación")
        if request_id != runtime.pending_approval_request_id:
            raise CorrectionApprovalError("request_id no coincide")
        if runtime_id != runtime.runtime_id:
            raise CorrectionApprovalError("runtime_id no coincide")
        if (
            runtime.validated_proposal is None
            or proposal_id != runtime.validated_proposal.proposal_id
        ):
            raise CorrectionApprovalError("proposal_id no coincide")
        decision = self.approval_service.decide(
            request_id,
            runtime_id=runtime_id,
            proposal_id=proposal_id,
            approved=approved,
        )
        if not isinstance(decision, bool) or decision is not approved:
            raise CorrectionApprovalError(
                "El servicio devolvió una decisión incompatible"
            )
        if not decision:
            runtime.record_rejection(
                "approval_rejected",
                await_correction=True,
            )
            return runtime

        runtime.mark_applying()
        try:
            transaction_result = self.transaction.apply(
                runtime.validated_proposal
            )
        except TransactionRollbackError as exc:
            result = exc.result
            runtime.mark_rollback_failed(
                str(exc),
                modified_paths=result.modified_paths,
                created_paths=result.created_paths,
                write_bytes=result.write_bytes,
                changed_lines=result.changed_lines,
            )
            return runtime
        except (
            TransactionApplyError,
            TransactionPreconditionError,
            DuplicateProposalApplicationError,
        ) as exc:
            runtime.mark_failed(str(exc))
            return runtime
        if (
            not isinstance(transaction_result, ChangeTransactionResult)
            or transaction_result.proposal_id
            != runtime.validated_proposal.proposal_id
            or transaction_result.applied is not True
            or transaction_result.rollback_attempted
        ):
            raise CorrectionEngineError(
                "La transacción no confirmó la aplicación exacta"
            )
        runtime.record_future_application()
        self._run_declared_tests(runtime)
        return runtime

    def cancel(self, reason: str = "cancelled") -> CorrectionRuntimeState:
        runtime = self._require_runtime()
        if runtime.status in TERMINAL_CORRECTION_STATUSES:
            raise CorrectionRuntimeTransitionError(
                f"El runtime ya terminó con estado {runtime.status}"
            )
        if runtime.pending_approval_request_id:
            self.approval_service.cancel(runtime.pending_approval_request_id)
        runtime.mark_cancelled(reason)
        return runtime

    def _validate_and_request(
        self,
        runtime: CorrectionRuntimeState,
        proposal: ChangeProposal,
    ) -> None:
        try:
            validated = self.validator.validate(proposal)
            self._validate_test_contract(validated)
            self._check_accumulated_limits(runtime, validated)
        except (ChangeValidationError, CorrectionBudgetExceededError) as exc:
            runtime.record_validation_failure(str(exc))
            return
        runtime.record_validation(validated)
        self._request_approval(runtime)

    @staticmethod
    def _validate_test_contract(validated: ValidatedChangeProposal) -> None:
        if not any(spec.scope == "full" for spec in validated.proposal.tests):
            raise ChangeValidationError(
                "CorrectionEngine exige al menos un TestSpec full"
            )

    def _request_approval(self, runtime: CorrectionRuntimeState) -> None:
        if runtime.pending_approval_request_id is not None:
            return
        request = self.approval_service.request(
            runtime,
            runtime.validated_proposal,
        )
        if not isinstance(request, CorrectionApprovalRequest):
            raise CorrectionApprovalError(
                "El servicio no devolvió CorrectionApprovalRequest"
            )
        if (
            request.runtime_id != runtime.runtime_id
            or request.proposal_id != runtime.validated_proposal.proposal_id
        ):
            raise CorrectionApprovalError(
                "El servicio devolvió una solicitud incompatible"
            )
        expected = runtime.validated_proposal
        if (
            request.goal != runtime.goal
            or request.changes
            != tuple(
                (change.relative_path, change.operation)
                for change in expected.resolved_changes
            )
            or dict(request.budget)
            != expected.calculated_budget.canonical_dict()
            or request.risks != tuple(expected.proposal.risks)
        ):
            raise CorrectionApprovalError(
                "El resumen de aprobación no coincide con la propuesta"
            )
        runtime.set_pending_approval(request.request_id)

    def _check_accumulated_limits(
        self,
        runtime: CorrectionRuntimeState,
        validated: ValidatedChangeProposal,
    ) -> None:
        paths = runtime.modified_files | {
            item.relative_path for item in validated.resolved_changes
        }
        new_paths = runtime.new_files | {
            item.relative_path
            for item in validated.resolved_changes
            if item.operation == "create"
        }
        budget = validated.calculated_budget
        if len(paths) > self.limits.max_modified_files:
            raise CorrectionBudgetExceededError("max_modified_files excedido")
        if len(new_paths) > self.limits.max_modified_files:
            raise CorrectionBudgetExceededError(
                "cantidad acumulada de archivos nuevos excedida"
            )
        if (
            runtime.total_write_bytes + budget.write_bytes
            > self.limits.max_total_change_bytes
        ):
            raise CorrectionBudgetExceededError(
                "max_total_change_bytes acumulado excedido"
            )
        if (
            runtime.total_changed_lines + budget.changed_lines
            > self.limits.max_changed_lines
        ):
            raise CorrectionBudgetExceededError(
                "max_changed_lines acumulado excedido"
            )

    def _run_declared_tests(self, runtime: CorrectionRuntimeState) -> None:
        specs = runtime.current_proposal.tests
        focused = tuple(spec for spec in specs if spec.scope == "focused")
        full = tuple(spec for spec in specs if spec.scope == "full")
        for spec in focused:
            if not self._run_one_test(runtime, spec):
                return
        runtime.begin_full_tests()
        for spec in full:
            if not self._run_one_test(runtime, spec):
                return
        runtime.mark_completed()

    def _run_one_test(
        self,
        runtime: CorrectionRuntimeState,
        spec: TestSpec,
    ) -> bool:
        timeout = (
            self.limits.focused_test_timeout
            if spec.scope == "focused"
            else self.limits.full_test_timeout
        )
        result = self.test_runner.execute(
            {"test_spec": spec, "timeout": timeout},
            structured=True,
        )
        if not isinstance(result, ToolResult) or result.tool_name != "test_runner":
            raise CorrectionTestResultError(
                "TestRunner debe devolver ToolResult de test_runner"
            )
        self._validate_test_result(result)
        fingerprint = None
        category = None
        if result.status != "ok":
            fingerprint = failure_fingerprint(spec, result)
            category = failure_category(result)
        runtime.record_test_run(spec, result, fingerprint, category)
        if result.status == "ok":
            return True
        runtime.register_failure(fingerprint)
        return False

    @staticmethod
    def _validate_test_result(result: ToolResult) -> None:
        if result.status != "ok":
            return
        data = result.data
        if not isinstance(data, dict):
            raise CorrectionTestResultError(
                "Un resultado ok debe contener datos estructurados"
            )
        integer_fields = (
            "tests_run",
            "failures",
            "errors",
            "skipped",
            "passed",
        )
        values = {}
        for name in integer_fields:
            value = data.get(name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise CorrectionTestResultError(
                    f"Resultado ok inválido: {name}"
                )
            values[name] = value
        if (
            data.get("returncode") != 0
            or values["tests_run"] <= 0
            or values["failures"] != 0
            or values["errors"] != 0
            or values["skipped"] > values["tests_run"]
            or values["passed"]
            != values["tests_run"] - values["skipped"]
        ):
            raise CorrectionTestResultError(
                "Resultado ok incompatible con sus conteos"
            )

    def _require_runtime(self) -> CorrectionRuntimeState:
        if self.runtime is None:
            raise CorrectionEngineError("No existe un runtime")
        return self.runtime

    def _validate_dependencies(self) -> None:
        for name, dependency in (
            ("validator", self.validator),
            ("transaction", self.transaction),
            ("test_runner", self.test_runner),
        ):
            base_dir = getattr(dependency, "base_dir", None)
            if base_dir is not None and Path(base_dir).resolve() != self.workspace:
                raise CorrectionEngineError(
                    f"{name} pertenece a otro workspace"
                )
        validator_limits = getattr(self.validator, "limits", self.limits)
        if validator_limits != self.limits:
            raise CorrectionEngineError(
                "validator usa límites incompatibles"
            )
        for name, method in (
            ("validator.validate", getattr(self.validator, "validate", None)),
            ("transaction.apply", getattr(self.transaction, "apply", None)),
            ("test_runner.execute", getattr(self.test_runner, "execute", None)),
            (
                "approval_service.request",
                getattr(self.approval_service, "request", None),
            ),
            (
                "approval_service.decide",
                getattr(self.approval_service, "decide", None),
            ),
            (
                "approval_service.cancel",
                getattr(self.approval_service, "cancel", None),
            ),
        ):
            if not callable(method):
                raise CorrectionEngineError(f"Dependencia inválida: {name}")
