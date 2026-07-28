from pathlib import Path

from brain.path_policy import PathPolicy, PathValidationError
from tools.tool_result import ToolResult, execute_and_normalize, legacy_tool_value


class CodeReader:
    name = "code_reader"
    risk = "low"

    def __init__(self, base_dir=None):
        self.base_dir = Path(base_dir or ".").resolve()
        self.path_policy = PathPolicy(self.base_dir)

    def read_file(self, relative_path):
        path = self.path_policy.resolve_for_read(relative_path).absolute
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(f"No existe el archivo: {relative_path}")
        return path.read_text(encoding="utf-8")

    def read_file_with_limit(self, relative_path, max_lines=80):
        content = self.read_file(relative_path)
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
        result = execute_and_normalize(
            self.name,
            lambda: self.read_file_with_limit(
                args["path"],
                max_lines,
            ),
            operational_exceptions=(OSError, UnicodeError, PathValidationError),
        )
        return result if structured else legacy_tool_value(result)
