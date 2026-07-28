import ast
from pathlib import Path
from typing import List, Dict, Any

from brain.path_policy import PathPolicy, PathValidationError
from tools.code_reader import ReadLimitExceededError
from tools.tool_result import ToolResult, execute_and_normalize, legacy_tool_value


class CodeAnalyzer:
    name = "code_analyzer"
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

    def analyze_file(
        self,
        relative_path: str,
        max_read_bytes_per_file=None,
    ) -> Dict[str, Any]:
        path = self.path_policy.resolve_for_read(relative_path).absolute
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(f"No existe el archivo: {relative_path}")

        limit = self._validate_max_bytes(max_read_bytes_per_file)
        if limit is None:
            source = path.read_text(encoding="utf-8")
        else:
            with path.open("rb") as handle:
                payload = handle.read(limit + 1)
            if len(payload) > limit:
                raise ReadLimitExceededError(
                    "El archivo supera max_read_bytes_per_file"
                )
            source = payload.decode("utf-8")
        tree = ast.parse(source, filename=str(path))

        functions = []
        classes = []
        imports = []

        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                functions.append(node.name)
            elif isinstance(node, ast.ClassDef):
                classes.append(node.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for alias in node.names:
                    imports.append(f"{module}.{alias.name}" if module else alias.name)

        return {
            "archivo": relative_path,
            "lineas": len(source.splitlines()),
            "funciones": functions,
            "clases": classes,
            "imports": imports,
        }

    def summarize(self, relative_path: str, max_read_bytes_per_file=None) -> str:
        analysis = self.analyze_file(
            relative_path,
            max_read_bytes_per_file=max_read_bytes_per_file,
        )
        parts = [f"Archivo: {analysis['archivo']}", f"Líneas: {analysis['lineas']}"]
        if analysis["funciones"]:
            parts.append("Funciones: " + ", ".join(analysis["funciones"]))
        if analysis["clases"]:
            parts.append("Clases: " + ", ".join(analysis["clases"]))
        if analysis["imports"]:
            parts.append("Imports: " + ", ".join(analysis["imports"][:10]))
        return "\n".join(parts)

    def execute(self, args=None, structured=False):
        if not isinstance(args, dict) or not isinstance(args.get("path"), str):
            result = ToolResult.failure(self.name, error="path debe ser una cadena")
            return result if structured else legacy_tool_value(result)
        max_bytes = args.get("max_read_bytes_per_file")
        try:
            self._validate_max_bytes(max_bytes)
        except (TypeError, ValueError) as exc:
            result = ToolResult.failure(self.name, error=str(exc))
            return result if structured else legacy_tool_value(result)
        result = execute_and_normalize(
            self.name,
            lambda: self.summarize(
                args["path"],
                max_read_bytes_per_file=max_bytes,
            ),
            operational_exceptions=(
                OSError,
                UnicodeError,
                SyntaxError,
                PathValidationError,
                ReadLimitExceededError,
            ),
        )
        return result if structured else legacy_tool_value(result)
