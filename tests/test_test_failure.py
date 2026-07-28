import unittest

from brain.change_proposal import TestSpec
from brain.test_failure import TestFailureFingerprintError, failure_fingerprint
from tools.tool_result import ToolResult


class TestFailureFingerprintTests(unittest.TestCase):
    def _result(self, stderr, *, failed_id="tests.test_x.Case.test_one"):
        return ToolResult.failure(
            "test_runner",
            error="Tests failed in 0.123s",
            data={
                "command_args": [
                    "C:\\Users\\black\\DeveloperAI\\.venv\\python.exe",
                    "-m",
                    "unittest",
                    failed_id,
                    "-v",
                ],
                "returncode": 1,
                "stderr": stderr,
                "stdout": "",
                "tests_run": 1,
                "failed_test_ids": [failed_id],
                "error_test_ids": [],
            },
        )

    def test_volatile_values_are_ignored(self):
        spec = TestSpec("focused", ("tests.test_x.Case.test_one",))
        first = self._result(
            "2025-01-01T10:00:00Z C:\\Users\\black\\DeveloperAI\\a.py "
            "C:\\Users\\black\\AppData\\Local\\Temp\\tmp123\\x at 0xABC in 0.1s"
        )
        second = self._result(
            "2026-02-02T11:22:33Z C:/Users/black/DeveloperAI/a.py "
            "C:/Users/black/AppData/Local/Temp/tmp999/x at 0xDEF in 9.9s"
        )
        self.assertEqual(
            failure_fingerprint(spec, first),
            failure_fingerprint(spec, second),
        )

    def test_real_failure_differences_change_fingerprint(self):
        spec = TestSpec("focused", ("tests.test_x.Case.test_one",))
        first = self._result("AssertionError: one")
        second = self._result(
            "AssertionError: two",
            failed_id="tests.test_x.Case.test_two",
        )
        self.assertNotEqual(
            failure_fingerprint(spec, first),
            failure_fingerprint(spec, second),
        )

    def test_mapping_order_does_not_change_fingerprint(self):
        spec = TestSpec("full")
        data = {
            "returncode": None,
            "tests_run": 0,
            "timed_out": True,
            "failed_test_ids": [],
            "error_test_ids": [],
        }
        first = ToolResult.failure(
            "test_runner",
            error="timeout",
            data=dict(data),
            metadata={"reason": "timeout", "other": 1},
            retryable=True,
        )
        second = ToolResult.failure(
            "test_runner",
            error="timeout",
            data=dict(reversed(list(data.items()))),
            metadata={"other": 1, "reason": "timeout"},
            retryable=True,
        )
        self.assertEqual(
            failure_fingerprint(spec, first),
            failure_fingerprint(spec, second),
        )

    def test_ok_and_invalid_inputs_are_rejected(self):
        with self.assertRaises(TestFailureFingerprintError):
            failure_fingerprint(TestSpec("full"), ToolResult.success("test_runner"))
        with self.assertRaises(TestFailureFingerprintError):
            failure_fingerprint("full", ToolResult.failure("test_runner", error="x"))


if __name__ == "__main__":
    unittest.main()
