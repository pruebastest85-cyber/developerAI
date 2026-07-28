import json
import unittest

from brain.workflow_report import (
    ApprovalReport, ChangeReport, ChangedFileReport, DiffSnapshot, WorkflowReport,
    WorkflowStepReport,
)


class WorkflowReportTests(unittest.TestCase):
    def test_is_deeply_immutable_and_json_serializable(self):
        files = [ChangedFileReport("brain/a.py", "modified", 1, 2)]
        report = WorkflowReport(
            "run-1", "goal", "completed",
            [WorkflowStepReport("one", "reader", "read", "inspect", "ok", True, 1)],
            ChangeReport(files),
            approval=ApprovalReport(),
            diff=DiffSnapshot(True, files=files),
        )
        files.append(ChangedFileReport("brain/b.py", "created"))
        self.assertEqual(1, len(report.changes.files))
        payload = report.to_dict()
        json.dumps(payload)
        self.assertIsInstance(payload["steps"], list)
        self.assertTrue(report.is_terminal)
        self.assertFalse(report.is_resumable)

    def test_only_general_statuses_and_real_change_kinds_are_allowed(self):
        with self.assertRaises(ValueError):
            WorkflowReport("x", "", "rollback_failed", (), ChangeReport())
        with self.assertRaises(ValueError):
            ChangedFileReport("a", "deleted")

    def test_suspended_report_is_resumable_not_terminal(self):
        report = WorkflowReport("x", "", "awaiting_approval", (), ChangeReport())
        self.assertTrue(report.is_resumable)
        self.assertFalse(report.is_terminal)

    def test_mutable_or_wrong_nested_values_are_rejected(self):
        with self.assertRaises(TypeError):
            ChangeReport(files=([],))
        with self.assertRaises(TypeError):
            WorkflowReport("x", "", "completed", ([],), ChangeReport())
        with self.assertRaises(TypeError):
            WorkflowReport([], "", "completed", (), ChangeReport())
        with self.assertRaises(TypeError):
            ChangeReport(insertions=[])


if __name__ == "__main__":
    unittest.main()
