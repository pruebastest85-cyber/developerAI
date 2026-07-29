import tempfile
import unittest
from pathlib import Path
from unittest import mock

from brain.agent import DeveloperAgent
from brain.approval_controller import ApprovalController, ApprovalRequiredError
from brain.execution_engine import ExecutionEngine
from brain.workflow_limits import WorkflowLimits
from brain.workflow_plan import ResultRef, StepSpec, WorkflowPlan
from brain.workflow_runtime import (
    WorkflowRuntimeMismatchError,
    WorkflowRuntimeTransitionError,
)
from brain.workflow_tool_executor import WorkflowExecutorConfigurationError
from tools.tool_result import ToolResult


def step(step_id, tool="code_reader", action="read_file", **overrides):
    values = {
        "id": step_id,
        "tool": tool,
        "action": action,
        "args": {"path": f"{step_id}.py"},
    }
    values.update(overrides)
    return StepSpec(**values)


class WorkflowExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.agent = DeveloperAgent(
            client=None,
            memory_file=root / "memory.json",
            prompt_dir="prompts",
            base_dir=root,
            action_log_file=root / "actions.json",
        )
        self.agent.permission_manager.medium_requires_confirmation = False
        self.engine = ExecutionEngine(self.agent)

    def _approve(self, error):
        controller = ApprovalController(self.agent)
        requested = controller.request_operation(
            error.tool_name,
            error.action_name,
            error.important_args,
            error.execute,
            force_approval=error.force_approval,
            on_cancel=error.on_cancel,
            on_request=error.on_request,
        )
        return controller, requested

    def test_empty_workflow_completes(self):
        runtime = self.engine.run_workflow(WorkflowPlan(()), goal="nothing")
        self.assertEqual(runtime.status, "completed")
        self.assertEqual(runtime.execution_order, ())

    def test_topological_execution_and_binding_use_latest_tool_result(self):
        calls = []

        def read_execute(args, structured=False):
            calls.append(("read", dict(args), structured))
            return ToolResult.success(
                "code_reader",
                data={"path": "module.py"},
            )

        def analyze_execute(args, structured=False):
            calls.append(("analyze", dict(args), structured))
            return ToolResult.success("code_analyzer", data="summary")

        self.agent.code_reader.execute = read_execute
        self.agent.code_analyzer.execute = analyze_execute
        producer = step("producer", args={"path": "source.py"})
        consumer = step(
            "consumer",
            tool="code_analyzer",
            action="summarize",
            args={},
            bindings={"path": ResultRef("producer", ("data", "path"))},
            depends_on=("producer",),
        )
        plan = WorkflowPlan((consumer, producer))
        identity = plan.identity()

        runtime = self.engine.run_workflow(plan)

        self.assertEqual([call[0] for call in calls], ["read", "analyze"])
        self.assertEqual(calls[1][1], {"path": "module.py"})
        self.assertTrue(all(call[2] for call in calls))
        self.assertEqual(runtime.status, "completed")
        self.assertEqual(runtime.steps["consumer"].resolved_args, {"path": "module.py"})
        self.assertEqual(plan.identity(), identity)
        self.assertEqual(dict(consumer.args), {})

    def test_failed_and_partial_dependencies_are_skipped_but_independent_runs(self):
        for failed_result in (
            ToolResult.failure("code_reader", error="boom"),
            ToolResult.incomplete("code_reader", data="some"),
        ):
            with self.subTest(status=failed_result.status):
                calls = []
                self.agent.code_reader.execute = (
                    lambda args, structured=False, result=failed_result: result
                )
                self.agent.code_analyzer.execute = (
                    lambda args, structured=False: calls.append("dependent")
                    or ToolResult.success("code_analyzer", data="unused")
                )
                self.agent.test_runner.execute = (
                    lambda args=None, structured=False: calls.append("independent")
                    or ToolResult.success("test_runner", data={"ok": True})
                )
                producer = step("producer", args={"path": "source.py"})
                dependent = step(
                    "dependent",
                    tool="code_analyzer",
                    action="summarize",
                    args={"path": "source.py"},
                    depends_on=("producer",),
                )
                independent = step(
                    "independent",
                    tool="test_runner",
                    action="run_tests",
                    args={},
                )

                runtime = self.engine.run_workflow(
                    WorkflowPlan((producer, dependent, independent))
                )

                self.assertEqual(runtime.status, "failed")
                self.assertEqual(runtime.steps["producer"].status, failed_result.status)
                self.assertEqual(runtime.steps["dependent"].status, "skipped")
                self.assertIn("producer=", runtime.steps["dependent"].reason)
                self.assertEqual(calls, ["independent"])

    def test_optional_failure_does_not_invalidate_workflow(self):
        self.agent.code_reader.execute = lambda args, structured=False: ToolResult.failure(
            "code_reader", error="optional failure"
        )
        plan = WorkflowPlan((step("optional", required=False),))

        runtime = self.engine.run_workflow(plan)

        self.assertEqual(runtime.steps["optional"].status, "failed")
        self.assertEqual(runtime.status, "completed")

    def test_prevalidation_rejects_all_invalid_steps_before_first_effect(self):
        calls = []
        self.agent.code_reader.execute = (
            lambda args, structured=False: calls.append("effect")
            or ToolResult.success("code_reader")
        )
        plan = WorkflowPlan(
            (
                step("valid"),
                step("invalid", action="__dict__"),
            )
        )

        with self.assertRaises(WorkflowExecutorConfigurationError):
            self.engine.run_workflow(plan)

        self.assertEqual(calls, [])

    def test_unregistered_and_unavailable_tools_are_rejected(self):
        unknown = WorkflowPlan(
            (
                step(
                    "unknown",
                    tool="missing",
                    action="run",
                    args={},
                ),
            )
        )
        with self.assertRaises(WorkflowExecutorConfigurationError):
            self.engine.run_workflow(unknown)

        saved = self.agent.code_reader
        del self.agent.code_reader
        self.addCleanup(setattr, self.agent, "code_reader", saved)
        with self.assertRaises(WorkflowExecutorConfigurationError):
            self.engine.run_workflow(WorkflowPlan((step("read"),)))

    def test_exact_payload_and_structured_result_are_required(self):
        seen = []

        def execute(args, structured=False):
            seen.append((args, structured))
            return ToolResult.success("code_reader", data="ok")

        self.agent.code_reader.execute = execute
        runtime = self.engine.run_workflow(
            WorkflowPlan(
                (
                    step(
                        "read",
                        args={"path": "main.py", "max_lines": 12},
                    ),
                )
            )
        )
        self.assertEqual(seen, [({"path": "main.py", "max_lines": 12}, True)])
        self.assertEqual(runtime.status, "completed")

        self.agent.code_reader.execute = lambda args, structured=False: "raw"
        invalid = self.engine.run_workflow(WorkflowPlan((step("raw"),)))
        self.assertEqual(invalid.status, "failed")
        self.assertIn(
            "no devolvió ToolResult",
            invalid.results["raw"].error,
        )

    def test_mismatched_tool_name_is_controlled_failure(self):
        self.agent.code_reader.execute = (
            lambda args, structured=False: ToolResult.success("code_analyzer")
        )

        runtime = self.engine.run_workflow(WorkflowPlan((step("read"),)))

        self.assertEqual(runtime.status, "failed")
        self.assertEqual(runtime.steps["read"].status, "failed")
        self.assertIn("no a code_reader", runtime.results["read"].error)

    def test_runtime_resume_preserves_completed_and_repeats_only_explicit_step(self):
        calls = []

        def execute(args, structured=False):
            calls.append(args["path"])
            return ToolResult.success("code_reader", data=args["path"])

        self.agent.code_reader.execute = execute
        plan = WorkflowPlan(
            (
                step("preserved", args={"path": "one.py"}),
                step(
                    "repeated",
                    args={"path": "two.py"},
                    repeat_completed=True,
                ),
            )
        )
        runtime = self.engine.run_workflow(plan)
        self.engine.run_workflow(plan, runtime=runtime)

        self.assertEqual(calls, ["one.py", "two.py", "two.py"])
        self.assertEqual(runtime.steps["preserved"].attempts, 1)
        self.assertEqual(runtime.steps["repeated"].attempts, 2)

    def test_runtime_for_another_plan_and_manual_awaiting_resume_are_rejected(self):
        first = WorkflowPlan((step("read", args={"path": "one.py"}),))
        second = WorkflowPlan((step("read", args={"path": "two.py"}),))
        runtime = self.engine.run_workflow(
            first,
            runtime=None,
        )
        with self.assertRaises(WorkflowRuntimeMismatchError):
            self.engine.run_workflow(second, runtime=runtime)

        pending = self.engine.run_workflow(first)
        pending.status = "awaiting_approval"
        with self.assertRaises(WorkflowRuntimeTransitionError):
            self.engine.run_workflow(first, runtime=pending)

    def test_required_approval_pauses_then_continues_without_repeating(self):
        calls = []

        def execute(args, structured=False):
            calls.append(args["path"])
            return ToolResult.success("code_reader", data=args["path"])

        self.agent.code_reader.execute = execute
        plan = WorkflowPlan(
            (
                step("before", args={"path": "before.py"}),
                step(
                    "approved",
                    args={"path": "approved.py"},
                    approval="required",
                    depends_on=("before",),
                ),
                step(
                    "after",
                    args={"path": "after.py"},
                    depends_on=("approved",),
                ),
            )
        )

        with self.assertRaises(ApprovalRequiredError) as raised:
            self.engine.run_workflow(plan)

        runtime = self.engine.last_workflow_runtime
        self.assertEqual(runtime.status, "awaiting_approval")
        self.assertEqual(runtime.awaiting_step_id, "approved")
        self.assertEqual(runtime.steps["approved"].attempts, 0)
        self.assertEqual(calls, ["before.py"])

        controller, requested = self._approve(raised.exception)
        self.assertEqual(runtime.approval_request_id, requested.request_id)
        approved = controller.approve(requested.request_id)

        self.assertEqual(approved.status, "approved")
        self.assertIs(approved.result, runtime)
        self.assertEqual(runtime.status, "completed")
        self.assertEqual(calls, ["before.py", "approved.py", "after.py"])
        self.assertEqual(runtime.steps["approved"].attempts, 1)

    def test_approved_continuation_exception_fails_runtime_before_reraising(self):
        calls = []
        failure = RuntimeError("approved continuation failed")

        def execute(args, structured=False):
            calls.append(args["path"])
            if args["path"] == "approved.py":
                raise failure
            return ToolResult.success("code_reader", data=args["path"])

        self.agent.code_reader.execute = execute
        plan = WorkflowPlan(
            (
                step("before", args={"path": "before.py"}),
                step(
                    "approved",
                    args={"path": "approved.py"},
                    approval="required",
                    depends_on=("before",),
                ),
                step(
                    "after",
                    args={"path": "after.py"},
                    depends_on=("approved",),
                ),
            )
        )

        with mock.patch.object(
            self.engine,
            "run_workflow",
            wraps=self.engine.run_workflow,
        ) as run:
            with self.assertRaises(ApprovalRequiredError) as raised:
                self.engine.run_workflow(plan)
            runtime = self.engine.last_workflow_runtime

            try:
                raised.exception.execute()
            except RuntimeError as caught_exception:
                propagated_exception = caught_exception
                caught_traceback = caught_exception.__traceback__
            else:
                self.fail("La continuación debía propagar RuntimeError")

        self.assertIs(propagated_exception, failure)
        self.assertIsNotNone(caught_traceback)
        self.assertIsNone(propagated_exception.__cause__)
        self.assertEqual(runtime.status, "failed")
        self.assertEqual(runtime.steps["before"].status, "ok")
        self.assertEqual(runtime.steps["approved"].status, "failed")
        self.assertEqual(runtime.steps["after"].status, "pending")
        self.assertEqual(runtime.results["approved"].status, "failed")
        self.assertEqual(runtime.steps["before"].attempts, 1)
        self.assertEqual(runtime.steps["approved"].attempts, 1)
        self.assertEqual(calls, ["before.py", "approved.py"])
        run.assert_called_once()

    def test_safe_logging_never_receives_approved_step_arguments(self):
        events = []
        self.agent.action_logger.log = lambda *args, **kwargs: events.append(
            {"args": args, "kwargs": kwargs}
        )
        failure = RuntimeError("PRIVATE_EXCEPTION_MESSAGE")
        self.agent.code_reader.execute = lambda args, structured=False: (
            (_ for _ in ()).throw(failure)
        )
        plan = WorkflowPlan(
            (
                step(
                    "approved",
                    args={"path": "PRIVATE_PATH.py"},
                    approval="required",
                ),
            )
        )

        with self.assertRaises(ApprovalRequiredError) as raised:
            self.engine.run_workflow(plan, safe_logging=True)
        with self.assertRaises(RuntimeError):
            raised.exception.execute()

        serialized = str(events)
        self.assertNotIn("PRIVATE_PATH.py", serialized)
        self.assertNotIn("PRIVATE_EXCEPTION_MESSAGE", serialized)
        self.assertNotIn("resolved_args", serialized)
        self.assertEqual(len(events), 1)
        self.assertEqual(
            events[0]["kwargs"]["params"],
            {
                "workflow_step": "approved",
                "action": "read_file",
                "status": "failed",
            },
        )
        self.assertEqual(events[0]["kwargs"]["result"], {"status": "failed"})

    def test_logger_failure_does_not_replace_approved_tool_exception(self):
        calls = []
        failure = RuntimeError("tool failure")
        self.agent.code_reader.execute = lambda args, structured=False: (
            calls.append("tool")
            or (_ for _ in ()).throw(failure)
        )
        self.agent.action_logger.log = mock.Mock(side_effect=RuntimeError("logger"))
        plan = WorkflowPlan((step("approved", approval="required"),))

        with mock.patch.object(
            self.engine,
            "run_workflow",
            wraps=self.engine.run_workflow,
        ) as run:
            with self.assertRaises(ApprovalRequiredError) as raised:
                self.engine.run_workflow(plan, safe_logging=True)
            try:
                raised.exception.execute()
            except RuntimeError as caught_exception:
                propagated_exception = caught_exception
                caught_traceback = caught_exception.__traceback__
            else:
                self.fail("La continuación debía propagar RuntimeError")
            with self.assertRaises(WorkflowRuntimeTransitionError):
                raised.exception.execute()

        self.assertIs(propagated_exception, failure)
        self.assertIsNotNone(caught_traceback)
        self.assertIsNone(propagated_exception.__cause__)
        self.assertEqual(calls, ["tool"])
        run.assert_called_once()

    def test_record_result_failure_does_not_replace_approved_tool_exception(self):
        calls = []
        failure = RuntimeError("tool failure")
        self.agent.code_reader.execute = lambda args, structured=False: (
            calls.append("tool")
            or (_ for _ in ()).throw(failure)
        )
        plan = WorkflowPlan((step("approved", approval="required"),))

        with mock.patch.object(
            self.engine,
            "run_workflow",
            wraps=self.engine.run_workflow,
        ) as run:
            with self.assertRaises(ApprovalRequiredError) as raised:
                self.engine.run_workflow(plan, safe_logging=True)
            runtime = self.engine.last_workflow_runtime
            with mock.patch.object(
                runtime,
                "record_result",
                side_effect=RuntimeError("record result"),
            ):
                try:
                    raised.exception.execute()
                except RuntimeError as caught_exception:
                    propagated_exception = caught_exception
                    caught_traceback = caught_exception.__traceback__
                else:
                    self.fail("La continuación debía propagar RuntimeError")
            with self.assertRaises(WorkflowRuntimeTransitionError):
                raised.exception.execute()

        self.assertIs(propagated_exception, failure)
        self.assertIsNotNone(caught_traceback)
        self.assertIsNone(propagated_exception.__cause__)
        self.assertEqual(runtime.status, "failed")
        self.assertEqual(runtime.steps["approved"].status, "running")
        self.assertEqual(calls, ["tool"])
        run.assert_called_once()

    def test_finish_failure_does_not_replace_approved_tool_exception(self):
        calls = []
        failure = RuntimeError("tool failure")
        self.agent.code_reader.execute = lambda args, structured=False: (
            calls.append("tool")
            or (_ for _ in ()).throw(failure)
        )
        plan = WorkflowPlan((step("approved", approval="required"),))

        with mock.patch.object(
            self.engine,
            "run_workflow",
            wraps=self.engine.run_workflow,
        ) as run:
            with self.assertRaises(ApprovalRequiredError) as raised:
                self.engine.run_workflow(plan, safe_logging=True)
            runtime = self.engine.last_workflow_runtime
            with mock.patch.object(
                runtime,
                "finish",
                side_effect=RuntimeError("finish"),
            ):
                try:
                    raised.exception.execute()
                except RuntimeError as caught_exception:
                    propagated_exception = caught_exception
                    caught_traceback = caught_exception.__traceback__
                else:
                    self.fail("La continuación debía propagar RuntimeError")
            with self.assertRaises(WorkflowRuntimeTransitionError):
                raised.exception.execute()

        self.assertIs(propagated_exception, failure)
        self.assertIsNotNone(caught_traceback)
        self.assertIsNone(propagated_exception.__cause__)
        self.assertEqual(runtime.status, "running")
        self.assertEqual(runtime.steps["approved"].status, "failed")
        self.assertEqual(calls, ["tool"])
        run.assert_called_once()

    def test_none_does_not_bypass_mandatory_policy(self):
        calls = []
        self.agent.file_creator.execute = (
            lambda args, structured=False: calls.append("write")
            or ToolResult.success("file_creator", data={"created": True})
        )
        plan = WorkflowPlan(
            (
                step(
                    "write",
                    tool="file_creator",
                    action="create_file",
                    args={"path": "new.txt", "content": "hello"},
                    approval="none",
                ),
            )
        )

        with self.assertRaises(ApprovalRequiredError):
            self.engine.run_workflow(plan)

        self.assertEqual(calls, [])
        self.assertEqual(self.engine.last_workflow_runtime.status, "awaiting_approval")

    def test_policy_uses_existing_permission_manager_decision(self):
        calls = []
        self.agent.code_reader.execute = (
            lambda args, structured=False: calls.append("read")
            or ToolResult.success("code_reader", data="ok")
        )

        runtime = self.engine.run_workflow(
            WorkflowPlan((step("read", approval="policy"),))
        )

        self.assertEqual(runtime.status, "completed")
        self.assertEqual(calls, ["read"])

    def test_consecutive_approvals_continue_same_runtime(self):
        calls = []
        self.agent.code_reader.execute = (
            lambda args, structured=False: calls.append(args["path"])
            or ToolResult.success("code_reader", data=args["path"])
        )
        plan = WorkflowPlan(
            (
                step("first", args={"path": "first.py"}, approval="required"),
                step(
                    "second",
                    args={"path": "second.py"},
                    approval="required",
                    depends_on=("first",),
                ),
            )
        )

        with self.assertRaises(ApprovalRequiredError) as raised:
            self.engine.run_workflow(plan)
        runtime = self.engine.last_workflow_runtime
        controller, first_request = self._approve(raised.exception)

        first_result = controller.approve(first_request.request_id)

        self.assertEqual(first_result.status, "awaiting_approval")
        second_request_id = first_result.request_id
        self.assertEqual(runtime.status, "awaiting_approval")
        self.assertEqual(runtime.awaiting_step_id, "second")
        self.assertEqual(runtime.approval_request_id, second_request_id)
        self.assertEqual(calls, ["first.py"])

        second_result = controller.approve(second_request_id)

        self.assertEqual(second_result.status, "approved")
        self.assertIs(second_result.result, runtime)
        self.assertEqual(runtime.status, "completed")
        self.assertEqual(calls, ["first.py", "second.py"])

    def test_reject_and_cancel_make_workflow_terminal_without_execution(self):
        for operation in ("reject", "cancel"):
            with self.subTest(operation=operation):
                calls = []
                self.agent.code_reader.execute = (
                    lambda args, structured=False: calls.append("run")
                    or ToolResult.success("code_reader")
                )
                plan = WorkflowPlan(
                    (
                        step(
                            "approval",
                            approval="required",
                        ),
                    )
                )
                with self.assertRaises(ApprovalRequiredError) as raised:
                    self.engine.run_workflow(plan)
                runtime = self.engine.last_workflow_runtime
                controller, requested = self._approve(raised.exception)

                result = getattr(controller, operation)(requested.request_id)

                expected_status = "rejected" if operation == "reject" else "cancelled"
                self.assertEqual(result.status, expected_status)
                self.assertEqual(runtime.status, "cancelled")
                self.assertEqual(runtime.steps["approval"].status, "skipped")
                self.assertEqual(calls, [])

    def test_limits_are_checked_before_effect(self):
        calls = []
        self.agent.file_creator.execute = (
            lambda args, structured=False: calls.append("effect")
            or ToolResult.success("file_creator")
        )
        plan = WorkflowPlan(
            (
                step(
                    "large",
                    tool="file_creator",
                    action="create_file",
                    args={"path": "large.txt", "content": "é" * 4},
                ),
            )
        )

        self.engine.workflow_limits = WorkflowLimits(max_new_file_bytes=7)
        runtime = self.engine.run_workflow(plan)

        self.assertEqual(runtime.status, "failed")
        self.assertIn("max_new_file_bytes", runtime.results["large"].error)
        self.assertEqual(calls, [])

    def test_inspected_file_limit_is_checked_before_second_read(self):
        calls = []
        self.agent.code_reader.execute = (
            lambda args, structured=False: calls.append(args["path"])
            or ToolResult.success("code_reader", data="ok")
        )
        plan = WorkflowPlan(
            (
                step("one", args={"path": "one.py"}),
                step("two", args={"path": "two.py"}),
            )
        )

        self.engine.workflow_limits = WorkflowLimits(max_inspected_files=1)
        runtime = self.engine.run_workflow(plan)

        self.assertEqual(calls, ["one.py"])
        self.assertEqual(runtime.results["two"].status, "failed")
        self.assertIn("max_inspected_files", runtime.results["two"].error)

    def test_write_aggregate_limits_are_checked_before_approval_or_effect(self):
        scenarios = (
            (
                "modified",
                WorkflowLimits(max_modified_files=1),
                {"modified_files": {"existing.txt"}},
                {"path": "new.txt", "content": "x"},
                "max_modified_files",
            ),
            (
                "bytes",
                WorkflowLimits(max_total_change_bytes=5),
                {"total_change_bytes": 4},
                {"path": "new.txt", "content": "xx"},
                "max_total_change_bytes",
            ),
            (
                "lines",
                WorkflowLimits(max_changed_lines=1),
                {"changed_lines": 1},
                {"path": "new.txt", "content": "line"},
                "max_changed_lines",
            ),
        )
        for name, limits, runtime_values, args, expected in scenarios:
            with self.subTest(name=name):
                calls = []
                self.agent.file_creator.execute = (
                    lambda payload, structured=False: calls.append("effect")
                    or ToolResult.success("file_creator")
                )
                plan = WorkflowPlan(
                    (
                        step(
                            "write",
                            tool="file_creator",
                            action="create_file",
                            args=args,
                        ),
                    )
                )
                from brain.workflow_runtime import WorkflowRuntimeState

                runtime = WorkflowRuntimeState.create(plan)
                for field_name, value in runtime_values.items():
                    setattr(runtime, field_name, value)

                self.engine.workflow_limits = limits
                result = self.engine.run_workflow(plan, runtime=runtime)

                self.assertEqual(result.status, "failed")
                self.assertIn(expected, result.results["write"].error)
                self.assertEqual(calls, [])

    def test_logger_failure_does_not_stop_workflow(self):
        self.agent.code_reader.execute = (
            lambda args, structured=False: ToolResult.success("code_reader", data="ok")
        )
        with mock.patch.object(
            self.agent.action_logger,
            "log",
            side_effect=lambda *args, **kwargs: None,
        ):
            runtime = self.engine.run_workflow(WorkflowPlan((step("read"),)))
        self.assertEqual(runtime.status, "completed")


if __name__ == "__main__":
    unittest.main()
