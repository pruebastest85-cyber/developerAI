import tempfile
import unittest
from pathlib import Path

from brain.agent import DeveloperAgent
from brain.approval_controller import ApprovalRequiredError, ConversationalController
from brain.execution_engine import ExecutionEngine
from brain.execution_state import ExecutionState
from brain.reflection_engine import ReflectionEngine


class ExecutionEngineTests(unittest.TestCase):
    def _build_agent(self):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        temp_dir = Path(tmpdir.name)
        agent = DeveloperAgent(
            client=None,
            memory_file=temp_dir / "memory.json",
            prompt_dir="prompts",
            base_dir=".",
            action_log_file=temp_dir / "agent_actions.json",
        )
        agent.permission_manager.medium_requires_confirmation = False
        return agent

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
            failed_once = False

            def _run_step(self, step, message):
                if step["name"] == "inspect_context" and not self.failed_once:
                    self.failed_once = True
                    return {"name": step["name"], "status": "failed", "result": "boom"}
                return {"name": step["name"], "status": "ok", "result": "ok"}

        engine = FailingExecutionEngine(agent)
        result = engine.run("Analiza el proyecto y dime por qué falla")

        self.assertGreaterEqual(len(result["State"]["failed_steps"]), 1)
        self.assertIn("replan", result["State"]["observations"])
        self.assertGreaterEqual(len(result["Plan"]), 2)
        self.assertEqual(result["Estado"], "completed")

    def test_failed_initial_plan_executes_fallback_plan(self):
        agent = self._build_agent()

        class FallbackEngine(ExecutionEngine):
            def __init__(self, agent):
                super().__init__(agent)
                self.calls = []

            def build_plan(self, message):
                return [{"name": "primary"}]

            def build_fallback_plan(self, failed_step, plan, state, decision):
                return [{"name": "fallback"}]

            def _run_step(self, step, message):
                self.calls.append(step["name"])
                if step["name"] == "primary":
                    return {"name": "primary", "status": "failed", "result": "boom"}
                return {"name": "fallback", "status": "ok", "result": "recovered"}

        engine = FallbackEngine(agent)
        result = engine.run("debug")

        self.assertEqual(engine.calls, ["primary", "fallback"])
        self.assertEqual(result["Estado"], "completed")
        self.assertEqual(result["State"]["retries"], 1)
        self.assertEqual(result["State"]["alternative_plans"][0][0]["name"], "fallback")

    def test_max_retries_is_consumed_exactly(self):
        agent = self._build_agent()

        class AlwaysFailingEngine(ExecutionEngine):
            def build_plan(self, message):
                return [{"name": "initial"}]

            def build_fallback_plan(self, failed_step, plan, state, decision):
                return [{"name": f"fallback_{state.retries + 1}"}]

            def _run_step(self, step, message):
                return {"name": step["name"], "status": "failed", "result": "boom"}

        engine = AlwaysFailingEngine(agent, max_retries=3)
        result = engine.run("debug")

        self.assertEqual(result["Estado"], "retry_limit_reached")
        self.assertEqual(result["State"]["retries"], 3)
        self.assertEqual(len(result["State"]["attempts"]), 4)
        self.assertEqual(len(result["State"]["failed_steps"]), 4)

    def test_repeated_fallback_plan_stops_cycle(self):
        agent = self._build_agent()

        class CyclicEngine(ExecutionEngine):
            def build_plan(self, message):
                return [{"name": "initial"}]

            def build_fallback_plan(self, failed_step, plan, state, decision):
                return [{"name": "same_fallback"}]

            def _run_step(self, step, message):
                return {"name": step["name"], "status": "failed", "result": "boom"}

        engine = CyclicEngine(agent, max_retries=10)
        result = engine.run("debug")

        self.assertEqual(result["Estado"], "failed")
        self.assertTrue(result["State"]["context"]["cycle_detected"])
        self.assertEqual(len(result["State"]["attempts"]), 2)
        self.assertEqual(result["State"]["retries"], 1)

    def test_empty_fallback_fails_without_consuming_retry(self):
        agent = self._build_agent()

        class EmptyFallbackEngine(ExecutionEngine):
            def build_plan(self, message):
                return [{"name": "initial"}]

            def build_fallback_plan(self, failed_step, plan, state, decision):
                return []

            def _run_step(self, step, message):
                return {"name": step["name"], "status": "failed", "result": "boom"}

        result = EmptyFallbackEngine(agent).run("debug")

        self.assertEqual(result["Estado"], "failed")
        self.assertEqual(result["State"]["retries"], 0)
        self.assertTrue(result["State"]["context"]["invalid_fallback"])

    def test_retry_limit_requires_valid_new_fallback_after_budget_is_used(self):
        agent = self._build_agent()

        class LimitedEngine(ExecutionEngine):
            def build_plan(self, message):
                return [{"name": "initial"}]

            def build_fallback_plan(self, failed_step, plan, state, decision):
                return [{"name": f"fallback_{state.retries + 1}"}]

            def _run_step(self, step, message):
                return {"name": step["name"], "status": "failed", "result": "boom"}

        result = LimitedEngine(agent, max_retries=1).run("debug")

        self.assertEqual(result["Estado"], "retry_limit_reached")
        self.assertEqual(result["State"]["retries"], 1)
        self.assertEqual(len(result["State"]["attempts"]), 2)
        self.assertEqual(
            result["State"]["context"]["fallback_plan"], ["fallback_2"]
        )

    def test_plan_identity_distinguishes_same_name_with_different_configuration(self):
        first = [
            {
                "name": "analyze",
                "goal": "first",
                "args": {"target": "a.py", "options": {"depth": 1}},
            }
        ]
        second = [
            {
                "name": "analyze",
                "goal": "second",
                "args": {"target": "b.py", "options": {"depth": 2}},
            }
        ]

        self.assertNotEqual(
            ExecutionEngine._plan_signature(first),
            ExecutionEngine._plan_signature(second),
        )

    def test_plan_identity_is_deterministic_for_dictionary_order(self):
        first = [
            {
                "name": "analyze",
                "args": {"target": "a.py", "options": {"b": 2, "a": 1}},
                "goal": "same",
            }
        ]
        second = [
            {
                "goal": "same",
                "args": {"options": {"a": 1, "b": 2}, "target": "a.py"},
                "name": "analyze",
            }
        ]

        self.assertEqual(
            ExecutionEngine._plan_signature(first),
            ExecutionEngine._plan_signature(second),
        )

    def test_step_identity_ignores_administrative_metadata(self):
        base = {
            "name": "analyze",
            "goal": "inspect",
            "tool": "code_analyzer",
            "args": {"target": "a.py"},
        }
        with_metadata = {
            **base,
            "result": "old",
            "status": "failed",
            "timestamp": "2026-07-27T12:00:00Z",
            "retries": 3,
        }

        self.assertEqual(
            ExecutionEngine._step_identity(base),
            ExecutionEngine._step_identity(with_metadata),
        )

    def test_step_identity_keeps_executable_configuration(self):
        base = {
            "name": "analyze",
            "goal": "inspect",
            "tool": "code_analyzer",
            "args": {"depth": 1},
            "target": "a.py",
        }

        for field, value in (
            ("goal", "repair"),
            ("tool", "code_reader"),
            ("args", {"depth": 2}),
            ("target", "b.py"),
        ):
            changed = dict(base)
            changed[field] = value
            with self.subTest(field=field):
                self.assertNotEqual(
                    ExecutionEngine._step_identity(base),
                    ExecutionEngine._step_identity(changed),
                )

    def test_completed_step_is_recognized_after_administrative_metadata_changes(self):
        state = ExecutionState(goal="debug")
        completed = {
            "name": "analyze",
            "goal": "inspect",
            "args": {"target": "a.py"},
        }
        state.record_success(
            "analyze",
            "ok",
            step_identity=ExecutionEngine._step_identity(completed),
        )
        enriched = {
            **completed,
            "status": "completed",
            "result": "summary",
            "updated_at": "later",
            "attempts": 2,
        }

        runnable = ExecutionEngine._skip_completed_steps([enriched], state)

        self.assertEqual(runnable, [])

    def test_repeated_plan_detection_ignores_only_administrative_metadata(self):
        base = [
            {
                "name": "analyze",
                "goal": "inspect",
                "args": {"target": "a.py", "status": "strict"},
            }
        ]
        administrative_change = [
            {
                **base[0],
                "status": "failed",
                "error": "temporary",
                "elapsed": 1.5,
            }
        ]
        executable_change = [
            {
                **base[0],
                "args": {"target": "a.py", "status": "lenient"},
            }
        ]

        self.assertEqual(
            ExecutionEngine._plan_signature(base),
            ExecutionEngine._plan_signature(administrative_change),
        )
        self.assertNotEqual(
            ExecutionEngine._plan_signature(base),
            ExecutionEngine._plan_signature(executable_change),
        )

    def test_repeat_completed_remains_part_of_step_identity(self):
        base = {"name": "analyze", "args": {"target": "a.py"}}
        repeated = {**base, "repeat_completed": True}

        self.assertNotEqual(
            ExecutionEngine._step_identity(base),
            ExecutionEngine._step_identity(repeated),
        )

    def test_plan_identity_tolerates_sets_tuples_paths_and_objects(self):
        class Options:
            def __init__(self):
                self.flags = {"b", "a"}
                self.self_reference = self

        first = [
            {
                "name": "analyze",
                "args": (Path("brain/agent.py"), {"tags": {"x", "y"}}),
                "options": Options(),
            }
        ]
        second = [
            {
                "options": Options(),
                "args": (Path("brain/agent.py"), {"tags": {"y", "x"}}),
                "name": "analyze",
            }
        ]

        self.assertEqual(
            ExecutionEngine._plan_signature(first),
            ExecutionEngine._plan_signature(second),
        )

    def test_completed_step_with_same_name_but_different_args_is_not_skipped(self):
        state = ExecutionState(goal="debug")
        completed = {"name": "analyze", "args": {"target": "a.py"}}
        pending = {"name": "analyze", "args": {"target": "b.py"}}
        state.record_success(
            "analyze",
            "ok",
            step_identity=ExecutionEngine._step_identity(completed),
        )

        runnable = ExecutionEngine._skip_completed_steps([completed, pending], state)

        self.assertEqual(runnable, [pending])

    def test_result_without_status_is_a_failure(self):
        agent = self._build_agent()

        class StopReflection:
            def decide(self, result):
                return {"action": "stop"}

        class MissingStatusEngine(ExecutionEngine):
            def build_plan(self, message):
                return [{"name": "legacy"}]

            def _run_step(self, step, message):
                return {"name": "legacy", "result": "ambiguous"}

        engine = MissingStatusEngine(agent)
        engine.reflection_engine = StopReflection()
        result = engine.run("debug")

        self.assertEqual(result["Estado"], "failed")
        self.assertEqual(result["State"]["failed_steps"][0]["step"], "legacy")

    def test_non_replannable_failure_finishes_as_failed(self):
        agent = self._build_agent()

        class StopReflection:
            def decide(self, result):
                return {"action": "stop", "reason": "not replannable"}

        class FailingEngine(ExecutionEngine):
            def build_plan(self, message):
                return [{"name": "fatal"}]

            def _run_step(self, step, message):
                return {"name": "fatal", "status": "failed", "result": "fatal"}

        engine = FailingEngine(agent)
        engine.reflection_engine = StopReflection()
        result = engine.run("debug")

        self.assertEqual(result["Estado"], "failed")
        self.assertTrue(result["State"]["finished"])
        self.assertEqual(result["State"]["retries"], 0)

    def test_approval_waits_without_retry_or_replanning(self):
        agent = self._build_agent()

        class ApprovalEngine(ExecutionEngine):
            def build_plan(self, message):
                return [{"name": "needs_approval"}]

            def _run_step(self, step, message):
                raise ApprovalRequiredError(
                    tool_name="patch_applier",
                    action_name="apply_patch",
                    important_args={"path": "safe.py"},
                    execute=lambda: None,
                    message="approval required",
                )

        engine = ApprovalEngine(agent)

        with self.assertRaises(ApprovalRequiredError):
            engine.run("debug")

        state = engine.last_state
        self.assertEqual(state.status, "awaiting_approval")
        self.assertFalse(state.finished)
        self.assertEqual(state.retries, 0)
        self.assertEqual(state.failed_steps, [])
        self.assertEqual(state.alternative_plans, [])

    def test_successful_steps_are_not_repeated_unless_explicit(self):
        agent = self._build_agent()

        class NoRepeatEngine(ExecutionEngine):
            def __init__(self, agent, repeat_completed=False):
                super().__init__(agent)
                self.calls = []
                self.repeat_completed = repeat_completed

            def build_plan(self, message):
                return [
                    {"name": "done"},
                    {"name": "other_done"},
                    {"name": "fails"},
                ]

            def build_fallback_plan(self, failed_step, plan, state, decision):
                return [
                    {"name": "done", "repeat_completed": self.repeat_completed},
                    {"name": "other_done"},
                    {"name": "recovery"},
                ]

            def _run_step(self, step, message):
                self.calls.append(step["name"])
                if step["name"] == "fails":
                    return {"name": "fails", "status": "failed", "result": "boom"}
                return {"name": step["name"], "status": "ok", "result": "ok"}

        engine = NoRepeatEngine(agent)
        result = engine.run("debug")
        self.assertEqual(result["Estado"], "completed")
        self.assertEqual(engine.calls, ["done", "other_done", "fails", "recovery"])

        repeat_engine = NoRepeatEngine(agent, repeat_completed=True)
        repeat_result = repeat_engine.run("debug")
        self.assertEqual(repeat_result["Estado"], "completed")
        self.assertEqual(
            repeat_engine.calls,
            ["done", "other_done", "fails", "done", "recovery"],
        )

    def test_approval_propagates_through_conversational_controller(self):
        agent = self._build_agent()

        class ApprovalEngine(ExecutionEngine):
            def build_plan(self, message):
                return [{"name": "needs_approval"}]

            def _run_step(self, step, message):
                raise ApprovalRequiredError(
                    tool_name="patch_applier",
                    action_name="apply_patch",
                    important_args={"path": "safe.py"},
                    execute=lambda: None,
                    message="approval required",
                )

        engine = ApprovalEngine(agent)
        agent.execution_engine = engine
        controller = ConversationalController(agent)

        response = controller.process_message("debug error")

        self.assertIn("Se requiere", response)
        self.assertEqual(engine.last_state.status, "awaiting_approval")
        self.assertFalse(engine.last_state.finished)
        self.assertEqual(engine.last_state.retries, 0)
        self.assertEqual(engine.last_state.failed_steps, [])

    def test_empty_successful_plan_is_completed(self):
        agent = self._build_agent()

        class EmptyEngine(ExecutionEngine):
            def build_plan(self, message):
                return []

        result = EmptyEngine(agent).run("inspect")

        self.assertEqual(result["Estado"], "completed")
        self.assertTrue(result["State"]["finished"])


if __name__ == "__main__":
    unittest.main()
