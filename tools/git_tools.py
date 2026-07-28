import subprocess
from pathlib import Path

from tools.tool_result import ToolResult, legacy_tool_value, normalize_tool_result


class GitTools:
    name = "git_tools"
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

    def execute(self, args=None, structured=False):
        payload = args or {}
        action = payload.get("action", "status")
        try:
            if action == "status":
                raw = self.status()
            elif action == "checkpoint":
                raw = self.checkpoint(payload.get("message", "Checkpoint before AI modification"))
            elif action == "rollback":
                raw = self.rollback()
            else:
                result = ToolResult.failure(
                    self.name,
                    error=f"Acción Git no soportada: {action}",
                )
                return result if structured else legacy_tool_value(result)
        except OSError as exc:
            result = ToolResult.failure(
                self.name,
                error=str(exc),
                metadata={"exception_type": type(exc).__name__},
            )
            return result if structured else legacy_tool_value(result)

        result = normalize_tool_result(raw, tool_name=self.name)
        message = self.format_result(raw)
        if result.status == "ok":
            result = ToolResult.success(self.name, data=raw, message=message)
        else:
            result = ToolResult.failure(
            self.name,
            error=raw["stderr"].strip() or f"Git finalizó con código {raw['returncode']}",
            message=message,
                data=raw,
            )
        return result if structured else legacy_tool_value(result)

    @staticmethod
    def format_result(result):
        parts = [f"Comando: {result['command']}", f"Código: {result['returncode']}"]
        if result["stdout"].strip():
            parts.append("Salida:\n" + result["stdout"].strip())
        if result["stderr"].strip():
            parts.append("Errores:\n" + result["stderr"].strip())
        return "\n\n".join(parts)
