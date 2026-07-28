import tempfile
import unittest
from pathlib import Path

from tools.git_tools import GitTools


class GitToolsTests(unittest.TestCase):
    def test_git_status_returns_structured_result(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            (repo / "file.txt").write_text("hola", encoding="utf-8")
            subprocess = __import__("subprocess")
            subprocess.run(["git", "init"], cwd=str(repo), capture_output=True, text=True, check=False)

            tools = GitTools(base_dir=repo)
            result = tools.status()

            self.assertIn("command", result)
            self.assertIn("returncode", result)
            self.assertIn("stdout", result)
            self.assertIn("ok", result)

    def test_execute_returns_failed_tool_result_for_nonzero_exit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tools = GitTools(base_dir=tmpdir)
            tools.status = lambda: {
                "command": "git status --short",
                "returncode": 128,
                "stdout": "",
                "stderr": "not a repository",
                "ok": False,
            }

            result = tools.execute({"action": "status"}, structured=True)

            self.assertEqual(result.status, "failed")
            self.assertEqual(result.data["returncode"], 128)
            self.assertEqual(result.error, "not a repository")


if __name__ == "__main__":
    unittest.main()
