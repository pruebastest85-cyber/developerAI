from __future__ import annotations

from typing import Dict, List

from brain.execution_state import ExecutionState
from brain.reflection_engine import ReflectionEngine


class ExecutionEngine:
    def __init__(self, agent):
        self.agent = agent
        self.reflection_engine = ReflectionEngine()

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
            report = self.agent.test_runner.run_tests_report()
            return {"name": name, "status": "ok", "result": report}

        if name == "analyze_code":
            target = "brain/agent.py"
            summary = self.agent.code_analyzer.summarize(target)
            return {"name": name, "status": "ok", "result": summary}

        if name == "propose_fix":
            return {"name": name, "status": "ok", "result": "Se propone un parche o ajuste basado en el análisis"}

        if name == "reflect":
            return {"name": name, "status": "ok", "result": "Revisión final del plan y del resultado"}

        return {"name": name, "status": "skipped", "result": "Paso no implementado"}

    def run(self, message: str) -> Dict[str, str]:
        plan = self.build_plan(message)
        state = ExecutionState(goal=message)
        executed = []

        for step in plan:
            step_result = self._run_step(step, message)
            executed.append(step_result)

            if step_result.get("status") == "failed":
                state.record_failure(step["name"], step_result.get("result"))
                decision = self.reflection_engine.decide({"status": "failed", "result": step_result})
                state.observations.append(decision["action"])
                state.context["replan_from"] = step["name"]
                state.context["fallback_plan"] = ["inspect_context", "run_tests", "analyze_code", "reflect"]
                break

            state.record_success(step["name"], step_result.get("result"))
            decision = self.reflection_engine.decide({"status": "ok", "result": step_result})
            state.observations.append(decision["action"])

        state.mark_finished()

        return {
            "Plan": [step["name"] for step in plan],
            "Estado": "completado",
            "Resultado": executed,
            "State": state.to_dict(),
        }
