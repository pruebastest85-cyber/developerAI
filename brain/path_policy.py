from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath


DEFAULT_FORBIDDEN_COMPONENTS = frozenset(
    {".git", "project", ".venv", "venv", "__pycache__"}
)
DEFAULT_SECRET_NAMES = frozenset(
    {".env", ".env.local", "credentials.json", "secrets.json"}
)
DEFAULT_BACKUP_SUFFIXES = (".backup", ".bak", "~")


class PathValidationError(ValueError):
    """A requested path violates the configured workspace confinement policy."""


@dataclass(frozen=True)
class ResolvedProjectPath:
    relative: Path
    absolute: Path


class PathPolicy:
    """Validate project-relative paths without changing the filesystem.

    Reads may traverse symlinks only when the fully resolved target remains
    inside ``base_dir``. Writes reject every existing symlink component,
    including a symlink at the final path.
    """

    def __init__(
        self,
        base_dir=None,
        *,
        forbidden_components=None,
        secret_names=None,
        backup_suffixes=None,
    ):
        self.base_dir = Path(base_dir or ".").resolve()
        self.forbidden_components = frozenset(
            item.casefold()
            for item in (
                DEFAULT_FORBIDDEN_COMPONENTS
                if forbidden_components is None
                else forbidden_components
            )
        )
        self.secret_names = frozenset(
            item.casefold()
            for item in (
                DEFAULT_SECRET_NAMES if secret_names is None else secret_names
            )
        )
        self.backup_suffixes = tuple(
            DEFAULT_BACKUP_SUFFIXES
            if backup_suffixes is None
            else backup_suffixes
        )

    def resolve_for_read(self, relative_path) -> ResolvedProjectPath:
        return self._resolve(relative_path, for_write=False)

    def resolve_for_write(self, relative_path) -> ResolvedProjectPath:
        return self._resolve(relative_path, for_write=True)

    def _resolve(self, relative_path, *, for_write: bool) -> ResolvedProjectPath:
        text = self._coerce_path(relative_path)
        windows = PureWindowsPath(text)
        posix = PurePosixPath(text.replace("\\", "/"))

        if windows.is_absolute() or windows.drive or windows.root:
            raise PathValidationError("No se permiten rutas absolutas o unidades Windows")
        if posix.is_absolute() or text.startswith(("\\\\", "//")):
            raise PathValidationError("No se permiten rutas absolutas o UNC")

        parts = tuple(part for part in posix.parts if part not in ("", "."))
        if not parts:
            raise PathValidationError("La ruta no puede estar vacía")
        if ".." in parts:
            raise PathValidationError("No se permiten componentes ..")

        folded_parts = tuple(part.casefold() for part in parts)
        forbidden = set(folded_parts).intersection(self.forbidden_components)
        if forbidden:
            raise PathValidationError(
                f"Componente de ruta prohibido: {sorted(forbidden)[0]}"
            )

        filename = folded_parts[-1]
        if filename in self.secret_names:
            raise PathValidationError(f"Archivo secreto prohibido: {parts[-1]}")
        if any(filename.endswith(suffix.casefold()) for suffix in self.backup_suffixes):
            raise PathValidationError(f"Sufijo de backup prohibido: {parts[-1]}")

        normalized = Path(*parts)
        unresolved = self.base_dir.joinpath(*parts)
        if for_write:
            self._reject_existing_symlinks(unresolved, parts)
        resolved = unresolved.resolve(strict=False)
        try:
            relative_resolved = resolved.relative_to(self.base_dir)
        except ValueError as exc:
            raise PathValidationError(
                "La ruta resuelta escapa del directorio base"
            ) from exc

        return ResolvedProjectPath(
            relative=Path(relative_resolved),
            absolute=resolved,
        )

    def _reject_existing_symlinks(self, unresolved: Path, parts: tuple[str, ...]) -> None:
        current = self.base_dir
        for part in parts:
            current = current / part
            if current.is_symlink():
                raise PathValidationError(
                    f"No se permiten symlinks para escritura: {part}"
                )

    @staticmethod
    def _coerce_path(value) -> str:
        if isinstance(value, os.PathLike):
            value = os.fspath(value)
        if not isinstance(value, str):
            raise PathValidationError("La ruta debe ser str u os.PathLike textual")
        if "\x00" in value:
            raise PathValidationError("La ruta contiene un byte nulo")
        return value.strip()
