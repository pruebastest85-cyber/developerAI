from __future__ import annotations

import math
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from brain.approval_controller import ApprovalRequiredError
from brain.change_proposal_adapter import ChangeProposalAdaptationError
from brain.correction_engine import CorrectionEngine
from brain.correction_runtime import (
    CorrectionRuntimeState,
)
from brain.correction_workflow import CorrectionWorkflowController
from brain.execution_state import ExecutionState
from brain.reflection_engine import ReflectionEngine
from brain.workflow_limits import WorkflowLimits
from brain.workflow_plan import ArgumentResolver, ResultResolutionError, WorkflowPlan
from brain.workflow_runtime import (
    WorkflowRuntimeState,
    WorkflowRuntimeTransitionError,
)
from brain.workflow_tool_executor import (
    InvalidWorkflowToolResultError,
    WorkflowLimitExceededError,
    WorkflowToolExecutor,
    is_controlled_workflow_step,
)
from tools.tool_result import ToolResult


ADMINISTRATIVE_STEP_FIELDS = frozenset(
    {
        "result",
        "resultado",
        "status",
        "state",
        "timestamp",
        "created_at",
        "updated_at",
        "started_at",
        "finished_at",
        "attempts",
        "retries",
        "retry_count",
        "counters",
        "error",
        "errors",
        "duration",
        "elapsed",
    }
)


class ExecutionEngine:
    def __init__(
        self,
        agent,
        max_retries: int = 2,
        *,
        correction_controller_factory=None,
    ):
        self.agent = agent
        self.reflection_engine = ReflectionEngine()
        self.max_retries = max_retries
        self.last_state: Optional[ExecutionState] = None
        self.last_workflow_runtime: Optional[WorkflowRuntimeState] = None
        self.workflow_limits = WorkflowLimits()
        self.correction_controller_factory = (
            correction_controller_factory
            or self._default_correction_controller
        )

    def _default_correction_controller(self) -> CorrectionWorkflowController:
        return CorrectionWorkflowController(
            engine=CorrectionEngine(
                workspace=self.agent.base_dir,
                limits=self.workflow_limits,
            )
        )

    def build_plan(self, message: str) -> List[Dict[str, str]]:
        text = message.lower().strip()
        plan = []

        if any(keyword in text for keyword in ["falla", "error", "fallo", "bug", "debug"]):
            plan.extend(
                [
                    {"name": "inspect_context", "goal": "Reunir contexto del proyecto y del error"},
                    {"name": "run_tests", "goal": "Ejecutar tests para reproducir el problema"},
                    {"name": "analyze_code", "goal": "Analizar archivos y funciones relevantes"},
                    {"name": "propose_fix", "goal": "Proponer una solución concreta"},
                    {"name": "reflect", "goal": "Evaluar si el plan resolvió el problema"},
                ]
            )
            return plan

        plan.extend(
            [
                {"name": "inspect_context", "goal": "Reunir contexto del proyecto"},
                {"name": "analyze_code", "goal": "Analizar archivos y dependencias relevantes"},
                {"name": "reflect", "goal": "Evaluar si el análisis es suficiente"},
            ]
        )
        return plan

    def _run_step(self, step: Dict[str, str], message: str) -> Dict[str, str]:
        name = step["name"]
        if name == "inspect_context":
            context = self.agent.context_manager.build_context(
                message,
                memory_file=self.agent.memory_file,
                project_context=self.agent._read_project_context(),
                history=self.agent.history,
            )
            return self._tool_step_result(
                name,
                ToolResult.success("context_manager", data=context[:500]),
            )

        if name == "run_tests":
            try:
                report = self.agent.execute_tool(
                    "test_runner",
                    lambda: self.agent.test_runner.execute(structured=True),
                    action_name="run_tests",
                    important_args={"scope": "default"},
                    structured=True,
                )
                return self._tool_step_result(name, report)
            except ApprovalRequiredError:
                raise
            except PermissionError as exc:
                return self._tool_step_result(
                    name,
                    ToolResult.failure("test_runner", error=str(exc)),
                )

        if name == "analyze_code":
            target = "brain/agent.py"
            try:
                summary = self.agent.execute_tool(
                    "code_analyzer",
                    lambda: self.agent.code_analyzer.summarize(target),
                    action_name="summarize",
                    important_args={"target": target},
                    structured=True,
                )
                return self._tool_step_result(name, summary)
            except ApprovalRequiredError:
                raise
            except PermissionError as exc:
                return self._tool_step_result(
                    name,
                    ToolResult.failure("code_analyzer", error=str(exc)),
                )

        if name == "propose_fix":
            return self._tool_step_result(
                name,
                ToolResult.success(
                    "execution_engine",
                    message="Se propone un parche o ajuste basado en el análisis",
                ),
            )

        if name == "reflect":
            return self._tool_step_result(
                name,
                ToolResult.success(
                    "reflection_engine",
                    message="Revisión final del plan y del resultado",
                ),
            )

        return self._tool_step_result(
            name,
            ToolResult.failure(
                "execution_engine",
                error="Paso no implementado",
            ),
        )

    @staticmethod
    def _tool_step_result(name: str, result: ToolResult) -> Dict[str, Any]:
        payload = result.to_dict()
        return {
            "name": name,
            "status": result.status,
            "result": payload,
            "data": payload["data"],
            "message": payload["message"],
            "error": payload["error"],
            "metadata": payload["metadata"],
            "retryable": payload["retryable"],
            "tool_name": payload["tool_name"],
        }

    def build_fallback_plan(
        self,
        failed_step: Dict[str, str],
        plan: List[Dict[str, str]],
        state: ExecutionState,
        decision: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        requested = decision.get("fallback_plan")
        if requested is None:
            requested = ["inspect_context", "run_tests", "analyze_code", "reflect"]

        fallback = []
        for item in requested:
            if isinstance(item, str):
                fallback.append({"name": item, "goal": f"Plan alternativo: {item}"})
            elif isinstance(item, dict) and item.get("name"):
                fallback.append(dict(item))
        return fallback

    @staticmethod
    def _canonical_identity(value: Any, active_ids=None) -> tuple:
        if active_ids is None:
            active_ids = set()

        if value is None:
            return ("none",)
        if isinstance(value, bool):
            return ("bool", value)
        if isinstance(value, int):
            return ("int", value)
        if isinstance(value, float):
            if math.isnan(value):
                return ("float", "nan")
            if math.isinf(value):
                return ("float", "inf" if value > 0 else "-inf")
            return ("float", value.hex())
        if isinstance(value, str):
            return ("str", value)
        if isinstance(value, bytes):
            return ("bytes", value.hex())
        if isinstance(value, Path):
            return ("path", value.as_posix())
        if isinstance(value, Enum):
            return (
                "enum",
                value.__class__.__module__,
                value.__class__.__qualname__,
                ExecutionEngine._canonical_identity(value.value, active_ids),
            )

        track_identity = (
            isinstance(value, (dict, list, tuple, set, frozenset))
            or is_dataclass(value)
            or (not isinstance(value, type) and hasattr(value, "__dict__"))
        )
        value_id = id(value)
        if track_identity:
            if value_id in active_ids:
                return ("cycle", value.__class__.__module__, value.__class__.__qualname__)
            active_ids.add(value_id)

        try:
            if isinstance(value, dict):
                items = [
                    (
                        ExecutionEngine._canonical_identity(key, active_ids),
                        ExecutionEngine._canonical_identity(nested, active_ids),
                    )
                    for key, nested in value.items()
                ]
                return ("dict", tuple(sorted(items, key=lambda item: item[0])))
            if isinstance(value, list):
                return (
                    "list",
                    tuple(ExecutionEngine._canonical_identity(item, active_ids) for item in value),
                )
            if isinstance(value, tuple):
                return (
                    "tuple",
                    tuple(ExecutionEngine._canonical_identity(item, active_ids) for item in value),
                )
            if isinstance(value, (set, frozenset)):
                items = [
                    ExecutionEngine._canonical_identity(item, active_ids) for item in value
                ]
                return ("set", tuple(sorted(items)))
            if is_dataclass(value) and not isinstance(value, type):
                return (
                    "dataclass",
                    value.__class__.__module__,
                    value.__class__.__qualname__,
                    ExecutionEngine._canonical_identity(asdict(value), active_ids),
                )
            if callable(value):
                return (
                    "callable",
                    getattr(value, "__module__", value.__class__.__module__),
                    getattr(value, "__qualname__", value.__class__.__qualname__),
                )
            attributes = getattr(value, "__dict__", None)
            if isinstance(attributes, dict):
                public_attributes = {
                    key: nested
                    for key, nested in attributes.items()
                    if not str(key).startswith("_")
                }
                return (
                    "object",
                    value.__class__.__module__,
                    value.__class__.__qualname__,
                    ExecutionEngine._canonical_identity(public_attributes, active_ids),
                )
            return ("object-type", value.__class__.__module__, value.__class__.__qualname__)
        finally:
            if track_identity:
                active_ids.remove(value_id)

    @classmethod
    def _executable_step_config(cls, step: Dict[str, Any]) -> Dict[str, Any]:
        """Return operation-defining fields, excluding mutable runtime metadata.

        Exclusions apply only to top-level step fields. Values nested under
        executable configuration such as args or options remain identity-bearing.
        """
        executable = {
            key: value
            for key, value in step.items()
            if not (
                isinstance(key, str)
                and key.casefold() in ADMINISTRATIVE_STEP_FIELDS
            )
        }
        if not executable.get("repeat_completed", False):
            executable.pop("repeat_completed", None)
        return executable

    @classmethod
    def _step_identity(cls, step: Dict[str, Any]) -> tuple:
        return cls._canonical_identity(cls._executable_step_config(step))

    @classmethod
    def _plan_signature(cls, plan: List[Dict[str, Any]]) -> tuple:
        return tuple(cls._step_identity(step) for step in plan)

    @staticmethod
    def _normalize_plan(plan: Any) -> List[Dict[str, Any]]:
        if not isinstance(plan, (list, tuple)):
            return []
        normalized = []
        for step in plan:
            if not isinstance(step, dict):
                continue
            name = step.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            normalized.append(dict(step))
        return normalized

    @staticmethod
    def _skip_completed_steps(
        plan: List[Dict[str, Any]], state: ExecutionState
    ) -> List[Dict[str, Any]]:
        completed = {
            entry.get(
                "identity",
                ExecutionEngine._step_identity({"name": entry["step"]}),
            )
            for entry in state.completed_steps
        }
        return [
            step
            for step in plan
            if step.get("repeat_completed", False)
            or ExecutionEngine._step_identity(step) not in completed
        ]

    def run(self, message: str) -> Dict[str, Any]:
        initial_plan = self.build_plan(message)
        state = ExecutionState(goal=message, max_retries=self.max_retries)
        self.last_state = state
        executed = []
        active_plan = initial_plan
        seen_plans = {self._plan_signature(self._normalize_plan(initial_plan))}

        while True:
            plan_to_run = self._skip_completed_steps(active_plan, state)
            state.attempts.append(
                {
                    "attempt": len(state.attempts) + 1,
                    "plan": [dict(step) for step in plan_to_run],
                }
            )
            failed_step = None
            failed_result = None

            for step in plan_to_run:
                try:
                    step_result = self._run_step(step, message)
                except ApprovalRequiredError:
                    state.mark_awaiting_approval()
                    state.context["awaiting_step"] = step["name"]
                    raise
                except Exception as exc:
                    step_result = {
                        "name": step["name"],
                        "status": "failed",
                        "result": str(exc),
                    }
                executed.append(step_result)

                if step_result.get("status") != "ok":
                    state.record_failure(step["name"], step_result.get("result"))
                    failed_step = step
                    failed_result = step_result
                    break

                state.record_success(
                    step["name"],
                    step_result.get("result"),
                    step_identity=self._step_identity(step),
                )
                decision = self.reflection_engine.decide({"status": "ok", "result": step_result})
                state.observations.append(decision["action"])

            if failed_step is None:
                state.mark_finished("completed")
                break

            decision = self.reflection_engine.decide(
                {
                    "status": failed_result.get("status", "failed"),
                    "result": failed_result,
                }
            )
            state.observations.append(decision["action"])
            if decision.get("action") != "replan":
                state.mark_finished("failed")
                break

            fallback_plan = self._normalize_plan(
                self.build_fallback_plan(
                    failed_step, active_plan, state, decision
                )
            )
            state.context["replan_from"] = failed_step["name"]
            state.context["fallback_plan"] = [
                step["name"] for step in fallback_plan
            ]

            runnable_fallback = self._skip_completed_steps(fallback_plan, state)
            signature = self._plan_signature(runnable_fallback)
            if not runnable_fallback:
                state.context["invalid_fallback"] = True
                state.mark_finished("failed")
                break
            if signature in seen_plans:
                state.context["cycle_detected"] = True
                state.mark_finished("failed")
                break
            if state.retries >= state.max_retries:
                state.mark_finished("retry_limit_reached")
                break

            state.record_replan([dict(step) for step in fallback_plan])
            seen_plans.add(signature)
            active_plan = fallback_plan

        return {
            "Plan": [step["name"] for step in initial_plan],
            "Estado": state.status,
            "Resultado": executed,
            "State": state.to_dict(),
        }

    def run_workflow(
        self,
        plan: WorkflowPlan,
        goal: str = "",
        runtime: WorkflowRuntimeState | None = None,
        *,
        safe_logging: bool = False,
    ) -> WorkflowRuntimeState:
        """Execute a validated declarative plan without using legacy replanning."""
        if not isinstance(plan, WorkflowPlan):
            raise TypeError("plan debe ser WorkflowPlan")
        if not isinstance(goal, str):
            raise TypeError("goal debe ser una cadena")
        if not isinstance(safe_logging, bool):
            raise TypeError("safe_logging debe ser bool")
        plan.validate()

        executor = WorkflowToolExecutor(self.agent, limits=self.workflow_limits)
        executor.validate_plan(plan)

        if runtime is None:
            runtime = WorkflowRuntimeState.create(plan, goal=goal)
        else:
            if not isinstance(runtime, WorkflowRuntimeState):
                raise TypeError("runtime debe ser WorkflowRuntimeState")
            runtime.validate_for_plan(plan)
            if runtime.status == "awaiting_approval":
                raise WorkflowRuntimeTransitionError(
                    "Un workflow pendiente debe reanudarse mediante su aprobación"
                )
            runtime.prepare_resume(plan)

        self.last_workflow_runtime = runtime
        resolver = ArgumentResolver()
        return self._continue_workflow(
            plan,
            runtime,
            executor,
            resolver,
            safe_logging=safe_logging,
        )

    def build_workflow_report(self, plan: WorkflowPlan, runtime=None):
        """Build a read-only report without changing or resuming the workflow."""
        if not isinstance(plan, WorkflowPlan):
            raise TypeError("plan debe ser WorkflowPlan")
        runtime = self.last_workflow_runtime if runtime is None else runtime
        if runtime is None:
            raise ValueError("No existe un runtime para construir el reporte")
        from brain.workflow_report_builder import WorkflowReportBuilder

        return WorkflowReportBuilder(
            self.agent.base_dir,
            limits=self.workflow_limits,
        ).build(plan, runtime)

    def _continue_workflow(
        self,
        plan: WorkflowPlan,
        runtime: WorkflowRuntimeState,
        executor: WorkflowToolExecutor,
        resolver: ArgumentResolver,
        *,
        safe_logging: bool = False,
    ) -> WorkflowRuntimeState:
        by_id = {step.id: step for step in plan.steps}

        for step_id in runtime.execution_order:
            step = by_id[step_id]
            step_runtime = runtime.steps[step_id]
            if step_runtime.status == "ok":
                continue
            if step_runtime.status in {"failed", "partial", "skipped"}:
                continue
            if step_runtime.status == "awaiting_approval":
                raise WorkflowRuntimeTransitionError(
                    f"El paso {step_id} todavía espera aprobación"
                )

            dependencies = set(step.depends_on)
            dependencies.update(ref.step_id for ref in step.bindings.values())
            blocked = [
                dependency
                for dependency in dependencies
                if runtime.steps[dependency].status != "ok"
            ]
            if blocked:
                reason = "dependency_not_ok:" + ",".join(
                    f"{dependency}={runtime.steps[dependency].status}"
                    for dependency in sorted(blocked)
                )
                step_runtime.mark_skipped(reason)
                continue

            try:
                resolved_args = resolver.resolve(step, runtime.results)
            except ResultResolutionError as exc:
                result = ToolResult.failure(step.tool, error=str(exc))
                step_runtime.mark_running({})
                runtime.record_result(step_id, result)
                continue

            step_runtime.mark_running(resolved_args)
            runtime.current_step_id = step_id
            try:
                if is_controlled_workflow_step(step):
                    result = self._start_correction_step(
                        step,
                        step_runtime,
                        resolved_args,
                    )
                else:
                    result = executor.execute(step, resolved_args, runtime)
            except ApprovalRequiredError as exc:
                if is_controlled_workflow_step(step):
                    step_runtime.correction_runtime = (
                        step_runtime.correction_controller.engine.runtime
                    )
                step_runtime.mark_awaiting_approval(resolved_args)
                runtime.mark_awaiting_approval(step_id)
                if is_controlled_workflow_step(step):
                    self._attach_correction_continuation(
                        exc,
                        plan,
                        runtime,
                        executor,
                        resolver,
                        step_id,
                        resolved_args,
                        safe_logging=safe_logging,
                    )
                else:
                    self._attach_workflow_continuation(
                        exc,
                        plan,
                        runtime,
                        executor,
                        resolver,
                        step_id,
                        resolved_args,
                        safe_logging=safe_logging,
                    )
                raise
            except ChangeProposalAdaptationError as exc:
                step_runtime.clear_correction_controller()
                result = ToolResult.failure(
                    step.tool,
                    error=str(exc),
                    metadata={"exception_type": type(exc).__name__},
                    retryable=False,
                )
            except (WorkflowLimitExceededError, InvalidWorkflowToolResultError) as exc:
                result = ToolResult.failure(
                    step.tool,
                    error=str(exc),
                    metadata={"exception_type": type(exc).__name__},
                    retryable=False,
                )
            except BaseException as exc:
                if is_controlled_workflow_step(step):
                    self._record_correction_exception(
                        plan,
                        runtime,
                        step,
                        resolved_args,
                        exc,
                        safe_logging=safe_logging,
                    )
                raise

            if isinstance(result, CorrectionRuntimeState):
                step_runtime.correction_runtime = result
                if result.status == "awaiting_correction":
                    runtime.mark_awaiting_correction(
                        step_id,
                        result.terminal_reason,
                    )
                    return runtime
                result = self._correction_tool_result(step.tool, result)
                step_runtime.clear_correction_controller()

            runtime.record_result(step_id, result)
            self._log_workflow_step(
                step,
                resolved_args,
                result,
                safe=safe_logging,
            )

        runtime.finish(plan)
        return runtime

    def submit_workflow_correction(
        self,
        plan: WorkflowPlan,
        runtime: WorkflowRuntimeState,
        arguments: Dict[str, Any],
    ) -> WorkflowRuntimeState:
        """Submit an explicit proposal to the correction step currently paused."""
        if not isinstance(plan, WorkflowPlan):
            raise TypeError("plan debe ser WorkflowPlan")
        if not isinstance(runtime, WorkflowRuntimeState):
            raise TypeError("runtime debe ser WorkflowRuntimeState")
        if not isinstance(arguments, dict):
            raise TypeError("arguments debe ser dict")
        runtime.validate_for_plan(plan)
        step_id = runtime.awaiting_step_id
        if runtime.status != "awaiting_correction" or step_id is None:
            raise WorkflowRuntimeTransitionError(
                "El workflow no espera una corrección"
            )
        step = next(item for item in plan.steps if item.id == step_id)
        if not is_controlled_workflow_step(step):
            raise WorkflowRuntimeTransitionError(
                f"El paso {step_id} no es un flujo de corrección"
            )
        step_runtime = runtime.steps[step_id]
        controller = step_runtime.correction_controller
        if controller is None:
            raise WorkflowRuntimeTransitionError(
                f"El paso {step_id} no conserva su controlador"
            )

        runtime.begin_correction_submission(step_id)
        submitted_args = dict(arguments)
        try:
            correction_runtime = controller.submit_correction(submitted_args)
        except ApprovalRequiredError as exc:
            step_runtime.correction_runtime = controller.engine.runtime
            step_runtime.mark_awaiting_approval(submitted_args)
            runtime.mark_awaiting_approval(step_id)
            self._attach_correction_continuation(
                exc,
                plan,
                runtime,
                WorkflowToolExecutor(self.agent, limits=self.workflow_limits),
                ArgumentResolver(),
                step_id,
                submitted_args,
            )
            raise
        except ChangeProposalAdaptationError:
            runtime.mark_awaiting_correction(step_id, "invalid_correction")
            raise
        except BaseException as exc:
            self._record_correction_exception(
                plan,
                runtime,
                step,
                submitted_args,
                exc,
            )
            raise

        return self._complete_correction_state(
            plan,
            runtime,
            WorkflowToolExecutor(self.agent, limits=self.workflow_limits),
            ArgumentResolver(),
            step_id,
            submitted_args,
            correction_runtime,
        )

    def _start_correction_step(
        self,
        step,
        step_runtime,
        resolved_args: Dict[str, Any],
    ) -> CorrectionRuntimeState:
        controller = self.correction_controller_factory()
        if not isinstance(controller, CorrectionWorkflowController):
            raise TypeError(
                "correction_controller_factory debe devolver "
                "CorrectionWorkflowController"
            )
        step_runtime.correction_controller = controller
        try:
            result = controller.start(step.goal, resolved_args)
            step_runtime.correction_runtime = result
            return result
        except ApprovalRequiredError:
            step_runtime.correction_runtime = controller.engine.runtime
            raise
        except BaseException:
            step_runtime.clear_correction_controller()
            raise

    @staticmethod
    def _correction_tool_result(
        tool_name: str,
        correction_runtime: CorrectionRuntimeState,
    ) -> ToolResult:
        metadata = {
            "correction_status": correction_runtime.status,
            "runtime_id": correction_runtime.runtime_id,
            "proposal_id": (
                correction_runtime.current_proposal.proposal_id
                if correction_runtime.current_proposal is not None
                else None
            ),
        }
        if correction_runtime.status == "completed":
            return ToolResult.success(
                tool_name,
                data=correction_runtime,
                metadata=metadata,
            )
        return ToolResult.failure(
            tool_name,
            data=correction_runtime,
            error=correction_runtime.terminal_reason or correction_runtime.status,
            metadata=metadata,
            retryable=False,
        )

    def _complete_correction_state(
        self,
        plan: WorkflowPlan,
        runtime: WorkflowRuntimeState,
        executor: WorkflowToolExecutor,
        resolver: ArgumentResolver,
        step_id: str,
        resolved_args: Dict[str, Any],
        correction_runtime: CorrectionRuntimeState,
        *,
        safe_logging: bool = False,
    ) -> WorkflowRuntimeState:
        step = next(item for item in plan.steps if item.id == step_id)
        runtime.steps[step_id].correction_runtime = correction_runtime
        if correction_runtime.status == "awaiting_correction":
            runtime.mark_awaiting_correction(
                step_id,
                correction_runtime.terminal_reason,
            )
            return runtime
        result = self._correction_tool_result(step.tool, correction_runtime)
        runtime.steps[step_id].clear_correction_controller()
        runtime.record_result(step_id, result)
        self._log_workflow_step(
            step,
            resolved_args,
            result,
            safe=safe_logging,
        )
        return self._continue_workflow(
            plan,
            runtime,
            executor,
            resolver,
            safe_logging=safe_logging,
        )

    def _record_correction_exception(
        self,
        plan: WorkflowPlan,
        runtime: WorkflowRuntimeState,
        step,
        resolved_args: Dict[str, Any],
        error: BaseException,
        *,
        safe_logging: bool = False,
    ) -> None:
        controller = runtime.steps[step.id].correction_controller
        if controller is not None:
            runtime.steps[step.id].correction_runtime = controller.engine.runtime
        result = ToolResult.failure(
            step.tool,
            error=str(error) or type(error).__name__,
            metadata={"exception_type": type(error).__name__},
            retryable=False,
        )
        runtime.steps[step.id].clear_correction_controller()
        runtime.record_result(step.id, result)
        self._log_workflow_step(
            step,
            resolved_args,
            result,
            safe=safe_logging,
        )
        runtime.finish(plan)

    def _attach_correction_continuation(
        self,
        error: ApprovalRequiredError,
        plan: WorkflowPlan,
        runtime: WorkflowRuntimeState,
        executor: WorkflowToolExecutor,
        resolver: ArgumentResolver,
        step_id: str,
        resolved_args: Dict[str, Any],
        *,
        safe_logging: bool = False,
    ) -> None:
        approved_action = error.execute
        cancelled_action = error.on_cancel

        def continue_after_approval():
            runtime.begin_approved_step(step_id)
            try:
                correction_runtime = approved_action()
                return self._complete_correction_state(
                    plan,
                    runtime,
                    executor,
                    resolver,
                    step_id,
                    resolved_args,
                    correction_runtime,
                    safe_logging=safe_logging,
                )
            except BaseException as exc:
                step = next(item for item in plan.steps if item.id == step_id)
                self._record_correction_exception(
                    plan,
                    runtime,
                    step,
                    resolved_args,
                    exc,
                    safe_logging=safe_logging,
                )
                raise

        def cancel_pending(reason: str) -> None:
            try:
                if cancelled_action is not None:
                    cancelled_action(reason)
            finally:
                runtime.mark_cancelled(step_id, reason)

        def record_request(request_id: str) -> None:
            runtime.record_approval_request(step_id, request_id)

        error.execute = continue_after_approval
        error.on_cancel = cancel_pending
        error.on_request = record_request

    def _attach_workflow_continuation(
        self,
        error: ApprovalRequiredError,
        plan: WorkflowPlan,
        runtime: WorkflowRuntimeState,
        executor: WorkflowToolExecutor,
        resolver: ArgumentResolver,
        step_id: str,
        resolved_args: Dict[str, Any],
        *,
        safe_logging: bool = False,
    ) -> None:
        step = next(item for item in plan.steps if item.id == step_id)
        approved_action = error.execute

        def continue_after_approval():
            runtime.begin_approved_step(step_id)
            try:
                result = executor.complete_approved(
                    step,
                    resolved_args,
                    runtime,
                    approved_action,
                )
            except (WorkflowLimitExceededError, InvalidWorkflowToolResultError) as exc:
                result = ToolResult.failure(
                    step.tool,
                    error=str(exc),
                    metadata={"exception_type": type(exc).__name__},
                    retryable=False,
                )
            except BaseException as exc:
                result = ToolResult.failure(
                    step.tool,
                    error=str(exc) or type(exc).__name__,
                    metadata={"exception_type": type(exc).__name__},
                    retryable=False,
                )
                try:
                    runtime.record_result(step_id, result)
                except BaseException:
                    pass
                try:
                    runtime.finish(plan)
                except BaseException:
                    pass
                try:
                    self._log_workflow_step(
                        step,
                        resolved_args,
                        result,
                        safe=safe_logging,
                    )
                except BaseException:
                    pass
                raise
            runtime.record_result(step_id, result)
            self._log_workflow_step(
                step,
                resolved_args,
                result,
                safe=safe_logging,
            )
            return self._continue_workflow(
                plan,
                runtime,
                executor,
                resolver,
                safe_logging=safe_logging,
            )

        def cancel_pending(reason: str) -> None:
            runtime.mark_cancelled(step_id, reason)

        def record_request(request_id: str) -> None:
            runtime.record_approval_request(step_id, request_id)

        error.execute = continue_after_approval
        error.on_cancel = cancel_pending
        error.on_request = record_request

    def _log_workflow_step(
        self,
        step,
        resolved_args: Dict[str, Any],
        result: ToolResult,
        *,
        safe: bool = False,
    ) -> None:
        if safe:
            self.agent.action_logger.log(
                step.tool,
                params={
                    "workflow_step": step.id,
                    "action": step.action,
                    "status": result.status,
                },
                result={"status": result.status},
            )
            return
        self.agent.action_logger.log(
            step.tool,
            params={
                "workflow_step": step.id,
                "action": step.action,
                "args": resolved_args,
            },
            result=result,
        )
