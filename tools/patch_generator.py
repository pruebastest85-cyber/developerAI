from pathlib import Path
from difflib import unified_diff


class PatchGenerator:
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
