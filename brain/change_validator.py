from __future__ import annotations

import hashlib
import stat
import weakref
from dataclasses import dataclass
from difflib import SequenceMatcher, unified_diff
from pathlib import Path

from brain.change_proposal import (
    ChangeProposal,
    FileChange,
    ProposalBudget,
    TestSpec,
)
from brain.path_policy import PathPolicy, PathValidationError
from brain.workflow_limits import WorkflowLimits


_AUTHENTIC_VALIDATIONS: dict[int, weakref.ReferenceType] = {}


def _register_authentic_validation(
    validated: "ValidatedChangeProposal",
) -> "ValidatedChangeProposal":
    key = id(validated)

    def discard(reference):
        if _AUTHENTIC_VALIDATIONS.get(key) is reference:
            _AUTHENTIC_VALIDATIONS.pop(key, None)

    _AUTHENTIC_VALIDATIONS[key] = weakref.ref(validated, discard)
    return validated


def is_authentic_validated_proposal(value) -> bool:
    reference = _AUTHENTIC_VALIDATIONS.get(id(value))
    return reference is not None and reference() is value


class ChangeValidationError(ValueError):
    """Base error for validation of a complete proposal."""


class ChangePathError(ChangeValidationError):
    pass


class ChangeContentError(ChangeValidationError):
    pass


class ChangePreconditionError(ChangeValidationError):
    pass


class ProposalBudgetMismatchError(ChangeValidationError):
    pass


class ChangeLimitExceededError(ChangeValidationError):
    pass


class ChangeTestSpecificationError(ChangeValidationError):
    pass


@dataclass(frozen=True)
class ResolvedFileChange:
    relative_path: str
    absolute_path: Path
    operation: str
    new_bytes: bytes
    current_sha256: str | None
    current_bytes: bytes | None
    original_mode: int | None
    rendered_diff: str
    write_bytes: int
    changed_lines: int


@dataclass(frozen=True)
class ValidatedChangeProposal:
    proposal: ChangeProposal
    proposal_id: str
    resolved_changes: tuple[ResolvedFileChange, ...]
    calculated_budget: ProposalBudget
    rendered_diffs: tuple[str, ...]


class ChangeProposalValidator:
    """Validate a complete proposal without producing filesystem effects."""

    def __init__(self, base_dir=None, limits: WorkflowLimits | None = None):
        self.base_dir = Path(base_dir or ".").resolve()
        self.limits = limits or WorkflowLimits()
        self.path_policy = PathPolicy(self.base_dir)

    def validate(self, proposal: ChangeProposal) -> ValidatedChangeProposal:
        if not isinstance(proposal, ChangeProposal):
            raise ChangeValidationError("proposal debe ser ChangeProposal")
        resolved = tuple(self._validate_change(change) for change in proposal.changes)
        self._validate_tests(proposal.tests)
        calculated = ProposalBudget(
            modified_files=len(resolved),
            new_files=sum(change.operation == "create" for change in resolved),
            write_bytes=sum(change.write_bytes for change in resolved),
            changed_lines=sum(change.changed_lines for change in resolved),
        )
        if calculated != proposal.budget:
            raise ProposalBudgetMismatchError(
                f"Presupuesto declarado {proposal.budget} no coincide con {calculated}"
            )
        self._validate_limits(resolved, calculated)
        return _register_authentic_validation(
            ValidatedChangeProposal(
                proposal=proposal,
                proposal_id=proposal.proposal_id,
                resolved_changes=resolved,
                calculated_budget=calculated,
                rendered_diffs=tuple(
                    change.rendered_diff for change in resolved
                ),
            )
        )

    def _validate_change(self, change: FileChange) -> ResolvedFileChange:
        try:
            target = self.path_policy.resolve_for_write(change.path)
        except PathValidationError as exc:
            raise ChangePathError(str(exc)) from exc
        path = target.absolute
        try:
            new_bytes = change.new_content.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ChangeContentError("new_content no es UTF-8 válido") from exc

        if change.operation == "replace":
            return self._validate_replace(change, target.relative.as_posix(), path, new_bytes)
        return self._validate_create(change, target.relative.as_posix(), path, new_bytes)

    def _validate_replace(
        self,
        change: FileChange,
        relative_path: str,
        path: Path,
        new_bytes: bytes,
    ) -> ResolvedFileChange:
        if not path.exists():
            raise ChangePreconditionError(f"No existe el archivo: {relative_path}")
        if path.is_symlink() or not path.is_file():
            raise ChangePathError(f"El destino no es un archivo regular: {relative_path}")
        current_bytes = self._read_limited(path)
        try:
            current_text = current_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ChangeContentError(
                f"El archivo actual no es UTF-8: {relative_path}"
            ) from exc
        actual_hash = hashlib.sha256(current_bytes).hexdigest()
        if actual_hash != change.expected_sha256:
            raise ChangePreconditionError(
                f"Hash obsoleto para {relative_path}"
            )
        diff = self._render_diff(relative_path, current_text, change.new_content, create=False)
        return ResolvedFileChange(
            relative_path=relative_path,
            absolute_path=path,
            operation=change.operation,
            new_bytes=new_bytes,
            current_sha256=actual_hash,
            current_bytes=current_bytes,
            original_mode=stat.S_IMODE(path.stat().st_mode),
            rendered_diff=diff,
            write_bytes=len(new_bytes),
            changed_lines=self._changed_lines(current_text, change.new_content),
        )

    def _validate_create(
        self,
        change: FileChange,
        relative_path: str,
        path: Path,
        new_bytes: bytes,
    ) -> ResolvedFileChange:
        if path.exists() or path.is_symlink():
            raise ChangePreconditionError(
                f"El destino ya existe: {relative_path}"
            )
        parent = path.parent
        if not parent.exists() or not parent.is_dir() or parent.is_symlink():
            raise ChangePathError(
                f"El directorio padre no es válido: {relative_path}"
            )
        diff = self._render_diff(relative_path, "", change.new_content, create=True)
        return ResolvedFileChange(
            relative_path=relative_path,
            absolute_path=path,
            operation=change.operation,
            new_bytes=new_bytes,
            current_sha256=None,
            current_bytes=None,
            original_mode=None,
            rendered_diff=diff,
            write_bytes=len(new_bytes),
            changed_lines=len(change.new_content.splitlines()),
        )

    def _read_limited(self, path: Path) -> bytes:
        limit = self.limits.max_read_bytes_per_file
        try:
            with path.open("rb") as handle:
                payload = handle.read(limit + 1)
        except OSError as exc:
            raise ChangeContentError(f"No se pudo leer {path.name}: {exc}") from exc
        if len(payload) > limit:
            raise ChangeLimitExceededError(
                f"max_read_bytes_per_file excedido por {path.name}"
            )
        return payload

    def _validate_limits(
        self,
        changes: tuple[ResolvedFileChange, ...],
        budget: ProposalBudget,
    ) -> None:
        if budget.modified_files > self.limits.max_modified_files:
            raise ChangeLimitExceededError("max_modified_files excedido")
        if budget.write_bytes > self.limits.max_total_change_bytes:
            raise ChangeLimitExceededError("max_total_change_bytes excedido")
        if budget.changed_lines > self.limits.max_changed_lines:
            raise ChangeLimitExceededError("max_changed_lines excedido")
        for change in changes:
            if (
                change.operation == "create"
                and change.write_bytes > self.limits.max_new_file_bytes
            ):
                raise ChangeLimitExceededError(
                    f"max_new_file_bytes excedido por {change.relative_path}"
                )

    @staticmethod
    def _validate_tests(tests: tuple[TestSpec, ...]) -> None:
        if any(not isinstance(test, TestSpec) for test in tests):
            raise ChangeTestSpecificationError(
                "La propuesta contiene una especificación de prueba inválida"
            )

    @staticmethod
    def _render_diff(
        path: str,
        old_content: str,
        new_content: str,
        *,
        create: bool,
    ) -> str:
        old_name = "/dev/null" if create else path
        return "\n".join(
            unified_diff(
                old_content.splitlines(),
                new_content.splitlines(),
                fromfile=old_name,
                tofile=path,
                lineterm="",
            )
        )

    @staticmethod
    def _changed_lines(old_content: str, new_content: str) -> int:
        matcher = SequenceMatcher(
            None,
            old_content.splitlines(),
            new_content.splitlines(),
            autojunk=False,
        )
        return sum(
            max(old_end - old_start, new_end - new_start)
            for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes()
            if tag != "equal"
        )
