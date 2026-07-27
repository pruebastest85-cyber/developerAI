import subprocess
from pathlib import Path


class GitTools:
    def __init__(self, base_dir=None):
        self.base_dir = Path(base_dir or ".").resolve()

    def run(self, command):
        completed = subprocess.run(
            command,
            cwd=str(self.base_dir),
            capture_output=True,
            text=True,
        )
        return {
            "command": " ".join(command),
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "ok": completed.returncode == 0,
        }

    def status(self):
        return self.run(["git", "status", "--short"])

    def checkpoint(self, message="Checkpoint before AI modification"):
        add_result = self.run(["git", "add", "."])
        if not add_result["ok"]:
            return add_result

        return self.run(["git", "commit", "-m", message])

    def rollback(self):
        return self.run(["git", "reset", "--hard", "HEAD"])
