import unittest

from brain.workflow_limits import WorkflowLimits


class WorkflowLimitsTests(unittest.TestCase):
    def test_defaults_are_exact(self):
        limits = WorkflowLimits()
        self.assertEqual(limits.max_correction_iterations, 2)
        self.assertEqual(limits.max_modified_files, 5)
        self.assertEqual(limits.max_inspected_files, 20)
        self.assertEqual(limits.max_read_bytes_per_file, 256 * 1024)
        self.assertEqual(limits.max_total_change_bytes, 256 * 1024)
        self.assertEqual(limits.max_changed_lines, 500)
        self.assertEqual(limits.max_new_file_bytes, 64 * 1024)
        self.assertEqual(limits.focused_test_timeout, 60)
        self.assertEqual(limits.full_test_timeout, 180)
        self.assertEqual(limits.max_repeated_failure, 2)

    def test_valid_configuration_is_accepted(self):
        limits = WorkflowLimits(max_modified_files=1, full_test_timeout=30)
        self.assertEqual(limits.max_modified_files, 1)
        self.assertEqual(limits.full_test_timeout, 30)

    def test_booleans_and_wrong_types_are_rejected(self):
        for value in (True, False, 1.5, "2", None):
            with self.subTest(value=value):
                with self.assertRaises(TypeError):
                    WorkflowLimits(max_modified_files=value)

    def test_zero_and_negative_values_are_rejected(self):
        for value in (0, -1):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    WorkflowLimits(max_correction_iterations=value)


if __name__ == "__main__":
    unittest.main()
