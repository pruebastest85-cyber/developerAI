from pathlib import Path


class CodeReader:
    risk = "low"

    def __init__(self, base_dir=None):
        self.base_dir = Path(base_dir or ".").resolve()

    def read_file(self, relative_path):
        path = (self.base_dir / relative_path).resolve()
        if not str(path).startswith(str(self.base_dir)):
            raise ValueError("Ruta fuera del directorio permitido")
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(f"No existe el archivo: {relative_path}")
        return path.read_text(encoding="utf-8")

    def read_file_with_limit(self, relative_path, max_lines=80):
        content = self.read_file(relative_path)
        lines = content.splitlines()
        if len(lines) > max_lines:
            return "\n".join(lines[:max_lines]) + "\n..."
        return content
