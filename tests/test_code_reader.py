import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.code_reader import CodeReader
from tools.tool_result import ToolResult


class CodeReaderTests(unittest.TestCase):
    def test_read_file_returns_content(self):
        reader = CodeReader(base_dir=Path("."))
        content = reader.read_file("main.py")
        self.assertIn("DeveloperAI", content)

    def test_read_file_rejects_path_traversal(self):
        reader = CodeReader(base_dir=Path("."))
        with self.assertRaises(ValueError):
            reader.read_file("../secret.txt")

    def test_optional_byte_limit_is_exact_and_multibyte_safe(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "value.txt").write_text("é", encoding="utf-8")
            reader = CodeReader(root)
            self.assertEqual(
                reader.read_file("value.txt", max_read_bytes_per_file=2),
                "é",
            )
            result = reader.execute(
                {"path": "value.txt", "max_read_bytes_per_file": 1},
                structured=True,
            )
            self.assertIsInstance(result, ToolResult)
            self.assertEqual(result.status, "failed")
            self.assertFalse(result.retryable)

    def test_invalid_byte_limits_are_explicit_failures(self):
        reader = CodeReader(Path("."))
        for value in (True, False, 0, -1, 1.5, "2"):
            with self.subTest(value=value):
                result = reader.execute(
                    {"path": "main.py", "max_read_bytes_per_file": value},
                    structured=True,
                )
                self.assertEqual(result.status, "failed")

    def test_unexpected_internal_error_propagates(self):
        reader = CodeReader(Path("."))
        with mock.patch.object(
            reader,
            "read_file_with_limit",
            side_effect=RuntimeError("programming defect"),
        ):
            with self.assertRaises(RuntimeError):
                reader.execute({"path": "main.py"}, structured=True)


if __name__ == "__main__":
    unittest.main()
