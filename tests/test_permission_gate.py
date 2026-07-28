import hashlib
import tempfile
import unittest
from pathlib import Path

from brain.approval_controller import ConversationalController
from brain.agent import DeveloperAgent


class PermissionGateTests(unittest.TestCase):
    def _sha256(self, file_path):
        return hashlib.sha256(file_path.read_bytes()).hexdigest()

    def _file_creator_args(self, relative_path, content):
        content_bytes = content.encode("utf-8")
        return {
            "path": relative_path,
            "content_sha256": hashlib.sha256(content_bytes).hexdigest(),
            "content_bytes": len(content_bytes),
        }

    def _build_agent(self, settings_content=None):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        temp_dir = Path(tmpdir.name)
        config_dir = temp_dir / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        if settings_content is not None:
            (config_dir / "settings.json").write_text(settings_content, encoding="utf-8")

        return DeveloperAgent(
            client=None,
            memory_file=temp_dir / "memory.json",
            prompt_dir="prompts",
            base_dir=temp_dir,
            action_log_file=temp_dir / "agent_actions.json",
        )

    def test_unknown_tool_is_rejected_by_permission_gate(self):
        agent = self._build_agent(settings_content='{"medium_risk_requires_confirmation": false}')

        with self.assertRaises(PermissionError):
            agent.execute_tool("not_registered_tool", lambda: "should not run", action_name="run")

    def test_create_operation_approval_request_returns_request_not_token(self):
        agent = self._build_agent(settings_content='{"medium_risk_requires_confirmation": false}')

        request = agent.create_operation_approval_request("patch_applier", "apply_patch", {"path": "main.py"})

        self.assertIn("request_id", request)
        self.assertNotIn("approval_token", request)

    def test_high_cannot_execute_using_only_request(self):
        agent = self._build_agent(settings_content='{"medium_risk_requires_confirmation": false}')
        request = agent.create_operation_approval_request("patch_applier", "apply_patch", {"path": "main.py"})

        self.assertIsNotNone(request)
        with self.assertRaises(PermissionError):
            agent.execute_tool(
                "patch_applier",
                lambda: "executed",
                action_name="apply_patch",
                important_args={"path": "main.py"},
            )

    def test_request_id_is_not_usable_as_approval_token(self):
        agent = self._build_agent(settings_content='{"medium_risk_requires_confirmation": false}')
        request = agent.create_operation_approval_request("patch_applier", "apply_patch", {"path": "main.py"})

        with self.assertRaises(PermissionError):
            agent.execute_tool(
                "patch_applier",
                lambda: "executed",
                action_name="apply_patch",
                important_args={"path": "main.py"},
                approval_token=request["request_id"],
            )

    def test_granted_token_allows_exact_operation_once(self):
        agent = self._build_agent(settings_content='{"medium_risk_requires_confirmation": false}')
        request = agent.create_operation_approval_request("patch_applier", "apply_patch", {"path": "main.py"})
        token = agent.permission_manager.grant_approval(request["request_id"])

        first = agent.execute_tool(
            "patch_applier",
            lambda: "executed",
            action_name="apply_patch",
            important_args={"path": "main.py"},
            approval_token=token,
        )
        self.assertEqual(first, "executed")

        with self.assertRaises(PermissionError):
            agent.execute_tool(
                "patch_applier",
                lambda: "executed-again",
                action_name="apply_patch",
                important_args={"path": "main.py"},
                approval_token=token,
            )

    def test_token_wrong_args_not_consumed_then_correct_works_once(self):
        agent = self._build_agent(settings_content='{"medium_risk_requires_confirmation": false}')
        request = agent.create_operation_approval_request("patch_applier", "apply_patch", {"path": "main.py"})
        token = agent.permission_manager.grant_approval(request["request_id"])

        with self.assertRaises(PermissionError):
            agent.execute_tool(
                "patch_applier",
                lambda: "should-not-run",
                action_name="apply_patch",
                important_args={"path": "other.py"},
                approval_token=token,
            )

        self.assertEqual(
            agent.execute_tool(
                "patch_applier",
                lambda: "executed",
                action_name="apply_patch",
                important_args={"path": "main.py"},
                approval_token=token,
            ),
            "executed",
        )

        with self.assertRaises(PermissionError):
            agent.execute_tool(
                "patch_applier",
                lambda: "executed-again",
                action_name="apply_patch",
                important_args={"path": "main.py"},
                approval_token=token,
            )

    def test_token_wrong_action_not_consumed_then_correct_works_once(self):
        agent = self._build_agent(settings_content='{"medium_risk_requires_confirmation": false}')
        request = agent.create_operation_approval_request("patch_applier", "apply_patch", {"path": "main.py"})
        token = agent.permission_manager.grant_approval(request["request_id"])

        with self.assertRaises(PermissionError):
            agent.execute_tool(
                "patch_applier",
                lambda: "should-not-run",
                action_name="different_action",
                important_args={"path": "main.py"},
                approval_token=token,
            )

        self.assertEqual(
            agent.execute_tool(
                "patch_applier",
                lambda: "executed",
                action_name="apply_patch",
                important_args={"path": "main.py"},
                approval_token=token,
            ),
            "executed",
        )

    def test_grant_unknown_request_returns_none(self):
        agent = self._build_agent(settings_content='{"medium_risk_requires_confirmation": false}')

        self.assertIsNone(agent.permission_manager.grant_approval("missing-request-id"))

    def test_request_only_granted_once(self):
        agent = self._build_agent(settings_content='{"medium_risk_requires_confirmation": false}')
        request = agent.create_operation_approval_request("patch_applier", "apply_patch", {"path": "main.py"})

        first = agent.permission_manager.grant_approval(request["request_id"])
        second = agent.permission_manager.grant_approval(request["request_id"])

        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_medium_true_cannot_autoapprove(self):
        agent = self._build_agent(settings_content='{"medium_risk_requires_confirmation": true}')
        request = agent.create_operation_approval_request("test_runner", "run_tests_report", {"scope": "default"})

        self.assertIsNotNone(request)
        with self.assertRaises(PermissionError):
            agent.execute_tool("test_runner", lambda: "ran", action_name="run_tests_report", important_args={"scope": "default"})

    def test_medium_false_does_not_require_request_or_token(self):
        agent = self._build_agent(settings_content='{"medium_risk_requires_confirmation": false}')
        result = agent.execute_tool("test_runner", lambda: "ran", action_name="run_tests_report")
        self.assertEqual(result, "ran")

    def test_low_operations_continue_without_token(self):
        agent = self._build_agent(settings_content='{"medium_risk_requires_confirmation": true}')

        self.assertEqual(
            agent.execute_tool("git_tools", lambda: "status-ran", action_name="status", important_args={"command": "git status --short"}),
            "status-ran",
        )
        self.assertEqual(
            agent.execute_tool("git_tools", lambda: "diff-ran", action_name="diff", important_args={"command": "git diff"}),
            "diff-ran",
        )

    def test_unknown_tool_and_low_ops_cannot_create_requests(self):
        agent = self._build_agent(settings_content='{"medium_risk_requires_confirmation": true}')

        self.assertIsNone(agent.create_operation_approval_request("does_not_exist", "run", {}))
        self.assertIsNone(agent.create_operation_approval_request("git_tools", "status", {"command": "git status --short"}))

    def test_non_serializable_args_cannot_create_request(self):
        agent = self._build_agent(settings_content='{"medium_risk_requires_confirmation": true}')

        self.assertIsNone(agent.create_operation_approval_request("patch_applier", "apply_patch", {"bad": {1, 2}}))

    def test_model_routes_do_not_call_grant_approval(self):
        repo_root = Path(__file__).resolve().parents[1]
        files = [
            repo_root / "brain" / "planner.py",
            repo_root / "brain" / "tool_router.py",
            repo_root / "brain" / "execution_engine.py",
            repo_root / "brain" / "agent.py",
        ]

        for file_path in files:
            text = file_path.read_text(encoding="utf-8")
            self.assertNotIn("grant_approval(", text)

    def test_router_path_is_rejected_when_permission_denied(self):
        agent = self._build_agent(settings_content='{"medium_risk_requires_confirmation": true}')
        controller = ConversationalController(agent)
        result = controller.process_message("prueba")

        self.assertIn("Se requiere aprobación", result)

    def test_no_user_confirmation_bypass_supported(self):
        agent = self._build_agent(settings_content='{"medium_risk_requires_confirmation": false}')

        with self.assertRaises(TypeError):
            agent.permission_manager.can_execute("patch_applier", action_name="apply_patch", user_confirmation=True)

    def test_architectural_limit_direct_instances_remain_invokable(self):
        agent = self._build_agent(settings_content='{"medium_risk_requires_confirmation": false}')

        # Architectural limitation:
        # Python callers holding the DeveloperAgent instance can still access raw tool
        # objects directly. The permission gate protects orchestrated/model-driven
        # execution paths, not trusted Python code with direct object access.
        direct_routes = [
            "git_tools.status",
            "git_tools.checkpoint",
            "git_tools.rollback",
            "patch_applier.apply_patch",
        ]
        self.assertGreaterEqual(len(direct_routes), 4)
        self.assertTrue(callable(agent.git_tools.status))
        self.assertTrue(callable(agent.git_tools.checkpoint))
        self.assertTrue(callable(agent.git_tools.rollback))
        self.assertTrue(callable(agent.patch_applier.apply_patch))

    def test_injected_paths_avoid_writing_real_persistent_files(self):
        repo_root = Path(__file__).resolve().parents[1]
        real_memory = repo_root / "memory" / "memory.json"
        real_logs = repo_root / "logs" / "agent_actions.json"
        memory_before = self._sha256(real_memory)
        logs_before = self._sha256(real_logs)

        agent = self._build_agent(settings_content='{"medium_risk_requires_confirmation": false}')
        self.assertIn("Lo recordaré", agent.handle_memory("Recuerda que test usa rutas temporales"))
        agent.action_logger.log("test", params={"x": 1}, result="ok")

        self.assertEqual(memory_before, self._sha256(real_memory))
        self.assertEqual(logs_before, self._sha256(real_logs))

    def test_file_creator_requires_approval_and_does_not_create_without_it(self):
        agent = self._build_agent(settings_content='{"medium_risk_requires_confirmation": false}')
        (agent.base_dir / "notas").mkdir(parents=True, exist_ok=True)
        target = agent.base_dir / "notas" / "hola.txt"
        args = self._file_creator_args("notas/hola.txt", "Hola mundo")

        request = agent.create_operation_approval_request("file_creator", "create_file", args)

        self.assertIsNotNone(request)
        with self.assertRaises(PermissionError):
            agent.execute_tool(
                "file_creator",
                lambda: agent.file_creator.create_file("notas/hola.txt", "Hola mundo"),
                action_name="create_file",
                important_args=args,
            )
        self.assertFalse(target.exists())

    def test_file_creator_exact_approval_creates_once(self):
        agent = self._build_agent(settings_content='{"medium_risk_requires_confirmation": false}')
        (agent.base_dir / "notas").mkdir(parents=True, exist_ok=True)
        target = agent.base_dir / "notas" / "hola.txt"
        content = "Hola mundo"
        args = self._file_creator_args("notas/hola.txt", content)
        request = agent.create_operation_approval_request("file_creator", "create_file", args)
        token = agent.permission_manager.grant_approval(request["request_id"])

        result = agent.execute_tool(
            "file_creator",
            lambda: agent.file_creator.create_file("notas/hola.txt", content),
            action_name="create_file",
            important_args=args,
            approval_token=token,
        )

        self.assertEqual(result["archivo"], "notas/hola.txt")
        self.assertEqual(target.read_text(encoding="utf-8"), content)

        with self.assertRaises(PermissionError):
            agent.execute_tool(
                "file_creator",
                lambda: agent.file_creator.create_file("notas/hola.txt", content),
                action_name="create_file",
                important_args=args,
                approval_token=token,
            )


if __name__ == "__main__":
    unittest.main()
