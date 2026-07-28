import tempfile
import unittest
import hashlib
from pathlib import Path
from unittest import mock

from brain.planner import Planner
from brain.tool_router import ToolRouter
from brain.agent import DeveloperAgent


class PlannerRouterTests(unittest.TestCase):
    def test_planner_returns_expected_plan(self):
        planner = Planner()
        plan = planner.plan("Analiza brain/agent.py")
        self.assertIn("code_analyzer", plan)
        self.assertIn("code_reader", plan)

    def test_planner_selects_file_creator_only_for_explicit_create_commands(self):
        planner = Planner()

        positive_cases = [
            "crea archivo",
            "crear archivo",
            "crea archivo notas/hola.txt | Hola mundo",
            "crear archivo notas/hola.txt | Hola mundo",
        ]

        negative_cases = [
            "explica cómo crear archivos",
            "dónde está el archivo",
            "crea una prueba",
            "archivo nuevo",
            "recrea archivo",
        ]

        for message in positive_cases:
            with self.subTest(message=message):
                self.assertEqual(planner.plan(message), ["file_creator"])

        for message in negative_cases:
            with self.subTest(message=message):
                self.assertNotIn("file_creator", planner.plan(message))

    def test_router_dispatches_file_creator_with_exact_separator_and_hashed_args(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp_dir = Path(tmpdir)
            agent = DeveloperAgent(
                client=None,
                memory_file=temp_dir / "memory.json",
                prompt_dir="prompts",
                base_dir=temp_dir,
                action_log_file=temp_dir / "agent_actions.json",
            )
            router = ToolRouter(agent)
            content = "Hola mundo"
            expected_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

            with mock.patch.object(agent, "execute_tool", return_value={"archivo": "notas/hola.txt", "creado": True, "bytes": len(content.encode("utf-8"))}) as execute_tool:
                result = router.dispatch(["file_creator"], "crea archivo notas/hola.txt | Hola mundo")

            self.assertEqual(result["archivo"], "notas/hola.txt")
            execute_tool.assert_called_once()
            args, kwargs = execute_tool.call_args
            self.assertEqual(args[0], "file_creator")
            self.assertEqual(kwargs["action_name"], "create_file")
            self.assertEqual(set(kwargs["important_args"].keys()), {"path", "content_sha256", "content_bytes"})
            self.assertEqual(kwargs["important_args"]["path"], "notas/hola.txt")
            self.assertEqual(kwargs["important_args"]["content_sha256"], expected_hash)
            self.assertEqual(kwargs["important_args"]["content_bytes"], len(content.encode("utf-8")))

    def test_router_rejects_malformed_file_creator_commands(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp_dir = Path(tmpdir)
            agent = DeveloperAgent(
                client=None,
                memory_file=temp_dir / "memory.json",
                prompt_dir="prompts",
                base_dir=temp_dir,
                action_log_file=temp_dir / "agent_actions.json",
            )
            router = ToolRouter(agent)

            with mock.patch.object(agent, "execute_tool") as execute_tool:
                malformed_messages = [
                    "crea archivo notas/hola.txt|Hola mundo",
                    "crea archivo  | Hola mundo",
                    "crea archivo notas/hola.txt | ",
                    "crea archivo",
                ]

                for message in malformed_messages:
                    with self.subTest(message=message):
                        result = router.dispatch(["file_creator"], message)
                        self.assertIsInstance(result, str)

            execute_tool.assert_not_called()

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
