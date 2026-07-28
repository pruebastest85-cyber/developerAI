import tempfile
import unittest

from brain.correction_runtime import CorrectionRuntimeState
from brain.execution_engine import ExecutionEngine
from brain.workflow_plan import StepSpec, WorkflowPlan
from brain.workflow_report import DiffSnapshot
from brain.workflow_report_builder import WorkflowReportBuilder
from brain.workflow_runtime import WorkflowRuntimeState
from tools.tool_result import ToolResult


def plan():
    return WorkflowPlan((
        StepSpec("read", "read_file", "code_reader", goal="inspect"),
        StepSpec("tests", "run", "test_runner", depends_on=("read",), goal="test"),
    ))


class EmptyDiff:
    def __init__(self):
        self.paths = None

    def capture(self, paths, *, max_bytes):
        self.paths = set(paths)
        return DiffSnapshot(True)


class WorkflowReportBuilderTests(unittest.TestCase):
    def test_projects_steps_tests_and_stable_runtime_id(self):
        workflow = plan()
        runtime = WorkflowRuntimeState.create(workflow, "goal")
        original_id = runtime.runtime_id
        runtime.steps["read"].mark_running({})
        runtime.record_result("read", ToolResult.success("code_reader", data="x"))
        runtime.steps["tests"].mark_running({})
        runtime.record_result("tests", ToolResult.success(
            "test_runner", data={"tests_run": 2, "passed": 2, "skipped": 0}
        ))
        runtime.finish(workflow)
        collector = EmptyDiff()
        report = WorkflowReportBuilder(".", diff_collector=collector).build(workflow, runtime)
        self.assertEqual(original_id, report.workflow_id)
        self.assertEqual("completed", report.status)
        self.assertEqual(2, report.tests[0].tests_run)
        self.assertFalse(report.automatic_commit_performed)
        self.assertFalse(report.automatic_push_performed)

    def test_collects_only_runtime_owned_paths_and_correction_evidence(self):
        workflow = WorkflowPlan((StepSpec(
            "correct", "apply_change_proposal", "patch_applier",
        ),))
        runtime = WorkflowRuntimeState.create(workflow)
        correction = CorrectionRuntimeState("goal")
        correction.modified_files = frozenset({"brain/a.py"})
        correction.new_files = frozenset({"brain/new.py"})
        correction.status = "rollback_failed"
        correction.terminal_reason = "rollback"
        runtime.steps["correct"].correction_runtime = correction
        runtime.steps["correct"].mark_running({})
        runtime.record_result("correct", ToolResult.failure("patch_applier", error="rollback"))
        runtime.finish(workflow)
        collector = EmptyDiff()
        report = WorkflowReportBuilder(".", diff_collector=collector).build(workflow, runtime)
        self.assertEqual({"brain/a.py", "brain/new.py"}, collector.paths)
        self.assertEqual("failed", report.status)
        self.assertEqual("rollback_failed", report.corrections.status)

    def test_approval_history_distinguishes_pending_approved_and_denied(self):
        workflow = WorkflowPlan((StepSpec("read", "read", "code_reader"),))
        runtime = WorkflowRuntimeState.create(workflow)
        state = runtime.steps["read"]
        state.mark_running({})
        state.mark_awaiting_approval({})
        runtime.mark_awaiting_approval("read")
        runtime.record_approval_request("read", "visible-id")
        report = WorkflowReportBuilder(".", diff_collector=EmptyDiff()).build(workflow, runtime)
        self.assertEqual("pending", report.approval.status)
        self.assertEqual("visible-id", report.approval.request_id)
        runtime.mark_cancelled("read", "rejected")
        report = WorkflowReportBuilder(".", diff_collector=EmptyDiff()).build(workflow, runtime)
        self.assertEqual("denied", report.approval.status)

    def test_engine_report_api_preserves_run_workflow_contract(self):
        class Agent:
            base_dir = "."
        engine = ExecutionEngine(Agent())
        workflow = WorkflowPlan(())
        runtime = WorkflowRuntimeState.create(workflow)
        runtime.finish(workflow)
        engine.last_workflow_runtime = runtime
        report = engine.build_workflow_report(workflow)
        self.assertEqual(runtime.runtime_id, report.workflow_id)


if __name__ == "__main__":
    unittest.main()
