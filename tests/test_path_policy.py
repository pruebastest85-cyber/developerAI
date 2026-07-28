import tempfile
import unittest
from pathlib import Path

from brain.path_policy import PathPolicy, PathValidationError


class PathPolicyTests(unittest.TestCase):
    def test_valid_relative_path_and_normalization(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            (base / "pkg").mkdir()
            (base / "pkg" / "file.py").write_text("value = 1\n", encoding="utf-8")
            policy = PathPolicy(base)

            result = policy.resolve_for_read("pkg/./file.py")

            self.assertEqual(result.relative, Path("pkg/file.py"))
            self.assertEqual(result.absolute, (base / "pkg/file.py").resolve())

    def test_rejects_posix_windows_drive_unc_and_parent_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            policy = PathPolicy(tmpdir)
            invalid = (
                "/etc/passwd",
                r"C:\Windows\system.ini",
                r"C:relative.txt",
                r"\\server\share\file.txt",
                "//server/share/file.txt",
                "../outside.txt",
                "pkg/../outside.txt",
            )
            for path in invalid:
                with self.subTest(path=path):
                    with self.assertRaises(PathValidationError):
                        policy.resolve_for_read(path)

    def test_resolution_cannot_escape_through_symlink(self):
        with tempfile.TemporaryDirectory() as tmpdir, tempfile.TemporaryDirectory() as outside:
            base = Path(tmpdir)
            link = base / "link"
            try:
                link.symlink_to(Path(outside), target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"No se pudieron crear symlinks: {exc}")

            policy = PathPolicy(base)
            with self.assertRaises(PathValidationError):
                policy.resolve_for_read("link/file.py")

    def test_rejects_forbidden_components_secrets_and_backups(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            policy = PathPolicy(
                tmpdir,
                secret_names={".env", "token.txt"},
                backup_suffixes=(".backup", ".bak"),
            )
            invalid = (
                ".git/config",
                "project/index.json",
                ".venv/lib.py",
                "venv/lib.py",
                "pkg/__pycache__/item.pyc",
                ".env",
                "config/token.txt",
                "module.py.backup",
                "module.py.bak",
            )
            for path in invalid:
                with self.subTest(path=path):
                    with self.assertRaises(PathValidationError):
                        policy.resolve_for_read(path)

    def test_similar_prefixes_remain_valid(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            policy = PathPolicy(tmpdir)
            for path in (
                "project_notes/file.py",
                ".github/workflow.yml",
                "venv_tools/helper.py",
                "module.backup_notes",
            ):
                with self.subTest(path=path):
                    result = policy.resolve_for_write(path)
                    self.assertEqual(result.relative, Path(path))

    def test_write_rejects_existing_symlink_component(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            real = base / "real"
            real.mkdir()
            link = base / "link"
            try:
                link.symlink_to(real, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"No se pudieron crear symlinks: {exc}")

            policy = PathPolicy(base)
            with self.assertRaises(PathValidationError):
                policy.resolve_for_write("link/new.py")
            self.assertEqual(
                policy.resolve_for_read("link/new.py").absolute,
                (real / "new.py").resolve(),
            )

    def test_new_file_is_valid_without_filesystem_changes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            policy = PathPolicy(base)

            before = set(base.iterdir())
            result = policy.resolve_for_write("new/deep/file.py")

            self.assertEqual(result.relative, Path("new/deep/file.py"))
            self.assertEqual(result.absolute, (base / "new/deep/file.py").resolve())
            self.assertEqual(set(base.iterdir()), before)


if __name__ == "__main__":
    unittest.main()
