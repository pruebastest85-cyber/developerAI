import unittest

from brain.execution_provenance import (
    ExecutionProvenanceError,
    ExecutionProvenanceRegistry,
)
from brain.change_proposal import TestSpec
from brain.correction_engine import CorrectionEngine, CorrectionTestResultError
from brain.workflow_limits import WorkflowLimits
from brain.workflow_plan import StepSpec, WorkflowPlan
from brain.workflow_runtime import WorkflowRuntimeState
from tools.tool_result import ToolResult


class _Session:
    def __init__(self):
        self.state = "running"
        self.session_id = "session"
        self.epoch = 1
        self.plan_id = "plan"
        self.plan = None
        self.runtime = None

    def _execution_authority_context(self):
        return (
            self.session_id,
            self.epoch,
            self.plan_id,
            self.plan,
            self.runtime,
            self.state,
        )


def _red_result():
    return ToolResult.failure(
        "test_runner",
        error="test failure",
        data={
            "tests_run": 1,
            "failures": 1,
            "errors": 0,
            "skipped": 0,
            "passed": 0,
            "returncode": 1,
            "timed_out": False,
        },
        retryable=True,
    )


class ExecutionProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.limits = WorkflowLimits()
        self.session = _Session()
        self.registry = ExecutionProvenanceRegistry(
            self.limits,
            session_owner=lambda: self.session,
        )
        self.step = StepSpec(
            id="focused",
            tool="test_runner",
            action="run_tests",
            args={"test_id": "tests.test_sample.SampleTests.test_red"},
            approval="required",
        )
        self.plan = WorkflowPlan((self.step,))
        self.session.plan = self.plan
        self.runtime = WorkflowRuntimeState.create(self.plan)
        capability = self.registry.authorize(self.session, self.plan)
        self.binding = self.registry.bind_pending(self.plan, self.runtime)
        self.session.runtime = self.runtime
        self.assertIsNotNone(capability)
        self.assertIsNotNone(self.binding)

    def _approved_running_step(self):
        state = self.runtime.steps[self.step.id]
        state.mark_running(dict(self.step.args))
        state.mark_awaiting_approval(dict(self.step.args))
        self.runtime.mark_awaiting_approval(self.step.id)
        self.runtime.record_approval_request(self.step.id, "approval-1")
        self.runtime.begin_approved_step(self.step.id)

    def _event(self, result=None):
        self._approved_running_step()
        result = result or _red_result()
        event = self.registry.issue_test_event(
            self.binding,
            self.plan,
            self.runtime,
            self.step,
            result,
        )
        self.assertIsNotNone(event)
        return event, result

    def _consume(self, event, result):
        return self.registry.consume_red_test_event(
            event,
            plan=self.plan,
            runtime=self.runtime,
            step=self.step,
            result=result,
        )

    def test_authentic_event_is_identity_bound_and_consumed_once(self):
        event, result = self._event()
        with self.assertRaises(ExecutionProvenanceError):
            self.registry.consume_red_test_event(
                event,
                plan=self.plan,
                runtime=self.runtime,
                step=self.step,
                result=_red_result(),
            )
        authority = self._consume(event, result)
        self.assertIsNotNone(authority)

        with self.assertRaises(ExecutionProvenanceError):
            self._consume(event, result)

    def test_forged_receipt_and_authority_cannot_seed_correction_engine(self):
        result = _red_result()
        engine = CorrectionEngine(
            execution_provenance=self.registry,
        )
        with self.assertRaises(CorrectionTestResultError):
            engine.start_from_test_failure(
                "repair",
                TestSpec("focused", (self.step.args["test_id"],)),
                result,
                initial_plan_identity=self.plan.identity(),
                execution_event=object(),
                workflow_plan=self.plan,
                workflow_runtime=self.runtime,
                workflow_step=self.step,
                execution_authority=object(),
            )
        self.assertIsNone(engine.runtime)

    def test_event_cannot_move_to_another_runtime_plan_or_step(self):
        event, result = self._event()
        other_runtime = WorkflowRuntimeState.create(self.plan)
        other_step = StepSpec(
            id="other",
            tool="test_runner",
            action="run_tests",
            args={"test_id": "tests.test_sample.SampleTests.test_red"},
            approval="required",
        )
        other_plan = WorkflowPlan((other_step,))

        for plan, runtime, step in (
            (self.plan, other_runtime, self.step),
            (other_plan, self.runtime, other_step),
        ):
            with self.subTest(runtime=runtime.runtime_id, step=step.id):
                with self.assertRaises(ExecutionProvenanceError):
                    self.registry.consume_red_test_event(
                        event,
                        plan=plan,
                        runtime=runtime,
                        step=step,
                        result=result,
                    )

    def test_terminal_session_and_stale_operational_state_reject_event(self):
        for mutation in (
            lambda: setattr(self.session, "state", "cancelled"),
            lambda: setattr(
                self.runtime.steps[self.step.id],
                "approval_status",
                "cancelled",
            ),
            lambda: setattr(self.runtime, "current_step_id", None),
        ):
            with self.subTest(mutation=mutation):
                event, result = self._event()
                mutation()
                with self.assertRaises(ExecutionProvenanceError):
                    self._consume(event, result)
                self.setUp()

    def test_event_from_an_earlier_session_epoch_stays_invalid(self):
        event, result = self._event()
        self.session.state = "cancelled"
        with self.assertRaises(ExecutionProvenanceError):
            self._consume(event, result)

        self.session.epoch += 1
        self.session.state = "running"
        with self.assertRaises(ExecutionProvenanceError):
            self._consume(event, result)

    def test_exhausted_change_budget_rejects_before_consuming_event(self):
        event, result = self._event()
        self.runtime.total_change_bytes = self.limits.max_total_change_bytes
        event = self.registry.issue_test_event(
            self.binding,
            self.plan,
            self.runtime,
            self.step,
            result,
        )
        self.assertIsNotNone(event)

        with self.assertRaisesRegex(
            ExecutionProvenanceError,
            "max_total_change_bytes",
        ):
            self._consume(event, result)

    def test_iteration_limit_is_checked_at_activation(self):
        limits = WorkflowLimits(max_correction_iterations=1)
        session = _Session()
        registry = ExecutionProvenanceRegistry(
            limits,
            session_owner=lambda: session,
        )
        runtime = WorkflowRuntimeState.create(self.plan)
        session.plan = self.plan
        registry.authorize(session, self.plan)
        binding = registry.bind_pending(self.plan, runtime)
        session.runtime = runtime
        state = runtime.steps[self.step.id]
        state.mark_running(dict(self.step.args))
        state.mark_awaiting_approval(dict(self.step.args))
        runtime.mark_awaiting_approval(self.step.id)
        runtime.record_approval_request(self.step.id, "approval-1")
        runtime.begin_approved_step(self.step.id)
        first_result = _red_result()
        first = registry.issue_test_event(
            binding, self.plan, runtime, self.step, first_result
        )
        registry.consume_red_test_event(
            first,
            plan=self.plan,
            runtime=runtime,
            step=self.step,
            result=first_result,
        )
        second_result = _red_result()
        second = registry.issue_test_event(
            binding, self.plan, runtime, self.step, second_result
        )

        with self.assertRaisesRegex(
            ExecutionProvenanceError,
            "max_correction_iterations",
        ):
            registry.consume_red_test_event(
                second,
                plan=self.plan,
                runtime=runtime,
                step=self.step,
                result=second_result,
            )

    def test_unapproved_execution_cannot_emit_event(self):
        self.runtime.steps[self.step.id].mark_running(dict(self.step.args))
        self.runtime.current_step_id = self.step.id
        self.assertIsNone(
            self.registry.issue_test_event(
                self.binding,
                self.plan,
                self.runtime,
                self.step,
                _red_result(),
            )
        )

    def test_registry_rejects_a_session_other_than_its_exact_owner(self):
        other = _Session()
        with self.assertRaises(ExecutionProvenanceError):
            self.registry.authorize(other, self.plan)
