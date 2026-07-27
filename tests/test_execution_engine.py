import tempfile
import unittest
from pathlib import Path

from brain.agent import DeveloperAgent
from brain.execution_engine import ExecutionEngine
from brain.execution_state import ExecutionState
from brain.reflection_engine import ReflectionEngine


class ExecutionEngineTests(unittest.TestCase):
    def _build_agent(self):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        temp_dir = Path(tmpdir.name)
        return DeveloperAgent(
            client=None,
            memory_file=temp_dir / "memory.json",
            prompt_dir="prompts",
            base_dir=".",
            action_log_file=temp_dir / "agent_actions.json",
        )

    def test_build_plan_for_debug_task_creates_multi_step_plan(self):
        agent = self._build_agent()
        engine = ExecutionEngine(agent)

        plan = engine.build_plan("Analiza el proyecto y dime por qué falla")

        self.assertGreaterEqual(len(plan), 3)
        steps = [step["name"] for step in plan]
        self.assertIn("inspect_context", steps)
        self.assertIn("run_tests", steps)
        self.assertIn("reflect", steps)

    def test_execute_plan_returns_structured_report(self):
        agent = self._build_agent()
        agent.test_runner.run_tests_report = lambda: "tests-ok"
        engine = ExecutionEngine(agent)

        result = engine.run("Analiza el proyecto y dime por qué falla")

        self.assertIn("Plan", result)
        self.assertIn("Estado", result)
        self.assertIn("Resultado", result)

    def test_execution_state_tracks_success_and_failure(self):
        state = ExecutionState(goal="debug")
        state.record_success("code_reader", "ok")
        state.record_failure("tests", "boom")

        self.assertEqual(state.current_step, 2)
        self.assertEqual(len(state.completed_steps), 1)
        self.assertEqual(len(state.failed_steps), 1)
        self.assertFalse(state.finished)

    def test_reflection_engine_recommends_replanning_on_failure(self):
        engine = ReflectionEngine()
        decision = engine.decide({"status": "failed", "result": "error"})

        self.assertEqual(decision["action"], "replan")
        self.assertIn("replanificar", decision["reason"].lower())

    def test_run_replans_when_a_step_fails(self):
        agent = self._build_agent()

        class FailingExecutionEngine(ExecutionEngine):
            def _run_step(self, step, message):
                if step["name"] == "inspect_context":
                    return {"name": step["name"], "status": "failed", "result": "boom"}
                return {"name": step["name"], "status": "ok", "result": "ok"}

        engine = FailingExecutionEngine(agent)
        result = engine.run("Analiza el proyecto y dime por qué falla")

        self.assertGreaterEqual(len(result["State"]["failed_steps"]), 1)
        self.assertIn("replan", result["State"]["observations"])
        self.assertGreaterEqual(len(result["Plan"]), 2)


if __name__ == "__main__":
    unittest.main()
