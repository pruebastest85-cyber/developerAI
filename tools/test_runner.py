import re
import subprocess
import sys
from pathlib import Path

from brain.change_proposal import TestSpec
from tools.tool_result import ToolResult, legacy_tool_value, normalize_tool_result


class TestRunner:
    name = "test_runner"

    def __init__(self, base_dir=None):
        self.base_dir = Path(base_dir or ".").resolve()

    @staticmethod
    def _validate_timeout(timeout):
        if timeout is None:
            return None
        if isinstance(timeout, bool) or not isinstance(timeout, int):
            raise TypeError("timeout debe ser un entero")
        if timeout <= 0:
            raise ValueError("timeout debe ser mayor que cero")
        return timeout

    def run_tests(self, command=None, timeout=None):
        if command is None:
            command = [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"]
        timeout = self._validate_timeout(timeout)

        completed = subprocess.run(
            command,
            cwd=str(self.base_dir),
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        result = {
            "command": " ".join(command),
            "command_args": list(command),
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "ok": completed.returncode == 0,
            "timed_out": False,
        }
        result.update(self._parse_unittest_output(completed.stdout, completed.stderr))
        return result

    def run_test_spec(self, test_spec: TestSpec, timeout=None):
        if not isinstance(test_spec, TestSpec):
            raise TypeError("test_spec debe ser TestSpec")
        command = list(test_spec.canonical_command(sys.executable))
        return self.run_tests(command=command, timeout=timeout)

    def run_tests_report(self, command=None, timeout=None):
        result = self.run_tests(command=command, timeout=timeout)
        return self._format_report(result)

    def execute(self, args=None, structured=False):
        payload = args or {}
        command = payload.get("command") if isinstance(payload, dict) else None
        test_spec = payload.get("test_spec") if isinstance(payload, dict) else None
        timeout = payload.get("timeout") if isinstance(payload, dict) else None
        try:
            self._validate_timeout(timeout)
        except (TypeError, ValueError) as exc:
            result = ToolResult.failure(self.name, error=str(exc))
            return result if structured else legacy_tool_value(result)
        if test_spec is not None and command is not None:
            result = ToolResult.failure(
                self.name,
                error="No se puede combinar test_spec con command",
            )
            return result if structured else legacy_tool_value(result)
        if test_spec is not None and not isinstance(test_spec, TestSpec):
            result = ToolResult.failure(
                self.name,
                error="test_spec debe ser TestSpec",
            )
            return result if structured else legacy_tool_value(result)
        try:
            raw = (
                self.run_test_spec(test_spec, timeout=timeout)
                if test_spec is not None
                else self.run_tests(command=command, timeout=timeout)
            )
        except subprocess.TimeoutExpired as exc:
            command_args = (
                list(test_spec.canonical_command(sys.executable))
                if isinstance(test_spec, TestSpec)
                else list(exc.cmd)
            )
            raw = {
                "command": " ".join(command_args),
                "command_args": command_args,
                "returncode": None,
                "stdout": self._coerce_timeout_output(exc.stdout),
                "stderr": self._coerce_timeout_output(exc.stderr),
                "ok": False,
                "timed_out": True,
                "tests_run": 0,
                "failures": 0,
                "errors": 0,
                "skipped": 0,
                "failed_test_ids": [],
                "error_test_ids": [],
            }
            result = ToolResult.failure(
                self.name,
                error=f"Las pruebas excedieron el timeout de {timeout} segundos",
                data=raw,
                metadata={"reason": "timeout"},
                retryable=True,
            )
            return result if structured else legacy_tool_value(result)
        except OSError as exc:
            result = ToolResult.failure(
                self.name,
                error=str(exc),
                metadata={"exception_type": type(exc).__name__},
            )
            return result if structured else legacy_tool_value(result)
        parsed = self._parse_unittest_output(raw.get("stdout", ""), raw.get("stderr", ""))
        for key, value in parsed.items():
            raw.setdefault(key, value)
        raw.setdefault("timed_out", False)
        if "command_args" not in raw:
            raw["command_args"] = (
                list(command) if isinstance(command, (list, tuple)) else []
            )
        result = normalize_tool_result(raw, tool_name=self.name)
        message = self._format_report(raw)
        if result.status == "ok" and raw["tests_run"] > 0:
            result = ToolResult.success(
                self.name,
                data=raw,
                message=message,
            )
        elif result.status == "ok":
            result = ToolResult.failure(
                self.name,
                error="La ejecución no descubrió pruebas",
                message=message,
                data=raw,
                metadata={"reason": "zero_tests"},
                retryable=False,
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

    @staticmethod
    def _coerce_timeout_output(value):
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return str(value)

    @staticmethod
    def _parse_unittest_output(stdout, stderr):
        combined = "\n".join((stdout or "", stderr or ""))
        run_match = re.search(r"\bRan\s+(\d+)\s+tests?\b", combined)
        tests_run = int(run_match.group(1)) if run_match else 0
        summary_match = re.search(r"FAILED\s*\(([^)]*)\)", combined)
        counts = {"failures": 0, "errors": 0, "skipped": 0}
        if summary_match:
            for name, value in re.findall(
                r"(failures|errors|skipped)=(\d+)",
                summary_match.group(1),
            ):
                counts[name] = int(value)
        else:
            skipped_match = re.search(r"OK\s*\(skipped=(\d+)\)", combined)
            if skipped_match:
                counts["skipped"] = int(skipped_match.group(1))

        failed_ids = []
        error_ids = []
        for kind, label, qualified in re.findall(
            r"^(FAIL|ERROR):\s+([^\s(]+)(?:\s+\(([^)]+)\))?",
            combined,
            flags=re.MULTILINE,
        ):
            test_id = qualified or label
            target = failed_ids if kind == "FAIL" else error_ids
            target.append(test_id)
        return {
            "tests_run": tests_run,
            "failures": counts["failures"],
            "errors": counts["errors"],
            "skipped": counts["skipped"],
            "failed_test_ids": sorted(set(failed_ids)),
            "error_test_ids": sorted(set(error_ids)),
        }

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
