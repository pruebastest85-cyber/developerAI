"""Fail-closed temporary Git workspace for controlled programming sessions."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


_EXCLUDED_NAMES = frozenset(
    name.casefold()
    for name in {".git", ".hg", ".svn", ".venv", "venv", "__pycache__", "project"}
)

class IsolatedEnvironmentError(RuntimeError):
    """Closed public failure raised before any programming workflow can start."""

    __slots__ = ("code",)

    def __init__(self, code: str = "isolated_environment_failed"):
        self.code = (
            code
            if code in {
                "invalid_source_repository",
                "isolated_environment_failed",
                "source_contains_symlink",
                "git_initialization_failed",
            }
            else "isolated_environment_failed"
        )
        super().__init__("No fue posible crear el entorno aislado.")


@dataclass(frozen=True)
class IsolatedRepositorySnapshot:
    repository: Path
    runtime_directory: Path
    baseline_commit: str
    initial_commit_count: int


class IsolatedRepository:
    """Own one temporary copy and never fall back to the source checkout."""

    def __init__(self, source_repository, *, keep=False, temp_parent=None):
        supplied_source = Path(source_repository).absolute()
        if self._is_link_like(supplied_source):
            raise IsolatedEnvironmentError("source_contains_symlink")
        try:
            source = supplied_source.resolve(strict=True)
        except (OSError, RuntimeError):
            raise IsolatedEnvironmentError("invalid_source_repository") from None
        if not source.is_dir():
            raise IsolatedEnvironmentError("invalid_source_repository")
        if type(keep) is not bool:
            raise IsolatedEnvironmentError()
        self.supplied_source = supplied_source
        self.source = source
        self.keep = keep
        self.temp_parent = (
            Path(temp_parent).resolve() if temp_parent is not None else None
        )
        if self.temp_parent is not None and (
            self.temp_parent == self.source
            or self.source in self.temp_parent.parents
        ):
            raise IsolatedEnvironmentError("invalid_source_repository")
        self._root: Path | None = None
        self._snapshot: IsolatedRepositorySnapshot | None = None

    @property
    def snapshot(self) -> IsolatedRepositorySnapshot:
        if self._snapshot is None:
            raise IsolatedEnvironmentError()
        return self._snapshot

    def create(self) -> IsolatedRepositorySnapshot:
        if self._root is not None:
            raise IsolatedEnvironmentError()
        try:
            root = Path(
                tempfile.mkdtemp(
                    prefix="developerai-",
                    dir=str(self.temp_parent) if self.temp_parent else None,
                )
            ).resolve()
            self._root = root
            repository = root / "repository"
            runtime = root / "runtime"
            runtime.mkdir()
            git_template = runtime / "git-template"
            git_hooks = runtime / "git-hooks"
            git_template.mkdir()
            git_hooks.mkdir()
            if (
                self._is_link_like(self.supplied_source)
                or self.supplied_source.resolve(strict=True) != self.source
            ):
                raise IsolatedEnvironmentError("source_contains_symlink")
            self._copy_source(repository)
            self._run_git(
                repository,
                "init",
                "-q",
                f"--template={git_template}",
                hooks_path=git_hooks,
            )
            self._run_git(
                repository,
                "config",
                "user.name",
                "DeveloperAI Isolated",
                hooks_path=git_hooks,
            )
            self._run_git(
                repository,
                "config",
                "user.email",
                "developerai-isolated@example.invalid",
                hooks_path=git_hooks,
            )
            self._run_git(
                repository,
                "remote",
                "remove",
                "origin",
                allow_failure=True,
                hooks_path=git_hooks,
            )
            self._run_git(repository, "add", "--all", hooks_path=git_hooks)
            self._run_git(
                repository,
                "commit",
                "-q",
                "-m",
                "isolated baseline",
                hooks_path=git_hooks,
            )
            baseline = self._git_text(
                repository, "rev-parse", "HEAD", hooks_path=git_hooks
            )
            count = int(
                self._git_text(
                    repository, "rev-list", "--count", "HEAD", hooks_path=git_hooks
                )
            )
            if (
                self._run_git(
                    repository,
                    "diff",
                    "--cached",
                    "--quiet",
                    hooks_path=git_hooks,
                ).returncode
                != 0
            ):
                raise IsolatedEnvironmentError("git_initialization_failed")
            if self._git_text(repository, "remote", hooks_path=git_hooks):
                raise IsolatedEnvironmentError("git_initialization_failed")
        except IsolatedEnvironmentError:
            if self._root is not None and not self.keep:
                self._remove_tree(self._root)
            raise
        except (OSError, subprocess.SubprocessError, ValueError):
            if self._root is not None and not self.keep:
                self._remove_tree(self._root)
            raise IsolatedEnvironmentError() from None
        self._snapshot = IsolatedRepositorySnapshot(
            repository=repository,
            runtime_directory=runtime,
            baseline_commit=baseline,
            initial_commit_count=count,
        )
        return self._snapshot

    def close(self) -> None:
        root = self._root
        self._snapshot = None
        self._root = None
        if root is not None and not self.keep:
            self._remove_tree(root)

    def __enter__(self) -> IsolatedRepositorySnapshot:
        return self.create()

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def _copy_source(self, destination: Path) -> None:
        for current, directories, files in os.walk(
            self.source, topdown=True, followlinks=False
        ):
            current_path = Path(current)
            relative = current_path.relative_to(self.source)
            kept_directories = []
            for name in directories:
                candidate = current_path / name
                if self._is_link_like(candidate):
                    raise IsolatedEnvironmentError("source_contains_symlink")
                if name.casefold() not in _EXCLUDED_NAMES:
                    kept_directories.append(name)
            directories[:] = kept_directories
            target_directory = destination / relative
            target_directory.mkdir(parents=True, exist_ok=True)
            for name in files:
                source_file = current_path / name
                if self._is_link_like(source_file):
                    raise IsolatedEnvironmentError("source_contains_symlink")
                shutil.copy2(source_file, target_directory / name)

    @staticmethod
    def _is_link_like(path: Path) -> bool:
        try:
            return path.is_symlink() or (
                hasattr(path, "is_junction") and path.is_junction()
            )
        except OSError:
            return True

    @staticmethod
    def _git_environment() -> dict[str, str]:
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.upper().startswith("GIT_")
        }
        environment.update(
            {
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_SYSTEM": os.devnull,
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_PAGER": "cat",
            }
        )
        return environment

    @classmethod
    def _run_git(
        cls,
        repository: Path,
        *arguments,
        allow_failure=False,
        hooks_path: Path | None = None,
    ):
        safe_hooks = hooks_path or repository.parent / "git-hooks"
        result = subprocess.run(
            [
                "git",
                "--no-pager",
                "-c",
                f"core.hooksPath={safe_hooks}",
                "-c",
                "commit.gpgSign=false",
                "-c",
                "tag.gpgSign=false",
                *arguments,
            ],
            cwd=repository,
            env=cls._git_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if result.returncode and not allow_failure:
            raise IsolatedEnvironmentError("git_initialization_failed")
        return result

    @classmethod
    def _git_text(
        cls,
        repository: Path,
        *arguments,
        hooks_path: Path | None = None,
    ) -> str:
        return cls._run_git(
            repository, *arguments, hooks_path=hooks_path
        ).stdout.strip()

    @staticmethod
    def _remove_tree(root: Path) -> None:
        def make_writable(function, path, error):
            try:
                os.chmod(path, stat.S_IWRITE)
                function(path)
            except OSError:
                pass

        try:
            shutil.rmtree(root, onerror=make_writable)
        except OSError:
            # Cleanup is best-effort and must never replace the closed public
            # environment error or interrupt an operator shutdown.
            pass
