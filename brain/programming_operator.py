"""Minimal operator-facing adapter for the existing controlled session."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from brain.agent import DeveloperAgent
from brain.isolated_environment import IsolatedRepository, IsolatedRepositorySnapshot
from brain.local_model_client import LocalModelClient
from brain.local_model_config import LocalModelConfig
from brain.model_errors import LocalModelError
from brain.model_correction import ModelCorrectionAdapter, ModelCorrectionService
from brain.model_planning_service import ModelPlanningService


def _freeze_public(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType(
            {str(key): _freeze_public(item) for key, item in value.items()}
        )
    if isinstance(value, (tuple, list)):
        return tuple(_freeze_public(item) for item in value)
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise TypeError("La vista pública contiene un valor no permitido")


@dataclass(frozen=True)
class ProgrammingOperatorView:
    session_id: str
    state: str
    plan_id: str | None
    approval_request_id: str | None
    error_code: str | None
    workflow_runtime_id: str | None
    correction_applications: int
    presentation: str
    plan: tuple[MappingProxyType, ...] = ()
    approval: MappingProxyType | None = None
    tests: tuple[MappingProxyType, ...] = ()
    diff: str = ""


class _DiagnosticPlanningService(ModelPlanningService):
    """Keep a sanitized transport code at the UI adapter boundary only."""

    def __init__(self, model_client):
        super().__init__(model_client)
        self.last_model_error_code = None

    def plan(self, user_request):
        self.last_model_error_code = None
        try:
            return super().plan(user_request)
        except LocalModelError as exc:
            self.last_model_error_code = exc.code
            raise


class ProgrammingOperator:
    """Present and route commands without owning execution authority."""

    def __init__(self, agent: DeveloperAgent, isolation: IsolatedRepository):
        if not isinstance(agent, DeveloperAgent):
            raise TypeError("agent debe ser DeveloperAgent")
        if not isinstance(isolation, IsolatedRepository):
            raise TypeError("isolation debe ser IsolatedRepository")
        self.agent = agent
        self.isolation = isolation
        self.session = agent.get_programming_session()

    @classmethod
    def from_config(
        cls,
        source_repository,
        config: LocalModelConfig,
        *,
        keep_environment=False,
        transport=None,
        temp_parent=None,
    ) -> "ProgrammingOperator":
        isolation = IsolatedRepository(
            source_repository,
            keep=keep_environment,
            temp_parent=temp_parent,
        )
        snapshot = isolation.create()
        try:
            client = LocalModelClient(config, transport=transport)
            planning = _DiagnosticPlanningService(client)
            correction = ModelCorrectionService(client)
            agent = DeveloperAgent(
                None,
                base_dir=snapshot.repository,
                action_log_file=snapshot.runtime_directory / "actions.json",
                model_planning_service=planning,
                model_correction_service=correction,
                model_correction_adapter=ModelCorrectionAdapter(snapshot.repository),
            )
            return cls(agent, isolation)
        except BaseException:
            isolation.close()
            raise

    @property
    def isolated_snapshot(self) -> IsolatedRepositorySnapshot:
        return self.isolation.snapshot

    def execute(self, command: str) -> ProgrammingOperatorView:
        presentation = self.session.handle_message(command)
        return self._view(presentation)

    def current(self) -> ProgrammingOperatorView:
        return self._view(self.session.render_current_report())

    def close(self) -> None:
        self.isolation.close()

    def _view(self, presentation: str) -> ProgrammingOperatorView:
        result = self.session.current_result()
        model_error_code = None
        for service in (
            self.agent.model_planning_service,
            self.agent.model_correction_service,
        ):
            candidate = getattr(service, "last_model_error_code", None)
            if candidate is not None:
                model_error_code = candidate
        plan = ()
        if result.plan is not None:
            plan = tuple(
                MappingProxyType(
                    {
                        "id": step.id,
                        "tool": step.tool,
                        "action": step.action,
                        "goal": step.goal,
                        "depends_on": step.depends_on,
                        "arguments": step.arguments,
                    }
                )
                for step in result.plan.steps
            )
        approval = None
        if result.pending_approval_request_id is not None:
            pending = self.session.approval_controller.get_pending(
                result.pending_approval_request_id
            )
            if pending is not None:
                approval = MappingProxyType(
                    {
                        "request_id": pending.request_id,
                        "tool": pending.tool_name,
                        "action": pending.action_name,
                        "arguments": _freeze_public(pending.important_args),
                    }
                )
        tests = ()
        diff = ""
        if result.report is not None:
            tests = tuple(
                MappingProxyType(
                    {
                        "scope": test.scope,
                        "status": test.status,
                        "tests_run": test.tests_run,
                        "passed": test.passed,
                        "failures": test.failures,
                        "errors": test.errors,
                        "skipped": test.skipped,
                    }
                )
                for test in result.report.tests
            )
            if result.report.diff is not None and result.report.diff.available:
                diff = result.report.diff.text
        return ProgrammingOperatorView(
            session_id=result.session_id,
            state=result.state.value,
            plan_id=result.plan_id,
            approval_request_id=result.pending_approval_request_id,
            error_code=(
                model_error_code
                if result.error_code == "planning_failed"
                and model_error_code is not None
                else result.error_code
            ),
            workflow_runtime_id=result.workflow_runtime_id,
            correction_applications=result.correction_applications,
            presentation=presentation,
            plan=plan,
            approval=approval,
            tests=tests,
            diff=diff,
        )


def create_operator_from_env(
    source_repository,
    *,
    environ=None,
    keep_environment=False,
    transport=None,
    temp_parent=None,
) -> ProgrammingOperator:
    return ProgrammingOperator.from_config(
        source_repository,
        LocalModelConfig.from_env(environ),
        keep_environment=keep_environment,
        transport=transport,
        temp_parent=temp_parent,
    )
