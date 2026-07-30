import hashlib
import tempfile
import unittest
from pathlib import Path

from brain.change_proposal import ChangeProposal, FileChange, ProposalBudget, TestSpec
from brain.change_transaction import (
    ChangeTransaction,
    ChangeTransactionResult,
    TransactionApplyError,
    TransactionErrorInfo,
    TransactionRollbackError,
)
from brain.change_validator import ChangeProposalValidator
from brain.correction_engine import (
    CorrectionApprovalError,
    CorrectionBudgetExceededError,
    CorrectionEngine,
    CorrectionEngineError,
    CorrectionTestResultError,
    InMemoryCorrectionApprovalService,
    PermissionManagerCorrectionApprovalAdapter,
)
from brain.correction_runtime import CorrectionRuntimeTransitionError
from brain.permission_manager import PermissionManager
from brain.workflow_limits import WorkflowLimits
from tools.registry import build_default_registry
from tools.tool_result import ToolResult


FOCUSED = TestSpec("focused", ("tests.test_demo.Demo.test_one",))
FULL = TestSpec("full")


def make_proposal(path, content="x", tests=(FOCUSED, FULL)):
    return ChangeProposal(
        changes=(FileChange(path, "create", content, None),),
        tests=tests,
        justification="change",
        risks=("writes a test file",),
        budget=ProposalBudget(
            modified_files=1,
            new_files=1,
            write_bytes=len(content.encode("utf-8")),
            changed_lines=len(content.splitlines()),
        ),
    )


def make_replace_proposal(path, old_content, new_content, tests=(FOCUSED, FULL)):
    return ChangeProposal(
        changes=(
            FileChange(
                path,
                "replace",
                new_content,
                hashlib.sha256(old_content.encode("utf-8")).hexdigest(),
            ),
        ),
        tests=tests,
        justification="correct previous change",
        risks=("replaces a test file",),
        budget=ProposalBudget(
            modified_files=1,
            new_files=0,
            write_bytes=len(new_content.encode("utf-8")),
            changed_lines=max(
                len(old_content.splitlines()),
                len(new_content.splitlines()),
            ),
        ),
    )


def ok_result(tests_run=1, skipped=0):
    return ToolResult.success(
        "test_runner",
        data={
            "command_args": ["python", "-m", "unittest", "-v"],
            "returncode": 0,
            "tests_run": tests_run,
            "failures": 0,
            "errors": 0,
            "skipped": skipped,
            "passed": tests_run - skipped,
            "failed_test_ids": [],
            "error_test_ids": [],
            "stdout": "",
            "stderr": "",
        },
    )


def failed_result(label="one", reason=None):
    metadata = {} if reason is None else {"reason": reason}
    return ToolResult.failure(
        "test_runner",
        error=f"AssertionError: {label}",
        data={
            "command_args": ["python", "-m", "unittest", "-v"],
            "returncode": 1 if reason is None else 0,
            "tests_run": 1 if reason is None else 0,
            "failures": 1 if reason is None else 0,
            "errors": 0,
            "skipped": 0,
            "failed_test_ids": (
                ["tests.test_demo.Demo.test_one"] if reason is None else []
            ),
            "error_test_ids": [],
            "stdout": "",
            "stderr": f"AssertionError: {label}",
            "timed_out": reason == "timeout",
        },
        metadata=metadata,
        retryable=reason == "timeout",
    )


class FakeTestRunner:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def execute(self, args, structured=False):
        self.calls.append((args, structured))
        return self.results.pop(0)


class FakeTransaction:
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
                item.relative_path for item in validated.resolved_changes
            ),
            write_bytes=validated.calculated_budget.write_bytes,
            changed_lines=validated.calculated_budget.changed_lines,
            applied=True,
            rollback_attempted=False,
            rollback_succeeded=None,
        )


class CorrectionEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ids = iter(("request-1", "request-2", "request-3", "request-4"))
        self.approvals = InMemoryCorrectionApprovalService(
            id_factory=lambda: next(self.ids)
        )

    def _engine(self, results, **overrides):
        values = {
            "workspace": self.root,
            "approval_service": self.approvals,
            "test_runner": FakeTestRunner(results),
            "runtime_id_factory": lambda: "runtime-1",
        }
        values.update(overrides)
        return CorrectionEngine(**values)

    def _approve(self, engine):
        runtime = engine.runtime
        return engine.resume(
            runtime.pending_approval_request_id,
            runtime_id=runtime.runtime_id,
            proposal_id=runtime.current_proposal.proposal_id,
            approved=True,
        )

    def test_start_validates_without_writing_and_requests_once(self):
        proposal = make_proposal("new.py")
        engine = self._engine([ok_result(), ok_result()])

        runtime = engine.start("goal", proposal)
        first_request = runtime.pending_approval_request_id
        engine._request_approval(runtime)

        self.assertEqual(runtime.status, "awaiting_approval")
        self.assertEqual(runtime.correction_iterations, 0)
        self.assertEqual(first_request, "request-1")
        self.assertEqual(runtime.pending_approval_request_id, first_request)
        self.assertFalse((self.root / "new.py").exists())
        self.assertEqual(runtime.proposal_history, (proposal,))

    def test_dependencies_must_match_workspace_and_contract(self):
        other = self.root / "other"
        other.mkdir()
        with self.assertRaisesRegex(CorrectionEngineError, "otro workspace"):
            CorrectionEngine(
                self.root,
                validator=ChangeProposalValidator(other),
            )
        with self.assertRaisesRegex(CorrectionEngineError, "límites incompatibles"):
            CorrectionEngine(
                self.root,
                limits=WorkflowLimits(max_modified_files=1),
                validator=ChangeProposalValidator(self.root),
            )

    def test_invalid_initial_proposal_does_not_request_or_consume(self):
        proposal = make_proposal("missing/new.py")
        engine = self._engine([])

        runtime = engine.start("goal", proposal)

        self.assertEqual(runtime.status, "awaiting_correction")
        self.assertEqual(runtime.correction_iterations, 0)
        self.assertIsNone(runtime.pending_approval_request_id)
        self.assertFalse((self.root / "missing").exists())

    def test_approval_is_bound_to_request_runtime_and_proposal(self):
        engine = self._engine([ok_result(), ok_result()])
        runtime = engine.start("goal", make_proposal("new.py"))
        request_id = runtime.pending_approval_request_id
        proposal_id = runtime.current_proposal.proposal_id

        invalid = (
            ("wrong", runtime.runtime_id, proposal_id),
            (request_id, "wrong-runtime", proposal_id),
            (request_id, runtime.runtime_id, "0" * 64),
        )
        for supplied_request, supplied_runtime, supplied_proposal in invalid:
            with self.subTest(values=(supplied_request, supplied_runtime, supplied_proposal)):
                with self.assertRaises(CorrectionApprovalError):
                    engine.resume(
                        supplied_request,
                        runtime_id=supplied_runtime,
                        proposal_id=supplied_proposal,
                        approved=True,
                    )
        self.assertEqual(runtime.status, "awaiting_approval")
        with self.assertRaises(CorrectionApprovalError):
            engine.resume(
                request_id,
                runtime_id=runtime.runtime_id,
                proposal_id=proposal_id,
                approved=1,
            )

    def test_approval_snapshot_blocks_all_mutated_budget_state_before_apply(self):
        mutations = (
            lambda runtime: setattr(runtime, "total_write_bytes", 1),
            lambda runtime: setattr(runtime, "total_changed_lines", 1),
            lambda runtime: setattr(runtime, "modified_files", frozenset({"old.py"})),
            lambda runtime: setattr(runtime, "new_files", frozenset({"old.py"})),
            lambda runtime: setattr(runtime, "correction_iterations", 1),
            lambda runtime: setattr(runtime, "test_runs", (object(),)),
            lambda runtime: setattr(runtime, "applied_proposal_ids", frozenset({"old"})),
            lambda runtime: setattr(
                runtime,
                "limits",
                WorkflowLimits(max_total_change_bytes=1),
            ),
            lambda runtime: setattr(runtime, "runtime_id", "mutated-runtime"),
        )
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                approvals = InMemoryCorrectionApprovalService(
                    id_factory=lambda: f"snapshot-{index}"
                )
                transaction = FakeTransaction()
                engine = CorrectionEngine(
                    self.root,
                    approval_service=approvals,
                    test_runner=FakeTestRunner([]),
                    transaction=transaction,
                    runtime_id_factory=lambda: "runtime",
                )
                runtime = engine.start("goal", make_proposal(f"file-{index}.py"))
                request_id = runtime.pending_approval_request_id
                proposal_id = runtime.current_proposal.proposal_id
                mutate(runtime)

                with self.assertRaises(CorrectionApprovalError):
                    engine.resume(
                        request_id,
                        runtime_id=runtime.runtime_id,
                        proposal_id=proposal_id,
                        approved=True,
                    )

                self.assertEqual(transaction.calls, [])
                self.assertEqual(runtime.status, "cancelled")
                self.assertIsNone(runtime.pending_approval_request_id)

    def test_accumulated_byte_limit_allows_exact_boundary_and_rejects_plus_one(self):
        limits = WorkflowLimits(max_total_change_bytes=2)
        transaction = FakeTransaction()
        engine = self._engine(
            [failed_result(), ok_result(), ok_result()],
            limits=limits,
            transaction=transaction,
        )
        runtime = engine.start("goal", make_proposal("one.py", content="x"))
        self._approve(engine)

        runtime.total_write_bytes = 1
        engine.submit_correction(make_proposal("two.py", content="x"))
        self.assertEqual(runtime.status, "awaiting_approval")
        self._approve(engine)
        self.assertEqual(len(transaction.calls), 2)

        other = self._engine(
            [failed_result()],
            limits=limits,
            transaction=FakeTransaction(),
        )
        other_runtime = other.start("goal", make_proposal("three.py", content="xx"))
        self._approve(other)
        other.submit_correction(make_proposal("four.py", content="x"))
        self.assertEqual(other_runtime.status, "awaiting_correction")
        self.assertIsNone(other_runtime.pending_approval_request_id)

    def test_approval_summary_must_match_validated_proposal(self):
        class WrongSummaryApprovalService(InMemoryCorrectionApprovalService):
            def request(self, runtime, validated):
                return type(super().request(runtime, validated))(
                    request_id="wrong-summary",
                    runtime_id=runtime.runtime_id,
                    proposal_id=validated.proposal_id,
                    goal=runtime.goal,
                    changes=(),
                    budget=validated.calculated_budget.canonical_dict(),
                )

        engine = CorrectionEngine(
            self.root,
            approval_service=WrongSummaryApprovalService(),
            test_runner=FakeTestRunner([]),
            transaction=FakeTransaction(),
            runtime_id_factory=lambda: "runtime",
        )
        with self.assertRaisesRegex(CorrectionApprovalError, "resumen"):
            engine.start("goal", make_proposal("new.py"))
        self.assertIsNone(engine.runtime.pending_approval_request_id)

    def test_rejection_does_not_apply_or_consume_budget(self):
        transaction = FakeTransaction()
        engine = self._engine([], transaction=transaction)
        runtime = engine.start("goal", make_proposal("new.py"))

        engine.resume(
            runtime.pending_approval_request_id,
            runtime_id=runtime.runtime_id,
            proposal_id=runtime.current_proposal.proposal_id,
            approved=False,
        )

        self.assertEqual(runtime.status, "awaiting_correction")
        self.assertEqual(runtime.correction_iterations, 0)
        self.assertEqual(runtime.total_write_bytes, 0)
        self.assertEqual(transaction.calls, [])

    def test_approval_applies_once_then_runs_focused_before_full(self):
        runner = FakeTestRunner([ok_result(), ok_result(skipped=1)])
        transaction = FakeTransaction()
        engine = self._engine([], test_runner=runner, transaction=transaction)
        runtime = engine.start("goal", make_proposal("new.py"))

        self._approve(engine)

        self.assertEqual(runtime.status, "completed")
        self.assertEqual(len(transaction.calls), 1)
        self.assertEqual(
            [call[0]["test_spec"].scope for call in runner.calls],
            ["focused", "full"],
        )
        self.assertTrue(all(call[1] for call in runner.calls))
        self.assertEqual(runtime.total_write_bytes, 1)
        self.assertEqual(len(runtime.applied_proposal_ids), 1)
        with self.assertRaises(CorrectionApprovalError):
            self._approve(engine)
        self.assertEqual(len(transaction.calls), 1)

    def test_focused_failure_blocks_full_and_records_fingerprint(self):
        runner = FakeTestRunner([failed_result()])
        engine = self._engine([], test_runner=runner, transaction=FakeTransaction())
        runtime = engine.start("goal", make_proposal("new.py"))

        self._approve(engine)

        self.assertEqual(runtime.status, "awaiting_correction")
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(runner.calls[0][0]["test_spec"].scope, "focused")
        self.assertIsNotNone(runtime.current_failure_fingerprint)
        self.assertEqual(runtime.test_runs[0].category, "test_failure")
        self.assertEqual(
            runtime.failure_counts[runtime.current_failure_fingerprint],
            1,
        )

    def test_timeout_zero_tests_and_spawn_error_block_full(self):
        cases = (
            failed_result("timeout", "timeout"),
            failed_result("zero", "zero_tests"),
            ToolResult.incomplete(
                "test_runner",
                data={"returncode": 0, "tests_run": 1},
                message="incomplete",
            ),
            ToolResult.failure(
                "test_runner",
                error="spawn",
                data={"returncode": None, "tests_run": 0},
                metadata={"exception_type": "OSError"},
            ),
        )
        for index, result in enumerate(cases):
            with self.subTest(index=index):
                approvals = InMemoryCorrectionApprovalService(
                    id_factory=lambda: f"case-{index}"
                )
                runner = FakeTestRunner([result])
                engine = CorrectionEngine(
                    self.root,
                    approval_service=approvals,
                    test_runner=runner,
                    transaction=FakeTransaction(),
                    runtime_id_factory=lambda: f"runtime-{index}",
                )
                engine.start("goal", make_proposal(f"case{index}.py"))
                self._approve(engine)
                self.assertEqual(len(runner.calls), 1)
                self.assertEqual(engine.runtime.status, "awaiting_correction")

    def test_full_failure_waits_for_correction(self):
        runner = FakeTestRunner([ok_result(), failed_result("full")])
        engine = self._engine([], test_runner=runner, transaction=FakeTransaction())
        runtime = engine.start("goal", make_proposal("new.py"))

        self._approve(engine)

        self.assertEqual(runtime.status, "awaiting_correction")
        self.assertEqual(len(runtime.test_runs), 2)
        self.assertEqual(runtime.test_runs[-1].test_spec.scope, "full")

    def test_repeated_failure_limit_is_exact_for_one_and_two(self):
        first_engine = self._engine(
            [],
            limits=WorkflowLimits(max_repeated_failure=1),
            test_runner=FakeTestRunner([failed_result()]),
            transaction=FakeTransaction(),
        )
        first_engine.start("goal", make_proposal("one.py"))
        self._approve(first_engine)
        self.assertEqual(
            first_engine.runtime.status,
            "repeated_failure_limit_reached",
        )

        approvals = InMemoryCorrectionApprovalService(
            id_factory=iter(("a", "b")).__next__
        )
        second_engine = CorrectionEngine(
            self.root,
            limits=WorkflowLimits(max_repeated_failure=2),
            approval_service=approvals,
            test_runner=FakeTestRunner([failed_result(), failed_result()]),
            transaction=FakeTransaction(),
            runtime_id_factory=lambda: "runtime-two",
        )
        second_engine.start("goal", make_proposal("two.py", tests=(FOCUSED, FULL)))
        self._approve(second_engine)
        self.assertEqual(second_engine.runtime.status, "awaiting_correction")
        second_engine.submit_correction(
            make_proposal("three.py", tests=(FOCUSED, FULL))
        )
        self._approve(second_engine)
        self.assertEqual(
            second_engine.runtime.status,
            "repeated_failure_limit_reached",
        )

    def test_different_failures_have_independent_counts(self):
        runner = FakeTestRunner([failed_result("one"), failed_result("two")])
        engine = self._engine(
            [],
            test_runner=runner,
            transaction=FakeTransaction(),
        )
        runtime = engine.start("goal", make_proposal("one.py"))
        self._approve(engine)
        engine.submit_correction(make_proposal("two.py"))
        self._approve(engine)

        self.assertEqual(runtime.status, "awaiting_correction")
        self.assertEqual(sorted(runtime.failure_counts.values()), [1, 1])

    def test_corrections_consume_only_after_successful_application(self):
        runner = FakeTestRunner(
            [
                failed_result("initial"),
                failed_result("fix1"),
                failed_result("fix2"),
            ]
        )
        engine = self._engine([], test_runner=runner, transaction=FakeTransaction())
        runtime = engine.start("goal", make_proposal("initial.py"))
        self._approve(engine)

        engine.submit_correction(make_proposal("fix1.py"))
        self.assertEqual(runtime.correction_iterations, 0)
        self._approve(engine)
        self.assertEqual(runtime.correction_iterations, 1)

        engine.submit_correction(make_proposal("fix2.py"))
        self._approve(engine)
        self.assertEqual(runtime.correction_iterations, 2)

        with self.assertRaises(CorrectionRuntimeTransitionError):
            engine.submit_correction(make_proposal("fix3.py"))
        self.assertEqual(runtime.status, "correction_limit_reached")

    def test_invalid_or_rejected_correction_consumes_no_iteration(self):
        engine = self._engine(
            [],
            test_runner=FakeTestRunner([failed_result()]),
            transaction=FakeTransaction(),
        )
        runtime = engine.start("goal", make_proposal("initial.py"))
        self._approve(engine)

        engine.submit_correction(make_proposal("missing/fix.py"))
        self.assertEqual(runtime.status, "awaiting_correction")
        self.assertEqual(runtime.correction_iterations, 0)

        engine.submit_correction(make_proposal("fix.py"))
        engine.resume(
            runtime.pending_approval_request_id,
            runtime_id=runtime.runtime_id,
            proposal_id=runtime.current_proposal.proposal_id,
            approved=False,
        )
        self.assertEqual(runtime.correction_iterations, 0)
        self.assertEqual(runtime.total_write_bytes, 1)

    def test_accumulated_limits_are_checked_before_approval(self):
        limits = WorkflowLimits(
            max_modified_files=1,
            max_total_change_bytes=2,
            max_changed_lines=1,
        )
        approvals = InMemoryCorrectionApprovalService(
            id_factory=iter(("one", "two")).__next__
        )
        engine = CorrectionEngine(
            self.root,
            limits=limits,
            approval_service=approvals,
            test_runner=FakeTestRunner([failed_result()]),
            transaction=FakeTransaction(),
            runtime_id_factory=lambda: "runtime",
        )
        runtime = engine.start("goal", make_proposal("one.py"))
        self._approve(engine)

        engine.submit_correction(make_proposal("two.py"))

        self.assertEqual(runtime.status, "awaiting_correction")
        self.assertIsNone(runtime.pending_approval_request_id)
        self.assertEqual(runtime.correction_iterations, 0)
        self.assertIn("max_modified_files", runtime.terminal_reason)

    def test_repeated_path_counts_once_but_accumulates_change_budget(self):
        limits = WorkflowLimits(
            max_modified_files=1,
            max_total_change_bytes=2,
            max_changed_lines=2,
        )
        engine = self._engine(
            [failed_result("initial"), ok_result(), ok_result()],
            limits=limits,
        )
        runtime = engine.start("goal", make_proposal("same.py", "x"))
        self._approve(engine)

        engine.submit_correction(
            make_replace_proposal("same.py", "x", "y")
        )
        self._approve(engine)

        self.assertEqual(runtime.status, "completed")
        self.assertEqual(runtime.modified_files, frozenset({"same.py"}))
        self.assertEqual(runtime.total_write_bytes, 2)
        self.assertEqual(runtime.total_changed_lines, 2)
        self.assertEqual((self.root / "same.py").read_text(encoding="utf-8"), "y")

    def test_transaction_failures_select_failed_or_rollback_failed(self):
        base_result = ChangeTransactionResult(
            proposal_id="x",
            modified_paths=(),
            created_paths=(),
            write_bytes=0,
            changed_lines=0,
            applied=False,
            rollback_attempted=True,
            rollback_succeeded=True,
            errors=(TransactionErrorInfo("apply", None, "OSError", "fail"),),
        )
        apply_error = TransactionApplyError("apply", base_result)
        rollback_result = ChangeTransactionResult(
            **{
                **base_result.__dict__,
                "rollback_succeeded": False,
                "modified_paths": ("uncertain.py",),
                "write_bytes": 4,
                "changed_lines": 1,
            }
        )
        rollback_error = TransactionRollbackError(
            "rollback",
            original_error=OSError("original"),
            result=rollback_result,
        )
        for error, expected in (
            (apply_error, "failed"),
            (rollback_error, "rollback_failed"),
        ):
            with self.subTest(expected=expected):
                approvals = InMemoryCorrectionApprovalService(
                    id_factory=lambda: expected
                )
                engine = CorrectionEngine(
                    self.root,
                    approval_service=approvals,
                    test_runner=FakeTestRunner([]),
                    transaction=FakeTransaction(error),
                    runtime_id_factory=lambda: f"runtime-{expected}",
                )
                engine.start("goal", make_proposal(f"{expected}.py"))
                self._approve(engine)
                self.assertEqual(engine.runtime.status, expected)
                expected_bytes = 0 if expected == "failed" else rollback_result.write_bytes
                self.assertEqual(engine.runtime.total_write_bytes, expected_bytes)
                if expected == "rollback_failed":
                    self.assertEqual(
                        engine.runtime.modified_files,
                        frozenset(rollback_result.modified_paths),
                    )

    def test_cancel_and_terminal_states_block_actions(self):
        engine = self._engine([])
        runtime = engine.start("goal", make_proposal("new.py"))
        engine.cancel()
        self.assertEqual(runtime.status, "cancelled")
        with self.assertRaises(CorrectionRuntimeTransitionError):
            engine.cancel()
        with self.assertRaises(CorrectionRuntimeTransitionError):
            engine.submit_correction(make_proposal("other.py"))

    def test_invalid_test_result_and_no_arbitrary_commands(self):
        class BadRunner:
            def execute(self, args, structured=False):
                self.args = args
                return "raw"

        runner = BadRunner()
        engine = self._engine([], test_runner=runner, transaction=FakeTransaction())
        engine.start("goal", make_proposal("new.py"))
        with self.assertRaises(CorrectionTestResultError):
            self._approve(engine)
        self.assertEqual(
            set(runner.args),
            {"test_spec", "timeout"},
        )

    def test_malformed_successful_test_result_is_rejected(self):
        runner = FakeTestRunner(
            [ToolResult.success("test_runner", data={"tests_run": 0})]
        )
        engine = self._engine(
            [],
            test_runner=runner,
            transaction=FakeTransaction(),
        )
        engine.start("goal", make_proposal("new.py"))
        with self.assertRaises(CorrectionTestResultError):
            self._approve(engine)
        self.assertNotEqual(engine.runtime.status, "completed")

        inconsistent = ok_result()
        inconsistent.data["passed"] = 0
        approvals = InMemoryCorrectionApprovalService(
            id_factory=lambda: "inconsistent"
        )
        second = CorrectionEngine(
            self.root,
            approval_service=approvals,
            test_runner=FakeTestRunner([inconsistent]),
            transaction=FakeTransaction(),
            runtime_id_factory=lambda: "runtime-inconsistent",
        )
        second.start("goal", make_proposal("other.py"))
        with self.assertRaises(CorrectionTestResultError):
            self._approve(second)

    def test_transaction_result_must_confirm_exact_application(self):
        class FalseTransaction(FakeTransaction):
            def apply(self, validated):
                self.calls.append(validated.proposal_id)
                return ChangeTransactionResult(
                    proposal_id="0" * 64,
                    modified_paths=(),
                    created_paths=(),
                    write_bytes=0,
                    changed_lines=0,
                    applied=False,
                    rollback_attempted=False,
                    rollback_succeeded=None,
                )

        transaction = FalseTransaction()
        engine = self._engine(
            [],
            transaction=transaction,
            test_runner=FakeTestRunner([]),
        )
        engine.start("goal", make_proposal("new.py"))
        with self.assertRaises(CorrectionEngineError):
            self._approve(engine)
        self.assertEqual(engine.runtime.applied_proposal_ids, frozenset())
        self.assertEqual(engine.runtime.total_write_bytes, 0)

    def test_permission_manager_adapter_binds_and_consumes_once(self):
        manager = PermissionManager(build_default_registry())
        adapter = PermissionManagerCorrectionApprovalAdapter(manager)
        engine = CorrectionEngine(
            self.root,
            approval_service=adapter,
            test_runner=FakeTestRunner([ok_result(), ok_result()]),
            transaction=FakeTransaction(),
            runtime_id_factory=lambda: "runtime",
        )
        runtime = engine.start("goal", make_proposal("new.py"))
        request_id = runtime.pending_approval_request_id

        self._approve(engine)

        self.assertEqual(runtime.status, "completed")
        with self.assertRaises(CorrectionApprovalError):
            adapter.decide(
                request_id,
                runtime_id=runtime.runtime_id,
                proposal_id=runtime.current_proposal.proposal_id,
                approved=True,
            )

    def test_real_transaction_applies_only_after_approval(self):
        engine = self._engine(
            [ok_result(), ok_result()],
            transaction=ChangeTransaction(self.root),
            validator=ChangeProposalValidator(self.root),
        )
        runtime = engine.start("goal", make_proposal("real.py", "hello"))
        self.assertFalse((self.root / "real.py").exists())

        self._approve(engine)

        self.assertEqual((self.root / "real.py").read_text(), "hello")
        self.assertEqual(runtime.status, "completed")


if __name__ == "__main__":
    unittest.main()
