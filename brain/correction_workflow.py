from __future__ import annotations

import copy
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from brain.approval_controller import ApprovalRequiredError
from brain.change_proposal import ChangeProposal
from brain.change_proposal_adapter import ChangeProposalAdapter
from brain.correction_engine import (
    CorrectionApprovalError,
    CorrectionApprovalRequest,
    CorrectionEngine,
    InMemoryCorrectionApprovalService,
)
from brain.correction_runtime import CorrectionRuntimeState


class CorrectionWorkflowConfigurationError(ValueError):
    """The controller received dependencies with incompatible boundaries."""


class CorrectionWorkflowController:
    """Expose one CorrectionEngine logical approval as one workflow signal."""

    tool_name = "patch_applier"
    action_name = "apply_change_proposal"

    def __init__(
        self,
        workspace=None,
        *,
        adapter: ChangeProposalAdapter | None = None,
        engine: CorrectionEngine | None = None,
    ):
        if adapter is not None and not isinstance(adapter, ChangeProposalAdapter):
            raise CorrectionWorkflowConfigurationError(
                "adapter debe ser ChangeProposalAdapter"
            )
        if engine is not None and not isinstance(engine, CorrectionEngine):
            raise CorrectionWorkflowConfigurationError(
                "engine debe ser CorrectionEngine"
            )
        if engine is None:
            engine = CorrectionEngine(
                Path(workspace or ".").resolve(),
                approval_service=InMemoryCorrectionApprovalService(),
            )
        elif not isinstance(
            engine.approval_service,
            InMemoryCorrectionApprovalService,
        ):
            raise CorrectionWorkflowConfigurationError(
                "El controlador exige aprobaciones lógicas en memoria"
            )
        self.adapter = adapter or ChangeProposalAdapter()
        self.engine = engine

    def start(
        self,
        goal: str,
        arguments: Mapping[str, Any],
    ) -> CorrectionRuntimeState:
        proposal = self.adapter.adapt(arguments)
        runtime = self.engine.start(goal, proposal)
        return self._suspend_if_required(runtime)

    def submit_correction(
        self,
        arguments: Mapping[str, Any] | ChangeProposal,
    ) -> CorrectionRuntimeState:
        proposal = (
            arguments
            if isinstance(arguments, ChangeProposal)
            else self.adapter.adapt(arguments)
        )
        runtime = self.engine.submit_correction(proposal)
        return self._suspend_if_required(runtime)

    def resume(
        self,
        request_id: str,
        *,
        runtime_id: str,
        proposal_id: str,
        approved: bool,
    ) -> CorrectionRuntimeState:
        return self.engine.resume(
            request_id,
            runtime_id=runtime_id,
            proposal_id=proposal_id,
            approved=approved,
        )

    def _suspend_if_required(
        self,
        runtime: CorrectionRuntimeState,
    ) -> CorrectionRuntimeState:
        if runtime.status != "awaiting_approval":
            return runtime
        request = self.engine.pending_approval_request
        self._validate_request(runtime, request)
        raise self._approval_signal(request)

    def _approval_signal(
        self,
        request: CorrectionApprovalRequest,
    ) -> ApprovalRequiredError:
        important_args = self._important_args(request)

        def approve() -> CorrectionRuntimeState:
            return self.resume(
                request.request_id,
                runtime_id=request.runtime_id,
                proposal_id=request.proposal_id,
                approved=True,
            )

        def deny(reason: str) -> CorrectionRuntimeState:
            return self.resume(
                request.request_id,
                runtime_id=request.runtime_id,
                proposal_id=request.proposal_id,
                approved=False,
            )

        return ApprovalRequiredError(
            tool_name=self.tool_name,
            action_name=self.action_name,
            important_args=important_args,
            execute=approve,
            message=self._message(request),
            force_approval=True,
            on_cancel=deny,
        )

    @staticmethod
    def _validate_request(
        runtime: CorrectionRuntimeState,
        request: CorrectionApprovalRequest | None,
    ) -> None:
        if request is None:
            raise CorrectionApprovalError(
                "El motor no expuso la solicitud lógica pendiente"
            )
        if (
            request.request_id != runtime.pending_approval_request_id
            or request.runtime_id != runtime.runtime_id
            or runtime.validated_proposal is None
            or request.proposal_id != runtime.validated_proposal.proposal_id
        ):
            raise CorrectionApprovalError(
                "La solicitud lógica no corresponde al runtime pendiente"
            )

    @staticmethod
    def _important_args(
        request: CorrectionApprovalRequest,
    ) -> dict[str, Any]:
        return {
            "logical_request_id": request.request_id,
            "runtime_id": request.runtime_id,
            "proposal_id": request.proposal_id,
            "goal": request.goal,
            "changes": [list(item) for item in request.changes],
            "budget": copy.deepcopy(dict(request.budget)),
        }

    @staticmethod
    def _message(request: CorrectionApprovalRequest) -> str:
        return (
            "Se requiere aprobación para aplicar la propuesta "
            f"{request.proposal_id} del runtime {request.runtime_id}."
        )
