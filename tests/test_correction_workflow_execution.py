import copy
import tempfile
import unittest
from pathlib import Path

from brain.agent import DeveloperAgent
from brain.approval_controller import ApprovalController, ApprovalRequiredError
from brain.change_proposal_adapter import ChangeProposalAdapter
from brain.change_transaction import (
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
from brain.correction_workflow import CorrectionWorkflowController
from brain.execution_engine import ExecutionEngine
from brain.workflow_plan import ResultRef, StepSpec, WorkflowPlan
from brain.workflow_runtime import WorkflowRuntimeTransitionError
from tools.tool_result import ToolResult


def proposal_arguments(content="x"):
    return {
        "changes": [{
            "path": "new.py",
            "operation": "create",
            "new_content": content,
            "expected_sha256": None,
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
            "new_files": 1,
            "write_bytes": len(content.encode("utf-8")),
            "changed_lines": len(content.splitlines()),
        },
    }


def test_result(status="ok"):
    data = {
        "command_args": ["python", "-m", "unittest", "-v"],
        "returncode": 0 if status == "ok" else 1,
        "tests_run": 1,
        "failures": 0 if status == "ok" else 1,
        "errors": 0,
        "skipped": 0,
        "passed": 1 if status == "ok" else 0,
        "failed_test_ids": (
            [] if status == "ok" else ["tests.test_demo.Demo.test_one"]
        ),
        "error_test_ids": [],
        "stdout": "",
        "stderr": "" if status == "ok" else "AssertionError: failed",
    }
    if status == "ok":
        return ToolResult.success("test_runner", data=data)
    return ToolResult.failure(
        "test_runner",
        data=data,
        error="AssertionError: failed",
    )


class RecordingRunner:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def execute(self, args, structured=False):
        self.calls.append((args["test_spec"].scope, structured))
        return self.results.pop(0)


class RecordingTransaction:
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


class RecordingApprovals(InMemoryCorrectionApprovalService):
    def __init__(self):
        self.counter = 0
        self.requests = 0
        super().__init__(id_factory=self._next_id)

    def _next_id(self):
        self.counter += 1
        return f"logical-request-{self.counter}"

    def request(self, runtime, validated):
        self.requests += 1
        return super().request(runtime, validated)


class CorrectionWorkflowExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.agent = DeveloperAgent(
            client=None,
            memory_file=self.root / "memory.json",
            prompt_dir="prompts",
            base_dir=self.root,
            action_log_file=self.root / "actions.json",
        )
        self.controllers = []

    def _engine(self, results, transaction=None, adapter=None):
        runner = RecordingRunner(results)
        transaction = transaction or RecordingTransaction()
        approvals = RecordingApprovals()

        def factory():
            correction_engine = CorrectionEngine(
                self.root,
                approval_service=approvals,
                test_runner=runner,
                transaction=transaction,
                runtime_id_factory=lambda: "correction-runtime",
            )
            controller = CorrectionWorkflowController(
                engine=correction_engine,
                adapter=adapter,
            )
            self.controllers.append(controller)
            return controller

        return (
            ExecutionEngine(
                self.agent,
                correction_controller_factory=factory,
            ),
            runner,
            transaction,
            approvals,
        )

    @staticmethod
    def _correction_step(args=None, bindings=None, depends_on=()):
        return StepSpec(
            id="correct",
            tool="correction_workflow",
            action="apply_change_proposal",
            args=args or {},
            bindings=bindings or {},
            depends_on=depends_on,
            goal="fix the defect",
            approval="required",
        )

    def _transport(self, error):
        controller = ApprovalController(self.agent)
        request = controller.request_operation(
            error.tool_name,
            error.action_name,
            error.important_args,
            error.execute,
            force_approval=error.force_approval,
            on_cancel=error.on_cancel,
            on_request=error.on_request,
        )
        return controller, request

    def test_controlled_step_resolves_refs_and_completes_through_one_approval(self):
        engine, runner, transaction, approvals = self._engine(
            [test_result(), test_result()]
        )
        arguments = proposal_arguments()
        original = copy.deepcopy(arguments)
        self.agent.code_reader.execute = (
            lambda args, structured=False: ToolResult.success(
                "code_reader",
                data=copy.deepcopy(arguments),
            )
        )
        delivered = []
        self.agent.code_analyzer.execute = (
            lambda args, structured=False: delivered.append(copy.deepcopy(args))
            or ToolResult.success("code_analyzer", data="done")
        )
        bindings = {
            name: ResultRef("proposal", ("data", name))
            for name in arguments
        }
        plan = WorkflowPlan((
            StepSpec(
                id="proposal",
                tool="code_reader",
                action="read_file",
                args={"path": "proposal.json"},
            ),
            self._correction_step(
                bindings=bindings,
                depends_on=("proposal",),
            ),
            StepSpec(
                id="after",
                tool="code_analyzer",
                action="summarize",
                args={},
                bindings={
                    "path": ResultRef(
                        "correct",
                        ("metadata", "correction_status"),
                    )
                },
                depends_on=("correct",),
            ),
        ))

        with self.assertRaises(ApprovalRequiredError) as raised:
            engine.run_workflow(plan)

        runtime = engine.last_workflow_runtime
        correction = runtime.steps["correct"]
        logical = self.controllers[0].engine.pending_approval_request
        self.assertEqual(correction.resolved_args, arguments)
        self.assertEqual(arguments, original)
        self.assertIs(correction.correction_controller, self.controllers[0])
        self.assertEqual(approvals.requests, 1)
        self.assertEqual(
            raised.exception.important_args["logical_request_id"],
            logical.request_id,
        )
        self.assertEqual(
            raised.exception.important_args["runtime_id"],
            logical.runtime_id,
        )
        self.assertEqual(
            raised.exception.important_args["proposal_id"],
            logical.proposal_id,
        )
        self.assertEqual(transaction.calls, [])

        visible, request = self._transport(raised.exception)
        approved = visible.approve(request.request_id)

        self.assertEqual(approved.status, "approved")
        self.assertIs(approved.result, runtime)
        self.assertEqual(runtime.status, "completed")
        self.assertIsInstance(runtime.results["correct"].data, CorrectionRuntimeState)
        self.assertEqual(runtime.results["correct"].data.status, "completed")
        self.assertEqual(delivered, [{"path": "completed"}])
        self.assertEqual(len(transaction.calls), 1)
        self.assertEqual(runner.calls, [("focused", True), ("full", True)])
        self.assertEqual(approvals.requests, 1)
        self.assertIsNone(correction.correction_controller)
        self.assertEqual(arguments, original)
        self.assertEqual(visible.approve(request.request_id).status, "not_found")
        self.assertEqual(len(transaction.calls), 1)

    def test_rejection_and_cancellation_clean_controller_without_effect(self):
        for operation in ("reject", "cancel"):
            with self.subTest(operation=operation):
                engine, _, transaction, approvals = self._engine([])
                plan = WorkflowPlan((
                    self._correction_step(args=proposal_arguments()),
                ))
                with self.assertRaises(ApprovalRequiredError) as raised:
                    engine.run_workflow(plan)
                runtime = engine.last_workflow_runtime
                visible, request = self._transport(raised.exception)

                result = getattr(visible, operation)(request.request_id)

                self.assertIn(result.status, {"rejected", "cancelled"})
                self.assertEqual(runtime.status, "cancelled")
                self.assertEqual(runtime.steps["correct"].status, "skipped")
                self.assertIsNone(runtime.steps["correct"].correction_controller)
                self.assertEqual(transaction.calls, [])
                self.assertEqual(approvals.requests, 1)

    def test_wrong_logical_ids_are_rejected_and_original_request_survives(self):
        engine, _, transaction, _ = self._engine(
            [test_result(), test_result()]
        )
        plan = WorkflowPlan((
            self._correction_step(args=proposal_arguments()),
        ))
        with self.assertRaises(ApprovalRequiredError) as raised:
            engine.run_workflow(plan)
        controller = self.controllers[0]
        request = controller.engine.pending_approval_request

        with self.assertRaises(CorrectionApprovalError):
            controller.resume(
                "wrong",
                runtime_id=request.runtime_id,
                proposal_id=request.proposal_id,
                approved=True,
            )

        visible, visible_request = self._transport(raised.exception)
        self.assertEqual(visible.approve(visible_request.request_id).status, "approved")
        self.assertEqual(len(transaction.calls), 1)

    def test_invalid_declaration_fails_without_approval_or_controller_leak(self):
        engine, _, transaction, approvals = self._engine([])
        invalid = proposal_arguments()
        invalid["unknown"] = True
        runtime = engine.run_workflow(
            WorkflowPlan((self._correction_step(args=invalid),))
        )

        self.assertEqual(runtime.status, "failed")
        self.assertEqual(runtime.steps["correct"].status, "failed")
        self.assertEqual(approvals.requests, 0)
        self.assertEqual(transaction.calls, [])
        self.assertIsNone(runtime.steps["correct"].correction_controller)

    def test_invalid_budget_fails_without_approval_or_effect(self):
        engine, _, transaction, approvals = self._engine([])
        invalid = proposal_arguments()
        invalid["budget"]["write_bytes"] += 1

        runtime = engine.run_workflow(
            WorkflowPlan((self._correction_step(args=invalid),))
        )

        self.assertEqual(runtime.status, "failed")
        self.assertEqual(runtime.steps["correct"].status, "failed")
        self.assertIn(
            "Presupuesto declarativo inconsistente",
            runtime.results["correct"].error,
        )
        self.assertEqual(approvals.requests, 0)
        self.assertEqual(transaction.calls, [])

    def test_failed_focused_test_accepts_explicit_integrated_correction(self):
        engine, runner, transaction, approvals = self._engine(
            [test_result("failed"), test_result(), test_result()]
        )
        plan = WorkflowPlan((
            self._correction_step(args=proposal_arguments("x")),
        ))
        with self.assertRaises(ApprovalRequiredError) as initial:
            engine.run_workflow(plan)
        visible, request = self._transport(initial.exception)

        first = visible.approve(request.request_id)
        runtime = first.result
        self.assertEqual(runtime.status, "awaiting_correction")
        self.assertEqual(runtime.steps["correct"].status, "awaiting_correction")
        self.assertIsNotNone(runtime.steps["correct"].correction_controller)

        with self.assertRaises(ApprovalRequiredError) as correction:
            engine.submit_workflow_correction(
                plan,
                runtime,
                proposal_arguments("y"),
            )
        second_visible, second_request = self._transport(correction.exception)
        second = second_visible.approve(second_request.request_id)

        self.assertEqual(second.status, "approved")
        self.assertEqual(runtime.status, "completed")
        self.assertEqual(len(transaction.calls), 2)
        self.assertEqual(
            runner.calls,
            [("focused", True), ("focused", True), ("full", True)],
        )
        self.assertEqual(approvals.requests, 2)
        self.assertIsNone(runtime.steps["correct"].correction_controller)
        with self.assertRaises(WorkflowRuntimeTransitionError):
            engine.submit_workflow_correction(
                plan,
                runtime,
                proposal_arguments("z"),
            )

    def test_rollback_failed_is_exposed_as_typed_terminal_failure(self):
        rollback_result = ChangeTransactionResult(
            proposal_id="untrusted",
            modified_paths=("new.py",),
            created_paths=("new.py",),
            write_bytes=1,
            changed_lines=1,
            applied=False,
            rollback_attempted=True,
            rollback_succeeded=False,
            errors=(
                TransactionErrorInfo(
                    "rollback",
                    "new.py",
                    "OSError",
                    "rollback failed",
                ),
            ),
        )
        transaction = RecordingTransaction(
            TransactionRollbackError(
                "rollback failed",
                original_error=OSError("write failed"),
                result=rollback_result,
            )
        )
        engine, _, _, _ = self._engine([], transaction=transaction)
        plan = WorkflowPlan((
            self._correction_step(args=proposal_arguments()),
        ))
        with self.assertRaises(ApprovalRequiredError) as raised:
            engine.run_workflow(plan)
        visible, request = self._transport(raised.exception)

        approved = visible.approve(request.request_id)
        runtime = approved.result

        self.assertEqual(runtime.status, "failed")
        result = runtime.results["correct"]
        self.assertEqual(result.status, "failed")
        self.assertIsInstance(result.data, CorrectionRuntimeState)
        self.assertEqual(result.data.status, "rollback_failed")
        self.assertIsNone(runtime.steps["correct"].correction_controller)

    def test_unexpected_error_before_approval_leaves_terminal_clean_runtime(self):
        class ExplodingAdapter(ChangeProposalAdapter):
            def adapt(self, arguments):
                raise RuntimeError("adapter defect")

        engine, _, transaction, approvals = self._engine(
            [],
            adapter=ExplodingAdapter(),
        )
        plan = WorkflowPlan((
            self._correction_step(args=proposal_arguments()),
        ))

        with self.assertRaisesRegex(RuntimeError, "adapter defect"):
            engine.run_workflow(plan)

        runtime = engine.last_workflow_runtime
        self.assertEqual(runtime.status, "failed")
        self.assertEqual(runtime.steps["correct"].status, "failed")
        self.assertEqual(
            runtime.results["correct"].metadata["exception_type"],
            "RuntimeError",
        )
        self.assertIsNone(runtime.steps["correct"].correction_controller)
        self.assertEqual(approvals.requests, 0)
        self.assertEqual(transaction.calls, [])

    def test_unexpected_resume_error_is_terminal_and_cannot_be_retried(self):
        transaction = RecordingTransaction(RuntimeError("transaction defect"))
        engine, _, _, approvals = self._engine([], transaction=transaction)
        plan = WorkflowPlan((
            self._correction_step(args=proposal_arguments()),
        ))
        with self.assertRaises(ApprovalRequiredError) as raised:
            engine.run_workflow(plan)
        runtime = engine.last_workflow_runtime
        visible, request = self._transport(raised.exception)

        approved = visible.approve(request.request_id)

        self.assertEqual(approved.status, "failed")
        self.assertEqual(runtime.status, "failed")
        self.assertEqual(runtime.steps["correct"].status, "failed")
        self.assertEqual(
            runtime.results["correct"].metadata["exception_type"],
            "RuntimeError",
        )
        self.assertIsNone(runtime.steps["correct"].correction_controller)
        self.assertEqual(len(transaction.calls), 1)
        self.assertEqual(approvals.requests, 1)
        self.assertEqual(visible.approve(request.request_id).status, "not_found")
        self.assertEqual(len(transaction.calls), 1)
        with self.assertRaises(WorkflowRuntimeTransitionError):
            engine.submit_workflow_correction(
                plan,
                runtime,
                proposal_arguments("retry"),
            )


if __name__ == "__main__":
    unittest.main()
