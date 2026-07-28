from pathlib import Path
from difflib import unified_diff

from tools.tool_result import ToolResult, execute_and_normalize, legacy_tool_value


class PatchGenerator:
    name = "patch_generator"
    def __init__(self, base_dir=None):
        self.base_dir = Path(base_dir or ".").resolve()

    def generate_patch(self, relative_path, old_content, new_content):
        path = (self.base_dir / relative_path).resolve()
        if not str(path).startswith(str(self.base_dir)):
            raise ValueError("Ruta fuera del directorio permitido")

        old_lines = old_content.splitlines()
        new_lines = new_content.splitlines()

        diff = list(unified_diff(
            old_lines,
            new_lines,
            fromfile=relative_path,
            tofile=f"{relative_path} (propuesto)",
            lineterm="",
        ))
        return "\n".join(diff)

    def generate_patch_from_file(self, relative_path, new_content):
        path = (self.base_dir / relative_path).resolve()
        if not str(path).startswith(str(self.base_dir)):
            raise ValueError("Ruta fuera del directorio permitido")
        if not path.exists():
            raise FileNotFoundError(f"No existe el archivo: {relative_path}")
        old_content = path.read_text(encoding="utf-8")
        return self.generate_patch(relative_path, old_content, new_content)

    def execute(self, args=None, structured=False):
        if (
            not isinstance(args, dict)
            or not isinstance(args.get("path"), str)
            or not isinstance(args.get("new_content"), str)
        ):
            result = ToolResult.failure(
                self.name, error="path y new_content deben ser cadenas"
            )
            return result if structured else legacy_tool_value(result)
        result = execute_and_normalize(
            self.name,
            lambda: self.generate_patch_from_file(
                args["path"],
                args["new_content"],
            ),
            operational_exceptions=(OSError, UnicodeError),
        )
        return result if structured else legacy_tool_value(result)
