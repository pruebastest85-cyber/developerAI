from pathlib import Path

from brain.path_policy import PathPolicy, PathValidationError
from tools.tool_result import ToolResult, execute_and_normalize, legacy_tool_value


class ReadLimitExceededError(ValueError):
    """A complete UTF-8 file exceeds an explicitly requested byte limit."""


class CodeReader:
    name = "code_reader"
    risk = "low"

    def __init__(self, base_dir=None):
        self.base_dir = Path(base_dir or ".").resolve()
        self.path_policy = PathPolicy(self.base_dir)

    @staticmethod
    def _validate_max_bytes(max_read_bytes_per_file):
        if max_read_bytes_per_file is None:
            return None
        if (
            isinstance(max_read_bytes_per_file, bool)
            or not isinstance(max_read_bytes_per_file, int)
        ):
            raise TypeError("max_read_bytes_per_file debe ser un entero")
        if max_read_bytes_per_file <= 0:
            raise ValueError("max_read_bytes_per_file debe ser mayor que cero")
        return max_read_bytes_per_file

    def read_file(self, relative_path, max_read_bytes_per_file=None):
        path = self.path_policy.resolve_for_read(relative_path).absolute
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(f"No existe el archivo: {relative_path}")
        limit = self._validate_max_bytes(max_read_bytes_per_file)
        if limit is not None:
            with path.open("rb") as handle:
                payload = handle.read(limit + 1)
            if len(payload) > limit:
                raise ReadLimitExceededError(
                    "El archivo supera max_read_bytes_per_file"
                )
            return payload.decode("utf-8")
        return path.read_text(encoding="utf-8")

    def read_file_with_limit(
        self,
        relative_path,
        max_lines=80,
        max_read_bytes_per_file=None,
    ):
        content = self.read_file(
            relative_path,
            max_read_bytes_per_file=max_read_bytes_per_file,
        )
        lines = content.splitlines()
        if len(lines) > max_lines:
            return "\n".join(lines[:max_lines]) + "\n..."
        return content

    def execute(self, args=None, structured=False):
        if not isinstance(args, dict) or not isinstance(args.get("path"), str):
            result = ToolResult.failure(self.name, error="path debe ser una cadena")
            return result if structured else legacy_tool_value(result)
        max_lines = args.get("max_lines", 80)
        if not isinstance(max_lines, int) or isinstance(max_lines, bool) or max_lines < 0:
            result = ToolResult.failure(self.name, error="max_lines debe ser un entero no negativo")
            return result if structured else legacy_tool_value(result)
        max_bytes = args.get("max_read_bytes_per_file")
        try:
            self._validate_max_bytes(max_bytes)
        except (TypeError, ValueError) as exc:
            result = ToolResult.failure(self.name, error=str(exc))
            return result if structured else legacy_tool_value(result)
        result = execute_and_normalize(
            self.name,
            lambda: self.read_file_with_limit(
                args["path"],
                max_lines,
                max_read_bytes_per_file=max_bytes,
            ),
            operational_exceptions=(
                OSError,
                UnicodeError,
                PathValidationError,
                ReadLimitExceededError,
            ),
        )
        return result if structured else legacy_tool_value(result)
