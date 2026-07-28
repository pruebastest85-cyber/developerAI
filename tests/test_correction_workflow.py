import copy
import hashlib
import tempfile
import unittest
from pathlib import Path

from brain.approval_controller import ApprovalRequiredError
from brain.change_proposal_adapter import ChangeProposalAdaptationError
from brain.change_transaction import (
    ChangeTransaction,
    ChangeTransactionResult,
    TransactionErrorInfo,
    TransactionRollbackError,
)
from brain.correction_engine import (
    CorrectionApprovalError,
    CorrectionEngine,
    InMemoryCorrectionApprovalService,
)
from brain.correction_runtime import CorrectionRuntimeState
from brain.correction_workflow import (
    CorrectionWorkflowConfigurationError,
    CorrectionWorkflowController,
)
from tools.tool_result import ToolResult


def proposal_arguments(
    path="new.py",
    content="x",
    *,
    operation="create",
    expected_sha256=None,
):
    return {
        "changes": [{
            "path": path,
            "operation": operation,
            "new_content": content,
            "expected_sha256": expected_sha256,
        }],
        "tests": [
            {
                "scope": "focused",
                "targets": ["tests.test_demo.Demo.test_one"],
            },
            {"scope": "full", "targets": []},
        ],
        "justification": "controlled change",
        "risks": ["writes one file"],
        "budget": {
            "modified_files": 1,
            "new_files": int(operation == "create"),
            "write_bytes": len(content.encode("utf-8")),
            "changed_lines": len(content.splitlines()),
        },
    }


def ok_result():
    return ToolResult.success(
        "test_runner",
        data={
            "command_args": ["python", "-m", "unittest", "-v"],
            "returncode": 0,
            "tests_run": 1,
            "failures": 0,
            "errors": 0,
            "skipped": 0,
            "passed": 1,
            "failed_test_ids": [],
            "error_test_ids": [],
            "stdout": "",
            "stderr": "",
        },
    )


def failed_result():
    return ToolResult.failure(
        "test_runner",
        error="AssertionError: failed",
        data={
            "command_args": ["python", "-m", "unittest", "-v"],
            "returncode": 1,
            "tests_run": 1,
            "failures": 1,
            "errors": 0,
            "skipped": 0,
            "passed": 0,
            "failed_test_ids": ["tests.test_demo.Demo.test_one"],
            "error_test_ids": [],
            "stdout": "",
            "stderr": "AssertionError: failed",
        },
    )


class FakeTestRunner:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def execute(self, args, structured=False):
        self.calls.append((args, structured))
        return self.results.pop(0)


class CountingTransaction:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def apply(self, validated):
        self.calls.append(validated.proposal_id)
        if self.error is not None:
            raise self.error
        return ChangeTransactionResult(
            proposal_id=validated.proposal_id,
            modified_paths=(),
            created_paths=tuple(
                change.relative_path for change in validated.resolved_changes
            ),
            write_bytes=validated.calculated_budget.write_bytes,
            changed_lines=validated.calculated_budget.changed_lines,
            applied=True,
            rollback_attempted=False,
            rollback_succeeded=None,
        )


class CountingApprovalService(InMemoryCorrectionApprovalService):
    def __init__(self):
        super().__init__(id_factory=lambda: "logical-request")
        self.requests = 0

    def request(self, runtime, validated):
        self.requests += 1
        return super().request(runtime, validated)


class CorrectionWorkflowControllerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def _controller(self, results=(), transaction=None, approvals=None):
        runner = FakeTestRunner(results)
        transaction = transaction or CountingTransaction()
        approvals = approvals or CountingApprovalService()
        engine = CorrectionEngine(
            self.root,
            approval_service=approvals,
            test_runner=runner,
            transaction=transaction,
            runtime_id_factory=lambda: "correction-runtime",
        )
        return (
            CorrectionWorkflowController(engine=engine),
            engine,
            runner,
            transaction,
            approvals,
        )

    def _start_signal(self, controller, arguments=None):
        with self.assertRaises(ApprovalRequiredError) as raised:
            controller.start("goal", arguments or proposal_arguments())
        return raised.exception

    def test_adapts_and_exposes_exactly_one_matching_logical_request(self):
        controller, engine, _, _, approvals = self._controller()

        signal = self._start_signal(controller)
        request = engine.pending_approval_request

        self.assertEqual(approvals.requests, 1)
        self.assertEqual(
            signal.important_args["logical_request_id"],
            request.request_id,
        )
        self.assertEqual(signal.important_args["runtime_id"], request.runtime_id)
        self.assertEqual(
            signal.important_args["proposal_id"],
            request.proposal_id,
        )
        self.assertEqual(signal.tool_name, "patch_applier")
        self.assertEqual(signal.action_name, "apply_change_proposal")
        self.assertTrue(signal.force_approval)
        self.assertEqual(
            engine.runtime.current_proposal.changes[0].path,
            "new.py",
        )

    def test_declarative_error_happens_before_engine_invocation(self):
        controller, engine, _, transaction, approvals = self._controller()
        arguments = proposal_arguments()
        arguments["unknown"] = True

        with self.assertRaises(ChangeProposalAdaptationError):
            controller.start("goal", arguments)

        self.assertIsNone(engine.runtime)
        self.assertEqual(approvals.requests, 0)
        self.assertEqual(transaction.calls, [])

    def test_validator_budget_failure_creates_no_approval_or_write(self):
        target = self.root / "existing.py"
        target.write_text("old\n", encoding="utf-8")
        arguments = proposal_arguments(
            "existing.py",
            "new\ncontent\n",
            operation="replace",
            expected_sha256=hashlib.sha256(b"old\n").hexdigest(),
        )
        arguments["budget"]["changed_lines"] = 99
        controller, _, _, transaction, approvals = self._controller()

        runtime = controller.start("goal", arguments)

        self.assertEqual(runtime.status, "awaiting_correction")
        self.assertEqual(approvals.requests, 0)
        self.assertEqual(transaction.calls, [])
        self.assertEqual(target.read_text(encoding="utf-8"), "old\n")

    def test_approval_runs_transaction_then_focused_and_full_once(self):
        controller, engine, runner, transaction, _ = self._controller(
            [ok_result(), ok_result()]
        )
        target = self.root / "new.py"
        signal = self._start_signal(controller)
        self.assertFalse(target.exists())

        result = signal.execute()

        self.assertIs(result, engine.runtime)
        self.assertIsInstance(result, CorrectionRuntimeState)
        self.assertEqual(result.status, "completed")
        self.assertEqual(len(transaction.calls), 1)
        self.assertEqual(
            [call[0]["test_spec"].scope for call in runner.calls],
            ["focused", "full"],
        )

    def test_real_transaction_writes_only_after_positive_approval(self):
        controller, _, _, _, _ = self._controller(
            [ok_result(), ok_result()],
            transaction=ChangeTransaction(self.root),
        )
        target = self.root / "new.py"

        signal = self._start_signal(controller)
        self.assertFalse(target.exists())
        runtime = signal.execute()

        self.assertEqual(runtime.status, "completed")
        self.assertEqual(target.read_text(encoding="utf-8"), "x")

    def test_denial_is_safe_and_consumes_logical_request(self):
        controller, engine, runner, transaction, approvals = self._controller()
        signal = self._start_signal(controller)

        result = signal.on_cancel("rejected")

        self.assertIs(result, engine.runtime)
        self.assertEqual(result.status, "awaiting_correction")
        self.assertEqual(transaction.calls, [])
        self.assertEqual(runner.calls, [])
        self.assertEqual(approvals._pending, {})
        self.assertFalse((self.root / "new.py").exists())

    def test_invalid_and_repeated_resume_cannot_duplicate_application(self):
        transaction = CountingTransaction()
        controller, engine, _, _, _ = self._controller(
            [ok_result(), ok_result()],
            transaction=transaction,
        )
        signal = self._start_signal(controller)
        request = engine.pending_approval_request

        invalid_values = (
            ("wrong", request.runtime_id, request.proposal_id),
            (request.request_id, "wrong", request.proposal_id),
            (request.request_id, request.runtime_id, "0" * 64),
        )
        for request_id, runtime_id, proposal_id in invalid_values:
            with self.subTest(
                request_id=request_id,
                runtime_id=runtime_id,
                proposal_id=proposal_id,
            ):
                with self.assertRaises(CorrectionApprovalError):
                    controller.resume(
                        request_id,
                        runtime_id=runtime_id,
                        proposal_id=proposal_id,
                        approved=True,
                    )
        result = signal.execute()
        self.assertEqual(result.status, "completed")
        with self.assertRaises(CorrectionApprovalError):
            signal.execute()
        self.assertEqual(transaction.calls, [request.proposal_id])

    def test_failed_focused_test_accepts_explicit_correction(self):
        controller, engine, runner, transaction, approvals = self._controller(
            [failed_result(), ok_result(), ok_result()]
        )
        first = self._start_signal(controller)
        failed_runtime = first.execute()

        self.assertEqual(failed_runtime.status, "awaiting_correction")
        self.assertEqual(len(runner.calls), 1)
        with self.assertRaises(ApprovalRequiredError) as raised:
            controller.submit_correction(proposal_arguments("fix.py", "y"))
        self.assertEqual(approvals.requests, 2)

        completed = raised.exception.execute()

        self.assertEqual(completed.status, "completed")
        self.assertEqual(len(transaction.calls), 2)
        self.assertEqual(
            [call[0]["test_spec"].scope for call in runner.calls],
            ["focused", "focused", "full"],
        )
        self.assertIs(completed, engine.runtime)

    def test_transaction_rollback_failure_is_preserved_by_engine(self):
        transaction_result = ChangeTransactionResult(
            proposal_id="unknown",
            modified_paths=("new.py",),
            created_paths=("new.py",),
            write_bytes=1,
            changed_lines=1,
            applied=False,
            rollback_attempted=True,
            rollback_succeeded=False,
            errors=(
                TransactionErrorInfo("rollback", "new.py", "OSError", "fail"),
            ),
        )
        error = TransactionRollbackError(
            "rollback failed",
            original_error=OSError("write failed"),
            result=transaction_result,
        )
        controller, engine, runner, _, _ = self._controller(
            transaction=CountingTransaction(error)
        )
        signal = self._start_signal(controller)

        runtime = signal.execute()

        self.assertIs(runtime, engine.runtime)
        self.assertEqual(runtime.status, "rollback_failed")
        self.assertEqual(runner.calls, [])

    def test_arguments_are_not_mutated(self):
        controller, _, _, _, _ = self._controller()
        arguments = proposal_arguments()
        original = copy.deepcopy(arguments)

        self._start_signal(controller, arguments)

        self.assertEqual(arguments, original)

    def test_rejects_nonlogical_approval_engine(self):
        class ExternalApprovalService:
            def request(self, runtime, validated):
                raise AssertionError("not used")

            def decide(
                self,
                request_id,
                *,
                runtime_id,
                proposal_id,
                approved,
            ):
                raise AssertionError("not used")

            def cancel(self, request_id):
                raise AssertionError("not used")

        engine = CorrectionEngine(
            self.root,
            approval_service=ExternalApprovalService(),
        )
        with self.assertRaises(CorrectionWorkflowConfigurationError):
            CorrectionWorkflowController(engine=engine)

    def test_module_has_no_premature_integration(self):
        source = Path("brain/correction_workflow.py").read_text(encoding="utf-8")

        self.assertNotIn("ExecutionEngine", source)
        self.assertNotIn("WorkflowRuntime", source)
        self.assertNotIn("DeveloperAgent", source)


if __name__ == "__main__":
    unittest.main()
