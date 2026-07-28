import hashlib
import tempfile
import unittest
from pathlib import Path

from brain.change_proposal import ChangeProposal, FileChange, ProposalBudget, TestSpec
from brain.change_validator import ChangeProposalValidator
from brain.correction_runtime import (
    CorrectionRuntimeCompatibilityError,
    CorrectionRuntimeState,
    CorrectionRuntimeTransitionError,
)
from brain.workflow_limits import WorkflowLimits
from tools.tool_result import ToolResult


def make_proposal(path="new.py", content="x"):
    return ChangeProposal(
        (FileChange(path, "create", content, None),),
        (TestSpec("full"),),
        "reason",
        (),
        ProposalBudget(1, 1, len(content.encode("utf-8")), len(content.splitlines())),
    )


class CorrectionRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def _validated(self, proposal):
        return ChangeProposalValidator(self.root).validate(proposal)

    def test_creation_is_separate_and_collections_are_defensive(self):
        identity = {"plan": [1]}
        runtime = CorrectionRuntimeState("goal", initial_plan_identity=identity)
        identity["plan"].append(2)
        self.assertEqual(runtime.status, "validating")
        self.assertEqual(runtime.correction_iterations, 0)
        self.assertEqual(runtime.initial_plan_identity["plan"], (1,))
        with self.assertRaises(TypeError):
            runtime.initial_plan_identity["other"] = 2
        self.assertIsInstance(runtime.proposal_history, tuple)
        self.assertIsInstance(runtime.applied_proposal_ids, frozenset)
        with self.assertRaises(TypeError):
            runtime.failure_counts["x"] = 1

    def test_validation_and_future_application_transitions(self):
        proposal = make_proposal()
        validated = self._validated(proposal)
        runtime = CorrectionRuntimeState("goal")
        runtime.accept_proposal(proposal)
        runtime.record_validation(validated)
        self.assertEqual(runtime.status, "awaiting_approval")
        self.assertEqual(runtime.total_write_bytes, 0)
        runtime.mark_applying()
        runtime.record_future_application()
        self.assertEqual(runtime.status, "testing_focused")
        self.assertIn(proposal.proposal_id, runtime.applied_proposal_ids)
        self.assertEqual(runtime.total_write_bytes, 1)
        self.assertEqual(runtime.modified_files, frozenset({"new.py"}))

    def test_validation_or_rejection_does_not_consume_budget(self):
        proposal = make_proposal()
        runtime = CorrectionRuntimeState("goal")
        runtime.accept_proposal(proposal)
        runtime.record_validation(self._validated(proposal))
        self.assertEqual(runtime.total_write_bytes, 0)
        runtime.record_rejection()
        self.assertEqual(runtime.status, "cancelled")
        self.assertEqual(runtime.total_write_bytes, 0)
        self.assertEqual(runtime.applied_proposal_ids, frozenset())

    def test_two_corrections_are_allowed_and_third_reaches_limit(self):
        limits = WorkflowLimits(max_correction_iterations=2)
        runtime = CorrectionRuntimeState("goal", limits=limits)
        initial = make_proposal("initial.py")
        runtime.accept_proposal(initial)
        runtime.status = "awaiting_correction"
        runtime.accept_proposal(make_proposal("fix1.py"), correction=True)
        self.assertEqual(runtime.correction_iterations, 1)
        runtime.status = "awaiting_correction"
        runtime.accept_proposal(make_proposal("fix2.py"), correction=True)
        self.assertEqual(runtime.correction_iterations, 2)
        runtime.status = "awaiting_correction"
        with self.assertRaises(CorrectionRuntimeTransitionError):
            runtime.accept_proposal(make_proposal("fix3.py"), correction=True)
        self.assertEqual(runtime.status, "correction_limit_reached")

    def test_repeated_failure_stops_on_nth_occurrence(self):
        runtime = CorrectionRuntimeState(
            "goal",
            limits=WorkflowLimits(max_repeated_failure=2),
            status="testing_focused",
        )
        self.assertEqual(runtime.register_failure("same"), 1)
        self.assertEqual(runtime.status, "awaiting_correction")
        runtime.status = "testing_full"
        self.assertEqual(runtime.register_failure("same"), 2)
        self.assertEqual(runtime.status, "repeated_failure_limit_reached")

    def test_test_runs_and_completion_are_controlled(self):
        runtime = CorrectionRuntimeState("goal", status="testing_focused")
        result = ToolResult.success("test_runner", data={"tests_run": 1})
        runtime.record_test_run(TestSpec("focused", ("tests.test_x.Case.test_y",)), result)
        self.assertEqual(len(runtime.test_runs), 1)
        runtime.begin_full_tests()
        runtime.record_test_run(TestSpec("full"), result)
        runtime.mark_completed()
        self.assertEqual(runtime.status, "completed")
        with self.assertRaises(CorrectionRuntimeTransitionError):
            runtime.mark_failed("late")

    def test_invalid_transitions_are_rejected(self):
        runtime = CorrectionRuntimeState("goal")
        with self.assertRaises(CorrectionRuntimeTransitionError):
            runtime.mark_applying()
        with self.assertRaises(CorrectionRuntimeTransitionError):
            runtime.begin_full_tests()
        with self.assertRaises(CorrectionRuntimeTransitionError):
            runtime.record_test_run(TestSpec("full"), ToolResult.success("test_runner"))

    def test_duplicate_future_application_is_rejected(self):
        proposal = make_proposal()
        validated = self._validated(proposal)
        runtime = CorrectionRuntimeState("goal")
        runtime.accept_proposal(proposal)
        runtime.record_validation(validated)
        runtime.mark_applying()
        runtime.record_future_application()
        runtime.status = "applying"
        with self.assertRaises(CorrectionRuntimeCompatibilityError):
            runtime.record_future_application()


if __name__ == "__main__":
    unittest.main()
