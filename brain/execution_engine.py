from __future__ import annotations

import math
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from brain.approval_controller import ApprovalRequiredError
from brain.execution_state import ExecutionState
from brain.reflection_engine import ReflectionEngine


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
    def __init__(self, agent, max_retries: int = 2):
        self.agent = agent
        self.reflection_engine = ReflectionEngine()
        self.max_retries = max_retries
        self.last_state: Optional[ExecutionState] = None

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
            return {"name": name, "status": "ok", "result": context[:500]}

        if name == "run_tests":
            try:
                report = self.agent.execute_tool(
                    "test_runner",
                    lambda: self.agent.test_runner.run_tests_report(),
                    action_name="run_tests_report",
                    important_args={"scope": "default"},
                )
                return {"name": name, "status": "ok", "result": report}
            except ApprovalRequiredError:
                raise
            except PermissionError as exc:
                return {"name": name, "status": "failed", "result": str(exc)}

        if name == "analyze_code":
            target = "brain/agent.py"
            try:
                summary = self.agent.execute_tool(
                    "code_analyzer",
                    lambda: self.agent.code_analyzer.summarize(target),
                    action_name="summarize",
                    important_args={"target": target},
                )
                return {"name": name, "status": "ok", "result": summary}
            except ApprovalRequiredError:
                raise
            except PermissionError as exc:
                return {"name": name, "status": "failed", "result": str(exc)}

        if name == "propose_fix":
            return {"name": name, "status": "ok", "result": "Se propone un parche o ajuste basado en el análisis"}

        if name == "reflect":
            return {"name": name, "status": "ok", "result": "Revisión final del plan y del resultado"}

        return {"name": name, "status": "skipped", "result": "Paso no implementado"}

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
                {"status": "failed", "result": failed_result}
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
