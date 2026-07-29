import tempfile
import threading
import unittest
from pathlib import Path
from types import MappingProxyType
from unittest import mock

from brain.agent import DeveloperAgent
from brain.approval_controller import (
    ApprovalController,
    ApprovalRequiredError,
    ConversationalController,
)
from brain.model_plan import (
    SAFE_MODEL_OPERATION_CATALOG,
    ModelPlanAdapter,
    ModelPlanDecision,
)
from brain.model_plan_review import (
    AUTHORITY_WARNING,
    ModelPlanReviewController,
    ModelPlanReviewError,
    ModelPlanReviewResult,
    ModelPlanReviewView,
    model_plan_id,
    parse_model_plan_command,
)
from brain.model_planning_service import ModelPlanningResult
from brain.local_model_client import ModelResponseMetadata
from brain.workflow_runtime import WorkflowRuntimeState
from brain.workflow_plan import StepSpec, WorkflowPlan
from tools.tool_result import ToolResult


def model_step(**changes):
    value = {
        "id": "inspect_1",
        "tool": "code_reader",
        "action": "read_file",
        "args": {"path": "brain/agent.py", "max_lines": 100},
        "goal": "Inspect safely",
        "depends_on": [],
        "justification": "Needed",
    }
    value.update(changes)
    return value


def planning_result(*, goal="Inspect", message="", steps=None):
    payload = {
        "schema_version": "1",
        "goal": goal,
        "completed": False,
        "steps": [model_step()] if steps is None else steps,
        "message": message,
    }
    decision = ModelPlanDecision.from_mapping(payload)
    workflow = ModelPlanAdapter(SAFE_MODEL_OPERATION_CATALOG).adapt(decision)
    metadata = ModelResponseMetadata(
        provider="lm_studio",
        requested_model="qwen",
        reported_model=None,
        request_id=None,
        input_tokens=None,
        output_tokens=None,
        total_tokens=None,
        finish_reason=None,
        duration_seconds=0,
        endpoint_id="lm_studio@localhost:1234",
        structured_format="json_schema",
    )
    return ModelPlanningResult(decision, workflow, metadata)


class ModelPlanReviewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.agent = DeveloperAgent(
            None,
            base_dir=self.temporary.name,
            action_log_file=Path(self.temporary.name) / "actions.json",
        )
        self.controller = self.agent.model_plan_review_controller

    def assert_code(self, code, operation):
        with self.assertRaises(ModelPlanReviewError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)

    def start_protected_pause(self, *, tool_execute=None, steps=None):
        calls = []
        if tool_execute is None:
            tool_execute = (
                lambda args, structured=False: calls.append(args["path"])
                or ToolResult.success("patch_generator", data="patch")
            )
        self.agent.patch_generator.execute = tool_execute
        if steps is None:
            steps = [model_step(
                tool="patch_generator",
                action="generate_patch",
                args={"path": "brain/agent.py", "new_content": "safe"},
            )]
        plan_id = self.controller.register(
            planning_result(steps=steps)
        ).plan_id
        with self.assertRaises(ApprovalRequiredError) as raised:
            self.controller.approve(plan_id)
        return raised.exception, calls

    def test_register_preserves_exact_result_and_replaces_only_after_success(self):
        first = planning_result(goal="First")
        view = self.controller.register(first)
        self.assertIs(type(view), ModelPlanReviewView)
        self.assertIs(self.controller._pending.result, first)

        second = planning_result(goal="Second")
        second_view = self.controller.register(second)
        self.assertIs(self.controller._pending.result, second)
        self.assertNotEqual(view.plan_id, second_view.plan_id)
        self.assertEqual(self.controller._last_terminal.status, "superseded")
        self.assert_code(
            "plan_id_mismatch",
            lambda: self.controller.approve(view.plan_id),
        )

    def test_identity_is_deterministic_and_covers_display_and_execution(self):
        baseline = planning_result()
        self.assertEqual(model_plan_id(baseline), model_plan_id(planning_result()))
        variants = [
            planning_result(goal="Different"),
            planning_result(message="Different"),
            planning_result(steps=[model_step(goal="Different")]),
            planning_result(steps=[model_step(args={"path": "other.py"})]),
            planning_result(steps=[model_step(id="other")]),
        ]
        for variant in variants:
            with self.subTest(variant=variant.decision):
                self.assertNotEqual(model_plan_id(baseline), model_plan_id(variant))
        self.assertRegex(model_plan_id(baseline), r"^mp1_[0-9a-f]{64}$")

    def test_justification_is_not_part_of_display_or_execution_identity(self):
        baseline = planning_result()
        changed = planning_result(steps=[model_step(
            justification="A different non-operational explanation",
        )])
        self.assertEqual(model_plan_id(baseline), model_plan_id(changed))
        self.assertNotIn(
            changed.decision.steps[0].justification,
            self.controller.register(changed).text,
        )
        self.assertNotIn(
            "justification",
            str(self.agent.action_logger.log_file.read_text(encoding="utf-8")),
        )

    def test_view_is_immutable_deterministic_bounded_and_sanitized(self):
        marker = "SECRET_NEW_CONTENT"
        result = planning_result(
            steps=[model_step(
                tool="patch_generator",
                action="generate_patch",
                args={
                    "new_content": marker,
                    "path": "brain/agent.py",
                },
            )]
        )
        first = self.controller.register(result)
        second = self.controller.get_pending()
        self.assertEqual(first.text, second.text)
        self.assertIn(first.plan_id, first.text)
        self.assertIn("patch_generator", first.text)
        self.assertIn("generate_patch", first.text)
        self.assertIn(AUTHORITY_WARNING, first.text)
        self.assertNotIn(marker, first.text)
        self.assertIn("UTF-8 bytes", first.text)
        self.assertLessEqual(len(first.text), 12_000)
        with self.assertRaises((AttributeError, TypeError)):
            first.status = "changed"
        log_text = (Path(self.temporary.name) / "actions.json").read_text(
            encoding="utf-8"
        )
        self.assertNotIn(marker, log_text)

    def test_commands_have_exact_separate_namespace(self):
        plan_id = model_plan_id(planning_result())
        self.assertEqual(
            parse_model_plan_command(f"aprobar-plan {plan_id}"),
            ("approve", plan_id),
        )
        invalid = [
            f"aprobar {plan_id}",
            f"aprobar-plan {plan_id[:-1]}",
            f"aprobar-plan {plan_id} extra",
            f"aprobar-plan {plan_id.upper()}",
            "aprobar-plan 123e4567-e89b-12d3-a456-426614174000",
        ]
        for command in invalid:
            with self.subTest(command=command):
                self.assertIsNone(parse_model_plan_command(command))

    def test_approve_revalidates_then_calls_run_workflow_once(self):
        result = planning_result()
        plan_id = self.controller.register(result).plan_id
        runtime = mock.Mock(spec=WorkflowRuntimeState)
        runtime.status = "completed"
        with mock.patch.object(
            self.agent.execution_engine,
            "run_workflow",
            return_value=runtime,
        ) as run, mock.patch.object(
            self.agent.permission_manager,
            "grant_approval",
            wraps=self.agent.permission_manager.grant_approval,
        ) as grant, mock.patch.object(
            self.agent,
            "execute_tool",
        ) as execute:
            returned = self.controller.approve(plan_id)
        self.assertIs(returned, runtime)
        run.assert_called_once()
        called_plan = run.call_args.args[0]
        self.assertEqual(called_plan.identity(), result.workflow.identity())
        self.assertEqual(
            run.call_args.kwargs,
            {"goal": result.decision.goal, "safe_logging": True},
        )
        grant.assert_not_called()
        execute.assert_not_called()
        self.assertEqual(self.controller._last_terminal.status, "completed")

    def test_wrong_id_reject_cancel_and_double_approval_do_not_execute(self):
        run = mock.Mock()
        self.agent.execution_engine.run_workflow = run
        plan_id = self.controller.register(planning_result()).plan_id
        wrong = "mp1_" + ("0" * 64)
        self.assert_code("plan_id_mismatch", lambda: self.controller.approve(wrong))
        self.assertIsNotNone(self.controller.get_pending())

        rejected = self.controller.reject(plan_id)
        self.assertIs(type(rejected), ModelPlanReviewResult)
        self.assertEqual(rejected.status, "rejected")
        run.assert_not_called()
        self.assert_code("plan_rejected", lambda: self.controller.approve(plan_id))

        plan_id = self.controller.register(planning_result(goal="Cancel")).plan_id
        cancelled = self.controller.cancel(plan_id)
        self.assertEqual(cancelled.status, "cancelled")
        run.assert_not_called()
        self.assert_code("plan_cancelled", lambda: self.controller.approve(plan_id))

        runtime = mock.Mock(spec=WorkflowRuntimeState)
        runtime.status = "completed"
        run.return_value = runtime
        plan_id = self.controller.register(planning_result(goal="Run")).plan_id
        self.controller.approve(plan_id)
        self.assert_code(
            "plan_already_consumed",
            lambda: self.controller.approve(plan_id),
        )
        run.assert_called_once()

    def test_concurrent_approval_cannot_execute_twice(self):
        plan_id = self.controller.register(planning_result()).plan_id
        entered = threading.Event()
        release = threading.Event()
        runtime = mock.Mock(spec=WorkflowRuntimeState)
        runtime.status = "completed"
        calls = []
        errors = []

        def run_workflow(*args, **kwargs):
            calls.append((args, kwargs))
            entered.set()
            release.wait(timeout=2)
            return runtime

        self.agent.execution_engine.run_workflow = run_workflow

        def approve():
            try:
                self.controller.approve(plan_id)
            except ModelPlanReviewError as exc:
                errors.append(exc.code)

        first = threading.Thread(target=approve)
        second = threading.Thread(target=approve)
        first.start()
        self.assertTrue(entered.wait(timeout=2))
        second.start()
        second.join(timeout=2)
        release.set()
        first.join(timeout=2)
        self.assertEqual(len(calls), 1)
        self.assertEqual(errors, ["plan_already_consumed"])

    def test_mutation_is_rejected_before_engine_and_consumes_plan(self):
        result = planning_result()
        plan_id = self.controller.register(result).plan_id
        object.__setattr__(result.decision, "goal", "mutated")
        with mock.patch.object(
            self.agent.execution_engine,
            "run_workflow",
        ) as run:
            self.assert_code(
                "plan_revalidation_failed",
                lambda: self.controller.approve(plan_id),
            )
        run.assert_not_called()
        self.assertEqual(
            self.controller._last_terminal.status,
            "revalidation_failed",
        )
        self.assert_code(
            "plan_already_consumed",
            lambda: self.controller.approve(plan_id),
        )

    def test_hostile_runtime_mutation_is_rejected_without_traversal(self):
        calls = {"items": 0, "iter": 0, "repr": 0}

        class HostileDict(dict):
            def items(self):
                calls["items"] += 1
                raise RuntimeError("SECRET_HOSTILE")

            def __iter__(self):
                calls["iter"] += 1
                raise RuntimeError("SECRET_HOSTILE")

            def __repr__(self):
                calls["repr"] += 1
                raise RuntimeError("SECRET_HOSTILE")

        result = planning_result()
        plan_id = self.controller.register(result).plan_id
        object.__setattr__(result.workflow.steps[0], "args", HostileDict())
        with mock.patch.object(
            self.agent.execution_engine,
            "run_workflow",
        ) as run:
            self.assert_code(
                "plan_revalidation_failed",
                lambda: self.controller.approve(plan_id),
            )
        run.assert_not_called()
        self.assertEqual(calls, {"items": 0, "iter": 0, "repr": 0})

    def test_hostile_mappingproxy_args_and_bindings_are_rejected_unread(self):
        for field in ("args", "bindings"):
            with self.subTest(field=field):
                calls = {
                    "items": 0,
                    "keys": 0,
                    "values": 0,
                    "iter": 0,
                    "get": 0,
                    "repr": 0,
                    "str": 0,
                }

                class HostileDict(dict):
                    def items(self):
                        calls["items"] += 1
                        raise RuntimeError("SECRET_PROXY_ITEMS")

                    def keys(self):
                        calls["keys"] += 1
                        raise RuntimeError("SECRET_PROXY_KEYS")

                    def values(self):
                        calls["values"] += 1
                        raise RuntimeError("SECRET_PROXY_VALUES")

                    def __iter__(self):
                        calls["iter"] += 1
                        raise RuntimeError("SECRET_PROXY_ITER")

                    def get(self, *args):
                        calls["get"] += 1
                        raise RuntimeError("SECRET_PROXY_GET")

                    def __repr__(self):
                        calls["repr"] += 1
                        raise RuntimeError("SECRET_PROXY_REPR")

                    def __str__(self):
                        calls["str"] += 1
                        raise RuntimeError("SECRET_PROXY_STR")

                result = planning_result(goal=f"Hostile {field}")
                plan_id = self.controller.register(result).plan_id
                object.__setattr__(
                    result.workflow.steps[0],
                    field,
                    MappingProxyType(HostileDict()),
                )
                with mock.patch.object(
                    self.agent.execution_engine,
                    "run_workflow",
                ) as run:
                    self.assert_code(
                        "plan_revalidation_failed",
                        lambda: self.controller.approve(plan_id),
                    )
                run.assert_not_called()
                self.assertEqual(calls, {name: 0 for name in calls})
                self.assertEqual(
                    self.controller._last_terminal.status,
                    "revalidation_failed",
                )
                self.assert_code(
                    "plan_already_consumed",
                    lambda: self.controller.approve(plan_id),
                )
                public_error = str(ModelPlanReviewError("plan_revalidation_failed"))
                log_text = self.agent.action_logger.log_file.read_text(
                    encoding="utf-8"
                )
                self.assertNotIn("SECRET_PROXY", public_error)
                self.assertNotIn("SECRET_PROXY", log_text)

    def test_recursive_preview_redacts_nested_secrets_and_content(self):
        marker_values = (
            "SECRET_TOKEN_VALUE",
            "SECRET_PASSWORD_VALUE",
            "SECRET_AUTH_VALUE",
            "SECRET_CONTENT_VALUE",
        )
        base = planning_result()
        step = StepSpec(
            id="inspect_1",
            tool="code_reader",
            action="read_file",
            args={
                "options": {
                    "Api_ToKeN": marker_values[0],
                    "nested": [
                        {"PASSWORD": marker_values[1]},
                        {"Authorization": marker_values[2]},
                        {"new_content": marker_values[3]},
                    ],
                }
            },
            goal="Inspect safely",
        )
        result = ModelPlanningResult(
            decision=base.decision,
            workflow=WorkflowPlan(
                (step,),
                allowed_tools=frozenset({"code_reader"}),
            ),
            metadata=base.metadata,
        )
        view = self.controller.register(result)
        for marker in marker_values:
            self.assertNotIn(marker, view.text)
        self.assertGreaterEqual(view.text.count("[REDACTED]"), 3)
        self.assertIn("UTF-8 bytes", view.text)

    def test_recursive_and_custom_preview_values_fail_closed(self):
        from brain.model_plan_review import _sanitize_preview_value

        recursive = {}
        recursive["nested"] = [recursive]
        hostile_calls = {"repr": 0, "str": 0}

        class Hostile:
            def __repr__(self):
                hostile_calls["repr"] += 1
                raise RuntimeError("SECRET_REPR")

            def __str__(self):
                hostile_calls["str"] += 1
                raise RuntimeError("SECRET_STR")

        recursive_result = _sanitize_preview_value(
            "options",
            recursive,
            frozenset(),
            depth=0,
            active=set(),
        )
        hostile_result = _sanitize_preview_value(
            "options",
            Hostile(),
            frozenset(),
            depth=0,
            active=set(),
        )
        self.assertIn("[UNSUPPORTED]", str(recursive_result))
        self.assertEqual(hostile_result, "[UNSUPPORTED]")
        self.assertEqual(hostile_calls, {"repr": 0, "str": 0})

    def test_mutated_operation_names_never_reach_failure_log(self):
        result = planning_result()
        original = self.controller.register(result)
        object.__setattr__(
            result.workflow.steps[0],
            "tool",
            "SECRET_MUTATED_TOOL",
        )
        object.__setattr__(
            result.workflow.steps[0],
            "action",
            "SECRET_MUTATED_ACTION",
        )
        self.assert_code(
            "plan_revalidation_failed",
            lambda: self.controller.approve(original.plan_id),
        )
        log_text = self.agent.action_logger.log_file.read_text(encoding="utf-8")
        self.assertNotIn("SECRET_MUTATED_TOOL", log_text)
        self.assertNotIn("SECRET_MUTATED_ACTION", log_text)
        self.assertIn("code_reader/read_file", log_text)

    def test_engine_failure_consumes_plan_and_propagates(self):
        plan_id = self.controller.register(planning_result()).plan_id
        failure = RuntimeError("engine failure")
        with mock.patch.object(
            self.agent.execution_engine,
            "run_workflow",
            side_effect=failure,
        ) as run:
            with self.assertRaises(RuntimeError) as caught:
                self.controller.approve(plan_id)
        self.assertIs(caught.exception, failure)
        run.assert_called_once()
        self.assertEqual(self.controller._last_terminal.status, "failed")
        self.assert_code(
            "plan_already_consumed",
            lambda: self.controller.approve(plan_id),
        )

    def test_real_read_workflow_uses_existing_policy(self):
        calls = []
        self.agent.code_reader.execute = (
            lambda args, structured=False: calls.append(dict(args))
            or ToolResult.success("code_reader", data="ok")
        )
        result = planning_result()
        plan_id = self.controller.register(result).plan_id
        with mock.patch.object(
            self.agent.permission_manager,
            "grant_approval",
            wraps=self.agent.permission_manager.grant_approval,
        ) as grant:
            runtime = self.controller.approve(plan_id)
        self.assertEqual(runtime.status, "completed")
        self.assertEqual(len(calls), 1)
        grant.assert_not_called()

    def test_path_policy_remains_active_for_model_workflow(self):
        result = planning_result(
            steps=[model_step(args={"path": "../outside.py"})]
        )
        plan_id = self.controller.register(result).plan_id
        runtime = self.controller.approve(plan_id)
        self.assertEqual(runtime.status, "failed")
        self.assertEqual(runtime.steps["inspect_1"].status, "failed")
        self.assertIn("..", runtime.results["inspect_1"].error)

    def test_protected_operation_still_requires_operational_approval(self):
        calls = []
        result = planning_result(steps=[model_step(
            tool="patch_generator",
            action="generate_patch",
            args={"path": "brain/agent.py", "new_content": "safe"},
        )])
        plan_id = self.controller.register(result).plan_id
        self.agent.patch_generator.execute = (
            lambda args, structured=False: calls.append("tool")
            or ToolResult.success("patch_generator", data="patch")
        )
        with mock.patch.object(
            self.agent.permission_manager,
            "grant_approval",
            wraps=self.agent.permission_manager.grant_approval,
        ) as grant, mock.patch.object(
            self.agent.execution_engine,
            "run_workflow",
            wraps=self.agent.execution_engine.run_workflow,
        ) as run:
            with self.assertRaises(ApprovalRequiredError) as raised:
                self.controller.approve(plan_id)
        grant.assert_not_called()
        run.assert_called_once()
        self.assertEqual(calls, [])
        runtime = self.agent.execution_engine.last_workflow_runtime
        self.assertEqual(runtime.status, "awaiting_approval")
        self.assertEqual(
            self.controller._last_terminal.status,
            "awaiting_operation_approval",
        )

        operational = ApprovalController(self.agent)
        pending = operational.request_operation(
            tool_name=raised.exception.tool_name,
            action_name=raised.exception.action_name,
            important_args=raised.exception.important_args,
            execute=raised.exception.execute,
            force_approval=raised.exception.force_approval,
            on_cancel=raised.exception.on_cancel,
            on_request=raised.exception.on_request,
        )
        approved = operational.approve(pending.request_id)
        self.assertEqual(approved.status, "approved")
        self.assertEqual(runtime.status, "completed")
        self.assertEqual(self.controller._last_terminal.status, "completed")
        self.assertEqual(calls, ["tool"])
        self.assertEqual(runtime.steps["inspect_1"].attempts, 1)
        run.assert_called_once()

    def test_operational_pause_logs_finish_once_only_after_completion(self):
        events = []
        self.agent.action_logger.log = lambda *args, **kwargs: events.append(
            kwargs["params"]
        )
        calls = []
        self.agent.patch_generator.execute = (
            lambda args, structured=False: calls.append("tool")
            or ToolResult.success("patch_generator", data="patch")
        )
        result = planning_result(steps=[model_step(
            tool="patch_generator",
            action="generate_patch",
            args={"path": "brain/agent.py", "new_content": "safe"},
        )])
        plan_id = self.controller.register(result).plan_id
        events.clear()
        with self.assertRaises(ApprovalRequiredError) as raised:
            self.controller.approve(plan_id)
        self.assertNotIn(
            "model_plan_execution_finished",
            [event["event"] for event in events],
        )

        operational = ApprovalController(self.agent)
        pending = operational.request_operation(
            tool_name=raised.exception.tool_name,
            action_name=raised.exception.action_name,
            important_args=raised.exception.important_args,
            execute=raised.exception.execute,
            force_approval=raised.exception.force_approval,
            on_cancel=raised.exception.on_cancel,
            on_request=raised.exception.on_request,
        )
        operational.approve(pending.request_id)
        finished = [
            event for event in events
            if event.get("event") == "model_plan_execution_finished"
        ]
        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0]["status"], "completed")
        self.assertNotIn(pending.request_id, str(events))
        self.assertEqual(calls, ["tool"])

    def test_operational_continuation_failure_finishes_global_record_once(self):
        events = []
        self.agent.action_logger.log = lambda *args, **kwargs: events.append(
            kwargs["params"]
        )
        runtime = mock.Mock(spec=WorkflowRuntimeState)
        runtime.status = "awaiting_approval"
        self.agent.execution_engine.last_workflow_runtime = runtime
        failure = RuntimeError("continuation failed")
        approval_error = ApprovalRequiredError(
            "patch_generator",
            "generate_patch",
            {},
            lambda: (_ for _ in ()).throw(failure),
            "approval",
        )
        self.agent.execution_engine.run_workflow = mock.Mock(
            side_effect=approval_error
        )
        plan_id = self.controller.register(planning_result()).plan_id
        events.clear()
        with self.assertRaises(ApprovalRequiredError) as raised:
            self.controller.approve(plan_id)
        with self.assertRaises(RuntimeError) as caught:
            raised.exception.execute()
        self.assertIs(caught.exception, failure)
        self.assertEqual(self.controller._last_terminal.status, "failed")
        self.assertEqual(
            [event.get("event") for event in events].count(
                "model_plan_execution_finished"
            ),
            1,
        )

    def test_real_operational_failure_keeps_runtime_and_global_terminal_consistent(self):
        events = []
        logger_calls = []

        def capture_log(*args, **kwargs):
            logger_calls.append({"args": args, "kwargs": kwargs})
            events.append(kwargs["params"])

        self.agent.action_logger.log = capture_log
        calls = []
        private_exception = "PRIVATE_EXCEPTION_MESSAGE"
        private_path = "PRIVATE_PATH.py"
        private_content = "PRIVATE_NEW_CONTENT"
        private_binding = "PRIVATE_BINDING"
        private_uuid = "11111111-2222-4333-8444-555555555555"
        private_token = "PRIVATE_APPROVAL_TOKEN"
        failure = RuntimeError(private_exception)

        def fail_after_approval(args, structured=False):
            calls.append("tool")
            raise failure

        result = planning_result(steps=[model_step(
            tool="patch_generator",
            action="generate_patch",
            args={
                "path": private_path,
                "new_content": "|".join(
                    (
                        private_content,
                        private_binding,
                        private_uuid,
                        private_token,
                    )
                ),
            },
        )])
        plan_id = self.controller.register(result).plan_id
        self.agent.patch_generator.execute = fail_after_approval
        events.clear()

        with mock.patch.object(
            self.agent.execution_engine,
            "run_workflow",
            wraps=self.agent.execution_engine.run_workflow,
        ) as run:
            with self.assertRaises(ApprovalRequiredError) as raised:
                self.controller.approve(plan_id)
            runtime = self.agent.execution_engine.last_workflow_runtime
            propagated = []
            continuation = raised.exception.execute

            def capture_continuation_exception():
                try:
                    return continuation()
                except BaseException as exc:
                    propagated.append(exc)
                    raise

            operational = ApprovalController(self.agent)
            pending = operational.request_operation(
                tool_name=raised.exception.tool_name,
                action_name=raised.exception.action_name,
                important_args=raised.exception.important_args,
                execute=capture_continuation_exception,
                force_approval=raised.exception.force_approval,
                on_cancel=raised.exception.on_cancel,
                on_request=raised.exception.on_request,
            )
            approved = operational.approve(pending.request_id)

        self.assertEqual(approved.status, "failed")
        self.assertEqual(propagated, [failure])
        self.assertIs(propagated[0], failure)
        self.assertIsNotNone(propagated[0].__traceback__)
        self.assertIsNone(propagated[0].__cause__)
        self.assertEqual(runtime.status, "failed")
        self.assertEqual(runtime.steps["inspect_1"].status, "failed")
        self.assertEqual(self.controller._last_terminal.status, "failed")
        finished = [
            event for event in events
            if event.get("event") == "model_plan_execution_finished"
        ]
        self.assertEqual([event["status"] for event in finished], ["failed"])
        self.assertNotIn("completed", [event.get("status") for event in finished])
        self.assertNotIn("cancelled", [event.get("status") for event in finished])
        serialized_events = str(events)
        serialized_logger_calls = str(logger_calls)
        for secret in (
            private_exception,
            private_path,
            private_content,
            private_binding,
            private_uuid,
            private_token,
            pending.request_id,
            runtime.runtime_id,
        ):
            self.assertNotIn(secret, serialized_events)
            self.assertNotIn(secret, serialized_logger_calls)
        safe_step_calls = [
            call for call in logger_calls
            if call["kwargs"].get("params", {}).get("workflow_step") == "inspect_1"
        ]
        self.assertEqual(len(safe_step_calls), 1)
        self.assertEqual(
            safe_step_calls[0]["kwargs"]["params"],
            {
                "workflow_step": "inspect_1",
                "action": "generate_patch",
                "status": "failed",
            },
        )
        self.assertEqual(
            safe_step_calls[0]["kwargs"]["result"],
            {"status": "failed"},
        )
        self.assertIsNone(raised.exception.execute())
        self.assertIsNone(raised.exception.on_cancel("cancelled"))
        self.assertEqual(runtime.status, "failed")
        self.assertEqual(self.controller._last_terminal.status, "failed")
        finished_after_late_calls = [
            event for event in events
            if event.get("event") == "model_plan_execution_finished"
        ]
        self.assertEqual(
            [event["status"] for event in finished_after_late_calls],
            ["failed"],
        )
        self.assertEqual(calls, ["tool"])
        run.assert_called_once()

    def test_operational_cancellation_synchronizes_global_record(self):
        events = []
        self.agent.action_logger.log = lambda *args, **kwargs: events.append(
            kwargs["params"]
        )
        result = planning_result(steps=[model_step(
            tool="patch_generator",
            action="generate_patch",
            args={"path": "brain/agent.py", "new_content": "safe"},
        )])
        plan_id = self.controller.register(result).plan_id
        events.clear()
        with self.assertRaises(ApprovalRequiredError) as raised:
            self.controller.approve(plan_id)
        operational = ApprovalController(self.agent)
        pending = operational.request_operation(
            tool_name=raised.exception.tool_name,
            action_name=raised.exception.action_name,
            important_args=raised.exception.important_args,
            execute=raised.exception.execute,
            force_approval=raised.exception.force_approval,
            on_cancel=raised.exception.on_cancel,
            on_request=raised.exception.on_request,
        )
        operational.cancel(pending.request_id)
        runtime = self.controller._last_terminal.runtime
        self.assertEqual(runtime.status, "cancelled")
        self.assertEqual(self.controller._last_terminal.status, "cancelled")
        finished = [
            event for event in events
            if event.get("event") == "model_plan_execution_finished"
        ]
        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0]["status"], "cancelled")
        self.assertNotIn(pending.request_id, str(events))

    def test_operational_execute_gate_is_single_use_and_terminal_is_immutable(self):
        events = []
        self.agent.action_logger.log = lambda *args, **kwargs: events.append(
            kwargs["params"]
        )
        pending, calls = self.start_protected_pause()
        first = pending.execute()
        self.assertEqual(first.status, "completed")
        self.assertIsNone(pending.execute())
        self.assertIsNone(pending.on_cancel("cancelled"))

        record = self.controller._last_terminal
        self.assertEqual(calls, ["brain/agent.py"])
        self.assertEqual(record.runtime.status, "completed")
        self.assertEqual(record.status, "completed")
        self.assertEqual(
            [event.get("event") for event in events].count(
                "model_plan_execution_finished"
            ),
            1,
        )

    def test_operational_cancel_gate_is_single_use_and_blocks_late_execute(self):
        events = []
        self.agent.action_logger.log = lambda *args, **kwargs: events.append(
            kwargs["params"]
        )
        pending, calls = self.start_protected_pause()
        self.assertIsNone(pending.on_cancel("cancelled"))
        self.assertIsNone(pending.on_cancel("cancelled"))
        self.assertIsNone(pending.execute())

        record = self.controller._last_terminal
        self.assertEqual(calls, [])
        self.assertEqual(record.runtime.status, "cancelled")
        self.assertEqual(record.status, "cancelled")
        self.assertEqual(
            [event.get("event") for event in events].count(
                "model_plan_execution_finished"
            ),
            1,
        )

    def test_operational_execute_cancel_race_has_one_deterministic_winner(self):
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def blocking_execute(args, structured=False):
            calls.append(args["path"])
            entered.set()
            release.wait(timeout=2)
            return ToolResult.success("patch_generator", data="patch")

        pending, _ = self.start_protected_pause(tool_execute=blocking_execute)
        outcomes = []

        def execute():
            outcomes.append(("execute", pending.execute()))

        execute_thread = threading.Thread(target=execute)
        execute_thread.start()
        self.assertTrue(entered.wait(timeout=2))
        outcomes.append(("cancel", pending.on_cancel("cancelled")))
        release.set()
        execute_thread.join(timeout=2)

        record = self.controller._last_terminal
        self.assertEqual(calls, ["brain/agent.py"])
        self.assertEqual(record.runtime.status, "completed")
        self.assertEqual(record.status, "completed")
        self.assertIsNone(dict(outcomes)["cancel"])
        self.assertEqual(dict(outcomes)["execute"].status, "completed")

    def test_consecutive_operational_pauses_receive_independent_gates(self):
        steps = [
            model_step(
                id="first",
                tool="patch_generator",
                action="generate_patch",
                args={"path": "first.py", "new_content": "first"},
            ),
            model_step(
                id="second",
                tool="patch_generator",
                action="generate_patch",
                args={"path": "second.py", "new_content": "second"},
                depends_on=["first"],
            ),
        ]
        first_pause, calls = self.start_protected_pause(steps=steps)
        operational = ApprovalController(self.agent)
        first_request = operational.request_operation(
            tool_name=first_pause.tool_name,
            action_name=first_pause.action_name,
            important_args=first_pause.important_args,
            execute=first_pause.execute,
            force_approval=first_pause.force_approval,
            on_cancel=first_pause.on_cancel,
            on_request=first_pause.on_request,
        )
        first_result = operational.approve(first_request.request_id)
        self.assertEqual(first_result.status, "awaiting_approval")
        self.assertEqual(calls, ["first.py"])
        self.assertIsNone(first_pause.execute())

        second_request = operational.get_pending()[0]
        second_result = operational.approve(second_request.request_id)
        record = self.controller._last_terminal
        self.assertEqual(second_result.status, "approved")
        self.assertEqual(calls, ["first.py", "second.py"])
        self.assertEqual(record.runtime.status, "completed")
        self.assertEqual(record.status, "completed")
        self.assertEqual(record.runtime.steps["first"].attempts, 1)
        self.assertEqual(record.runtime.steps["second"].attempts, 1)

    def test_register_logs_its_local_record_during_concurrent_replacement(self):
        first_entered = threading.Event()
        release_first = threading.Event()
        logged = []
        original_log = self.controller._log

        def controlled_log(event, record):
            if event == "model_plan_generated" and record.goal == "First":
                first_entered.set()
                release_first.wait(timeout=2)
            logged.append((event, record.plan_id, record.goal))
            original_log(event, record)

        self.controller._log = controlled_log
        first = planning_result(goal="First")
        second = planning_result(goal="Second")
        first_thread = threading.Thread(
            target=lambda: self.controller.register(first)
        )
        first_thread.start()
        self.assertTrue(first_entered.wait(timeout=2))
        second_view = self.controller.register(second)
        release_first.set()
        first_thread.join(timeout=2)

        generated = [
            entry for entry in logged if entry[0] == "model_plan_generated"
        ]
        self.assertCountEqual(
            generated,
            [
                ("model_plan_generated", model_plan_id(first), "First"),
                ("model_plan_generated", model_plan_id(second), "Second"),
            ],
        )
        self.assertEqual(self.controller._pending.plan_id, second_view.plan_id)

    def test_global_id_cannot_authorize_operation(self):
        plan_id = self.controller.register(planning_result()).plan_id
        self.assertFalse(
            self.agent.permission_manager.can_execute(
                "file_creator",
                action_name="create_file",
                important_args={"path": "x", "content": "x"},
                approval_token=plan_id,
            )
        )
        self.assertIsNone(self.agent.permission_manager.grant_approval(plan_id))

    def test_conversational_commands_are_separate_and_exact(self):
        conversation = ConversationalController(self.agent)
        runtime = mock.Mock(spec=WorkflowRuntimeState)
        runtime.status = "completed"
        plan_id = self.controller.register(planning_result()).plan_id
        with mock.patch.object(
            self.agent.execution_engine,
            "run_workflow",
            return_value=runtime,
        ) as run, mock.patch.object(
            self.agent.permission_manager,
            "grant_approval",
            wraps=self.agent.permission_manager.grant_approval,
        ) as grant:
            output = conversation.process_message(f"aprobar-plan {plan_id}")
        self.assertIn("completed", output)
        run.assert_called_once()
        grant.assert_not_called()

        with mock.patch.object(self.agent, "respond") as respond:
            invalid = conversation.process_message(
                "aprobar-plan 123e4567-e89b-12d3-a456-426614174000"
            )
        self.assertIn("inválido", invalid)
        respond.assert_not_called()

    def test_conversational_global_approval_routes_operational_pause(self):
        conversation = ConversationalController(self.agent)
        calls = []
        self.agent.patch_generator.execute = (
            lambda args, structured=False: calls.append("tool")
            or ToolResult.success("patch_generator", data="patch")
        )
        result = planning_result(steps=[model_step(
            tool="patch_generator",
            action="generate_patch",
            args={"path": "brain/agent.py", "new_content": "safe"},
        )])
        plan_id = self.controller.register(result).plan_id
        with mock.patch.object(
            self.agent.permission_manager,
            "grant_approval",
            wraps=self.agent.permission_manager.grant_approval,
        ) as grant:
            pending_message = conversation.process_message(
                f"aprobar-plan {plan_id}"
            )
        grant.assert_not_called()
        self.assertIn("Se requiere aprobación", pending_message)
        pending = conversation.approval_controller.get_pending()[0]
        self.assertNotEqual(pending.request_id, plan_id)
        approved = conversation.process_message(
            f"aprobar {pending.request_id}"
        )
        self.assertIn("correctamente", approved)
        self.assertEqual(calls, ["tool"])

    def test_no_pending_and_invalid_result_are_closed(self):
        self.assert_code("no_pending_plan", self.controller.render_pending)
        self.assert_code(
            "plan_revalidation_failed",
            lambda: self.controller.register(object()),
        )


if __name__ == "__main__":
    unittest.main()
