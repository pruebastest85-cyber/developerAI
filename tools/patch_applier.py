import os
import shutil
import tempfile
from pathlib import Path


MAX_CONTENT_BYTES = 1024 * 1024


class PatchApplier:
    def __init__(self, base_dir=None):
        self.base_dir = Path(base_dir or ".").resolve()

    def _resolve_existing_file(self, relative_path):
        if not isinstance(relative_path, str):
            raise TypeError("La ruta debe ser una cadena de texto")

        if not relative_path.strip():
            raise ValueError("La ruta no puede estar vacía")

        candidate = Path(relative_path)
        if candidate.is_absolute() or candidate.drive:
            raise ValueError("Ruta fuera del directorio permitido")

        if any(part == ".." for part in candidate.parts):
            raise ValueError("Ruta fuera del directorio permitido")

        if any(part == ".git" for part in candidate.parts):
            raise ValueError("Ruta dentro de .git no permitida")

        current = self.base_dir
        for part in candidate.parts[:-1]:
            current = current / part
            if not current.exists():
                raise FileNotFoundError(f"No existe el directorio padre: {relative_path}")
            if current.is_symlink():
                raise ValueError("No se permiten symlinks en la ruta")
            if not current.is_dir():
                raise NotADirectoryError(f"No es un directorio: {relative_path}")

        target = current / candidate.parts[-1]
        if not target.exists():
            raise FileNotFoundError(f"No existe el archivo: {relative_path}")
        if target.is_symlink():
            raise ValueError("No se permiten symlinks en la ruta")

        resolved = target.resolve(strict=True)
        try:
            resolved.relative_to(self.base_dir)
        except ValueError as exc:
            raise ValueError("Ruta fuera del directorio permitido") from exc

        if not resolved.is_file():
            raise IsADirectoryError(f"No es un archivo: {relative_path}")

        return resolved

    def _encode_and_validate_content(self, content, label):
        if not isinstance(content, str):
            raise TypeError(f"{label} debe ser una cadena de texto")

        encoded = content.encode("utf-8")
        if len(encoded) > MAX_CONTENT_BYTES:
            raise ValueError("El contenido supera el límite de 1 MiB UTF-8")
        return encoded

    def apply_patch(self, relative_path, old_content, new_content):
        path = self._resolve_existing_file(relative_path)
        old_bytes = self._encode_and_validate_content(old_content, "old_content")
        new_bytes = self._encode_and_validate_content(new_content, "new_content")

        current_bytes = path.read_bytes()
        if current_bytes != old_bytes:
            raise ValueError("El contenido actual del archivo no coincide con el esperado para aplicar el parche")

        backup_path = path.with_suffix(path.suffix + ".backup")
        if backup_path.is_symlink():
            raise ValueError("La ruta de backup no puede ser un symlink")
        shutil.copy2(path, backup_path)

        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="",
                delete=False,
                dir=str(path.parent),
                prefix=f".{path.name}.",
                suffix=".tmp",
            ) as temp_handle:
                temp_path = Path(temp_handle.name)
                temp_handle.write(new_content)
                temp_handle.flush()
                os.fsync(temp_handle.fileno())

            try:
                shutil.copymode(path, temp_path, follow_symlinks=False)
            except OSError:
                pass

            os.replace(temp_path, path)
        except Exception:
            if temp_path is not None and temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass
            raise

        return {
            "archivo": relative_path,
            "actualizado": True,
            "backup": str(backup_path).replace("\\", "/"),
            "bytes": len(new_bytes),
            "aplicado": True,
        }
