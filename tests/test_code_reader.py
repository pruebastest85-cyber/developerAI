import unittest
from pathlib import Path

from tools.code_reader import CodeReader


class CodeReaderTests(unittest.TestCase):
    def test_read_file_returns_content(self):
        reader = CodeReader(base_dir=Path("."))
        content = reader.read_file("main.py")
        self.assertIn("DeveloperAI", content)

    def test_read_file_rejects_path_traversal(self):
        reader = CodeReader(base_dir=Path("."))
        with self.assertRaises(ValueError):
            reader.read_file("../secret.txt")


if __name__ == "__main__":
    unittest.main()
