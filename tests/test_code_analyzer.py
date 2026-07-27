import unittest
from pathlib import Path

from tools.code_analyzer import CodeAnalyzer


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


if __name__ == "__main__":
    unittest.main()
