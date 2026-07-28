import subprocess
import sys
from pathlib import Path

from tools.tool_result import ToolResult, legacy_tool_value, normalize_tool_result


class TestRunner:
    name = "test_runner"
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
        return self._format_report(result)

    def execute(self, args=None, structured=False):
        payload = args or {}
        command = payload.get("command") if isinstance(payload, dict) else None
        try:
            raw = self.run_tests(command=command)
        except OSError as exc:
            result = ToolResult.failure(
                self.name,
                error=str(exc),
                metadata={"exception_type": type(exc).__name__},
            )
            return result if structured else legacy_tool_value(result)
        result = normalize_tool_result(raw, tool_name=self.name)
        message = self._format_report(raw)
        if result.status == "ok":
            result = ToolResult.success(
                self.name,
                data=raw,
                message=message,
            )
        else:
            result = ToolResult.failure(
                self.name,
            error=raw["stderr"].strip() or f"Tests finalizaron con código {raw['returncode']}",
                message=message,
                data=raw,
                retryable=True,
            )
        return result if structured else legacy_tool_value(result)

    def _format_report(self, result):
        report = []
        report.append(f"Comando: {result['command']}")
        report.append(f"Código de salida: {result['returncode']}")
        report.append("Resultado: OK ✅" if result["ok"] else "Resultado: FALLÓ ❌")
        if result["stdout"].strip():
            report.append("Salida:\n" + result["stdout"].strip())
        if result["stderr"].strip():
            report.append("Errores:\n" + result["stderr"].strip())
        return "\n\n".join(report)
