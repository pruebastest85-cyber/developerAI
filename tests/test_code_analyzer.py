import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.code_analyzer import CodeAnalyzer
from tools.tool_result import ToolResult


class CodeAnalyzerTests(unittest.TestCase):
    def test_analyze_file_detects_functions_and_classes(self):
        analyzer = CodeAnalyzer(base_dir=Path("."))
        result = analyzer.analyze_file("brain/agent.py")
        self.assertIn("DeveloperAgent", result["clases"])
        self.assertIn("respond", result["funciones"])

    def test_summarize_produces_structured_output(self):
        analyzer = CodeAnalyzer(base_dir=Path("."))
        summary = analyzer.summarize("brain/agent.py")
        self.assertIn("Archivo:", summary)
        self.assertIn("Funciones:", summary)

    def test_byte_limit_prevents_parsing_truncated_source(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = "def café():\n    return 1\n"
            source_path = root / "module.py"
            source_path.write_text(source, encoding="utf-8")
            analyzer = CodeAnalyzer(root)
            exact = len(source_path.read_bytes())
            self.assertIn(
                "café",
                analyzer.analyze_file(
                    "module.py",
                    max_read_bytes_per_file=exact,
                )["funciones"],
            )
            result = analyzer.execute(
                {
                    "path": "module.py",
                    "max_read_bytes_per_file": exact - 1,
                },
                structured=True,
            )
            self.assertIsInstance(result, ToolResult)
            self.assertEqual(result.status, "failed")
            self.assertFalse(result.retryable)

    def test_invalid_byte_limits_are_explicit_failures(self):
        analyzer = CodeAnalyzer(Path("."))
        for value in (True, 0, -1, 1.5, "2"):
            with self.subTest(value=value):
                result = analyzer.execute(
                    {
                        "path": "brain/agent.py",
                        "max_read_bytes_per_file": value,
                    },
                    structured=True,
                )
                self.assertEqual(result.status, "failed")

    def test_unexpected_internal_error_propagates(self):
        analyzer = CodeAnalyzer(Path("."))
        with mock.patch.object(
            analyzer,
            "summarize",
            side_effect=RuntimeError("programming defect"),
        ):
            with self.assertRaises(RuntimeError):
                analyzer.execute({"path": "brain/agent.py"}, structured=True)


if __name__ == "__main__":
    unittest.main()
