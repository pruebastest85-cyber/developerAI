import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType
from unittest.mock import Mock

from brain.agent import DeveloperAgent
from brain.controlled_programming_session import ProgrammingSessionState
from brain.isolated_environment import IsolatedRepository
from brain.local_model_client import LocalModelClient
from brain.local_model_config import LocalModelConfig
from brain.model_errors import ModelConnectionError, ModelTimeoutError
from brain.model_correction import ModelCorrectionAdapter, ModelCorrectionService
from brain.model_planning_service import ModelPlanningService
from brain.model_transport import TransportResponse
from brain.programming_operator import ProgrammingOperator


class FailingTransport:
    def __init__(self, error):
        self.error = error
        self.requests = []

    def send(self, request):
        self.requests.append(request)
        raise self.error


class DeterministicModelTransport:
    def __init__(self, *payloads):
        self.payloads = list(payloads)
        self.requests = []

    def send(self, request):
        self.requests.append(request)
        payload = self.payloads.pop(0)
        content = json.dumps(payload, ensure_ascii=False)
        body = json.dumps(
            {
                "id": f"response-{len(self.requests)}",
                "model": "qwen",
                "choices": [
                    {
                        "message": {"content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }
        ).encode("utf-8")
        return TransportResponse(
            200,
            (("Content-Type", "application/json"),),
            body,
        )


class ProgrammingOperatorTests(unittest.TestCase):
    def test_idle_view_is_structured_and_uses_isolated_agent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "app.py").write_text("value = 1\n", encoding="utf-8")
            isolation = IsolatedRepository(source, temp_parent=root)
            snapshot = isolation.create()
            agent = DeveloperAgent(
                None,
                base_dir=snapshot.repository,
                action_log_file=snapshot.runtime_directory / "actions.json",
            )
            operator = ProgrammingOperator(agent, isolation)
            try:
                view = operator.current()
                self.assertEqual(view.state, "idle")
                self.assertEqual(view.plan, ())
                self.assertIsNone(view.approval)
                self.assertEqual(view.tests, ())
                self.assertEqual(view.diff, "")
                self.assertEqual(agent.base_dir, snapshot.repository)
                self.assertNotEqual(agent.base_dir, source)
            finally:
                operator.close()

    def test_failed_planning_is_sanitized_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            target = source / "app.py"
            target.write_text("value = 1\n", encoding="utf-8")
            isolation = IsolatedRepository(source, temp_parent=root)
            snapshot = isolation.create()
            agent = DeveloperAgent(
                None,
                base_dir=snapshot.repository,
                action_log_file=snapshot.runtime_directory / "actions.json",
            )
            operator = ProgrammingOperator(agent, isolation)
            try:
                view = operator.execute("programar: repair")
                self.assertEqual(view.state, "failed")
                self.assertEqual(view.error_code, "planning_failed")
                self.assertIn("No fue posible", view.presentation)
                self.assertEqual(
                    (snapshot.repository / "app.py").read_text(encoding="utf-8"),
                    "value = 1\n",
                )
                self.assertEqual(target.read_text(encoding="utf-8"), "value = 1\n")
            finally:
                operator.close()

    def test_view_contract_is_immutable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "app.py").write_text("value = 1\n", encoding="utf-8")
            isolation = IsolatedRepository(source, temp_parent=root)
            snapshot = isolation.create()
            operator = ProgrammingOperator(
                DeveloperAgent(None, base_dir=snapshot.repository),
                isolation,
            )
            try:
                view = operator.current()
                self.assertIsInstance(view.__dataclass_fields__, dict)
                self.assertIsNone(view.approval)
                sample = MappingProxyType({"safe": "value"})
                with self.assertRaises(TypeError):
                    sample["safe"] = "changed"
            finally:
                operator.close()

    def test_unavailable_or_timed_out_model_never_changes_isolated_git(self):
        for error in (ModelConnectionError(), ModelTimeoutError()):
            with self.subTest(error=type(error).__name__):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    source = root / "source"
                    source.mkdir()
                    (source / "app.py").write_text("value = 1\n", encoding="utf-8")
                    transport = FailingTransport(error)
                    operator = ProgrammingOperator.from_config(
                        source,
                        LocalModelConfig(
                            provider="lm_studio",
                            base_url="http://localhost:1234/v1",
                            model="qwen",
                        ),
                        transport=transport,
                        temp_parent=root,
                    )
                    try:
                        snapshot = operator.isolated_snapshot
                        view = operator.execute("programar: repair")
                        self.assertEqual(view.state, "failed")
                        self.assertIn("No fue posible", view.presentation)
                        self.assertEqual(view.error_code, error.code)
                        self.assertEqual(len(transport.requests), 1)
                        self.assertEqual(
                            (snapshot.repository / "app.py").read_text(
                                encoding="utf-8"
                            ),
                            "value = 1\n",
                        )
                        self.assertEqual(
                            subprocess.run(
                                ["git", "status", "--porcelain"],
                                cwd=snapshot.repository,
                                text=True,
                                capture_output=True,
                                check=True,
                            ).stdout,
                            "",
                        )
                    finally:
                        operator.close()

    def test_deterministic_isolated_cycle_uses_public_interface_and_real_git(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            tests_directory = source / "tests"
            tests_directory.mkdir(parents=True)
            (tests_directory / "__init__.py").write_text("", encoding="utf-8")
            source_target = source / "behavior.py"
            source_target.write_text(
                "def value():\n    return 1\n",
                encoding="utf-8",
            )
            (tests_directory / "test_behavior.py").write_text(
                "import unittest\n"
                "from behavior import value\n\n"
                "class BehaviorTests(unittest.TestCase):\n"
                "    def test_value(self):\n"
                "        self.assertEqual(value(), 2)\n",
                encoding="utf-8",
            )
            isolation = IsolatedRepository(source, temp_parent=root)
            snapshot = isolation.create()
            target = snapshot.repository / "behavior.py"
            initial = target.read_text(encoding="utf-8")
            corrected = "def value():\n    return 2\n"
            plan_payload = {
                "schema_version": "1",
                "goal": "Repair behavior",
                "completed": False,
                "steps": [
                    {
                        "id": "read_behavior",
                        "tool": "code_reader",
                        "action": "read_file",
                        "args": {"path": "behavior.py", "max_lines": 100},
                        "goal": "Inspect behavior",
                        "depends_on": [],
                        "justification": "Read the authorized source",
                    },
                    {
                        "id": "test_behavior",
                        "tool": "test_runner",
                        "action": "run_tests",
                        "args": {
                            "test_id": (
                                "tests.test_behavior.BehaviorTests.test_value"
                            )
                        },
                        "goal": "Verify behavior",
                        "depends_on": ["read_behavior"],
                        "justification": "Run one focused test",
                    },
                ],
                "message": "Inspect and verify before correction",
            }
            correction_payload = (
                {
                    "schema_version": "1",
                    "summary": "Repair behavior",
                    "changes": [
                        {
                            "operation": "replace",
                            "path": "behavior.py",
                            "new_content": corrected,
                            "expected_sha256": hashlib.sha256(
                                target.read_bytes()
                            ).hexdigest(),
                            "justification": "Make the tests pass",
                        }
                    ],
                    "risks": [],
                }
            )
            transport = DeterministicModelTransport(
                plan_payload,
                correction_payload,
            )
            client = LocalModelClient(
                LocalModelConfig(
                    provider="lm_studio",
                    base_url="http://localhost:1234/v1",
                    model="qwen",
                ),
                transport=transport,
            )
            planning = ModelPlanningService(client)
            agent = DeveloperAgent(
                None,
                base_dir=snapshot.repository,
                action_log_file=snapshot.runtime_directory / "actions.json",
                model_planning_service=planning,
                model_correction_service=ModelCorrectionService(client),
                model_correction_adapter=ModelCorrectionAdapter(snapshot.repository),
            )
            git_spy = Mock(spec_set=agent.git_tools)
            agent.git_tools = git_spy
            operator = ProgrammingOperator(agent, isolation)
            try:
                pending = operator.execute("programar: repair behavior")
                self.assertEqual(pending.state, "pending_plan")
                self.assertEqual(target.read_text(encoding="utf-8"), initial)

                initial_pause = operator.execute(
                    f"aprobar-plan {pending.plan_id}"
                )
                self.assertEqual(
                    initial_pause.state,
                    "awaiting_approval",
                    initial_pause.presentation,
                )
                runtime_id = initial_pause.workflow_runtime_id
                self.assertIsNotNone(runtime_id)
                self.assertEqual(target.read_text(encoding="utf-8"), initial)
                self.assertEqual(len(transport.requests), 1)

                correction_pause = operator.execute(
                    f"aprobar {initial_pause.approval_request_id}"
                )
                self.assertEqual(
                    correction_pause.state,
                    "awaiting_approval",
                    correction_pause.presentation,
                )
                self.assertEqual(
                    correction_pause.workflow_runtime_id,
                    runtime_id,
                )
                with self.assertRaises(TypeError):
                    correction_pause.approval["arguments"]["files"][0] = (
                        "other.py",
                        "replace",
                    )
                self.assertEqual(target.read_text(encoding="utf-8"), initial)
                self.assertEqual(len(transport.requests), 2)
                self.assertEqual(correction_pause.correction_applications, 0)

                completed = operator.execute(
                    f"aprobar {correction_pause.approval_request_id}"
                )
                self.assertEqual(completed.state, "completed")
                self.assertEqual(completed.workflow_runtime_id, runtime_id)
                self.assertEqual(completed.correction_applications, 1)
                self.assertEqual(target.read_text(encoding="utf-8"), corrected)
                self.assertEqual(
                    [test["scope"] for test in completed.tests[-3:]],
                    ["focused", "focused", "full"],
                )
                self.assertEqual(
                    [test["status"] for test in completed.tests[-3:]],
                    ["failed", "ok", "ok"],
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
                    cwd=snapshot.repository,
                    text=True,
                    encoding="utf-8",
                    capture_output=True,
                    check=True,
                ).stdout
                self.assertEqual(completed.diff, expected_diff)
                self.assertEqual(
                    subprocess.run(
                        ["git", "diff", "--cached", "--quiet"],
                        cwd=snapshot.repository,
                    ).returncode,
                    0,
                )
                self.assertEqual(
                    subprocess.run(
                        ["git", "rev-parse", "HEAD"],
                        cwd=snapshot.repository,
                        text=True,
                        capture_output=True,
                        check=True,
                    ).stdout.strip(),
                    snapshot.baseline_commit,
                )
                self.assertEqual(
                    subprocess.run(
                        ["git", "rev-list", "--count", "HEAD"],
                        cwd=snapshot.repository,
                        text=True,
                        capture_output=True,
                        check=True,
                    ).stdout.strip(),
                    "1",
                )
                self.assertEqual(
                    subprocess.run(
                        ["git", "remote"],
                        cwd=snapshot.repository,
                        text=True,
                        capture_output=True,
                        check=True,
                    ).stdout,
                    "",
                )
                self.assertEqual(git_spy.method_calls, [])
                self.assertEqual(
                    source_target.read_text(encoding="utf-8"),
                    "def value():\n    return 1\n",
                )
            finally:
                operator.close()


if __name__ == "__main__":
    unittest.main()
