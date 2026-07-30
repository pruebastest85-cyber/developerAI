import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from brain.agent import DeveloperAgent
from brain.approval_controller import ConversationalController
from brain.controlled_programming_session import (
    ControlledProgrammingResult,
    ControlledProgrammingSessionError,
    ProgrammingSessionState,
)
from brain.local_model_client import ModelResponseMetadata
from brain.model_plan import (
    SAFE_MODEL_OPERATION_CATALOG,
    ModelPlanAdapter,
    ModelPlanDecision,
)
from brain.model_planning_service import ModelPlanningResult
from brain.workflow_plan import StepSpec, WorkflowPlan
from brain.workflow_runtime import WorkflowRuntimeState


def planning_result(*, tool="code_reader", action="read_file", args=None):
    if args is None:
        args = {"path": "sample.py", "max_lines": 20}
    decision = ModelPlanDecision.from_mapping(
        {
            "schema_version": "1",
            "goal": "Controlled test",
            "completed": False,
            "steps": [
                {
                    "id": "step_1",
                    "tool": tool,
                    "action": action,
                    "args": args,
                    "goal": "Exercise the controlled boundary",
                    "depends_on": [],
                    "justification": "Required by the test",
                }
            ],
            "message": "",
        }
    )
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


class ControlledProgrammingSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        (self.base / "sample.py").write_text("value = 1\n", encoding="utf-8")
        self.result = planning_result()
        self.service = mock.Mock()
        self.service.plan.return_value = self.result
        self.agent = DeveloperAgent(
            None,
            base_dir=self.base,
            action_log_file=self.base / "actions.json",
            model_planning_service=self.service,
        )
        self.session = self.agent.get_programming_session()

    def test_idle_is_stable_and_structured(self):
        result = self.session.get_current_report()
        self.assertIsInstance(result, ControlledProgrammingResult)
        self.assertEqual(result.state, ProgrammingSessionState.IDLE)
        self.assertEqual(
            self.session.render_current_report(),
            "No hay una sesión de programación activa.",
        )

    def test_submit_preserves_exact_workflow_and_does_not_execute(self):
        with mock.patch.object(
            self.agent.execution_engine,
            "run_workflow",
            wraps=self.agent.execution_engine.run_workflow,
        ) as execute:
            result = self.session.submit("Inspect sample")
        self.assertEqual(result.state, ProgrammingSessionState.PENDING_PLAN)
        self.assertIs(self.session._workflow_plan, self.result.workflow)
        execute.assert_not_called()

    def test_respond_has_no_historical_fallback_after_registration(self):
        self.agent.execution_engine.run = mock.Mock(
            side_effect=AssertionError("legacy execution reached")
        )
        self.agent.tool_router.dispatch = mock.Mock(
            side_effect=AssertionError("legacy router reached")
        )

        rendered = self.agent.respond("programar: inspecciona sample.py")

        self.assertIn("Plan:", rendered)
        self.service.plan.assert_called_once_with("inspecciona sample.py")
        self.agent.execution_engine.run.assert_not_called()
        self.agent.tool_router.dispatch.assert_not_called()
        self.assertEqual(self.session.get_session_state(), "pending_plan")

    def test_planning_failure_is_sanitized_and_never_falls_back(self):
        self.service.plan.side_effect = RuntimeError(
            "SECRET C:\\private\\source.py"
        )
        self.agent.execution_engine.run = mock.Mock()
        self.agent.tool_router.dispatch = mock.Mock()

        rendered = self.agent.respond("programar: cambia el código")

        self.assertIn("planning_failed", rendered)
        self.assertNotIn("SECRET", rendered)
        self.assertNotIn("private", rendered)
        self.assertEqual(self.session.get_session_state(), "failed")
        self.agent.execution_engine.run.assert_not_called()
        self.agent.tool_router.dispatch.assert_not_called()

    def test_active_session_cannot_be_replaced(self):
        first = self.session.submit("First")
        with self.assertRaises(ControlledProgrammingSessionError) as caught:
            self.session.submit("Second")
        self.assertEqual(caught.exception.code, "active_session")
        self.assertEqual(self.session.current_result().plan_id, first.plan_id)
        self.service.plan.assert_called_once()

    def test_terminal_session_can_start_a_new_independent_cycle(self):
        first = self.session.submit("First")
        self.session.reject_plan(first.plan_id)
        first_session_id = self.session.current_result().session_id

        second = self.session.submit("Second")

        self.assertEqual(second.state, ProgrammingSessionState.PENDING_PLAN)
        self.assertNotEqual(second.session_id, first_session_id)

    def test_exact_plan_completes_and_builds_structured_report(self):
        pending = self.session.submit("Inspect")
        completed = self.session.approve_plan(pending.plan_id)

        self.assertEqual(completed.state, ProgrammingSessionState.COMPLETED)
        self.assertEqual(completed.runtime_status, "completed")
        self.assertIsNotNone(completed.report)
        self.assertFalse(completed.automatic_commit_performed)
        self.assertFalse(completed.automatic_push_performed)
        self.assertEqual(
            self.session._runtime.plan_identity,
            self.result.workflow.identity(),
        )

    def test_wrong_or_repeated_plan_approval_does_not_execute(self):
        pending = self.session.submit("Inspect")
        with mock.patch.object(
            self.agent.execution_engine,
            "run_workflow",
            wraps=self.agent.execution_engine.run_workflow,
        ) as execute:
            with self.assertRaises(ControlledProgrammingSessionError):
                self.session.approve_plan("wrong")
            self.session.approve_plan(pending.plan_id)
            with self.assertRaises(ControlledProgrammingSessionError):
                self.session.approve_plan(pending.plan_id)
        self.assertEqual(execute.call_count, 1)

    def test_reject_and_cancel_keep_terminal_plan_information(self):
        pending = self.session.submit("Inspect")
        rejected = self.session.reject_plan(pending.plan_id)
        self.assertEqual(rejected.state, ProgrammingSessionState.REJECTED)
        self.assertIs(rejected.plan, pending.plan)
        self.assertIn("rechazado", self.session.render_current_report())

        self.session.close()
        pending = self.session.submit("Inspect again")
        cancelled = self.session.cancel_plan(pending.plan_id)
        self.assertEqual(cancelled.state, ProgrammingSessionState.CANCELLED)
        self.assertIs(cancelled.plan, pending.plan)
        self.assertIn("cancelada", self.session.render_current_report())

    def test_terminal_state_rejects_late_commands(self):
        pending = self.session.submit("Inspect")
        self.session.reject_plan(pending.plan_id)
        before = self.session.current_result()
        with self.assertRaises(ControlledProgrammingSessionError):
            self.session.process_operational_command("aprobar", "late")
        self.assertEqual(self.session.current_result(), before)

    def test_operational_pause_is_bound_to_exact_runtime_request(self):
        patch = planning_result(
            tool="patch_generator",
            action="generate_patch",
            args={"path": "sample.py", "new_content": "value = 2\n"},
        )
        self.service.plan.return_value = patch
        pending = self.session.submit("Generate patch")

        paused = self.session.approve_plan(pending.plan_id)

        self.assertEqual(paused.state, ProgrammingSessionState.AWAITING_APPROVAL)
        self.assertEqual(paused.runtime_status, "awaiting_approval")
        self.assertEqual(
            paused.pending_approval_request_id,
            self.session._runtime.approval_request_id,
        )
        self.assertIs(self.session._workflow_plan, patch.workflow)

    def test_foreign_and_repeated_operational_ids_never_execute(self):
        patch = planning_result(
            tool="patch_generator",
            action="generate_patch",
            args={"path": "sample.py", "new_content": "value = 2\n"},
        )
        self.service.plan.return_value = patch
        pending = self.session.submit("Generate patch")
        paused = self.session.approve_plan(pending.plan_id)
        tool = self.agent.patch_generator
        original = tool.execute
        tool.execute = mock.Mock(wraps=original)
        self.addCleanup(setattr, tool, "execute", original)

        with self.assertRaises(ControlledProgrammingSessionError):
            self.session.process_operational_command("aprobar", "foreign")
        self.assertEqual(tool.execute.call_count, 0)

        completed = self.session.process_operational_command(
            "aprobar",
            paused.pending_approval_request_id,
        )
        self.assertEqual(completed.state, ProgrammingSessionState.COMPLETED)
        self.assertEqual(tool.execute.call_count, 1)
        with self.assertRaises(ControlledProgrammingSessionError):
            self.session.process_operational_command(
                "aprobar",
                paused.pending_approval_request_id,
            )
        self.assertEqual(tool.execute.call_count, 1)

    def test_real_conversational_route_keeps_both_approval_levels_in_session(self):
        patch = planning_result(
            tool="patch_generator",
            action="generate_patch",
            args={"path": "sample.py", "new_content": "value = 2\n"},
        )
        self.service.plan.return_value = patch
        conversation = ConversationalController(self.agent)

        pending_text = conversation.process_message("programar: genera un parche")
        self.assertIn("Plan:", pending_text)
        plan_id = self.session.current_result().plan_id

        approval_text = conversation.process_message(f"aprobar-plan {plan_id}")
        paused = self.session.current_result()
        self.assertEqual(paused.state, ProgrammingSessionState.AWAITING_APPROVAL)
        self.assertIn(paused.pending_approval_request_id, approval_text)

        completed_text = conversation.process_message(
            f"aprobar {paused.pending_approval_request_id}"
        )
        self.assertEqual(
            self.session.current_result().state,
            ProgrammingSessionState.COMPLETED,
        )
        self.assertIn("Estado: **completed**", completed_text)

    def test_unknown_approval_result_fails_closed(self):
        patch = planning_result(
            tool="patch_generator",
            action="generate_patch",
            args={"path": "sample.py", "new_content": "value = 2\n"},
        )
        self.service.plan.return_value = patch
        pending = self.session.submit("Generate patch")
        paused = self.session.approve_plan(pending.plan_id)
        self.session.approval_controller.approve = mock.Mock(
            return_value=mock.Mock(status="unknown")
        )

        result = self.session.process_operational_command(
            "aprobar",
            paused.pending_approval_request_id,
        )

        self.assertEqual(result.state, ProgrammingSessionState.FAILED)
        self.assertNotEqual(result.runtime_status, "completed")

    def test_operational_exception_is_sanitized(self):
        self.session._state = ProgrammingSessionState.AWAITING_APPROVAL
        self.session._runtime = WorkflowRuntimeState.create(self.result.workflow)
        self.session._runtime.status = "awaiting_approval"
        self.session._runtime.awaiting_step_id = "step_1"
        self.session._runtime.approval_request_id = "request"
        self.session._approval_request_id = "request"
        self.session.approval_controller.get_pending = mock.Mock(
            return_value=object()
        )
        self.session.approval_controller.approve = mock.Mock(
            side_effect=RuntimeError("SECRET C:\\private\\file.py")
        )

        rendered = self.session.handle_message("aprobar request")

        self.assertIn("execution_failed", rendered)
        self.assertNotIn("SECRET", rendered)
        self.assertNotIn("private", rendered)

    def test_correction_delegates_exact_plan_runtime_and_arguments(self):
        correction_plan = WorkflowPlan(
            (
                StepSpec(
                    id="correct",
                    tool="correction_workflow",
                    action="apply_change_proposal",
                    args={},
                    goal="Correct",
                    approval="required",
                ),
            )
        )
        runtime = WorkflowRuntimeState.create(correction_plan)
        runtime.status = "awaiting_correction"
        runtime.awaiting_step_id = "correct"
        runtime.steps["correct"].status = "awaiting_correction"
        self.session._workflow_plan = correction_plan
        self.session._runtime = runtime
        self.session._state = ProgrammingSessionState.AWAITING_CORRECTION
        arguments = {"proposal": "value"}
        self.agent.execution_engine.submit_workflow_correction = mock.Mock(
            return_value=runtime
        )

        result = self.session.submit_correction(arguments)

        self.agent.execution_engine.submit_workflow_correction.assert_called_once_with(
            correction_plan,
            runtime,
            arguments,
        )
        self.assertEqual(result.state, ProgrammingSessionState.AWAITING_CORRECTION)

    def test_logging_has_one_terminal_event_and_no_operational_payload(self):
        pending = self.session.submit("Inspect")
        self.session.approve_plan(pending.plan_id)
        entries = json.loads((self.base / "actions.json").read_text(encoding="utf-8"))
        session_entries = [
            item
            for item in entries
            if item["tool"] == "controlled_programming_session"
        ]
        terminal = [
            item
            for item in session_entries
            if item["params"]["event"] == "session_terminal"
        ]
        self.assertEqual(len(terminal), 1)
        serialized = str(session_entries)
        self.assertNotIn("sample.py", serialized)
        self.assertNotIn("approval_token", serialized)
        self.assertNotIn("resolved_args", serialized)

    def test_git_status_remains_on_historical_route(self):
        self.agent.tool_router.dispatch = mock.Mock(return_value="historical")
        response = self.agent.respond("git status")
        self.assertNotEqual(self.session.get_session_state(), "pending_plan")
        self.service.plan.assert_not_called()
        self.assertIsInstance(response, str)


if __name__ == "__main__":
    unittest.main()
