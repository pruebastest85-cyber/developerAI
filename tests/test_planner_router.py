import tempfile
import unittest
from pathlib import Path

from brain.planner import Planner
from brain.tool_router import ToolRouter
from brain.agent import DeveloperAgent


class PlannerRouterTests(unittest.TestCase):
    def test_planner_returns_expected_plan(self):
        planner = Planner()
        plan = planner.plan("Analiza brain/agent.py")
        self.assertIn("code_analyzer", plan)
        self.assertIn("code_reader", plan)

    def test_planner_selects_test_runner_only_for_explicit_test_commands(self):
        planner = Planner()

        positive_cases = [
            "prueba",
            "pruebas",
            "ejecutar pruebas",
            "ejecuta pruebas",
            "ejecutar tests",
            "ejecuta tests",
            "run tests",
        ]

        negative_cases = [
            "Crea prueba_aprobacion.txt",
            'Escribe el texto "prueba"',
            "Crea una prueba unitaria",
            "Explica los tests",
            "Abre tests/test_agent.py",
            "ejecuta este cambio",
            "testimonio",
        ]

        for message in positive_cases:
            with self.subTest(message=message):
                self.assertEqual(planner.plan(message), ["test_runner"])

        for message in negative_cases:
            with self.subTest(message=message):
                self.assertNotEqual(planner.plan(message), ["test_runner"])
                self.assertNotIn("test_runner", planner.plan(message))

    def test_router_dispatches_to_memory_tool(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp_dir = Path(tmpdir)
            agent = DeveloperAgent(
                client=None,
                memory_file=temp_dir / "memory.json",
                prompt_dir="prompts",
                base_dir=".",
                action_log_file=temp_dir / "agent_actions.json",
            )
            router = ToolRouter(agent)
            result = router.dispatch(["memory"], "Recuerda que estoy probando el router")
            self.assertIn("Lo recordaré", result)


if __name__ == "__main__":
    unittest.main()
