from __future__ import annotations

import copy
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

from brain.change_proposal import ChangeProposal
from brain.change_validator import ValidatedChangeProposal
from brain.workflow_limits import WorkflowLimits
from tools.tool_result import ToolResult


CORRECTION_STATUSES = frozenset(
    {
        "validating",
        "awaiting_approval",
        "applying",
        "testing_focused",
        "testing_full",
        "awaiting_correction",
        "completed",
        "failed",
        "cancelled",
        "rollback_failed",
        "correction_limit_reached",
        "repeated_failure_limit_reached",
    }
)
TERMINAL_CORRECTION_STATUSES = frozenset(
    {
        "completed",
        "failed",
        "cancelled",
        "rollback_failed",
        "correction_limit_reached",
        "repeated_failure_limit_reached",
    }
)


class CorrectionRuntimeTransitionError(RuntimeError):
    """A correction runtime transition is invalid."""


class CorrectionRuntimeCompatibilityError(ValueError):
    """A proposal, result, fingerprint, or limit is incompatible."""


def _freeze_external_value(value: Any) -> Any:
    """Defensively copy and freeze nested runtime input collections."""
    if isinstance(value, Mapping):
        return MappingProxyType(
            {
                copy.deepcopy(key): _freeze_external_value(item)
                for key, item in value.items()
            }
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_external_value(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze_external_value(item) for item in value)
    return copy.deepcopy(value)


@dataclass(frozen=True)
class CorrectionTestRun:
    test_spec: Any
    result: ToolResult
    fingerprint: str | None = None


@dataclass
class CorrectionRuntimeState:
    goal: str
    limits: WorkflowLimits = field(default_factory=WorkflowLimits)
    status: str = "validating"
    initial_plan_identity: Any = None
    current_proposal: ChangeProposal | None = None
    validated_proposal: ValidatedChangeProposal | None = None
    proposal_history: tuple[ChangeProposal, ...] = ()
    applied_proposal_ids: frozenset[str] = frozenset()
    correction_iterations: int = 0
    test_runs: tuple[CorrectionTestRun, ...] = ()
    failure_counts: Mapping[str, int] = field(
        default_factory=lambda: MappingProxyType({})
    )
    current_failure_fingerprint: str | None = None
    modified_files: frozenset[str] = frozenset()
    new_files: frozenset[str] = frozenset()
    total_write_bytes: int = 0
    total_changed_lines: int = 0
    pending_approval_request_id: str | None = None
    terminal_reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.goal, str):
            raise CorrectionRuntimeCompatibilityError("goal debe ser una cadena")
        if not isinstance(self.limits, WorkflowLimits):
            raise CorrectionRuntimeCompatibilityError(
                "limits debe ser WorkflowLimits"
            )
        if self.status not in CORRECTION_STATUSES:
            raise CorrectionRuntimeCompatibilityError(
                f"Estado de corrección no válido: {self.status}"
            )
        self.initial_plan_identity = _freeze_external_value(
            self.initial_plan_identity
        )
        self.proposal_history = tuple(self.proposal_history)
        self.applied_proposal_ids = frozenset(self.applied_proposal_ids)
        self.test_runs = tuple(copy.deepcopy(self.test_runs))
        self.failure_counts = MappingProxyType(dict(self.failure_counts))
        self.modified_files = frozenset(self.modified_files)
        self.new_files = frozenset(self.new_files)

    def accept_proposal(
        self,
        proposal: ChangeProposal,
        *,
        correction: bool = False,
    ) -> None:
        self._require_not_terminal()
        if not isinstance(proposal, ChangeProposal):
            raise CorrectionRuntimeCompatibilityError(
                "proposal debe ser ChangeProposal"
            )
        if correction:
            if self.status != "awaiting_correction":
                raise CorrectionRuntimeTransitionError(
                    "Una corrección solo se acepta desde awaiting_correction"
                )
            if self.correction_iterations >= self.limits.max_correction_iterations:
                self.status = "correction_limit_reached"
                self.terminal_reason = "max_correction_iterations"
                raise CorrectionRuntimeTransitionError(
                    "Se agotó max_correction_iterations"
                )
            self.correction_iterations += 1
        elif self.proposal_history:
            raise CorrectionRuntimeTransitionError(
                "La propuesta inicial ya fue registrada"
            )
        elif self.status != "validating":
            raise CorrectionRuntimeTransitionError(
                "La propuesta inicial requiere estado validating"
            )
        self.current_proposal = proposal
        self.validated_proposal = None
        self.proposal_history = (*self.proposal_history, proposal)
        self.status = "validating"
        self.terminal_reason = None

    def record_validation(self, validated: ValidatedChangeProposal) -> None:
        if self.status != "validating" or self.current_proposal is None:
            raise CorrectionRuntimeTransitionError(
                "No hay una propuesta pendiente de validación"
            )
        if not isinstance(validated, ValidatedChangeProposal):
            raise CorrectionRuntimeCompatibilityError(
                "validated debe ser ValidatedChangeProposal"
            )
        if validated.proposal.proposal_id != self.current_proposal.proposal_id:
            raise CorrectionRuntimeCompatibilityError(
                "La validación pertenece a otra propuesta"
            )
        self.validated_proposal = validated
        self.status = "awaiting_approval"

    def record_rejection(self, reason: str = "rejected") -> None:
        if self.status not in {"validating", "awaiting_approval"}:
            raise CorrectionRuntimeTransitionError(
                f"No se puede rechazar desde {self.status}"
            )
        self.status = "cancelled"
        self.terminal_reason = str(reason)
        self.pending_approval_request_id = None

    def mark_applying(self) -> None:
        if self.status != "awaiting_approval" or self.validated_proposal is None:
            raise CorrectionRuntimeTransitionError(
                "Solo una propuesta validada y aprobable puede aplicarse"
            )
        self.status = "applying"
        self.pending_approval_request_id = None

    def record_future_application(self) -> None:
        """Record a future successful application; this method has no effects."""
        if self.status != "applying" or self.validated_proposal is None:
            raise CorrectionRuntimeTransitionError(
                "No hay una aplicación futura en curso"
            )
        validated = self.validated_proposal
        proposal_id = validated.proposal_id
        if proposal_id in self.applied_proposal_ids:
            raise CorrectionRuntimeCompatibilityError(
                "La propuesta ya fue registrada como aplicada"
            )
        budget = validated.calculated_budget
        self.applied_proposal_ids = self.applied_proposal_ids | {proposal_id}
        self.modified_files = self.modified_files | {
            change.relative_path for change in validated.resolved_changes
        }
        self.new_files = self.new_files | {
            change.relative_path
            for change in validated.resolved_changes
            if change.operation == "create"
        }
        self.total_write_bytes += budget.write_bytes
        self.total_changed_lines += budget.changed_lines
        self.status = "testing_focused"

    def begin_full_tests(self) -> None:
        if self.status != "testing_focused":
            raise CorrectionRuntimeTransitionError(
                "Las pruebas completas requieren testing_focused"
            )
        self.status = "testing_full"

    def record_test_run(
        self,
        test_spec: Any,
        result: ToolResult,
        fingerprint: str | None = None,
    ) -> None:
        if self.status not in {"testing_focused", "testing_full"}:
            raise CorrectionRuntimeTransitionError(
                f"No se pueden registrar pruebas desde {self.status}"
            )
        if not isinstance(result, ToolResult):
            raise CorrectionRuntimeCompatibilityError(
                "result debe ser ToolResult"
            )
        if fingerprint is not None and (
            not isinstance(fingerprint, str) or not fingerprint
        ):
            raise CorrectionRuntimeCompatibilityError(
                "fingerprint debe ser una cadena no vacía"
            )
        self.test_runs = (
            *self.test_runs,
            CorrectionTestRun(
                test_spec=copy.deepcopy(test_spec),
                result=copy.deepcopy(result),
                fingerprint=fingerprint,
            ),
        )

    def register_failure(self, fingerprint: str) -> int:
        if self.status not in {"testing_focused", "testing_full"}:
            raise CorrectionRuntimeTransitionError(
                f"No se puede registrar un fallo desde {self.status}"
            )
        if not isinstance(fingerprint, str) or not fingerprint:
            raise CorrectionRuntimeCompatibilityError(
                "fingerprint debe ser una cadena no vacía"
            )
        counts = dict(self.failure_counts)
        count = counts.get(fingerprint, 0) + 1
        counts[fingerprint] = count
        self.failure_counts = MappingProxyType(counts)
        self.current_failure_fingerprint = fingerprint
        if count >= self.limits.max_repeated_failure:
            self.status = "repeated_failure_limit_reached"
            self.terminal_reason = "max_repeated_failure"
        else:
            self.status = "awaiting_correction"
        return count

    def mark_completed(self) -> None:
        if self.status != "testing_full":
            raise CorrectionRuntimeTransitionError(
                "completed solo es válido después de testing_full"
            )
        self.status = "completed"
        self.terminal_reason = None

    def mark_failed(self, reason: str) -> None:
        self._require_not_terminal()
        self.status = "failed"
        self.terminal_reason = str(reason)

    def mark_rollback_failed(self, reason: str) -> None:
        if self.status != "applying":
            raise CorrectionRuntimeTransitionError(
                "rollback_failed solo es válido durante applying"
            )
        self.status = "rollback_failed"
        self.terminal_reason = str(reason)

    def _require_not_terminal(self) -> None:
        if self.status in TERMINAL_CORRECTION_STATUSES:
            raise CorrectionRuntimeTransitionError(
                f"El runtime ya terminó con estado {self.status}"
            )
