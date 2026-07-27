import subprocess
import sys
from pathlib import Path


class TestRunner:
    def __init__(self, base_dir=None):
        self.base_dir = Path(base_dir or ".").resolve()

    def run_tests(self, command=None):
        if command is None:
            command = [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"]

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

    def run_tests_report(self, command=None):
        result = self.run_tests(command=command)
        report = []
        report.append(f"Comando: {result['command']}")
        report.append(f"Código de salida: {result['returncode']}")
        report.append("Resultado: OK ✅" if result["ok"] else "Resultado: FALLÓ ❌")

        if result["stdout"].strip():
            report.append("Salida:\n" + result["stdout"].strip())
        if result["stderr"].strip():
            report.append("Errores:\n" + result["stderr"].strip())

        return "\n\n".join(report)
