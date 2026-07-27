import tempfile
import unittest
from pathlib import Path

from tools.patch_applier import PatchApplier


class PatchApplierTests(unittest.TestCase):
    def test_apply_patch_creates_backup_and_updates_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir)
            target = base_dir / "sample.txt"
            target.write_text("hola\n", encoding="utf-8")

            applier = PatchApplier(base_dir=base_dir)
            result = applier.apply_patch("sample.txt", "hola\n", "hola mundo\n")

            self.assertTrue(result["aplicado"])
            self.assertTrue((base_dir / "sample.txt.backup").exists())
            self.assertEqual(target.read_text(encoding="utf-8"), "hola mundo\n")


if __name__ == "__main__":
    unittest.main()
