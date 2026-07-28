import ast
from pathlib import Path
from typing import List, Dict, Any

from tools.tool_result import ToolResult, execute_and_normalize, legacy_tool_value


class CodeAnalyzer:
    name = "code_analyzer"
    risk = "low"

    def __init__(self, base_dir=None):
        self.base_dir = Path(base_dir or ".").resolve()

    def analyze_file(self, relative_path: str) -> Dict[str, Any]:
        path = (self.base_dir / relative_path).resolve()
        if not str(path).startswith(str(self.base_dir)):
            raise ValueError("Ruta fuera del directorio permitido")
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(f"No existe el archivo: {relative_path}")

        source = path.read_text(encoding="utf-8")
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

    def summarize(self, relative_path: str) -> str:
        analysis = self.analyze_file(relative_path)
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
        result = execute_and_normalize(
            self.name,
            lambda: self.summarize(args["path"]),
            operational_exceptions=(OSError, UnicodeError, SyntaxError),
        )
        return result if structured else legacy_tool_value(result)
