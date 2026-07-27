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


if __name__ == "__main__":
    unittest.main()
