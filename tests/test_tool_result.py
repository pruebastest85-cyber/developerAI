import unittest

from brain.approval_controller import ApprovalRequiredError
from tools.tool_result import (
    ToolResult,
    execute_and_normalize,
    legacy_tool_value,
    normalize_tool_result,
)


class ToolResultTests(unittest.TestCase):
    def test_validates_status_and_tool_name(self):
        with self.assertRaises(ValueError):
            ToolResult(status="unknown", tool_name="demo")
        with self.assertRaises(ValueError):
            ToolResult(status="ok", tool_name="")

    def test_enforces_status_invariants(self):
        with self.assertRaises(ValueError):
            ToolResult(status="ok", tool_name="demo", error="boom")
        with self.assertRaises(ValueError):
            ToolResult(status="ok", tool_name="demo", retryable=True)
        with self.assertRaises(ValueError):
            ToolResult(status="failed", tool_name="demo")
        with self.assertRaises(ValueError):
            ToolResult(status="partial", tool_name="demo")

        failed = ToolResult.failure("demo", error="boom", retryable=True)
        partial = ToolResult.incomplete("demo", data=[1], retryable=True)
        self.assertEqual(failed.status, "failed")
        self.assertEqual(partial.status, "partial")

    def test_metadata_is_defensively_copied(self):
        metadata = {"nested": {"count": 1}}
        result = ToolResult.success("demo", metadata=metadata)
        metadata["nested"]["count"] = 2
        serialized = result.to_dict()
        serialized["metadata"]["nested"]["count"] = 3

        self.assertEqual(result.metadata["nested"]["count"], 1)
        self.assertEqual(result.to_dict()["metadata"]["nested"]["count"], 1)

    def test_to_dict_always_has_seven_keys(self):
        result = ToolResult.success("demo")
        self.assertEqual(
            set(result.to_dict()),
            {
                "status",
                "tool_name",
                "data",
                "message",
                "error",
                "metadata",
                "retryable",
            },
        )

    def test_normalization_is_idempotent_and_checks_provenance(self):
        result = ToolResult.success("demo", data=1)
        self.assertIs(
            normalize_tool_result(result, tool_name="demo"),
            result,
        )
        with self.assertRaises(ValueError):
            normalize_tool_result(result, tool_name="other")

    def test_normalizes_historical_ok_dicts(self):
        success = normalize_tool_result(
            {"ok": True, "stdout": "done"},
            tool_name="demo",
        )
        failure = normalize_tool_result(
            {"ok": False, "stderr": "boom"},
            tool_name="demo",
        )

        self.assertEqual(success.status, "ok")
        self.assertEqual(failure.status, "failed")
        self.assertEqual(failure.error, "boom")
        self.assertFalse(failure.retryable)

    def test_normalizes_raw_historical_values_without_parsing_text(self):
        for value in ("failed but historical", [1], (1, 2), True, {"value": 1}):
            with self.subTest(value=value):
                result = normalize_tool_result(value, tool_name="demo")
                self.assertEqual(result.status, "ok")
                self.assertEqual(result.data, value)

    def test_none_policies_are_explicit(self):
        self.assertEqual(
            normalize_tool_result(None, tool_name="demo", none_policy="ok").status,
            "ok",
        )
        self.assertEqual(
            normalize_tool_result(None, tool_name="demo", none_policy="failed").status,
            "failed",
        )
        self.assertIsNone(
            normalize_tool_result(
                None,
                tool_name="demo",
                none_policy="passthrough",
            )
        )

    def test_approval_required_error_is_never_converted(self):
        approval = ApprovalRequiredError(
            tool_name="demo",
            action_name="run",
            important_args={},
            execute=lambda: None,
            message="approval",
        )

        with self.assertRaises(ApprovalRequiredError):
            execute_and_normalize(
                "demo",
                lambda: (_ for _ in ()).throw(approval),
            )

    def test_only_declared_operational_exceptions_are_converted(self):
        converted = execute_and_normalize(
            "demo",
            lambda: (_ for _ in ()).throw(ValueError("bad input")),
            operational_exceptions=(ValueError,),
        )
        self.assertEqual(converted.status, "failed")
        self.assertEqual(converted.metadata["exception_type"], "ValueError")

        with self.assertRaises(RuntimeError):
            execute_and_normalize(
                "demo",
                lambda: (_ for _ in ()).throw(RuntimeError("bug")),
                operational_exceptions=(ValueError,),
            )

        with self.assertRaises(KeyError):
            execute_and_normalize(
                "demo",
                lambda: {}["missing"],
                operational_exceptions=(ValueError,),
            )

    def test_rejects_overly_broad_operational_exceptions(self):
        for exception_type in (Exception, BaseException):
            with self.subTest(exception_type=exception_type):
                with self.assertRaises(ValueError):
                    execute_and_normalize(
                        "demo",
                        lambda: "unused",
                        operational_exceptions=(exception_type,),
                    )

    def test_internal_value_error_propagates_when_not_declared(self):
        with self.assertRaises(ValueError):
            execute_and_normalize(
                "demo",
                lambda: (_ for _ in ()).throw(ValueError("programming defect")),
            )

    def test_legacy_success_values_keep_raw_compatibility(self):
        self.assertEqual(
            legacy_tool_value(ToolResult.success("demo", data={"value": 1})),
            {"value": 1},
        )
        self.assertEqual(
            legacy_tool_value(ToolResult.success("demo", message="done")),
            "done",
        )
        self.assertIsNone(legacy_tool_value(ToolResult.success("demo")))

    def test_legacy_failed_and_partial_results_keep_explicit_status(self):
        failed = legacy_tool_value(
            ToolResult.failure(
                "demo",
                error="boom",
                message="could not finish",
                data={"partial": 1},
                metadata={"attempt": 2},
                retryable=True,
            )
        )
        partial_data = legacy_tool_value(
            ToolResult.incomplete(
                "demo", data=[1], metadata={"page": 1}, retryable=True
            )
        )
        partial_message = legacy_tool_value(
            ToolResult.incomplete("demo", message="some output")
        )
        failed_message = legacy_tool_value(
            ToolResult.failure("demo", message="failed visibly")
        )

        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error"], "boom")
        self.assertEqual(failed["data"], {"partial": 1})
        self.assertEqual(failed["metadata"], {"attempt": 2})
        self.assertTrue(failed["retryable"])
        self.assertEqual(failed["tool_name"], "demo")
        self.assertEqual(failed_message["status"], "failed")
        self.assertEqual(partial_data["status"], "partial")
        self.assertEqual(partial_data["data"], [1])
        self.assertEqual(partial_message["status"], "partial")


if __name__ == "__main__":
    unittest.main()
