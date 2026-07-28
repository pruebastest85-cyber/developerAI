from pathlib import Path

from tools.base_tool import Tool
from tools.tool_result import ToolResult, execute_and_normalize, legacy_tool_value


class FileCreator(Tool):
    name = "file_creator"
    description = "Crea archivos nuevos de forma segura"
    requires_confirmation = True
    risk = "high"

    def __init__(self, base_dir=None):
        self.base_dir = Path(base_dir or ".").resolve()

    def _resolve_target(self, relative_path):
        if not isinstance(relative_path, str):
            raise TypeError("La ruta debe ser una cadena de texto")

        if not relative_path.strip():
            raise ValueError("La ruta no puede estar vacía")

        candidate = Path(relative_path)
        if candidate.is_absolute() or candidate.drive:
            raise ValueError("No se permiten rutas absolutas")

        if any(part == ".." for part in candidate.parts):
            raise ValueError("No se permiten rutas fuera del directorio base")

        if any(part == ".git" for part in candidate.parts):
            raise ValueError("No se permiten rutas dentro de .git")

        resolved = (self.base_dir / candidate).resolve()
        if resolved != self.base_dir and self.base_dir not in resolved.parents:
            raise ValueError("No se permiten rutas fuera del directorio base")

        if resolved.exists():
            raise FileExistsError(f"El archivo ya existe: {relative_path}")

        if not resolved.parent.exists() or not resolved.parent.is_dir():
            raise FileNotFoundError(f"No existe el directorio padre: {relative_path}")

        return resolved

    def create_file(self, relative_path, content):
        target = self._resolve_target(relative_path)
        content_bytes = content.encode("utf-8")
        if len(content_bytes) > 65536:
            raise ValueError("El contenido supera el límite de 65536 bytes UTF-8")

        with target.open("x", encoding="utf-8") as handle:
            handle.write(content)

        return {
            "archivo": Path(relative_path).as_posix(),
            "creado": True,
            "bytes": len(content_bytes),
        }

    def execute(self, args=None, structured=False):
        if not isinstance(args, dict):
            result = ToolResult.failure(self.name, error="Los argumentos deben ser un diccionario")
            return result if structured else legacy_tool_value(result)
        if not isinstance(args.get("path"), str) or not isinstance(args.get("content"), str):
            result = ToolResult.failure(self.name, error="path y content deben ser cadenas")
            return result if structured else legacy_tool_value(result)
        result = execute_and_normalize(
            self.name,
            lambda: self.create_file(args["path"], args["content"]),
            operational_exceptions=(OSError, UnicodeError),
        )
        return result if structured else legacy_tool_value(result)
