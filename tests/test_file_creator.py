import tempfile
import unittest
from pathlib import Path

from tools.file_creator import FileCreator
from tools.tool_result import ToolResult


class FileCreatorTests(unittest.TestCase):
    def test_create_file_creates_valid_file_with_utf8_bytes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir)
            (base_dir / "notas").mkdir()

            creator = FileCreator(base_dir=base_dir)
            content = "Hola ñ🙂"
            result = creator.create_file("notas/hola.txt", content)

            target = base_dir / "notas" / "hola.txt"
            self.assertTrue(result["creado"])
            self.assertEqual(result["archivo"], "notas/hola.txt")
            self.assertEqual(result["bytes"], len(content.encode("utf-8")))
            self.assertEqual(target.read_text(encoding="utf-8"), content)

    def test_existing_file_is_rejected_without_modification(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir)
            (base_dir / "notas").mkdir()
            target = base_dir / "notas" / "hola.txt"
            target.write_text("original", encoding="utf-8")

            creator = FileCreator(base_dir=base_dir)
            with self.assertRaises(FileExistsError):
                creator.create_file("notas/hola.txt", "nuevo")

            self.assertEqual(target.read_text(encoding="utf-8"), "original")

    def test_absolute_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir)
            creator = FileCreator(base_dir=base_dir)

            with self.assertRaises(ValueError):
                creator.create_file(str(base_dir / "abs.txt"), "hola")

    def test_parent_escape_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir)
            creator = FileCreator(base_dir=base_dir)

            with self.assertRaises(ValueError):
                creator.create_file("../escape.txt", "hola")

    def test_git_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir)
            creator = FileCreator(base_dir=base_dir)

            with self.assertRaises(ValueError):
                creator.create_file(".git/secret.txt", "hola")

    def test_missing_parent_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir)
            creator = FileCreator(base_dir=base_dir)

            with self.assertRaises(FileNotFoundError):
                creator.create_file("faltante/hola.txt", "hola")

    def test_oversized_content_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir)
            (base_dir / "notas").mkdir()
            creator = FileCreator(base_dir=base_dir)

            content = "a" * 65537
            with self.assertRaises(ValueError):
                creator.create_file("notas/hola.txt", content)

    def test_execute_preserves_historical_default_and_supports_structured(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir)
            creator = FileCreator(base_dir=base_dir)

            historical = creator.execute({"path": "one.txt", "content": "one"})
            structured = creator.execute(
                {"path": "two.txt", "content": "two"},
                structured=True,
            )

            self.assertIsInstance(historical, dict)
            self.assertNotIn("status", historical)
            self.assertIsInstance(structured, ToolResult)
            self.assertEqual(structured.status, "ok")

    def test_execute_validation_is_failed_but_internal_value_error_propagates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            creator = FileCreator(base_dir=tmpdir)
            validation = creator.execute({"path": 1}, structured=True)
            self.assertEqual(validation.status, "failed")

            creator.create_file = lambda path, content: (_ for _ in ()).throw(
                ValueError("internal")
            )
            with self.assertRaises(ValueError):
                creator.execute(
                    {"path": "file.txt", "content": "value"},
                    structured=True,
                )


if __name__ == "__main__":
    unittest.main()
