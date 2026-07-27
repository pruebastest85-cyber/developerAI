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


if __name__ == "__main__":
    unittest.main()
