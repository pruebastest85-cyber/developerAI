import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from brain.workflow_diff import WorkflowDiffCollector


def git(path, *args):
    return subprocess.run(
        ["git", *args], cwd=path, capture_output=True, text=True, check=True
    )


class WorkflowDiffCollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        git(self.root, "init")
        git(self.root, "config", "user.email", "test@example.invalid")
        git(self.root, "config", "user.name", "Test")
        (self.root / "tracked.txt").write_text("old\n", encoding="utf-8")
        git(self.root, "add", "tracked.txt")
        git(self.root, "commit", "-m", "base")

    def tearDown(self):
        self.temp.cleanup()

    def test_captures_tracked_and_untracked_without_staging(self):
        (self.root / "tracked.txt").write_text("new\n", encoding="utf-8")
        (self.root / "new.txt").write_text("á\n", encoding="utf-8")
        before = git(self.root, "diff", "--cached", "--name-only").stdout
        result = WorkflowDiffCollector(self.root).capture(
            ["tracked.txt", "new.txt"], max_bytes=10000
        )
        self.assertTrue(result.available)
        self.assertEqual(("new.txt", "tracked.txt"), tuple(item.path for item in result.files))
        self.assertIn("b/new.txt", result.text)
        self.assertEqual(before, git(self.root, "diff", "--cached", "--name-only").stdout)

    def test_binary_and_utf8_byte_truncation(self):
        (self.root / "binary.bin").write_bytes(b"\0abc")
        (self.root / "new.txt").write_text("é" * 100 + "\n", encoding="utf-8")
        result = WorkflowDiffCollector(self.root).capture(
            ["binary.bin", "new.txt"], max_bytes=80
        )
        self.assertLessEqual(len(result.text.encode("utf-8")), 80)
        self.assertTrue(result.truncated)
        self.assertIn("binary.bin", result.binary_files)

    def test_rejects_escape_and_parent_repository(self):
        rejected = WorkflowDiffCollector(self.root).capture(["../outside"], max_bytes=10)
        self.assertFalse(rejected.available)
        self.assertEqual("path_rejected", rejected.error_code)

        child = self.root / "child"
        child.mkdir()
        parent = WorkflowDiffCollector(child).capture([], max_bytes=10)
        self.assertFalse(parent.available)
        self.assertEqual("not_git_repository", parent.error_code)

    def test_git_failure_is_reported_without_failing_workflow(self):
        def missing(*args, **kwargs):
            raise FileNotFoundError("git missing")
        result = WorkflowDiffCollector(self.root, runner=missing).capture([], max_bytes=10)
        self.assertEqual("git_unavailable", result.error_code)

    def test_ls_files_git_error_is_not_mistaken_for_untracked_file(self):
        (self.root / "owned.txt").write_text("data\n", encoding="utf-8")

        def failing(args, **kwargs):
            if args[1:3] == ["rev-parse", "--show-toplevel"]:
                return SimpleNamespace(returncode=0, stdout=str(self.root), stderr="")
            return SimpleNamespace(returncode=128, stdout="", stderr="fatal: simulated")

        result = WorkflowDiffCollector(self.root, runner=failing).capture(
            ["owned.txt"], max_bytes=100
        )
        self.assertFalse(result.available)
        self.assertEqual("git_failed", result.error_code)

    def test_oversized_new_file_has_no_partial_statistics(self):
        (self.root / "large.txt").write_text("é" * 100, encoding="utf-8")
        result = WorkflowDiffCollector(self.root).capture(["large.txt"], max_bytes=80)
        self.assertTrue(result.truncated)
        self.assertEqual(("large.txt",), result.omitted_paths)
        self.assertIsNone(result.files[0].insertions)


if __name__ == "__main__":
    unittest.main()
