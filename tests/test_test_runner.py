import sys
import unittest
from pathlib import Path

from tools.test_runner import TestRunner


class TestRunnerTests(unittest.TestCase):
    def test_run_tests_returns_structured_result(self):
        runner = TestRunner(base_dir=Path("."))
        result = runner.run_tests(
            [
                sys.executable,
                "-m",
                "unittest",
                "discover",
                "-s",
                "tests",
                "-p",
                "test_code_reader.py",
                "-v",
            ]
        )
        self.assertIn("command", result)
        self.assertIn("returncode", result)
        self.assertIn("stdout", result)
        self.assertIn("stderr", result)
        self.assertIn("ok", result)

    def test_execute_returns_failed_tool_result_for_nonzero_exit(self):
        runner = TestRunner(base_dir=".")
        result = runner.execute(
            {
                "command": [
                    sys.executable,
                    "-c",
                    "import sys; print('bad'); sys.exit(3)",
                ]
            },
            structured=True,
        )

        self.assertEqual(result.status, "failed")
        self.assertTrue(result.retryable)
        self.assertEqual(result.data["returncode"], 3)
        self.assertIn("Resultado: FALLÓ", result.message)


if __name__ == "__main__":
    unittest.main()
