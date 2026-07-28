import unittest

from brain.workflow_report import (
    ApprovalReport, ChangeReport, ChangedFileReport, CorrectionReport, DiffSnapshot,
    LimitReport, TestRunReport, WorkflowReport, WorkflowStepReport,
)
from brain.workflow_report_renderer import WorkflowReportRenderer


class WorkflowReportRendererTests(unittest.TestCase):
    def make_report(self, status="completed", **overrides):
        values = dict(
            workflow_id="workflow-1", goal="fix bug", status=status,
            steps=(WorkflowStepReport(
                "inspect", "code_reader", "read", "inspect", "ok", True, 1
            ),),
            changes=ChangeReport((ChangedFileReport("brain/a.py", "modified", 2, 1),), 2, 1),
            tests=(TestRunReport("focused", "ok", tests_run=1, passed=1),),
            approval=ApprovalReport(),
            limits=LimitReport(2, 0, 5, 1, 1000, 20, 500, 3),
            diff=DiffSnapshot(True, "diff --git a/brain/a.py b/brain/a.py\n"),
        )
        values.update(overrides)
        return WorkflowReport(**values)

    def test_completed_report_contains_final_guarantees_and_diff(self):
        text = WorkflowReportRenderer.render_markdown(self.make_report())
        self.assertIn("Estado: **completed**", text)
        self.assertIn("Commit automático: no", text)
        self.assertIn("Push automático: no", text)
        self.assertIn("```diff", text)
        self.assertIn("focused", text)

    def test_suspended_denied_rollback_and_limit_states_are_explicit(self):
        pending = self.make_report(
            "awaiting_approval", approval=ApprovalReport("pending", "inspect", "req-1")
        )
        self.assertIn("Reanudable: sí", WorkflowReportRenderer.render_markdown(pending))
        denied = self.make_report(
            "cancelled", approval=ApprovalReport("denied", "inspect", "req-1")
        )
        self.assertIn("**denied**", WorkflowReportRenderer.render_markdown(denied))
        correction = CorrectionReport(
            "correction-1", "rollback_failed", correction_iterations=2,
            terminal_reason="rollback failed",
        )
        failed = self.make_report(
            "failed", corrections=correction,
            limits=LimitReport(2, 2, 5, 1, 1000, 20, 500, 3, ("max_correction_iterations",)),
        )
        text = WorkflowReportRenderer.render_markdown(failed)
        self.assertIn("rollback_failed", text)
        self.assertIn("max_correction_iterations", text)

    def test_awaiting_correction_truncation_git_error_binary_and_empty_diff(self):
        report = self.make_report(
            "awaiting_correction",
            corrections=CorrectionReport("c", "awaiting_correction"),
            diff=DiffSnapshot(
                True, "partial", (ChangedFileReport("a.bin", "created", None, None, True),),
                binary_files=("a.bin",), truncated=True, omitted_paths=("a.py",),
            ),
        )
        text = WorkflowReportRenderer.render_markdown(report)
        self.assertIn("diff truncado", text)
        self.assertIn("binario", text)
        unavailable = self.make_report(diff=DiffSnapshot(
            False, error_code="git_failed", error_message="boom"
        ))
        self.assertIn("git_failed", WorkflowReportRenderer.render_markdown(unavailable))
        empty = self.make_report(diff=DiffSnapshot(True))
        self.assertIn("Diff vacío", WorkflowReportRenderer.render_markdown(empty))

    def test_renderer_is_pure_and_rejects_internal_objects(self):
        report = self.make_report()
        before = report.to_dict()
        WorkflowReportRenderer.render_markdown(report)
        self.assertEqual(before, report.to_dict())
        with self.assertRaises(TypeError):
            WorkflowReportRenderer.render_markdown(object())


if __name__ == "__main__":
    unittest.main()
