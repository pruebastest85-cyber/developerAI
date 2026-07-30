import hashlib
import json
import subprocess
import tempfile
import unittest
from dataclasses import dataclass, replace
from pathlib import Path
from unittest.mock import Mock

from brain.agent import DeveloperAgent
from brain.change_proposal import (
    ChangeProposal,
    FileChange,
    ProposalBudget,
    TestSpec,
)
from brain.controlled_programming_session import (
    ControlledProgrammingSessionError,
    ProgrammingSessionState,
)
from brain.correction_engine import CorrectionEngine, InMemoryCorrectionApprovalService
from brain.correction_runtime import CorrectionRuntimeState, CorrectionTestRun
from brain.correction_workflow import CorrectionWorkflowController
from brain.local_model_client import ModelResponseMetadata
from brain.local_model_client import LocalModelClient
from brain.local_model_config import LocalModelConfig
from brain.model_correction import (
    ModelCorrectionAdapter,
    ModelCorrectionGenerationPolicy,
    ModelCorrectionGenerationResult,
    ModelCorrectionProposalDraft,
    ModelCorrectionService,
)
from brain.model_plan_review import ModelPlanReviewView
from brain.model_transport import TransportResponse
from brain.workflow_plan import StepSpec, WorkflowPlan
from brain.workflow_limits import WorkflowLimits
from brain.workflow_runtime import WorkflowRuntimeState
from tools.tool_result import ToolResult


def correction_draft(
    path: Path,
    content: str,
    *,
    summary="Fix the failing implementation",
    justification="Make the authorized test pass",
    risks=("Small isolated replacement",),
):
    return ModelCorrectionProposalDraft.from_mapping(
        {
            "schema_version": "1",
            "summary": summary,
            "changes": [
                {
                    "operation": "replace",
                    "path": path.name,
                    "new_content": content,
                    "expected_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "justification": justification,
                }
            ],
            "risks": list(risks),
        }
    )


def metadata():
    return ModelResponseMetadata(
        provider="lm_studio",
        requested_model="qwen",
        reported_model=None,
        request_id=None,
        input_tokens=None,
        output_tokens=None,
        total_tokens=None,
        finish_reason=None,
        duration_seconds=0,
        endpoint_id="lm_studio@localhost:1234",
        structured_format="json_schema",
    )


class ControlledCorrectionService:
    def __init__(self, drafts):
        self.drafts = list(drafts)
        self.contexts = []

    def propose(self, context):
        self.contexts.append(context)
        value = self.drafts.pop(0)
        if isinstance(value, BaseException):
            raise value
        return ModelCorrectionGenerationResult(value, metadata())


class JsonCorrectionTransport:
    def __init__(self, payload, *, raw_content=None):
        self.payload = payload
        self.raw_content = raw_content
        self.requests = []

    def send(self, request):
        self.requests.append(request)
        body = json.dumps(
            {
                "id": "correction-request",
                "model": "qwen",
                "choices": [
                    {
                        "message": {
                            "content": (
                                self.raw_content
                                if self.raw_content is not None
                                else json.dumps(self.payload)
                            ),
                        },
                        "finish_reason": "stop",
                    }
                ],
            }
        ).encode("utf-8")
        return TransportResponse(
            200,
            (("Content-Type", "application/json"),),
            body,
        )


@dataclass(frozen=True)
class TrustedPlanningResult:
    workflow: WorkflowPlan


class TrustedPlanningService:
    """Test-only source for the privileged workflow excluded from model authority."""

    def __init__(self, workflow):
        self.workflow = workflow

    def plan(self, request):
        return TrustedPlanningResult(self.workflow)


class TrustedCorrectionPlanReview:
    """Test-only trusted plan source; execution still uses the public session API."""

    def __init__(self, workflow):
        self.agent = None
        self.workflow = workflow
        self.plan_id = "trusted-correction-plan"

    def register(self, planning_result):
        return ModelPlanReviewView(
            plan_id=self.plan_id,
            status="pending",
            goal="Repair behavior",
            step_count=1,
            steps=(),
            text="Trusted correction integration plan",
        )

    def approve(self, plan_id):
        if plan_id != self.plan_id:
            raise ValueError("wrong plan")
        return self.agent.execution_engine.run_workflow(
            self.workflow,
            goal="Repair behavior",
            safe_logging=True,
        )

    def reject(self, plan_id):
        return None

    def cancel(self, plan_id):
        return None


class ModelCorrectionSessionIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        (self.base / "tests").mkdir()
        (self.base / "tests" / "test_smoke.py").write_text(
            "import unittest\n\n"
            "class SmokeTests(unittest.TestCase):\n"
            "    def test_ok(self):\n"
            "        self.assertTrue(True)\n",
            encoding="utf-8",
        )
        self.target = self.base / "sample.py"
        self.target.write_text("value = 1\n", encoding="utf-8")
        self.service = ControlledCorrectionService(
            [correction_draft(self.target, "value = 2\n")]
        )
        self.agent = DeveloperAgent(
            None,
            base_dir=self.base,
            action_log_file=self.base / "actions.json",
            model_correction_service=self.service,
            model_correction_adapter=ModelCorrectionAdapter(self.base),
        )
        self.session = self.agent.get_programming_session()

        self.plan = WorkflowPlan(
            (
                StepSpec(
                    id="correct",
                    tool="correction_workflow",
                    action="apply_change_proposal",
                    args={},
                    goal="Repair the failed test",
                    approval="required",
                ),
            )
        )
        initial = ChangeProposal(
            changes=(
                FileChange(
                    "unused.py",
                    "create",
                    "unused = True\n",
                    None,
                ),
            ),
            tests=(TestSpec("full"),),
            justification="Initial attempt",
            risks=(),
            budget=ProposalBudget(1, 1, 14, 1),
        )
        correction_runtime = CorrectionRuntimeState(
            "Repair",
            runtime_id="correction-runtime",
        )
        correction_runtime.current_proposal = initial
        correction_runtime.proposal_history = (initial,)
        correction_runtime.status = "awaiting_correction"
        engine = CorrectionEngine(
            self.base,
            approval_service=InMemoryCorrectionApprovalService(
                id_factory=lambda: "logical-request"
            ),
            runtime_id_factory=lambda: "unused-runtime",
        )
        engine.transaction = Mock(
            wraps=engine.transaction,
            spec_set=engine.transaction,
        )
        engine.test_runner = Mock(
            wraps=engine.test_runner,
            spec_set=engine.test_runner,
        )
        engine.runtime = correction_runtime
        self.correction_engine = engine
        controller = CorrectionWorkflowController(engine=engine)

        runtime = WorkflowRuntimeState.create(self.plan, goal="Repair")
        runtime.status = "awaiting_correction"
        runtime.awaiting_step_id = "correct"
        runtime.current_step_id = "correct"
        runtime.steps["correct"].status = "awaiting_correction"
        runtime.steps["correct"].correction_controller = controller
        runtime.steps["correct"].correction_runtime = correction_runtime
        self.runtime = runtime
        self.session._workflow_plan = self.plan
        self.session._runtime = runtime
        self.session._state = ProgrammingSessionState.RUNNING

    def test_model_draft_waits_for_exact_operational_approval_then_completes(self):
        before = self.target.read_bytes()
        runtime_identity = self.runtime.runtime_id

        self.session._synchronize_runtime()

        paused = self.session.current_result()
        self.assertEqual(paused.state, ProgrammingSessionState.AWAITING_APPROVAL)
        self.assertEqual(self.target.read_bytes(), before)
        self.assertEqual(len(self.service.contexts), 1)
        self.assertEqual(self.service.contexts[0].runtime_id, runtime_identity)
        pending = self.session.approval_controller.get_pending(
            paused.pending_approval_request_id
        )
        self.assertEqual(pending.important_args["step_id"], "correct")
        self.assertEqual(
            pending.important_args["proposal_id"],
            self.session._pending_model_correction.proposal_id,
        )
        serialized = str(pending.important_args)
        self.assertNotIn("value = 2", serialized)
        self.assertNotIn(str(self.base), serialized)

        completed = self.session.process_operational_command(
            "aprobar",
            paused.pending_approval_request_id,
        )

        self.assertEqual(completed.state, ProgrammingSessionState.COMPLETED)
        self.assertEqual(completed.runtime_status, "completed")
        self.assertEqual(self.runtime.runtime_id, runtime_identity)
        self.assertEqual(self.target.read_text(encoding="utf-8"), "value = 2\n")
        self.assertIsNotNone(completed.report)
        self.assertFalse(completed.automatic_commit_performed)
        self.assertFalse(completed.automatic_push_performed)
        logs = json.loads((self.base / "actions.json").read_text(encoding="utf-8"))
        self.assertNotIn("value = 2", str(logs))
        self.assertNotIn(str(self.base), str(logs))
        with self.assertRaises(ControlledProgrammingSessionError):
            self.session.process_operational_command(
                "aprobar",
                paused.pending_approval_request_id,
            )

    def test_rejection_writes_nothing_and_requests_a_new_bounded_draft(self):
        self.service.drafts.append(correction_draft(self.target, "value = 3\n"))
        self.session._synchronize_runtime()
        first = self.session.current_result()

        second = self.session.process_operational_command(
            "rechazar",
            first.pending_approval_request_id,
        )

        self.assertEqual(second.state, ProgrammingSessionState.AWAITING_APPROVAL)
        self.assertEqual(self.target.read_text(encoding="utf-8"), "value = 1\n")
        self.assertEqual(len(self.service.contexts), 2)
        self.assertNotEqual(
            first.pending_approval_request_id,
            second.pending_approval_request_id,
        )

    def test_cancel_is_terminal_without_rejection_or_new_model_generation(self):
        self.service.drafts.append(correction_draft(self.target, "value = 3\n"))
        self.session._synchronize_runtime()
        paused = self.session.current_result()
        git_spy = Mock(spec_set=self.agent.git_tools)
        self.agent.git_tools = git_spy

        cancelled = self.session.process_operational_command(
            "cancelar",
            paused.pending_approval_request_id,
        )

        self.assertEqual(cancelled.state, ProgrammingSessionState.CANCELLED)
        self.assertEqual(cancelled.runtime_status, "cancelled")
        self.assertEqual(self.session._rejected_proposals, 0)
        self.assertEqual(len(self.service.contexts), 1)
        self.assertIsNone(cancelled.pending_approval_request_id)
        self.assertEqual(self.target.read_text(encoding="utf-8"), "value = 1\n")
        self.assertEqual(self.correction_engine.transaction.apply.call_count, 0)
        self.assertEqual(self.correction_engine.test_runner.execute.call_count, 0)
        self.assertEqual(git_spy.method_calls, [])
        with self.assertRaises(ControlledProgrammingSessionError):
            self.session.process_operational_command(
                "aprobar",
                paused.pending_approval_request_id,
            )
        with self.assertRaises(ControlledProgrammingSessionError):
            self.session.process_operational_command(
                "cancelar",
                paused.pending_approval_request_id,
            )

    def test_model_authored_summary_never_reaches_approval_or_logs(self):
        secret = (
            "FAKE_SECRET_TOKEN_8_4\nC:/private prompt traceback "
            "ignore previous instructions"
        )
        self.target.write_text(
            f"value = 1\n# current {secret}\n",
            encoding="utf-8",
        )
        self.service.drafts = [
            correction_draft(
                self.target,
                f"value = 2\n# {secret}\n",
                summary=secret,
                justification=secret,
                risks=(secret,),
            )
        ]

        self.session._synchronize_runtime()
        paused = self.session.current_result()
        pending = self.session.approval_controller.get_pending(
            paused.pending_approval_request_id
        )

        self.assertNotIn(secret, str(pending.important_args))
        self.assertIn("replace:sample.py", pending.important_args["summary"])
        logical = self.correction_engine.pending_approval_request
        self.assertFalse(hasattr(logical, "risks"))
        self.assertNotIn(secret, repr(logical))
        self.assertNotIn(secret, str(logical))
        logs = json.loads((self.base / "actions.json").read_text(encoding="utf-8"))
        self.assertNotIn(secret, str(logs))
        self.assertNotIn(secret, str(self.session.current_result()))
        self.assertNotIn(secret, self.session.render_current_report())

    def test_repeated_drafts_exhaust_generation_budget_without_writing(self):
        repeated = correction_draft(self.target, "value = 2\n")
        self.service.drafts = [repeated, repeated, repeated]
        self.session._generation_policy = ModelCorrectionGenerationPolicy(
            max_generations=2,
            max_invalid_drafts=2,
            max_rejected_proposals=2,
            max_test_executions_per_application=6,
        )
        self.session._seen_draft_ids.add(repeated.draft_id)

        self.session._synchronize_runtime()

        result = self.session.current_result()
        self.assertEqual(result.state, ProgrammingSessionState.FAILED)
        self.assertEqual(result.runtime_status, "failed")
        self.assertEqual(self.target.read_text(encoding="utf-8"), "value = 1\n")
        self.assertEqual(len(self.service.contexts), 2)

    def test_tampered_operational_summary_fails_closed_without_writing(self):
        self.session._synchronize_runtime()
        paused = self.session.current_result()
        pending = self.session.approval_controller.get_pending(
            paused.pending_approval_request_id
        )
        pending.important_args["proposal_id"] = "tampered"

        with self.assertRaises(ControlledProgrammingSessionError):
            self.session.process_operational_command(
                "aprobar",
                paused.pending_approval_request_id,
            )

        self.assertEqual(self.target.read_text(encoding="utf-8"), "value = 1\n")
        self.assertEqual(
            self.session.current_result().state,
            ProgrammingSessionState.CANCELLED,
        )
        self.assertEqual(
            self.session.current_result().error_code,
            "invalid_correction_approval",
        )

    def test_all_stale_approval_dimensions_terminalize_without_reentry(self):
        def mutate_snapshot(session, runtime, correction):
            binding = session._pending_model_correction
            values = list(binding.runtime_snapshot)
            values[13] += 1
            session._pending_model_correction = replace(
                binding,
                runtime_snapshot=tuple(values),
            )

        def mutate_fingerprint(session, runtime, correction):
            proposal = correction.current_proposal
            change = proposal.changes[0]
            correction.current_proposal = replace(
                proposal,
                changes=(
                    replace(change, expected_sha256="0" * 64),
                ),
            )

        mutations = (
            lambda s, w, c: setattr(c, "total_write_bytes", c.total_write_bytes + 1),
            lambda s, w, c: setattr(c, "modified_files", frozenset({"other.py"})),
            mutate_snapshot,
            lambda s, w, c: setattr(c, "total_changed_lines", c.total_changed_lines + 1),
            lambda s, w, c: setattr(c, "applied_proposal_ids", frozenset({"applied"})),
            lambda s, w, c: setattr(
                c,
                "test_runs",
                (
                    CorrectionTestRun(
                        TestSpec("full"),
                        ToolResult.success(
                            "test_runner",
                            data={
                                "returncode": 0,
                                "tests_run": 1,
                                "passed": 1,
                                "failures": 0,
                                "errors": 0,
                                "skipped": 0,
                                "failed_test_ids": [],
                                "error_test_ids": [],
                            },
                        ),
                    ),
                ),
            ),
            lambda s, w, c: setattr(c, "limits", WorkflowLimits(max_total_change_bytes=1)),
            lambda s, w, c: setattr(c, "correction_iterations", c.correction_iterations + 1),
            lambda s, w, c: setattr(w, "runtime_id", "changed-workflow"),
            lambda s, w, c: setattr(c, "runtime_id", "changed-correction"),
            lambda s, w, c: setattr(w, "awaiting_step_id", "changed-step"),
            lambda s, w, c: setattr(
                s,
                "_pending_model_correction",
                replace(s._pending_model_correction, draft_id="changed-draft"),
            ),
            lambda s, w, c: setattr(
                s,
                "_pending_model_correction",
                replace(s._pending_model_correction, proposal_id="changed-proposal"),
            ),
            mutate_fingerprint,
        )
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                case = ModelCorrectionSessionIntegrationTests(
                    "test_model_draft_waits_for_exact_operational_approval_then_completes"
                )
                case.setUp()
                try:
                    case.session._synchronize_runtime()
                    paused = case.session.current_result()
                    before = case.target.read_bytes()
                    correction = case.runtime.steps["correct"].correction_runtime
                    model_calls = len(case.service.contexts)
                    git_spy = Mock(spec_set=case.agent.git_tools)
                    case.agent.git_tools = git_spy
                    mutate(case.session, case.runtime, correction)

                    with self.assertRaises(ControlledProgrammingSessionError) as caught:
                        case.session.process_operational_command(
                            "aprobar",
                            paused.pending_approval_request_id,
                        )

                    result = case.session.current_result()
                    self.assertEqual(
                        caught.exception.code,
                        "invalid_correction_approval",
                    )
                    self.assertEqual(result.error_code, "invalid_correction_approval")
                    self.assertEqual(result.state, ProgrammingSessionState.CANCELLED)
                    self.assertEqual(case.runtime.status, "cancelled")
                    self.assertEqual(correction.status, "cancelled")
                    self.assertEqual(case.target.read_bytes(), before)
                    self.assertEqual(
                        case.correction_engine.transaction.apply.call_count,
                        0,
                    )
                    self.assertEqual(
                        case.correction_engine.test_runner.execute.call_count,
                        0,
                    )
                    self.assertEqual(len(case.service.contexts), model_calls)
                    self.assertEqual(git_spy.method_calls, [])
                    self.assertIsNone(case.session._pending_model_correction)
                    self.assertIsNone(
                        case.session.approval_controller.get_pending(
                            paused.pending_approval_request_id
                        )
                    )
                    with self.assertRaises(ControlledProgrammingSessionError):
                        case.session.process_operational_command(
                            "aprobar",
                            paused.pending_approval_request_id,
                        )
                finally:
                    case.doCleanups()

    def test_model_service_cannot_be_called_outside_awaiting_correction(self):
        self.session._state = ProgrammingSessionState.RUNNING
        self.runtime.status = "running"
        with self.assertRaises(ControlledProgrammingSessionError):
            self.session._generate_model_correction()
        self.assertEqual(self.service.contexts, [])

    def test_duplicate_json_is_rejected_by_real_parser_through_session_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tests_dir = root / "tests"
            tests_dir.mkdir()
            (tests_dir / "__init__.py").write_text("", encoding="utf-8")
            target = root / "behavior.py"
            initial_content = "def value():\n    return 1\n"
            baseline_content = initial_content + "# controlled baseline\n"
            target.write_text(initial_content, encoding="utf-8")
            (tests_dir / "test_behavior.py").write_text(
                "import unittest\n"
                "from behavior import value\n\n"
                "class BehaviorTests(unittest.TestCase):\n"
                "    def test_value(self):\n"
                "        self.assertEqual(value(), 2)\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(
                ["git", "config", "user.name", "DeveloperAI Test"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.email", "developerai@example.invalid"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "add", "--", "behavior.py", "tests"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "-q", "-m", "test baseline"],
                cwd=root,
                check=True,
            )
            baseline_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                text=True,
                capture_output=True,
            ).stdout.strip()
            workflow = WorkflowPlan(
                (
                    StepSpec(
                        id="correct",
                        tool="correction_workflow",
                        action="apply_change_proposal",
                        args={
                            "changes": [
                                {
                                    "path": "behavior.py",
                                    "operation": "replace",
                                    "new_content": baseline_content,
                                    "expected_sha256": hashlib.sha256(
                                        target.read_bytes()
                                    ).hexdigest(),
                                    "justification": "Establish controlled baseline",
                                }
                            ],
                            "tests": [
                                {
                                    "scope": "focused",
                                    "targets": [
                                        "tests.test_behavior.BehaviorTests.test_value"
                                    ],
                                },
                                {"scope": "full", "targets": []},
                            ],
                            "justification": "Run the initial failing implementation",
                            "risks": [],
                            "budget": {
                                "modified_files": 1,
                                "new_files": 0,
                                "write_bytes": len(
                                    baseline_content.encode("utf-8")
                                ),
                                "changed_lines": 1,
                            },
                        },
                        goal="Repair behavior",
                        approval="required",
                    ),
                )
            )
            duplicate = (
                '{"schema_version":"1","schema_version":"1",'
                '"summary":"duplicate","changes":[],"risks":[]}'
            )
            transport = JsonCorrectionTransport({}, raw_content=duplicate)
            client = LocalModelClient(
                LocalModelConfig(
                    provider="lm_studio",
                    base_url="http://localhost:1234/v1",
                    model="qwen",
                ),
                transport=transport,
            )
            planning = TrustedPlanningService(workflow)
            review = TrustedCorrectionPlanReview(workflow)
            agent = DeveloperAgent(
                None,
                base_dir=root,
                action_log_file=root / "actions.json",
                model_planning_service=planning,
                model_plan_review_controller=review,
                model_correction_service=ModelCorrectionService(client),
                model_correction_adapter=ModelCorrectionAdapter(root),
            )
            review.agent = agent
            session = agent.get_programming_session()
            git_spy = Mock(spec_set=agent.git_tools)
            agent.git_tools = git_spy

            pending_plan = session.submit("Repair behavior")
            first_pause = session.approve_plan(pending_plan.plan_id)
            runtime = session._runtime
            self.assertEqual(
                first_pause.state,
                ProgrammingSessionState.AWAITING_APPROVAL,
                (
                    first_pause.runtime_status,
                    first_pause.error_code,
                    runtime.steps["correct"].status,
                    runtime.steps["correct"].reason,
                ),
            )

            result = session.process_operational_command(
                "aprobar",
                first_pause.pending_approval_request_id,
            )

            correction = runtime.steps["correct"].correction_runtime
            self.assertEqual(result.state, ProgrammingSessionState.FAILED)
            self.assertEqual(result.runtime_status, "failed")
            self.assertEqual(len(transport.requests), 2)
            self.assertEqual(target.read_text(encoding="utf-8"), baseline_content)
            self.assertEqual(len(correction.applied_proposal_ids), 1)
            self.assertEqual(len(correction.test_runs), 1)
            self.assertEqual(git_spy.method_calls, [])
            self.assertEqual(
                subprocess.run(
                    ["git", "diff", "--cached", "--quiet"],
                    cwd=root,
                ).returncode,
                0,
            )
            self.assertEqual(
                subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=root,
                    check=True,
                    text=True,
                    capture_output=True,
                ).stdout.strip(),
                baseline_commit,
            )
            self.assertEqual(
                subprocess.run(
                    ["git", "remote"],
                    cwd=root,
                    check=True,
                    text=True,
                    capture_output=True,
                ).stdout,
                "",
            )

    def test_stale_precondition_after_approval_request_never_overwrites(self):
        self.session._synchronize_runtime()
        paused = self.session.current_result()
        pending = self.session.approval_controller.get_pending(
            paused.pending_approval_request_id
        )
        callback = Mock(wraps=pending.execute)
        pending.execute = callback
        resume = Mock(wraps=self.correction_engine.resume)
        self.correction_engine.resume = resume
        correction = self.runtime.steps["correct"].correction_runtime
        self.target.write_text("external = True\n", encoding="utf-8")

        with self.assertRaises(ControlledProgrammingSessionError) as caught:
            self.session.process_operational_command(
                "aprobar",
                paused.pending_approval_request_id,
            )

        result = self.session.current_result()
        self.assertEqual(caught.exception.code, "invalid_correction_approval")
        self.assertEqual(result.state, ProgrammingSessionState.CANCELLED)
        self.assertEqual(result.runtime_status, "cancelled")
        self.assertEqual(correction.status, "cancelled")
        self.assertEqual(result.error_code, "invalid_correction_approval")
        self.assertEqual(
            self.target.read_text(encoding="utf-8"),
            "external = True\n",
        )
        self.assertEqual(callback.call_count, 0)
        self.assertEqual(resume.call_count, 0)
        self.assertEqual(self.correction_engine.transaction.apply.call_count, 0)
        self.assertEqual(self.correction_engine.test_runner.execute.call_count, 0)
        self.assertIsNone(self.session._pending_model_correction)
        self.assertIsNone(
            self.session.approval_controller.get_pending(
                paused.pending_approval_request_id
            )
        )
        for action in ("aprobar", "rechazar", "cancelar"):
            with self.subTest(action=action):
                with self.assertRaises(ControlledProgrammingSessionError):
                    self.session.process_operational_command(
                        action,
                        paused.pending_approval_request_id,
                    )


class PublicModelCorrectionFlowTests(unittest.TestCase):
    def test_public_session_uses_real_json_service_transaction_and_two_test_scopes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tests_dir = root / "tests"
            tests_dir.mkdir()
            (tests_dir / "__init__.py").write_text("", encoding="utf-8")
            target = root / "behavior.py"
            target.write_text("def value():\n    return 1\n", encoding="utf-8")
            (tests_dir / "test_behavior.py").write_text(
                "import unittest\n"
                "from behavior import value\n\n"
                "class BehaviorTests(unittest.TestCase):\n"
                "    def test_value(self):\n"
                "        self.assertEqual(value(), 2)\n",
                encoding="utf-8",
            )
            subprocess.run(
                ["git", "init", "-q"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "DeveloperAI Test"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.email", "developerai@example.invalid"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "add", "--", "behavior.py", "tests"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "-q", "-m", "test baseline"],
                cwd=root,
                check=True,
            )
            baseline_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                text=True,
                capture_output=True,
            ).stdout.strip()
            initial_content = target.read_text(encoding="utf-8")
            initial_hash = hashlib.sha256(target.read_bytes()).hexdigest()
            baseline_content = initial_content + "# controlled baseline\n"
            baseline_hash = hashlib.sha256(
                baseline_content.encode("utf-8")
            ).hexdigest()
            initial_args = {
                "changes": [
                    {
                        "path": "behavior.py",
                        "operation": "replace",
                        "new_content": baseline_content,
                        "expected_sha256": initial_hash,
                        "justification": "Establish controlled baseline",
                    }
                ],
                "tests": [
                    {
                        "scope": "focused",
                        "targets": [
                            "tests.test_behavior.BehaviorTests.test_value"
                        ],
                    },
                    {"scope": "full", "targets": []},
                ],
                "justification": "Run the initial failing implementation",
                "risks": [],
                "budget": {
                    "modified_files": 1,
                    "new_files": 0,
                    "write_bytes": len(baseline_content.encode("utf-8")),
                    "changed_lines": 1,
                },
            }
            workflow = WorkflowPlan(
                (
                    StepSpec(
                        id="correct",
                        tool="correction_workflow",
                        action="apply_change_proposal",
                        args=initial_args,
                        goal="Repair behavior",
                        approval="required",
                    ),
                )
            )
            corrected_content = "def value():\n    return 2\n"
            transport = JsonCorrectionTransport(
                {
                    "schema_version": "1",
                    "summary": "UNTRUSTED SECRET PROMPT",
                    "changes": [
                        {
                            "operation": "replace",
                            "path": "behavior.py",
                            "new_content": corrected_content,
                            "expected_sha256": baseline_hash,
                            "justification": "Repair test",
                        }
                    ],
                    "risks": ["UNTRUSTED RISK"],
                }
            )
            client = LocalModelClient(
                LocalModelConfig(
                    provider="lm_studio",
                    base_url="http://localhost:1234/v1",
                    model="qwen",
                ),
                transport=transport,
            )
            planning = TrustedPlanningService(workflow)
            review = TrustedCorrectionPlanReview(workflow)
            agent = DeveloperAgent(
                None,
                base_dir=root,
                action_log_file=root / "actions.json",
                model_planning_service=planning,
                model_plan_review_controller=review,
                model_correction_service=ModelCorrectionService(client),
                model_correction_adapter=ModelCorrectionAdapter(root),
            )
            review.agent = agent
            session = agent.get_programming_session()
            git_spy = Mock(spec_set=agent.git_tools)
            agent.git_tools = git_spy

            pending_plan = session.submit("Repair behavior")
            self.assertEqual(pending_plan.state, ProgrammingSessionState.PENDING_PLAN)
            first_pause = session.approve_plan(pending_plan.plan_id)
            runtime_identity = agent.execution_engine.last_workflow_runtime
            self.assertEqual(first_pause.state, ProgrammingSessionState.AWAITING_APPROVAL)
            self.assertEqual(target.read_text(encoding="utf-8"), initial_content)
            self.assertEqual(len(transport.requests), 0)

            correction_pause = session.process_operational_command(
                "aprobar",
                first_pause.pending_approval_request_id,
            )
            self.assertEqual(
                correction_pause.state,
                ProgrammingSessionState.AWAITING_APPROVAL,
                (
                    correction_pause.runtime_status,
                    len(transport.requests),
                    correction_pause.report,
                ),
            )
            self.assertEqual(target.read_text(encoding="utf-8"), baseline_content)
            self.assertEqual(len(transport.requests), 1)

            completed = session.process_operational_command(
                "aprobar",
                correction_pause.pending_approval_request_id,
            )

            self.assertEqual(completed.state, ProgrammingSessionState.COMPLETED)
            self.assertEqual(completed.runtime_status, "completed")
            self.assertIs(
                agent.execution_engine.last_workflow_runtime,
                runtime_identity,
            )
            self.assertEqual(target.read_text(encoding="utf-8"), corrected_content)
            self.assertIsNotNone(completed.report)
            self.assertEqual(
                tuple(run.scope for run in completed.report.tests[-2:]),
                ("focused", "full"),
            )
            expected_diff = subprocess.run(
                [
                    "git",
                    "diff",
                    "HEAD",
                    "--no-ext-diff",
                    "--no-color",
                    "--unified=3",
                    "--",
                    "behavior.py",
                ],
                cwd=root,
                check=True,
                text=True,
                encoding="utf-8",
                capture_output=True,
            ).stdout
            self.assertTrue(completed.report.diff.available)
            self.assertEqual(completed.report.diff.text, expected_diff)
            self.assertEqual(completed.report.diff.files[0].path, "behavior.py")
            self.assertEqual(
                subprocess.run(
                    ["git", "diff", "--cached", "--quiet"],
                    cwd=root,
                ).returncode,
                0,
            )
            self.assertEqual(
                subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=root,
                    check=True,
                    text=True,
                    capture_output=True,
                ).stdout.strip(),
                baseline_commit,
            )
            self.assertEqual(
                subprocess.run(
                    ["git", "remote"],
                    cwd=root,
                    check=True,
                    text=True,
                    capture_output=True,
                ).stdout,
                "",
            )
            self.assertEqual(git_spy.method_calls, [])
            serialized = (root / "actions.json").read_text(encoding="utf-8")
            self.assertNotIn("UNTRUSTED SECRET PROMPT", serialized)
            self.assertNotIn("UNTRUSTED RISK", serialized)


if __name__ == "__main__":
    unittest.main()
