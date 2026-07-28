import tempfile
import unittest
from pathlib import Path

from brain.agent import DeveloperAgent
from brain.approval_controller import ApprovalRequiredError
from memory.memory import leer_memoria
from tools.tool_result import ToolResult


class DeveloperAgentTests(unittest.TestCase):
    def test_memory_note_is_saved(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp_dir = Path(tmpdir)
            memory_file = temp_dir / "memory.json"
            agent = DeveloperAgent(
                client=None,
                memory_file=memory_file,
                prompt_dir=Path("prompts"),
                action_log_file=temp_dir / "agent_actions.json",
            )

            response = agent.handle_memory("Recuerda que estoy construyendo un agente modular")

            self.assertIn("Lo recordaré", response)
            self.assertIn(
                "estoy construyendo un agente modular",
                leer_memoria(memory_file=memory_file)["notas"],
            )

    def test_execute_tool_preserves_raw_compatibility_and_supports_structured_results(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp_dir = Path(tmpdir)
            agent = DeveloperAgent(
                client=None,
                memory_file=temp_dir / "memory.json",
                prompt_dir=Path("prompts"),
                action_log_file=temp_dir / "agent_actions.json",
            )

            raw = agent.execute_tool("code_reader", lambda: {"value": 1})
            structured = agent.execute_tool(
                "code_reader",
                lambda: {"value": 1},
                structured=True,
            )

            self.assertEqual(raw, {"value": 1})
            self.assertIsInstance(structured, ToolResult)
            self.assertEqual(structured.data, {"value": 1})

    def test_agent_presents_tool_result_message_instead_of_dataclass_repr(self):
        result = ToolResult.failure(
            "demo",
            error="technical",
            message="Mensaje legible",
        )

        self.assertEqual(
            DeveloperAgent._present_tool_result(result),
            "Mensaje legible\n\nError: technical",
        )

    def test_agent_presentation_combines_or_deduplicates_message_and_error(self):
        self.assertEqual(
            DeveloperAgent._present_tool_result(
                ToolResult.failure("demo", message="friendly", error="technical")
            ),
            "friendly\n\nError: technical",
        )
        self.assertEqual(
            DeveloperAgent._present_tool_result(
                ToolResult.failure("demo", message="same", error="same")
            ),
            "same",
        )
        self.assertEqual(
            DeveloperAgent._present_tool_result(
                ToolResult.failure("demo", error="only error")
            ),
            "only error",
        )
        self.assertEqual(
            DeveloperAgent._present_tool_result(
                ToolResult.success("demo", data={"value": 1})
            ),
            {"value": 1},
        )

    def test_approval_propagates_from_execute_tool_in_both_modes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp_dir = Path(tmpdir)
            agent = DeveloperAgent(
                client=None,
                memory_file=temp_dir / "memory.json",
                prompt_dir=Path("prompts"),
                action_log_file=temp_dir / "agent_actions.json",
            )

            for structured in (False, True):
                with self.subTest(structured=structured):
                    with self.assertRaises(ApprovalRequiredError):
                        agent.execute_tool(
                            "patch_applier",
                            lambda: "must not run",
                            action_name="apply_patch",
                            important_args={"path": "safe.py"},
                            structured=structured,
                        )


if __name__ == "__main__":
    unittest.main()
