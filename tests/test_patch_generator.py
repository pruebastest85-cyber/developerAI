import unittest
from pathlib import Path

from tools.patch_generator import PatchGenerator


class PatchGeneratorTests(unittest.TestCase):
    def test_generate_patch_contains_diff_markers(self):
        generator = PatchGenerator(base_dir=Path("."))
        patch = generator.generate_patch(
            "main.py",
            "print('hola')\n",
            "print('hola mundo')\n",
        )
        self.assertIn("---", patch)
        self.assertIn("+++", patch)
        self.assertIn("hola mundo", patch)


if __name__ == "__main__":
    unittest.main()
