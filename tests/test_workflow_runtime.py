import unittest

from brain.workflow_plan import StepSpec, WorkflowPlan
from brain.workflow_runtime import (
    RuntimeStepState,
    WorkflowRuntimeMismatchError,
    WorkflowRuntimeState,
    WorkflowRuntimeTransitionError,
)
from tools.tool_result import ToolResult


def make_plan(*steps):
    return WorkflowPlan(tuple(steps))


def make_step(step_id, **overrides):
    values = {
        "id": step_id,
        "action": "read_file",
        "tool": "code_reader",
        "args": {"path": f"{step_id}.py"},
    }
    values.update(overrides)
    return StepSpec(**values)


class WorkflowRuntimeTests(unittest.TestCase):
    def test_create_initializes_separate_pending_state(self):
        plan = make_plan(
            make_step("second", depends_on=("first",)),
            make_step("first"),
        )

        runtime = WorkflowRuntimeState.create(plan, goal="inspect")

        self.assertEqual(runtime.goal, "inspect")
        self.assertEqual(runtime.execution_order, ("first", "second"))
        self.assertEqual(runtime.status, "running")
        self.assertEqual(runtime.results, {})
        self.assertTrue(all(step.status == "pending" for step in runtime.steps.values()))

    def test_valid_transitions_record_only_runtime_data(self):
        step = make_step("read")
        plan = make_plan(step)
        identity = plan.identity()
        runtime = WorkflowRuntimeState.create(plan)
        args = {"path": "read.py"}
        result = ToolResult.success("code_reader", data="content")

        runtime.steps["read"].mark_running(args)
        runtime.record_result("read", result)
        runtime.finish(plan)

        self.assertEqual(runtime.status, "completed")
        self.assertEqual(runtime.steps["read"].attempts, 1)
        self.assertIs(runtime.results["read"], result)
        self.assertEqual(plan.identity(), identity)
        self.assertNotIn("content", repr(step.identity()))

    def test_invalid_step_and_workflow_transitions_are_rejected(self):
        step = RuntimeStepState("read", ("identity",))
        with self.assertRaises(WorkflowRuntimeTransitionError):
            step.record_result(ToolResult.success("code_reader"))

        plan = make_plan(make_step("read"))
        runtime = WorkflowRuntimeState.create(plan)
        runtime.status = "completed"
        with self.assertRaises(WorkflowRuntimeTransitionError):
            runtime.finish(plan)

    def test_runtime_from_another_plan_is_rejected(self):
        first = make_plan(make_step("read", args={"path": "one.py"}))
        second = make_plan(make_step("read", args={"path": "two.py"}))
        runtime = WorkflowRuntimeState.create(first)

        with self.assertRaises(WorkflowRuntimeMismatchError):
            runtime.validate_for_plan(second)

    def test_repeat_completed_resets_only_declaring_step(self):
        repeated = make_step("repeat", repeat_completed=True)
        preserved = make_step("preserve")
        plan = make_plan(repeated, preserved)
        runtime = WorkflowRuntimeState.create(plan)
        for step in plan.steps:
            runtime.steps[step.id].mark_running(dict(step.args))
            runtime.record_result(
                step.id,
                ToolResult.success("code_reader", data=step.id),
            )
        runtime.finish(plan)

        runtime.prepare_resume(plan)

        self.assertEqual(runtime.steps["repeat"].status, "pending")
        self.assertNotIn("repeat", runtime.results)
        self.assertEqual(runtime.steps["preserve"].status, "ok")
        self.assertIn("preserve", runtime.results)

    def test_cancelled_runtime_cannot_be_resumed(self):
        plan = make_plan(make_step("read"))
        runtime = WorkflowRuntimeState.create(plan)
        runtime.steps["read"].mark_awaiting_approval({"path": "read.py"})
        runtime.mark_awaiting_approval("read")
        runtime.mark_cancelled("read", "cancelled")

        with self.assertRaises(WorkflowRuntimeTransitionError):
            runtime.prepare_resume(plan)


if __name__ == "__main__":
    unittest.main()
