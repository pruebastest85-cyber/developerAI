import tempfile
import unittest
from pathlib import Path

from tools.code_analyzer import CodeAnalyzer
from tools.code_reader import CodeReader
from tools.patch_generator import PatchGenerator
from tools.tool_result import ToolResult


class MigratedPathToolsTests(unittest.TestCase):
    def _workspace(self):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        base = root / "app"
        sibling = root / "app_evil"
        base.mkdir()
        sibling.mkdir()
        (base / "module.py").write_text(
            "def answer():\n    return 42\n",
            encoding="utf-8",
        )
        (sibling / "secret.py").write_text("SECRET = True\n", encoding="utf-8")
        return temporary, base

    def test_valid_paths_preserve_both_result_modes(self):
        temporary, base = self._workspace()
        with temporary:
            cases = (
                (
                    CodeReader(base),
                    {"path": "module.py"},
                    lambda value: self.assertIn("return 42", value),
                ),
                (
                    CodeAnalyzer(base),
                    {"path": "module.py"},
                    lambda value: self.assertIn("Archivo:", value),
                ),
                (
                    PatchGenerator(base),
                    {
                        "path": "module.py",
                        "new_content": "def answer():\n    return 43\n",
                    },
                    lambda value: self.assertIn("return 43", value),
                ),
            )

            for tool, args, assertion in cases:
                with self.subTest(tool=tool.name, mode="historical"):
                    historical = tool.execute(args)
                    self.assertIsInstance(historical, str)
                    assertion(historical)
                with self.subTest(tool=tool.name, mode="structured"):
                    structured = tool.execute(args, structured=True)
                    self.assertIsInstance(structured, ToolResult)
                    self.assertEqual(structured.status, "ok")
                    assertion(structured.data)

    def test_sibling_prefix_escape_is_rejected_by_all_migrated_tools(self):
        temporary, base = self._workspace()
        with temporary:
            cases = (
                (CodeReader(base), {"path": "../app_evil/secret.py"}),
                (CodeAnalyzer(base), {"path": "../app_evil/secret.py"}),
                (
                    PatchGenerator(base),
                    {
                        "path": "../app_evil/secret.py",
                        "new_content": "SECRET = False\n",
                    },
                ),
            )

            for tool, args in cases:
                with self.subTest(tool=tool.name):
                    structured = tool.execute(args, structured=True)
                    self.assertEqual(structured.status, "failed")
                    self.assertIn("..", structured.error)

    def test_forbidden_path_is_failed_in_structured_and_historical_modes(self):
        temporary, base = self._workspace()
        with temporary:
            cases = (
                (CodeReader(base), {"path": "project/index.json"}),
                (CodeAnalyzer(base), {"path": "project/index.json"}),
                (
                    PatchGenerator(base),
                    {
                        "path": "project/index.json",
                        "new_content": "{}",
                    },
                ),
            )

            for tool, args in cases:
                with self.subTest(tool=tool.name, mode="structured"):
                    structured = tool.execute(args, structured=True)
                    self.assertEqual(structured.status, "failed")
                    self.assertIn("prohibido", structured.error)
                with self.subTest(tool=tool.name, mode="historical"):
                    historical = tool.execute(args)
                    self.assertEqual(historical["status"], "failed")
                    self.assertIn("prohibido", historical["error"])
                    self.assertEqual(len(historical), 7)


if __name__ == "__main__":
    unittest.main()
