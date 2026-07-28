from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath


CONTRACT_VERSION = 1
VALID_OPERATIONS = frozenset({"replace", "create"})
VALID_TEST_SCOPES = frozenset({"focused", "full"})
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
TEST_ID_PATTERN = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+$"
)


class ChangeProposalStructureError(ValueError):
    """A change proposal has an invalid declarative structure."""


class TestSpecificationError(ChangeProposalStructureError):
    """A test specification is not one of the closed unittest forms."""


def normalize_relative_path(value: str) -> str:
    if not isinstance(value, str):
        raise ChangeProposalStructureError("path debe ser una cadena")
    text = value.strip()
    if not text or "\x00" in text:
        raise ChangeProposalStructureError("path debe ser una ruta no vacía")
    windows = PureWindowsPath(text)
    posix = PurePosixPath(text.replace("\\", "/"))
    if (
        windows.is_absolute()
        or windows.drive
        or windows.root
        or posix.is_absolute()
        or text.startswith(("\\\\", "//"))
    ):
        raise ChangeProposalStructureError("path debe ser relativo")
    parts = tuple(part for part in posix.parts if part not in ("", "."))
    if not parts or ".." in parts:
        raise ChangeProposalStructureError("path no admite escapes ni rutas vacías")
    return PurePosixPath(*parts).as_posix()


@dataclass(frozen=True)
class FileChange:
    path: str
    operation: str
    new_content: str
    expected_sha256: str | None
    justification: str = ""

    def __post_init__(self) -> None:
        normalized = normalize_relative_path(self.path)
        if self.operation not in VALID_OPERATIONS:
            raise ChangeProposalStructureError(
                f"operation debe ser uno de {sorted(VALID_OPERATIONS)}"
            )
        if not isinstance(self.new_content, str):
            raise ChangeProposalStructureError("new_content debe ser una cadena")
        if not isinstance(self.justification, str):
            raise ChangeProposalStructureError("justification debe ser una cadena")
        if self.operation == "replace":
            if (
                not isinstance(self.expected_sha256, str)
                or not SHA256_PATTERN.fullmatch(self.expected_sha256)
            ):
                raise ChangeProposalStructureError(
                    "replace exige expected_sha256 hexadecimal en minúsculas"
                )
        elif self.expected_sha256 is not None:
            raise ChangeProposalStructureError(
                "create exige expected_sha256=None"
            )
        object.__setattr__(self, "path", normalized)

    @property
    def content_sha256(self) -> str:
        try:
            encoded = self.new_content.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ChangeProposalStructureError(
                "new_content debe ser UTF-8 válido"
            ) from exc
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class TestSpec:
    scope: str
    targets: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.scope not in VALID_TEST_SCOPES:
            raise TestSpecificationError(
                f"scope debe ser uno de {sorted(VALID_TEST_SCOPES)}"
            )
        if isinstance(self.targets, (str, bytes)):
            raise TestSpecificationError("targets debe ser una secuencia")
        try:
            targets = tuple(self.targets)
        except TypeError as exc:
            raise TestSpecificationError("targets debe ser una secuencia") from exc
        if self.scope == "focused" and not targets:
            raise TestSpecificationError("focused exige al menos un test-id")
        if self.scope == "full" and targets:
            raise TestSpecificationError("full no acepta targets")
        for target in targets:
            if not isinstance(target, str) or not TEST_ID_PATTERN.fullmatch(target):
                raise TestSpecificationError(
                    f"Identificador unittest no permitido: {target!r}"
                )
        if len(set(targets)) != len(targets):
            raise TestSpecificationError("targets no admite duplicados")
        object.__setattr__(self, "targets", targets)

    def canonical_command(self, executable: str) -> tuple[str, ...]:
        if self.scope == "full":
            return (
                executable,
                "-m",
                "unittest",
                "discover",
                "-s",
                "tests",
                "-v",
            )
        return (
            executable,
            "-m",
            "unittest",
            *self.targets,
            "-v",
        )


@dataclass(frozen=True)
class ProposalBudget:
    modified_files: int
    new_files: int
    write_bytes: int
    changed_lines: int

    def __post_init__(self) -> None:
        for name, value in vars(self).items():
            if isinstance(value, bool) or not isinstance(value, int):
                raise ChangeProposalStructureError(f"{name} debe ser un entero")
            if value < 0:
                raise ChangeProposalStructureError(
                    f"{name} debe ser no negativo"
                )

    def canonical_dict(self) -> dict[str, int]:
        return {
            "modified_files": self.modified_files,
            "new_files": self.new_files,
            "write_bytes": self.write_bytes,
            "changed_lines": self.changed_lines,
        }


@dataclass(frozen=True)
class ChangeProposal:
    changes: tuple[FileChange, ...]
    tests: tuple[TestSpec, ...]
    justification: str
    risks: tuple[str, ...]
    budget: ProposalBudget

    def __post_init__(self) -> None:
        if isinstance(self.changes, (str, bytes)):
            raise ChangeProposalStructureError("changes debe ser una secuencia")
        if isinstance(self.tests, (str, bytes)):
            raise ChangeProposalStructureError("tests debe ser una secuencia")
        if isinstance(self.risks, (str, bytes)):
            raise ChangeProposalStructureError("risks debe ser una secuencia")
        try:
            changes = tuple(self.changes)
            tests = tuple(self.tests)
            risks = tuple(self.risks)
        except TypeError as exc:
            raise ChangeProposalStructureError(
                "changes, tests y risks deben ser secuencias"
            ) from exc
        if not changes:
            raise ChangeProposalStructureError("La propuesta no puede estar vacía")
        if any(not isinstance(change, FileChange) for change in changes):
            raise ChangeProposalStructureError("changes solo admite FileChange")
        if any(not isinstance(test, TestSpec) for test in tests):
            raise ChangeProposalStructureError("tests solo admite TestSpec")
        if not isinstance(self.justification, str):
            raise ChangeProposalStructureError("justification debe ser una cadena")
        if any(not isinstance(risk, str) for risk in risks):
            raise ChangeProposalStructureError("risks solo admite cadenas")
        if not isinstance(self.budget, ProposalBudget):
            raise ChangeProposalStructureError("budget debe ser ProposalBudget")

        canonical_paths = [change.path.casefold() for change in changes]
        if len(canonical_paths) != len(set(canonical_paths)):
            raise ChangeProposalStructureError(
                "No se permiten rutas duplicadas tras normalización"
            )
        object.__setattr__(self, "changes", changes)
        object.__setattr__(self, "tests", tests)
        object.__setattr__(self, "risks", risks)

    def identity_payload(self) -> dict:
        return {
            "contract_version": CONTRACT_VERSION,
            "changes": [
                {
                    "operation": change.operation,
                    "path": change.path,
                    "expected_sha256": change.expected_sha256,
                    "content_sha256": change.content_sha256,
                }
                for change in self.changes
            ],
            "tests": [
                {"scope": test.scope, "targets": list(test.targets)}
                for test in self.tests
            ],
            "budget": self.budget.canonical_dict(),
        }

    @property
    def proposal_id(self) -> str:
        encoded = json.dumps(
            self.identity_payload(),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
