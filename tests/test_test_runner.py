import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from brain.change_proposal import TestSpec
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
        for key in ("command", "returncode", "stdout", "stderr", "ok"):
            self.assertIn(key, result)

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

    def test_focused_and_full_specs_build_canonical_commands(self):
        runner = TestRunner(".")
        with mock.patch("tools.test_runner.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                [], 0, stdout="", stderr="Ran 1 test in 0.001s\n\nOK\n"
            )
            focused = runner.execute(
                {
                    "test_spec": TestSpec(
                        "focused",
                        ("tests.test_code_reader.CodeReaderTests.test_read_file_returns_content",),
                    ),
                    "timeout": 5,
                },
                structured=True,
            )
            command = run.call_args.args[0]
            self.assertEqual(command[:3], [sys.executable, "-m", "unittest"])
            self.assertEqual(command[-1], "-v")
            self.assertEqual(run.call_args.kwargs["timeout"], 5)
            self.assertEqual(focused.status, "ok")
            runner.execute(
                {"test_spec": TestSpec("full"), "timeout": 10},
                structured=True,
            )
            self.assertEqual(
                run.call_args.args[0],
                [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
            )

    def test_invalid_test_spec_and_timeout_are_failed(self):
        runner = TestRunner(".")
        for args in (
            {"test_spec": "python -m unittest"},
            {"test_spec": TestSpec("full"), "command": [sys.executable]},
            {"test_spec": TestSpec("full"), "timeout": True},
            {"test_spec": TestSpec("full"), "timeout": 0},
        ):
            with self.subTest(args=args):
                self.assertEqual(runner.execute(args, structured=True).status, "failed")

    def test_timeout_is_retryable_structured_failure(self):
        runner = TestRunner(".")
        timeout = subprocess.TimeoutExpired(
            [sys.executable, "-m", "unittest"],
            3,
            output="partial",
            stderr="waiting",
        )
        with mock.patch("tools.test_runner.subprocess.run", side_effect=timeout):
            result = runner.execute(
                {"test_spec": TestSpec("full"), "timeout": 3},
                structured=True,
            )
        self.assertEqual(result.status, "failed")
        self.assertTrue(result.retryable)
        self.assertEqual(result.metadata["reason"], "timeout")
        self.assertTrue(result.data["timed_out"])

    def test_zero_tests_is_non_retryable_failure(self):
        runner = TestRunner(".")
        with mock.patch(
            "tools.test_runner.subprocess.run",
            return_value=subprocess.CompletedProcess(
                [], 0, stdout="", stderr="Ran 0 tests in 0.000s\n\nOK\n"
            ),
        ):
            result = runner.execute(
                {"test_spec": TestSpec("full")},
                structured=True,
            )
        self.assertEqual(result.status, "failed")
        self.assertFalse(result.retryable)
        self.assertEqual(result.metadata["reason"], "zero_tests")

    def test_counts_ids_and_skips_are_structured(self):
        runner = TestRunner(".")
        output = (
            "FAIL: test_one (tests.test_demo.Demo.test_one)\n"
            "ERROR: test_two (tests.test_demo.Demo.test_two)\n"
            "Ran 3 tests in 0.010s\n\n"
            "FAILED (failures=1, errors=1, skipped=1)\n"
        )
        with mock.patch(
            "tools.test_runner.subprocess.run",
            return_value=subprocess.CompletedProcess([], 1, stdout="", stderr=output),
        ):
            result = runner.execute(
                {"test_spec": TestSpec("full")},
                structured=True,
            )
        self.assertEqual(result.data["tests_run"], 3)
        self.assertEqual(result.data["failures"], 1)
        self.assertEqual(result.data["errors"], 1)
        self.assertEqual(result.data["skipped"], 1)
        self.assertEqual(
            result.data["failed_test_ids"],
            ["tests.test_demo.Demo.test_one"],
        )

    def test_success_with_skips_remains_ok_and_has_seven_keys(self):
        runner = TestRunner(".")
        with mock.patch(
            "tools.test_runner.subprocess.run",
            return_value=subprocess.CompletedProcess(
                [], 0, stdout="", stderr="Ran 2 tests in 0.001s\n\nOK (skipped=1)\n"
            ),
        ):
            result = runner.execute(
                {"test_spec": TestSpec("full")},
                structured=True,
            )
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.data["skipped"], 1)
        self.assertEqual(len(result.to_dict()), 7)

    def test_oserror_is_non_retryable_and_internal_errors_propagate(self):
        runner = TestRunner(".")
        with mock.patch("tools.test_runner.subprocess.run", side_effect=OSError("spawn")):
            result = runner.execute(
                {"test_spec": TestSpec("full")},
                structured=True,
            )
        self.assertEqual(result.status, "failed")
        self.assertFalse(result.retryable)
        with mock.patch(
            "tools.test_runner.subprocess.run",
            side_effect=RuntimeError("programming defect"),
        ):
            with self.assertRaises(RuntimeError):
                runner.execute(
                    {"test_spec": TestSpec("full")},
                    structured=True,
                )

    def test_historical_arbitrary_command_remains_supported(self):
        runner = TestRunner(".")
        result = runner.execute(
            {
                "command": [
                    sys.executable,
                    "-c",
                    "import sys; print('legacy'); sys.exit(2)",
                ]
            },
            structured=True,
        )
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data["returncode"], 2)


if __name__ == "__main__":
    unittest.main()
