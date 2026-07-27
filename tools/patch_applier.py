import shutil
from pathlib import Path


class PatchApplier:
    def __init__(self, base_dir=None):
        self.base_dir = Path(base_dir or ".").resolve()

    def apply_patch(self, relative_path, old_content, new_content):
        path = (self.base_dir / relative_path).resolve()
        if not str(path).startswith(str(self.base_dir)):
            raise ValueError("Ruta fuera del directorio permitido")
        if not path.exists():
            raise FileNotFoundError(f"No existe el archivo: {relative_path}")

        current_content = path.read_text(encoding="utf-8")
        if current_content != old_content:
            raise ValueError("El contenido actual del archivo no coincide con el esperado para aplicar el parche")

        backup_path = path.with_suffix(path.suffix + ".backup")
        shutil.copy2(path, backup_path)

        path.write_text(new_content, encoding="utf-8")

        return {
            "archivo": relative_path,
            "backup": str(backup_path).replace("\\", "/"),
            "aplicado": True,
        }
