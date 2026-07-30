import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from brain.isolated_environment import (
    IsolatedEnvironmentError,
    IsolatedRepository,
)


class IsolatedRepositoryTests(unittest.TestCase):
    def source(self, root):
        source = root / "source"
        source.mkdir()
        (source / "app.py").write_text("value = 1\n", encoding="utf-8")
        (source / "project").mkdir()
        (source / "project" / "ignored.py").write_text("ignored\n", encoding="utf-8")
        return source

    def test_creates_independent_git_baseline_without_remote_or_staging(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.source(root)
            isolation = IsolatedRepository(source, temp_parent=root)
            snapshot = isolation.create()
            try:
                self.assertNotEqual(snapshot.repository, source)
                self.assertEqual(
                    (snapshot.repository / "app.py").read_text(encoding="utf-8"),
                    "value = 1\n",
                )
                self.assertFalse((snapshot.repository / "project").exists())
                self.assertEqual(
                    subprocess.run(
                        ["git", "diff", "--cached", "--quiet"],
                        cwd=snapshot.repository,
                    ).returncode,
                    0,
                )
                self.assertEqual(
                    subprocess.run(
                        ["git", "remote"],
                        cwd=snapshot.repository,
                        text=True,
                        capture_output=True,
                        check=True,
                    ).stdout,
                    "",
                )
                self.assertEqual(snapshot.initial_commit_count, 1)
                self.assertEqual(
                    subprocess.run(
                        ["git", "rev-parse", "HEAD"],
                        cwd=snapshot.repository,
                        text=True,
                        capture_output=True,
                        check=True,
                    ).stdout.strip(),
                    snapshot.baseline_commit,
                )
                self.assertEqual(
                    (source / "app.py").read_text(encoding="utf-8"),
                    "value = 1\n",
                )
            finally:
                isolated_root = snapshot.repository.parent
                isolation.close()
            self.assertFalse(isolated_root.exists())

    def test_keep_preserves_environment_for_explicit_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            isolation = IsolatedRepository(
                self.source(root),
                keep=True,
                temp_parent=root,
            )
            snapshot = isolation.create()
            isolated_root = snapshot.repository.parent
            isolation.close()
            self.assertTrue(isolated_root.exists())

    def test_invalid_source_fails_without_checkout_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing"
            with self.assertRaises(IsolatedEnvironmentError) as caught:
                IsolatedRepository(missing).create()
            self.assertEqual(caught.exception.code, "invalid_source_repository")

    def test_symlink_source_is_rejected_when_supported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.source(root)
            link = source / "linked.py"
            try:
                link.symlink_to(source / "app.py")
            except (OSError, NotImplementedError):
                self.skipTest("symlinks unavailable")
            with self.assertRaises(IsolatedEnvironmentError) as caught:
                IsolatedRepository(source, temp_parent=root).create()
            self.assertEqual(caught.exception.code, "source_contains_symlink")

    def test_create_is_single_use(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            isolation = IsolatedRepository(self.source(root), temp_parent=root)
            isolation.create()
            try:
                with self.assertRaises(IsolatedEnvironmentError):
                    isolation.create()
            finally:
                isolation.close()

    def test_temporary_parent_cannot_be_inside_source(self):
        with tempfile.TemporaryDirectory() as directory:
            source = self.source(Path(directory))
            with self.assertRaises(IsolatedEnvironmentError):
                IsolatedRepository(source, temp_parent=source)

    def test_exclusions_are_case_insensitive_and_recursive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            variants = (
                ".GIT",
                ".Git",
                "Project",
                "PROJECT",
                "VENV",
                "__PYCACHE__",
                "nested/.GIT",
                "nested/PROJECT",
            )
            for index, relative in enumerate(variants):
                case_root = root / f"case-{index}"
                source = case_root / "source"
                source.mkdir(parents=True)
                (source / "app.py").write_text("value = 1\n", encoding="utf-8")
                path = source / relative
                path.mkdir(parents=True)
                (path / "forbidden.txt").write_text("x", encoding="utf-8")
                isolation = IsolatedRepository(source, temp_parent=case_root)
                snapshot = isolation.create()
                try:
                    self.assertFalse(
                        (snapshot.repository / relative / "forbidden.txt").exists()
                    )
                finally:
                    isolation.close()

    def test_hostile_global_hooks_templates_and_git_environment_are_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.source(root)
            marker = root / "HOOK_EXECUTED"
            hooks = root / "host-hooks"
            template_hooks = root / "host-template" / "hooks"
            hooks.mkdir()
            template_hooks.mkdir(parents=True)
            hook_body = (
                "#!/bin/sh\n"
                f"printf executed > '{marker.as_posix()}'\n"
                "exit 1\n"
            )
            for hook in (
                hooks / "pre-commit",
                template_hooks / "pre-commit",
            ):
                hook.write_text(hook_body, encoding="utf-8", newline="\n")
                hook.chmod(hook.stat().st_mode | stat.S_IEXEC)
            global_config = root / "host-global.gitconfig"
            system_config = root / "host-system.gitconfig"
            global_config.write_text(
                "[core]\n"
                f"\thooksPath = {hooks.as_posix()}\n"
                "[commit]\n\tgpgSign = true\n",
                encoding="utf-8",
            )
            system_config.write_text(
                "[init]\n"
                f"\ttemplateDir = {(root / 'host-template').as_posix()}\n",
                encoding="utf-8",
            )
            hostile_environment = {
                "GIT_CONFIG_GLOBAL": str(global_config),
                "GIT_CONFIG_SYSTEM": str(system_config),
                "GIT_TEMPLATE_DIR": str(root / "host-template"),
                "GIT_DIR": str(root / "not-a-repository"),
                "GIT_INDEX_FILE": str(root / "host-index"),
                "GIT_WORK_TREE": str(source),
            }
            with mock.patch.dict(os.environ, hostile_environment):
                isolation = IsolatedRepository(source, temp_parent=root)
                snapshot = isolation.create()
            try:
                self.assertFalse(marker.exists())
                self.assertEqual(snapshot.initial_commit_count, 1)
                self.assertEqual(
                    subprocess.run(
                        ["git", "status", "--porcelain"],
                        cwd=snapshot.repository,
                        text=True,
                        capture_output=True,
                        check=True,
                    ).stdout,
                    "",
                )
            finally:
                isolation.close()

    def test_source_directory_symlink_and_broken_link_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.source(root)
            directory_link = root / "source-link"
            broken_link = root / "broken-link"
            try:
                directory_link.symlink_to(source, target_is_directory=True)
                broken_link.symlink_to(
                    root / "missing-source",
                    target_is_directory=True,
                )
            except (OSError, NotImplementedError):
                self.skipTest("symlinks unavailable")
            for candidate in (directory_link, broken_link):
                with self.subTest(candidate=candidate.name):
                    with self.assertRaises(IsolatedEnvironmentError) as caught:
                        IsolatedRepository(candidate, temp_parent=root)
                    self.assertEqual(
                        caught.exception.code,
                        "source_contains_symlink",
                    )

    def test_relative_source_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.source(root)
            link = root / "relative-link"
            try:
                link.symlink_to(Path("source"), target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks unavailable")
            with self.assertRaises(IsolatedEnvironmentError) as caught:
                IsolatedRepository(link, temp_parent=root)
            self.assertEqual(caught.exception.code, "source_contains_symlink")

    def test_broken_internal_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.source(root)
            try:
                (source / "broken.py").symlink_to(source / "missing.py")
            except (OSError, NotImplementedError):
                self.skipTest("symlinks unavailable")
            with self.assertRaises(IsolatedEnvironmentError) as caught:
                IsolatedRepository(source, temp_parent=root).create()
            self.assertEqual(caught.exception.code, "source_contains_symlink")

    def test_copy_and_git_failures_cleanup_partial_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.source(root)
            with mock.patch(
                "brain.isolated_environment.shutil.copy2",
                side_effect=OSError("sensitive copy failure"),
            ):
                with self.assertRaises(IsolatedEnvironmentError) as caught:
                    IsolatedRepository(source, temp_parent=root).create()
            self.assertNotIn("sensitive", str(caught.exception))
            self.assertEqual(
                [item for item in root.iterdir() if item.name.startswith("developerai-")],
                [],
            )

            original = IsolatedRepository._run_git
            for failing_command in (
                "init",
                "remote",
                "add",
                "commit",
                "rev-parse",
                "diff",
            ):
                with self.subTest(command=failing_command):
                    def fail_selected(repository, *arguments, **kwargs):
                        if failing_command in arguments:
                            raise IsolatedEnvironmentError(
                                "git_initialization_failed"
                            )
                        return original(repository, *arguments, **kwargs)

                    with mock.patch.object(
                        IsolatedRepository,
                        "_run_git",
                        side_effect=fail_selected,
                    ):
                        with self.assertRaises(IsolatedEnvironmentError):
                            IsolatedRepository(source, temp_parent=root).create()
                    self.assertEqual(
                        [
                            item
                            for item in root.iterdir()
                            if item.name.startswith("developerai-")
                        ],
                        [],
                    )

    def test_readonly_file_and_paths_with_spaces_are_supported(self):
        with tempfile.TemporaryDirectory(prefix="developer ai test ") as directory:
            root = Path(directory)
            source = self.source(root)
            readonly = source / "read only.txt"
            readonly.write_text("immutable source\n", encoding="utf-8")
            readonly.chmod(stat.S_IREAD)
            isolation = IsolatedRepository(source, temp_parent=root)
            snapshot = isolation.create()
            isolated_root = snapshot.repository.parent
            try:
                self.assertEqual(
                    (snapshot.repository / "read only.txt").read_text(
                        encoding="utf-8"
                    ),
                    "immutable source\n",
                )
            finally:
                isolation.close()
                readonly.chmod(stat.S_IWRITE)
            self.assertFalse(isolated_root.exists())


if __name__ == "__main__":
    unittest.main()
