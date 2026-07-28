import copy
import unittest

from brain.workflow_plan import (
    ArgumentResolver,
    ResultRef,
    ResultResolutionError,
    StepSpec,
    WorkflowPlan,
    WorkflowValidationError,
)
from tools.tool_result import ToolResult


def make_step(step_id, **overrides):
    values = {
        "id": step_id,
        "action": "inspect",
        "tool": "code_reader",
        "args": {"path": "brain/agent.py"},
        "goal": "Inspect file",
    }
    values.update(overrides)
    return StepSpec(**values)


class WorkflowPlanTests(unittest.TestCase):
    def test_identity_is_deterministic_for_mapping_order(self):
        first = make_step(
            "inspect",
            args={"path": "brain/agent.py", "options": {"limit": 10, "mode": "text"}},
        )
        second = make_step(
            "inspect",
            args={"options": {"mode": "text", "limit": 10}, "path": "brain/agent.py"},
        )

        self.assertEqual(first.identity(), second.identity())
        self.assertEqual(WorkflowPlan((first,)).identity(), WorkflowPlan((second,)).identity())

    def test_duplicate_ids_are_rejected(self):
        with self.assertRaises(WorkflowValidationError):
            WorkflowPlan((make_step("same"), make_step("same")))

    def test_missing_and_self_dependencies_are_rejected(self):
        with self.assertRaises(WorkflowValidationError):
            WorkflowPlan((make_step("one", depends_on=("missing",)),))
        with self.assertRaises(WorkflowValidationError):
            WorkflowPlan((make_step("one", depends_on=("one",)),))

    def test_direct_and_indirect_cycles_are_rejected(self):
        direct = (
            make_step("one", depends_on=("two",)),
            make_step("two", depends_on=("one",)),
        )
        indirect = (
            make_step("one", depends_on=("three",)),
            make_step("two", depends_on=("one",)),
            make_step("three", depends_on=("two",)),
        )
        for steps in (direct, indirect):
            with self.subTest(steps=steps):
                with self.assertRaises(WorkflowValidationError):
                    WorkflowPlan(steps)

    def test_unknown_tool_is_rejected_when_allowlist_is_supplied(self):
        with self.assertRaises(WorkflowValidationError):
            WorkflowPlan(
                (make_step("one", tool="unknown"),),
                allowed_tools=frozenset({"code_reader"}),
            )

    def test_unsupported_and_circular_declarative_values_are_rejected(self):
        class Unsupported:
            pass

        with self.assertRaises(WorkflowValidationError):
            WorkflowPlan((make_step("one", args={"value": Unsupported()}),))

        circular = []
        circular.append(circular)
        with self.assertRaises(WorkflowValidationError):
            WorkflowPlan((make_step("one", args={"value": circular}),))

    def test_invalid_approval_and_overlapping_arguments_are_rejected(self):
        with self.assertRaises(WorkflowValidationError):
            make_step("one", approval="sometimes")
        with self.assertRaises(WorkflowValidationError):
            make_step(
                "one",
                args={"path": "literal.py"},
                bindings={"path": ResultRef("source", ("data", "path"))},
            )

    def test_result_reference_to_unknown_step_is_rejected(self):
        with self.assertRaises(WorkflowValidationError):
            WorkflowPlan(
                (
                    make_step(
                        "consumer",
                        bindings={"path": ResultRef("missing", ("data", "path"))},
                    ),
                )
            )

    def test_result_references_participate_in_order_and_cycle_validation(self):
        producer = make_step("producer")
        consumer = make_step(
            "consumer",
            args={},
            bindings={"path": ResultRef("producer", ("data", "path"))},
        )
        plan = WorkflowPlan((consumer, producer))
        self.assertEqual(
            [step.id for step in plan.execution_order()],
            ["producer", "consumer"],
        )

        with self.assertRaises(WorkflowValidationError):
            WorkflowPlan(
                (
                    make_step(
                        "one",
                        args={},
                        bindings={"value": ResultRef("two", ("data",))},
                    ),
                    make_step(
                        "two",
                        args={},
                        bindings={"value": ResultRef("one", ("data",))},
                    ),
                )
            )

    def test_result_ref_rejects_self_reference_and_negative_index(self):
        with self.assertRaises(WorkflowValidationError):
            ResultRef("producer", ("data", -1))
        with self.assertRaises(WorkflowValidationError):
            WorkflowPlan(
                (
                    make_step(
                        "one",
                        args={},
                        bindings={"value": ResultRef("one", ("data",))},
                    ),
                )
            )

    def test_repeat_completed_is_preserved_in_identity(self):
        normal = make_step("one", repeat_completed=False)
        repeated = make_step("one", repeat_completed=True)

        self.assertFalse(normal.repeat_completed)
        self.assertTrue(repeated.repeat_completed)
        self.assertNotEqual(normal.identity(), repeated.identity())

    def test_execution_order_respects_dependencies_stably(self):
        plan = WorkflowPlan(
            (
                make_step("inspect"),
                make_step("independent"),
                make_step("analyze", depends_on=("inspect",)),
            )
        )

        self.assertEqual(
            [step.id for step in plan.execution_order()],
            ["inspect", "independent", "analyze"],
        )

    def test_runtime_results_do_not_change_declarative_identity(self):
        producer = make_step("producer")
        consumer = make_step(
            "consumer",
            args={},
            bindings={"path": ResultRef("producer", ("data", "path"))},
            depends_on=("producer",),
        )
        plan = WorkflowPlan((producer, consumer))
        before = plan.identity()
        resolver = ArgumentResolver()

        first = resolver.resolve(
            consumer,
            {"producer": ToolResult.success("code_reader", data={"path": "one.py"})},
        )
        second = resolver.resolve(
            consumer,
            {"producer": ToolResult.success("code_reader", data={"path": "two.py"})},
        )

        self.assertNotEqual(first, second)
        self.assertEqual(plan.identity(), before)


class ArgumentResolverTests(unittest.TestCase):
    def setUp(self):
        self.resolver = ArgumentResolver()

    def test_extracts_nested_data_and_list_indices(self):
        step = make_step(
            "consumer",
            args={"literal": 1},
            bindings={
                "name": ResultRef("producer", ("data", "items", 1, "name")),
            },
        )
        result = ToolResult.success(
            "code_reader",
            data={"items": [{"name": "zero"}, {"name": "one"}]},
        )

        self.assertEqual(
            self.resolver.resolve(step, {"producer": result}),
            {"literal": 1, "name": "one"},
        )

    def test_accesses_only_seven_field_tool_result_view(self):
        step = make_step(
            "consumer",
            args={},
            bindings={
                "tool": ResultRef("producer", ("tool_name",)),
                "status": ResultRef("producer", ("status",)),
                "source": ResultRef("producer", ("metadata", "source")),
            },
        )
        result = ToolResult.success(
            "code_reader",
            metadata={"source": "inspection"},
        )

        resolved = self.resolver.resolve(step, {"producer": result})
        self.assertEqual(
            resolved,
            {"tool": "code_reader", "status": "ok", "source": "inspection"},
        )

    def test_missing_path_and_invalid_index_are_rejected(self):
        result = ToolResult.success("code_reader", data={"items": [1]})
        missing = make_step(
            "missing",
            args={},
            bindings={"value": ResultRef("producer", ("data", "unknown"))},
        )
        invalid_index = make_step(
            "index",
            args={},
            bindings={"value": ResultRef("producer", ("data", "items", 4))},
        )

        with self.assertRaises(ResultResolutionError):
            self.resolver.resolve(missing, {"producer": result})
        with self.assertRaises(ResultResolutionError):
            self.resolver.resolve(invalid_index, {"producer": result})

    def test_incomplete_failed_and_partial_steps_are_rejected(self):
        step = make_step(
            "consumer",
            args={},
            bindings={"value": ResultRef("producer", ("data",))},
        )
        with self.assertRaises(ResultResolutionError):
            self.resolver.resolve(step, {})
        with self.assertRaises(ResultResolutionError):
            self.resolver.resolve(
                step,
                {"producer": ToolResult.failure("code_reader", error="failed")},
            )
        with self.assertRaises(ResultResolutionError):
            self.resolver.resolve(
                step,
                {"producer": ToolResult.incomplete("code_reader", data=[1])},
            )

    def test_resolution_does_not_mutate_step_or_runtime_value(self):
        literal = {"options": {"limit": 2}}
        runtime_data = {"items": [{"name": "one"}]}
        step = make_step(
            "consumer",
            args=literal,
            bindings={"item": ResultRef("producer", ("data", "items", 0))},
        )
        identity = step.identity()

        resolved = self.resolver.resolve(
            step,
            {"producer": ToolResult.success("code_reader", data=runtime_data)},
        )
        resolved["options"]["limit"] = 99
        resolved["item"]["name"] = "changed"

        self.assertEqual(step.args["options"]["limit"], 2)
        self.assertEqual(runtime_data["items"][0]["name"], "one")
        self.assertEqual(step.identity(), identity)

    def test_arbitrary_attribute_access_is_rejected(self):
        class Payload:
            public = "not allowed"

        step = make_step(
            "consumer",
            args={},
            bindings={"value": ResultRef("producer", ("data", "public"))},
        )
        with self.assertRaises(ResultResolutionError):
            self.resolver.resolve(
                step,
                {"producer": ToolResult.success("code_reader", data=Payload())},
            )


if __name__ == "__main__":
    unittest.main()
