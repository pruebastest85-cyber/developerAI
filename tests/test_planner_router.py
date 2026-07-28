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

    def test_planner_selects_patch_applier_only_for_explicit_apply_commands(self):
        planner = Planner()

        positive_cases = [
            "aplica cambio main.py | nuevo | viejo",
            "aplica el cambio main.py | nuevo | viejo",
        ]

        negative_cases = [
            "explica cómo aplica cambios",
            "propón un cambio",
            "crea un patch",
            "cambio en archivo",
            "aplica parche",
        ]

        for message in positive_cases:
            with self.subTest(message=message):
                self.assertEqual(planner.plan(message), ["patch_generator", "patch_applier"])

        for message in negative_cases:
            with self.subTest(message=message):
                self.assertNotIn("patch_applier", planner.plan(message))

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

    def test_router_dispatches_patch_applier_with_exact_separator_and_hashed_args(self):
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
            old_content = "hola\n"
            new_content = "adios\n"
            old_bytes = old_content.encode("utf-8")
            new_bytes = new_content.encode("utf-8")
            expected_args = {
                "path": "main.py",
                "old_sha256": hashlib.sha256(old_bytes).hexdigest(),
                "new_sha256": hashlib.sha256(new_bytes).hexdigest(),
                "old_bytes": len(old_bytes),
                "new_bytes": len(new_bytes),
            }

            with mock.patch.object(agent, "execute_tool", return_value={"archivo": "main.py", "actualizado": True, "backup": "main.py.backup", "bytes": len(new_bytes), "aplicado": True}) as execute_tool:
                result = router.dispatch(["patch_applier"], "aplica cambio main.py | adios\n | hola\n")

            self.assertTrue(result["actualizado"])
            execute_tool.assert_called_once()
            args, kwargs = execute_tool.call_args
            self.assertEqual(args[0], "patch_applier")
            self.assertEqual(kwargs["action_name"], "apply_patch")
            self.assertEqual(kwargs["important_args"], expected_args)

    def test_router_dispatches_patch_applier_for_aplica_el_cambio(self):
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

            with mock.patch.object(agent, "execute_tool", return_value={"archivo": "main.py", "actualizado": True, "backup": "main.py.backup", "bytes": 4, "aplicado": True}) as execute_tool:
                router.dispatch(["patch_applier"], "aplica el cambio main.py | nuevo | viejo")

            execute_tool.assert_called_once()

    def test_router_rejects_malformed_patch_commands(self):
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

            malformed_messages = [
                "aplica cambio main.py|nuevo|viejo",
                "aplica cambio main.py | nuevo | ",
                "aplica cambio  | nuevo | viejo",
                "aplica cambio",
                "comando sin separadores completos",
            ]

            with mock.patch.object(agent, "execute_tool") as execute_tool:
                for message in malformed_messages:
                    with self.subTest(message=message):
                        result = router.dispatch(["patch_applier"], message)
                        if message.startswith("aplica"):
                            self.assertIsInstance(result, str)
                        else:
                            self.assertIsNone(result)

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
