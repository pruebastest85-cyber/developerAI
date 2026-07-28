import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.patch_applier import PatchApplier


class PatchApplierTests(unittest.TestCase):
    def _supports_symlinks(self):
        return hasattr(os, "symlink")

    def test_apply_patch_replaces_file_atomically_and_returns_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir)
            target = base_dir / "sample.txt"
            original = "hola ñ🙂\n"
            updated = "adiós ñ🙂\n"
            target.write_bytes(original.encode("utf-8"))

            applier = PatchApplier(base_dir=base_dir)
            with mock.patch("tools.patch_applier.os.replace", wraps=os.replace) as replace_mock:
                result = applier.apply_patch("sample.txt", original, updated)

            replace_mock.assert_called_once()
            self.assertTrue(result["aplicado"])
            self.assertTrue(result["actualizado"])
            self.assertTrue((base_dir / "sample.txt.backup").exists())
            self.assertEqual(result["archivo"], "sample.txt")
            self.assertEqual(result["bytes"], len(updated.encode("utf-8")))
            self.assertEqual(target.read_text(encoding="utf-8"), updated)
            self.assertEqual((base_dir / "sample.txt.backup").read_text(encoding="utf-8"), original)

    def test_absolute_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir)
            target = base_dir / "sample.txt"
            target.write_bytes("hola".encode("utf-8"))

            applier = PatchApplier(base_dir=base_dir)
            with self.assertRaises(ValueError):
                applier.apply_patch(str(target), "hola", "adios")

    def test_parent_escape_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir)
            (base_dir / "sample.txt").write_bytes("hola".encode("utf-8"))

            applier = PatchApplier(base_dir=base_dir)
            with self.assertRaises(ValueError):
                applier.apply_patch("../sample.txt", "hola", "adios")

    def test_git_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir)
            (base_dir / ".git").mkdir()
            (base_dir / ".git" / "config").write_bytes("hola".encode("utf-8"))

            applier = PatchApplier(base_dir=base_dir)
            with self.assertRaises(ValueError):
                applier.apply_patch(".git/config", "hola", "adios")

    def test_missing_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            applier = PatchApplier(base_dir=Path(tmpdir))

            with self.assertRaises(FileNotFoundError):
                applier.apply_patch("missing.txt", "hola", "adios")

    def test_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir)
            (base_dir / "folder").mkdir()

            applier = PatchApplier(base_dir=base_dir)
            with self.assertRaises(IsADirectoryError):
                applier.apply_patch("folder", "hola", "adios")

    def test_symlink_destination_is_rejected(self):
        if not self._supports_symlinks():
            self.skipTest("La plataforma no soporta symlinks")

        with tempfile.TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir)
            external = Path(tmpdir) / "external.txt"
            external.write_text("hola", encoding="utf-8")
            target = base_dir / "linked.txt"
            try:
                target.symlink_to(external)
            except OSError as exc:
                self.skipTest(f"No se pudieron crear symlinks: {exc}")

            applier = PatchApplier(base_dir=base_dir)
            with self.assertRaises(ValueError):
                applier.apply_patch("linked.txt", "hola", "adios")

    def test_parent_symlink_escape_is_rejected(self):
        if not self._supports_symlinks():
            self.skipTest("La plataforma no soporta symlinks")

        with tempfile.TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir)
            external_dir = Path(tmpdir) / "external"
            external_dir.mkdir()
            (external_dir / "outside.txt").write_text("hola", encoding="utf-8")
            link_dir = base_dir / "link"
            try:
                link_dir.symlink_to(external_dir, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"No se pudieron crear symlinks: {exc}")

            applier = PatchApplier(base_dir=base_dir)
            with self.assertRaises(ValueError):
                applier.apply_patch("link/outside.txt", "hola", "adios")

    def test_old_content_mismatch_leaves_file_intact(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir)
            target = base_dir / "sample.txt"
            original = "hola\n"
            target.write_bytes(original.encode("utf-8"))

            applier = PatchApplier(base_dir=base_dir)
            with self.assertRaises(ValueError):
                applier.apply_patch("sample.txt", "otro\n", "nuevo\n")

            self.assertEqual(target.read_text(encoding="utf-8"), original)
            self.assertFalse((base_dir / "sample.txt.backup").exists())

    def test_old_content_mismatch_is_checked_by_exact_bytes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir)
            target = base_dir / "sample.txt"
            target.write_bytes(b"hola\r\n")

            applier = PatchApplier(base_dir=base_dir)
            with self.assertRaises(ValueError):
                applier.apply_patch("sample.txt", "hola\n", "nuevo\n")

            self.assertEqual(target.read_bytes(), b"hola\r\n")

    def test_backup_symlink_is_rejected(self):
        if not self._supports_symlinks():
            self.skipTest("La plataforma no soporta symlinks")

        with tempfile.TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir)
            target = base_dir / "sample.txt"
            original = "hola\n"
            target.write_bytes(original.encode("utf-8"))
            backup = base_dir / "sample.txt.backup"
            external_backup = base_dir / "external.backup"
            external_backup.write_text("external", encoding="utf-8")
            try:
                backup.symlink_to(external_backup)
            except OSError as exc:
                self.skipTest(f"No se pudieron crear symlinks: {exc}")

            applier = PatchApplier(base_dir=base_dir)
            with self.assertRaises(ValueError):
                applier.apply_patch("sample.txt", original, "adios\n")

            self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_broken_backup_symlink_is_rejected(self):
        if not self._supports_symlinks():
            self.skipTest("La plataforma no soporta symlinks")

        with tempfile.TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir)
            target = base_dir / "sample.txt"
            original = "hola\n"
            target.write_bytes(original.encode("utf-8"))
            backup = base_dir / "sample.txt.backup"
            missing = base_dir / "missing.backup"
            try:
                backup.symlink_to(missing)
            except OSError as exc:
                self.skipTest(f"No se pudieron crear symlinks: {exc}")

            applier = PatchApplier(base_dir=base_dir)
            with self.assertRaises(ValueError):
                applier.apply_patch("sample.txt", original, "adios\n")

            self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_oversized_contents_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir)
            target = base_dir / "sample.txt"
            original = "hola"
            target.write_bytes(original.encode("utf-8"))

            applier = PatchApplier(base_dir=base_dir)
            huge = "a" * (1024 * 1024 + 1)

            with self.assertRaises(ValueError):
                applier.apply_patch("sample.txt", original, huge)

            with self.assertRaises(ValueError):
                applier.apply_patch("sample.txt", huge, original)

    def test_utf8_content_is_preserved_and_bytes_are_correct(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir)
            target = base_dir / "sample.txt"
            original = "hola ñ🙂\n"
            updated = "adiós ñ🙂\n"
            target.write_bytes(original.encode("utf-8"))

            applier = PatchApplier(base_dir=base_dir)
            result = applier.apply_patch("sample.txt", original, updated)

            self.assertEqual(target.read_text(encoding="utf-8"), updated)
            self.assertEqual(result["bytes"], len(updated.encode("utf-8")))

    def test_temp_file_is_removed_when_write_fails(self):
        class FailingTempFile:
            def __init__(self, temp_path):
                self.name = str(temp_path)

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def write(self, data):
                raise OSError("write failed")

            def flush(self):
                pass

            def fileno(self):
                return 1

        with tempfile.TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir)
            target = base_dir / "sample.txt"
            target.write_bytes("hola".encode("utf-8"))
            temp_fd, temp_name = tempfile.mkstemp(dir=base_dir)
            os.close(temp_fd)
            temp_path = Path(temp_name)

            applier = PatchApplier(base_dir=base_dir)
            with mock.patch("tools.patch_applier.tempfile.NamedTemporaryFile", return_value=FailingTempFile(temp_path)):
                with self.assertRaises(OSError):
                    applier.apply_patch("sample.txt", "hola", "adios")

            self.assertFalse(temp_path.exists())
            self.assertEqual(target.read_text(encoding="utf-8"), "hola")

    def test_original_file_stays_intact_when_replace_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir)
            target = base_dir / "sample.txt"
            original = "hola"
            target.write_bytes(original.encode("utf-8"))

            applier = PatchApplier(base_dir=base_dir)
            with mock.patch("tools.patch_applier.os.replace", side_effect=OSError("replace failed")):
                with self.assertRaises(OSError):
                    applier.apply_patch("sample.txt", original, "adios")

            self.assertEqual(target.read_text(encoding="utf-8"), original)
            self.assertFalse(any(path.name.startswith(".sample.txt.") for path in base_dir.iterdir()))


if __name__ == "__main__":
    unittest.main()
