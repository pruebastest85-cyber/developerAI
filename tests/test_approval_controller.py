import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from brain.agent import DeveloperAgent
from brain.approval_controller import (
    ApprovalController,
    ApprovalRequired,
    ApprovalRequiredError,
    ApprovalResult,
    ConversationalController,
    parse_approval_command,
    sanitize_important_args,
)


class ApprovalControllerTests(unittest.TestCase):
    def _sha256(self, file_path):
        return hashlib.sha256(file_path.read_bytes()).hexdigest()

    def _build_agent(self, settings_content='{"medium_risk_requires_confirmation": true}'):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        temp_dir = Path(tmpdir.name)
        config_dir = temp_dir / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "settings.json").write_text(settings_content, encoding="utf-8")
        return DeveloperAgent(
            client=None,
            memory_file=temp_dir / "memory.json",
            prompt_dir="prompts",
            base_dir=temp_dir,
            action_log_file=temp_dir / "agent_actions.json",
        )

    def _blocked_high_error(self, agent, execute=None, args=None):
        action = execute or (lambda: "ok")
        payload = args or {"path": "main.py"}
        with self.assertRaises(ApprovalRequiredError) as ctx:
            agent.execute_tool(
                "patch_applier",
                action,
                action_name="apply_patch",
                important_args=payload,
            )
        return ctx.exception

    def _make_pending(self, agent, execute=None, args=None):
        controller = ApprovalController(agent)
        err = self._blocked_high_error(agent, execute=execute, args=args)
        pending = controller.request_operation(
            tool_name=err.tool_name,
            action_name=err.action_name,
            important_args=err.important_args,
            execute=err.execute,
            description="modificar archivo de prueba",
        )
        return controller, pending

    def _seed_pending_conversation(self, conversation, execute=None, args=None):
        err = self._blocked_high_error(conversation.agent, execute=execute, args=args)
        pending = conversation.approval_controller.request_operation(
            tool_name=err.tool_name,
            action_name=err.action_name,
            important_args=err.important_args,
            execute=err.execute,
            description="op",
        )
        return pending

    def test_request_operation_creates_pending_operation(self):
        agent = self._build_agent()
        controller, pending = self._make_pending(agent)

        self.assertIsInstance(pending, ApprovalRequired)
        self.assertIsNotNone(controller.get_pending(pending.request_id))

    def test_request_operation_does_not_execute_action(self):
        agent = self._build_agent()
        calls = []

        def action():
            calls.append("run")
            return "done"

        controller, _ = self._make_pending(agent, execute=action)
        self.assertEqual(calls, [])
        self.assertEqual(len(controller.get_pending()), 1)

    def test_pending_result_contains_request_id(self):
        agent = self._build_agent()
        _, pending = self._make_pending(agent)
        self.assertTrue(pending.request_id)

    def test_pending_result_does_not_expose_approval_token(self):
        agent = self._build_agent()
        _, pending = self._make_pending(agent)

        self.assertNotIn("approval_token", pending.message)
        self.assertNotIn("approval_token", str(pending.important_args))

    def test_approve_with_correct_request_executes_once(self):
        agent = self._build_agent()
        calls = []

        def action():
            calls.append("run")
            return "done"

        controller, pending = self._make_pending(agent, execute=action)
        result = controller.approve(pending.request_id)

        self.assertEqual(result.status, "approved")
        self.assertEqual(result.result, "done")
        self.assertEqual(calls, ["run"])

    def test_approved_execution_goes_through_execute_tool(self):
        agent = self._build_agent()
        controller, pending = self._make_pending(agent, execute=lambda: "done")

        with patch.object(agent, "execute_tool", wraps=agent.execute_tool) as wrapped:
            result = controller.approve(pending.request_id)

        self.assertEqual(result.status, "approved")
        self.assertEqual(wrapped.call_count, 1)
        self.assertIn("approval_token", wrapped.call_args.kwargs)

    def test_approve_result_does_not_expose_token(self):
        agent = self._build_agent()
        controller, pending = self._make_pending(agent)

        result = controller.approve(pending.request_id)
        self.assertIsInstance(result, ApprovalResult)
        self.assertFalse(hasattr(result, "approval_token"))
        self.assertNotIn("token", result.message.lower())

    def test_approve_twice_does_not_execute_twice(self):
        agent = self._build_agent()
        calls = []

        def action():
            calls.append("run")
            return "done"

        controller, pending = self._make_pending(agent, execute=action)
        first = controller.approve(pending.request_id)
        second = controller.approve(pending.request_id)

        self.assertEqual(first.status, "approved")
        self.assertEqual(second.status, "not_found")
        self.assertEqual(calls, ["run"])

    def test_fake_request_id_does_not_execute(self):
        agent = self._build_agent()
        calls = []
        controller, _ = self._make_pending(agent, execute=lambda: calls.append("run"))
        fake_id = "123e4567-e89b-42d3-a456-426614174000"

        result = controller.approve(fake_id)
        self.assertEqual(result.status, "not_found")
        self.assertEqual(calls, [])

    def test_partial_request_id_command_does_not_execute(self):
        agent = self._build_agent()
        conv = ConversationalController(agent)
        calls = []

        pending = self._seed_pending_conversation(conv, execute=lambda: calls.append("run"))
        partial_id = pending.request_id[:-1]
        conv.process_message(f"aprobar {partial_id}")

        self.assertIsNotNone(conv.approval_controller.get_pending(pending.request_id))
        self.assertEqual(calls, [])

    def test_reject_removes_pending_request(self):
        agent = self._build_agent()
        controller, pending = self._make_pending(agent)

        result = controller.reject(pending.request_id)
        self.assertEqual(result.status, "rejected")
        self.assertIsNone(controller.get_pending(pending.request_id))

    def test_reject_does_not_execute(self):
        agent = self._build_agent()
        calls = []

        def action():
            calls.append("run")
            return "done"

        controller, pending = self._make_pending(agent, execute=action)
        result = controller.reject(pending.request_id)

        self.assertEqual(result.status, "rejected")
        self.assertEqual(calls, [])

    def test_reject_then_approve_does_not_execute(self):
        agent = self._build_agent()
        calls = []

        def action():
            calls.append("run")
            return "done"

        controller, pending = self._make_pending(agent, execute=action)
        controller.reject(pending.request_id)
        after = controller.approve(pending.request_id)

        self.assertEqual(after.status, "not_found")
        self.assertEqual(calls, [])

    def test_cancel_removes_pending_request(self):
        agent = self._build_agent()
        controller, pending = self._make_pending(agent)

        result = controller.cancel(pending.request_id)
        self.assertEqual(result.status, "cancelled")
        self.assertIsNone(controller.get_pending(pending.request_id))

    def test_cancel_then_approve_does_not_execute(self):
        agent = self._build_agent()
        calls = []

        def action():
            calls.append("run")
            return "done"

        controller, pending = self._make_pending(agent, execute=action)
        controller.cancel(pending.request_id)
        after = controller.approve(pending.request_id)

        self.assertEqual(after.status, "not_found")
        self.assertEqual(calls, [])

    def test_one_request_does_not_approve_other_id(self):
        agent = self._build_agent()
        calls = []

        def action():
            calls.append("run")
            return "done"

        controller, pending = self._make_pending(agent, execute=action)
        other = "223e4567-e89b-42d3-a456-426614174000"
        failed = controller.approve(other)
        approved = controller.approve(pending.request_id)

        self.assertEqual(failed.status, "not_found")
        self.assertEqual(approved.status, "approved")
        self.assertEqual(calls, ["run"])

    def test_changed_important_args_after_request_fails_execution(self):
        agent = self._build_agent()
        controller, pending = self._make_pending(agent, execute=lambda: "done")

        controller.get_pending(pending.request_id).important_args = {"path": "other.py"}
        result = controller.approve(pending.request_id)

        self.assertEqual(result.status, "failed")

    def test_action_failure_consumes_request(self):
        agent = self._build_agent()

        def action():
            raise RuntimeError("boom")

        controller, pending = self._make_pending(agent, execute=action)
        result = controller.approve(pending.request_id)

        self.assertEqual(result.status, "failed")
        self.assertIsNone(controller.get_pending(pending.request_id))

    def test_action_failure_cannot_be_retried_without_new_request(self):
        agent = self._build_agent()

        def action():
            raise RuntimeError("boom")

        controller, pending = self._make_pending(agent, execute=action)
        controller.approve(pending.request_id)
        retry = controller.approve(pending.request_id)

        self.assertEqual(retry.status, "not_found")

    def test_grant_approval_not_called_during_request_operation(self):
        agent = self._build_agent()
        controller = ApprovalController(agent)
        err = self._blocked_high_error(agent)

        with patch.object(agent.permission_manager, "grant_approval", wraps=agent.permission_manager.grant_approval) as wrapped:
            pending = controller.request_operation(
                tool_name=err.tool_name,
                action_name=err.action_name,
                important_args=err.important_args,
                execute=err.execute,
                description="op",
            )

        self.assertIsInstance(pending, ApprovalRequired)
        self.assertEqual(wrapped.call_count, 0)

    def test_grant_approval_called_only_after_explicit_valid_command(self):
        agent = self._build_agent()
        conv = ConversationalController(agent)

        with patch.object(agent.permission_manager, "grant_approval", wraps=agent.permission_manager.grant_approval) as wrapped:
            pending = self._seed_pending_conversation(conv)
            conv.process_message("sí")
            conv.process_message("ok")
            conv.process_message(f"aprobar {pending.request_id} extra")
            conv.process_message(f"aprobar {pending.request_id[:-1]}")
            conv.process_message(f"aprobar {pending.request_id}")

        self.assertEqual(wrapped.call_count, 1)

    def test_yes_message_does_not_approve(self):
        agent = self._build_agent()
        conv = ConversationalController(agent)
        pending = self._seed_pending_conversation(conv)

        conv.process_message("sí")
        self.assertIsNotNone(conv.approval_controller.get_pending(pending.request_id))

    def test_ok_message_does_not_approve(self):
        agent = self._build_agent()
        conv = ConversationalController(agent)
        pending = self._seed_pending_conversation(conv)

        conv.process_message("ok")
        self.assertIsNotNone(conv.approval_controller.get_pending(pending.request_id))

    def test_approve_command_with_extra_text_is_rejected(self):
        agent = self._build_agent()
        conv = ConversationalController(agent)
        pending = self._seed_pending_conversation(conv)

        conv.process_message(f"aprobar {pending.request_id} texto-extra")
        self.assertIsNotNone(conv.approval_controller.get_pending(pending.request_id))

    def test_approve_command_with_partial_id_is_rejected(self):
        agent = self._build_agent()
        conv = ConversationalController(agent)
        pending = self._seed_pending_conversation(conv)

        conv.process_message(f"aprobar {pending.request_id[:-1]}")
        self.assertIsNotNone(conv.approval_controller.get_pending(pending.request_id))

    def test_approve_command_with_exact_id_executes(self):
        agent = self._build_agent()
        conv = ConversationalController(agent)
        calls = []

        pending = self._seed_pending_conversation(conv, execute=lambda: calls.append("run") or "ok")
        out = conv.process_message(f"aprobar {pending.request_id}")

        self.assertIn("Operación aprobada", out)
        self.assertEqual(calls, ["run"])

    def test_command_verb_is_case_insensitive(self):
        agent = self._build_agent()
        conv = ConversationalController(agent)
        calls = []

        pending = self._seed_pending_conversation(conv, execute=lambda: calls.append("run") or "ok")
        out = conv.process_message(f"APROBAR {pending.request_id}")

        self.assertIn("Operación aprobada", out)
        self.assertEqual(calls, ["run"])

    def test_request_id_must_match_exactly(self):
        agent = self._build_agent()
        conv = ConversationalController(agent)

        pending = self._seed_pending_conversation(conv)
        conv.process_message(f"aprobar {pending.request_id.upper()}")

        self.assertIsNotNone(conv.approval_controller.get_pending(pending.request_id))

    def test_low_operation_runs_without_request(self):
        agent = self._build_agent()
        conv = ConversationalController(agent)

        result = conv.process_message("git status")
        self.assertIn("Comando: git status --short", result)

    def test_high_operation_does_not_run_before_approval(self):
        agent = self._build_agent()
        conv = ConversationalController(agent)

        out = conv.process_message("aplica cambio main.py | nuevo | viejo")
        self.assertIn("Se requiere aprobación", out)

    def test_medium_operation_does_not_run_before_approval_when_enabled(self):
        agent = self._build_agent(settings_content='{"medium_risk_requires_confirmation": true}')
        conv = ConversationalController(agent)

        out = conv.process_message("prueba")
        self.assertIn("Se requiere aprobación", out)

    def test_sensitive_data_is_redacted_in_display(self):
        args = {
            "path": "main.py",
            "api_key": "abc",
            "nested": {"Authorization": "Bearer token", "value": "ok"},
        }
        sanitized = sanitize_important_args(args)

        self.assertEqual(sanitized["api_key"], "[REDACTED]")
        self.assertEqual(sanitized["nested"]["Authorization"], "[REDACTED]")
        self.assertEqual(sanitized["nested"]["value"], "ok")

    def test_real_args_not_sanitized_args_define_fingerprint(self):
        agent = self._build_agent(settings_content='{"medium_risk_requires_confirmation": false}')
        request = agent.create_operation_approval_request(
            "patch_applier",
            "apply_patch",
            {"path": "main.py", "token": "real-secret"},
        )
        token = agent.permission_manager.grant_approval(request["request_id"])

        self.assertFalse(
            agent.permission_manager.can_execute(
                "patch_applier",
                action_name="apply_patch",
                important_args={"path": "main.py", "token": "[REDACTED]"},
                approval_token=token,
            )
        )
        self.assertTrue(
            agent.permission_manager.can_execute(
                "patch_applier",
                action_name="apply_patch",
                important_args={"path": "main.py", "token": "real-secret"},
                approval_token=token,
            )
        )

    def test_approval_methods_are_not_registered_as_tools(self):
        agent = self._build_agent()
        names = set(agent.registry.names())

        self.assertNotIn("approve", names)
        self.assertNotIn("reject", names)
        self.assertNotIn("grant_approval", names)

    def test_planner_does_not_call_grant_approval(self):
        text = (Path(__file__).resolve().parents[1] / "brain" / "planner.py").read_text(encoding="utf-8")
        self.assertNotIn("grant_approval(", text)

    def test_tool_router_does_not_call_grant_approval(self):
        text = (Path(__file__).resolve().parents[1] / "brain" / "tool_router.py").read_text(encoding="utf-8")
        self.assertNotIn("grant_approval(", text)

    def test_execution_engine_does_not_call_grant_approval(self):
        text = (Path(__file__).resolve().parents[1] / "brain" / "execution_engine.py").read_text(encoding="utf-8")
        self.assertNotIn("grant_approval(", text)

    def test_model_response_does_not_receive_approval_token(self):
        agent = self._build_agent()
        conv = ConversationalController(agent)

        out = conv.process_message("aplica cambio main.py | nuevo | viejo")
        self.assertNotIn("approval_token", out)

    def test_cancelled_request_disappears_from_permission_manager(self):
        agent = self._build_agent()
        controller, pending = self._make_pending(agent)

        controller.cancel(pending.request_id)
        self.assertFalse(agent.permission_manager.cancel_approval_request(pending.request_id))
        self.assertIsNone(agent.permission_manager.grant_approval(pending.request_id))

    def test_injected_paths_do_not_change_real_memory_or_logs(self):
        repo_root = Path(__file__).resolve().parents[1]
        real_memory = repo_root / "memory" / "memory.json"
        real_logs = repo_root / "logs" / "agent_actions.json"
        memory_before = self._sha256(real_memory)
        logs_before = self._sha256(real_logs)

        agent = self._build_agent(settings_content='{"medium_risk_requires_confirmation": false}')
        conv = ConversationalController(agent)
        conv.process_message("recuerda que prueba usa archivos temporales")
        conv.process_message("git status")

        self.assertEqual(memory_before, self._sha256(real_memory))
        self.assertEqual(logs_before, self._sha256(real_logs))

    def test_parse_approval_command_aprobar_uuid(self):
        cmd = parse_approval_command("aprobar 123e4567-e89b-42d3-a456-426614174000")
        self.assertIsNotNone(cmd)
        self.assertEqual(cmd.action, "approve")

    def test_parse_approval_command_uppercase_aprobar_uuid(self):
        cmd = parse_approval_command("APROBAR 123e4567-e89b-42d3-a456-426614174000")
        self.assertIsNotNone(cmd)
        self.assertEqual(cmd.action, "approve")

    def test_parse_approval_command_rechazar_uuid(self):
        cmd = parse_approval_command("rechazar 123e4567-e89b-42d3-a456-426614174000")
        self.assertIsNotNone(cmd)
        self.assertEqual(cmd.action, "reject")

    def test_parse_approval_command_cancelar_uuid(self):
        cmd = parse_approval_command("cancelar 123e4567-e89b-42d3-a456-426614174000")
        self.assertIsNotNone(cmd)
        self.assertEqual(cmd.action, "cancel")

    def test_parse_approval_command_si_is_invalid(self):
        self.assertIsNone(parse_approval_command("sí"))

    def test_parse_approval_command_missing_id_is_invalid(self):
        self.assertIsNone(parse_approval_command("aprobar"))

    def test_parse_approval_command_extra_text_is_invalid(self):
        self.assertIsNone(parse_approval_command("aprobar 123e4567-e89b-42d3-a456-426614174000 extra"))

    def test_parse_approval_command_partial_id_is_invalid(self):
        self.assertIsNone(parse_approval_command("aprobar 123e4567-e89b-42d3-a456-42661417400"))

    def test_invalid_approval_verb_commands_do_not_reach_model_or_grant(self):
        agent = self._build_agent()
        conv = ConversationalController(agent)
        pending = self._seed_pending_conversation(conv)

        invalid_messages = [
            "aprobar",
            f"aprobar {pending.request_id[:-1]}",
            f"aprobar {pending.request_id} texto-extra",
            f"APROBAR {pending.request_id} texto-extra",
        ]

        with patch.object(agent, "respond", wraps=agent.respond) as mocked_respond:
            with patch.object(agent.permission_manager, "grant_approval", wraps=agent.permission_manager.grant_approval) as mocked_grant:
                for message in invalid_messages:
                    response = conv.process_message(message)
                    self.assertIn("Comando de aprobación inválido", response)
                    self.assertIsNotNone(conv.approval_controller.get_pending(pending.request_id))

        self.assertEqual(mocked_respond.call_count, 0)
        self.assertEqual(mocked_grant.call_count, 0)

    def test_normal_message_can_reach_agent_respond(self):
        agent = self._build_agent(settings_content='{"medium_risk_requires_confirmation": false}')
        conv = ConversationalController(agent)

        with patch.object(agent, "respond", return_value="MODEL_OK") as mocked_respond:
            response = conv.process_message("mensaje normal")

        self.assertEqual(response, "MODEL_OK")
        self.assertEqual(mocked_respond.call_count, 1)


if __name__ == "__main__":
    unittest.main()
