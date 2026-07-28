from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from brain.change_validator import (
    ResolvedFileChange,
    ValidatedChangeProposal,
    is_authentic_validated_proposal,
)
from brain.path_policy import PathPolicy, PathValidationError


@dataclass(frozen=True)
class TransactionErrorInfo:
    phase: str
    path: str | None
    error_type: str
    message: str


@dataclass(frozen=True)
class ChangeTransactionResult:
    proposal_id: str
    modified_paths: tuple[str, ...]
    created_paths: tuple[str, ...]
    write_bytes: int
    changed_lines: int
    applied: bool
    rollback_attempted: bool
    rollback_succeeded: bool | None
    errors: tuple[TransactionErrorInfo, ...] = ()


class ChangeTransactionError(RuntimeError):
    """Base error for a controlled multi-file change transaction."""

    def __init__(self, message: str, result: ChangeTransactionResult | None = None):
        super().__init__(message)
        self.result = result


class TransactionPreconditionError(ChangeTransactionError):
    pass


class DuplicateProposalApplicationError(ChangeTransactionError):
    pass


class TransactionApplyError(ChangeTransactionError):
    pass


class TransactionRollbackError(ChangeTransactionError):
    def __init__(
        self,
        message: str,
        *,
        original_error: BaseException,
        result: ChangeTransactionResult,
    ):
        super().__init__(message, result)
        self.original_error = original_error


@dataclass(frozen=True)
class _Snapshot:
    change: ResolvedFileChange
    existed: bool
    content: bytes | None
    mode: int | None


class ChangeTransaction:
    """Apply one validated proposal atomically at the logical file-set level."""

    def __init__(self, base_dir=None, applied_proposal_ids=()):
        self.base_dir = Path(base_dir or ".").resolve()
        self.path_policy = PathPolicy(self.base_dir)
        self._applied_proposal_ids = set(applied_proposal_ids)

    @property
    def applied_proposal_ids(self) -> frozenset[str]:
        return frozenset(self._applied_proposal_ids)

    def apply(
        self,
        validated: ValidatedChangeProposal,
    ) -> ChangeTransactionResult:
        if not isinstance(validated, ValidatedChangeProposal):
            raise TransactionPreconditionError(
                "La transacción exige ValidatedChangeProposal"
            )
        if not is_authentic_validated_proposal(validated):
            raise TransactionPreconditionError(
                "ValidatedChangeProposal no procede de ChangeProposalValidator"
            )
        proposal_id = validated.proposal_id
        if proposal_id in self._applied_proposal_ids:
            raise DuplicateProposalApplicationError(
                f"La propuesta {proposal_id} ya fue aplicada"
            )
        if validated.proposal.proposal_id != proposal_id:
            raise TransactionPreconditionError(
                "La identidad de la propuesta ya no coincide con la validación"
            )
        self._validate_validation_integrity(validated)

        snapshots = self._revalidate_and_snapshot(validated)
        published: list[_Snapshot] = []
        temporaries: set[Path] = set()
        try:
            for snapshot in snapshots:
                self._publish(snapshot.change, temporaries)
                published.append(snapshot)
        except OSError as exc:
            raise self._handle_operational_failure(
                validated,
                published,
                temporaries,
                exc,
            ) from exc
        except Exception as exc:
            rollback_errors = self._rollback(published, temporaries)
            if rollback_errors:
                result = self._failure_result(
                    validated,
                    original=exc,
                    rollback_attempted=bool(published),
                    rollback_succeeded=False,
                    rollback_errors=rollback_errors,
                    published=published,
                )
                raise TransactionRollbackError(
                    "Falló la transacción y también su rollback",
                    original_error=exc,
                    result=result,
                ) from exc
            raise

        cleanup_errors = self._cleanup_temporaries(temporaries)
        if cleanup_errors:
            rollback_errors = self._rollback(published, temporaries)
            result = self._failure_result(
                validated,
                original=OSError("No se pudieron limpiar temporales"),
                rollback_attempted=bool(published),
                rollback_succeeded=not rollback_errors,
                rollback_errors=(*cleanup_errors, *rollback_errors),
                published=published,
            )
            if rollback_errors:
                raise TransactionRollbackError(
                    "Falló la limpieza y el rollback no pudo completarse",
                    original_error=OSError(
                        "No se pudieron limpiar temporales"
                    ),
                    result=result,
                )
            raise TransactionApplyError(
                "Falló la limpieza; la aplicación fue restaurada",
                result,
            )

        self._applied_proposal_ids.add(proposal_id)
        return ChangeTransactionResult(
            proposal_id=proposal_id,
            modified_paths=tuple(
                item.relative_path
                for item in validated.resolved_changes
                if item.operation == "replace"
            ),
            created_paths=tuple(
                item.relative_path
                for item in validated.resolved_changes
                if item.operation == "create"
            ),
            write_bytes=validated.calculated_budget.write_bytes,
            changed_lines=validated.calculated_budget.changed_lines,
            applied=True,
            rollback_attempted=False,
            rollback_succeeded=None,
        )

    def _revalidate_and_snapshot(
        self,
        validated: ValidatedChangeProposal,
    ) -> tuple[_Snapshot, ...]:
        snapshots: list[_Snapshot] = []
        for change in validated.resolved_changes:
            try:
                resolved = self.path_policy.resolve_for_write(
                    change.relative_path
                )
            except PathValidationError as exc:
                raise TransactionPreconditionError(str(exc)) from exc
            path = resolved.absolute
            if path != change.absolute_path:
                raise TransactionPreconditionError(
                    f"La ruta resuelta cambió: {change.relative_path}"
                )
            if change.operation == "replace":
                if not path.exists() or path.is_symlink() or not path.is_file():
                    raise TransactionPreconditionError(
                        f"El archivo a reemplazar ya no es válido: {change.relative_path}"
                    )
                try:
                    content = path.read_bytes()
                    mode = stat.S_IMODE(path.stat().st_mode)
                except OSError as exc:
                    raise TransactionPreconditionError(
                        f"No se pudo revalidar {change.relative_path}"
                    ) from exc
                current_hash = hashlib.sha256(content).hexdigest()
                if current_hash != change.current_sha256:
                    raise TransactionPreconditionError(
                        f"Hash obsoleto antes de aplicar: {change.relative_path}"
                    )
                snapshots.append(_Snapshot(change, True, content, mode))
            elif change.operation == "create":
                if path.exists() or path.is_symlink():
                    raise TransactionPreconditionError(
                        f"El destino apareció antes de aplicar: {change.relative_path}"
                    )
                parent = path.parent
                if not parent.exists() or not parent.is_dir() or parent.is_symlink():
                    raise TransactionPreconditionError(
                        f"El directorio padre ya no es válido: {change.relative_path}"
                    )
                snapshots.append(_Snapshot(change, False, None, None))
            else:
                raise TransactionPreconditionError(
                    f"Operación validada desconocida: {change.operation}"
                )
        return tuple(snapshots)

    @staticmethod
    def _validate_validation_integrity(
        validated: ValidatedChangeProposal,
    ) -> None:
        proposal = validated.proposal
        resolved = validated.resolved_changes
        if len(proposal.changes) != len(resolved):
            raise TransactionPreconditionError(
                "La validación no contiene todos los cambios de la propuesta"
            )
        total_bytes = 0
        total_lines = 0
        new_files = 0
        for declared, concrete in zip(proposal.changes, resolved):
            try:
                declared_bytes = declared.new_content.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise TransactionPreconditionError(
                    f"Contenido inválido en {declared.path}"
                ) from exc
            if (
                declared.path != concrete.relative_path
                or declared.operation != concrete.operation
                or declared_bytes != concrete.new_bytes
            ):
                raise TransactionPreconditionError(
                    "Los cambios resueltos no coinciden con la propuesta"
                )
            if (
                declared.operation == "replace"
                and declared.expected_sha256 != concrete.current_sha256
            ):
                raise TransactionPreconditionError(
                    f"Hash validado incompatible: {declared.path}"
                )
            total_bytes += concrete.write_bytes
            total_lines += concrete.changed_lines
            new_files += concrete.operation == "create"
        calculated = validated.calculated_budget
        if (
            calculated != proposal.budget
            or calculated.modified_files != len(resolved)
            or calculated.new_files != new_files
            or calculated.write_bytes != total_bytes
            or calculated.changed_lines != total_lines
        ):
            raise TransactionPreconditionError(
                "El presupuesto validado no corresponde a los cambios resueltos"
            )

    def _publish(
        self,
        change: ResolvedFileChange,
        temporaries: set[Path],
    ) -> None:
        self._revalidate_before_publication(change)
        target = change.absolute_path
        descriptor, temp_name = tempfile.mkstemp(
            dir=str(target.parent),
            prefix=f".{target.name}.",
            suffix=".tmp",
        )
        temp_path = Path(temp_name)
        temporaries.add(temp_path)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(change.new_bytes)
                handle.flush()
                try:
                    os.fsync(handle.fileno())
                except OSError:
                    pass
            if change.original_mode is not None:
                try:
                    os.chmod(temp_path, change.original_mode)
                except OSError:
                    pass
            os.replace(temp_path, target)
            temporaries.discard(temp_path)
        finally:
            try:
                os.close(descriptor)
            except OSError:
                pass

    def _revalidate_before_publication(
        self,
        change: ResolvedFileChange,
    ) -> None:
        try:
            target = self.path_policy.resolve_for_write(
                change.relative_path
            ).absolute
        except PathValidationError as exc:
            raise TransactionPreconditionError(str(exc)) from exc
        if target != change.absolute_path:
            raise TransactionPreconditionError(
                f"La ruta cambió antes de publicar: {change.relative_path}"
            )
        if change.operation == "create":
            if target.exists() or target.is_symlink():
                raise TransactionPreconditionError(
                    f"El destino apareció antes de publicar: {change.relative_path}"
                )
            return
        if not target.exists() or target.is_symlink() or not target.is_file():
            raise TransactionPreconditionError(
                f"El archivo dejó de ser válido: {change.relative_path}"
            )
        try:
            current_hash = hashlib.sha256(target.read_bytes()).hexdigest()
        except OSError as exc:
            raise TransactionPreconditionError(
                f"No se pudo revalidar {change.relative_path}"
            ) from exc
        if current_hash != change.current_sha256:
            raise TransactionPreconditionError(
                f"Hash obsoleto antes de publicar: {change.relative_path}"
            )

    def _handle_operational_failure(
        self,
        validated: ValidatedChangeProposal,
        published: list[_Snapshot],
        temporaries: set[Path],
        original: OSError,
    ) -> ChangeTransactionError:
        rollback_errors = self._rollback(published, temporaries)
        attempted = bool(published)
        result = self._failure_result(
            validated,
            original=original,
            rollback_attempted=attempted,
            rollback_succeeded=not rollback_errors if attempted else None,
            rollback_errors=rollback_errors,
            published=published,
        )
        if rollback_errors:
            return TransactionRollbackError(
                "Falló la transacción y también su rollback",
                original_error=original,
                result=result,
            )
        return TransactionApplyError("Falló la aplicación de la propuesta", result)

    def _rollback(
        self,
        published: list[_Snapshot],
        temporaries: set[Path],
    ) -> tuple[TransactionErrorInfo, ...]:
        errors: list[TransactionErrorInfo] = []
        for snapshot in reversed(published):
            path = snapshot.change.absolute_path
            try:
                self._ensure_rollback_owns_path(path, snapshot)
                if snapshot.existed:
                    self._restore_snapshot(path, snapshot, temporaries)
                elif path.exists() or path.is_symlink():
                    path.unlink()
            except OSError as exc:
                errors.append(
                    self._error_info("rollback", snapshot.change.relative_path, exc)
                )
        errors.extend(self._cleanup_temporaries(temporaries))
        return tuple(errors)

    @staticmethod
    def _ensure_rollback_owns_path(
        path: Path,
        snapshot: _Snapshot,
    ) -> None:
        if path.is_symlink() or not path.exists() or not path.is_file():
            raise OSError(
                f"La ruta cambió externamente durante rollback: {path.name}"
            )
        current = path.read_bytes()
        if current != snapshot.change.new_bytes:
            raise OSError(
                f"El contenido cambió externamente durante rollback: {path.name}"
            )

    def _restore_snapshot(
        self,
        path: Path,
        snapshot: _Snapshot,
        temporaries: set[Path],
    ) -> None:
        descriptor, temp_name = tempfile.mkstemp(
            dir=str(path.parent),
            prefix=f".{path.name}.rollback.",
            suffix=".tmp",
        )
        temp_path = Path(temp_name)
        temporaries.add(temp_path)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(snapshot.content or b"")
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
        if snapshot.mode is not None:
            try:
                os.chmod(temp_path, snapshot.mode)
            except OSError:
                pass
        os.replace(temp_path, path)
        temporaries.discard(temp_path)

    def _cleanup_temporaries(
        self,
        temporaries: set[Path],
    ) -> tuple[TransactionErrorInfo, ...]:
        errors: list[TransactionErrorInfo] = []
        for path in tuple(temporaries):
            try:
                if path.exists() or path.is_symlink():
                    path.unlink()
                temporaries.discard(path)
            except OSError as exc:
                errors.append(self._error_info("cleanup", str(path), exc))
        return tuple(errors)

    @staticmethod
    def _error_info(
        phase: str,
        path: str | None,
        error: BaseException,
    ) -> TransactionErrorInfo:
        return TransactionErrorInfo(
            phase=phase,
            path=path,
            error_type=type(error).__name__,
            message=str(error),
        )

    def _failure_result(
        self,
        validated: ValidatedChangeProposal,
        *,
        original: BaseException,
        rollback_attempted: bool,
        rollback_succeeded: bool | None,
        rollback_errors: tuple[TransactionErrorInfo, ...],
        published: list[_Snapshot],
    ) -> ChangeTransactionResult:
        uncertain = rollback_succeeded is False
        modified = tuple(
            snapshot.change.relative_path
            for snapshot in published
            if snapshot.existed
        )
        created = tuple(
            snapshot.change.relative_path
            for snapshot in published
            if not snapshot.existed
        )
        return ChangeTransactionResult(
            proposal_id=validated.proposal_id,
            modified_paths=modified if uncertain else (),
            created_paths=created if uncertain else (),
            write_bytes=(
                sum(snapshot.change.write_bytes for snapshot in published)
                if uncertain
                else 0
            ),
            changed_lines=(
                sum(snapshot.change.changed_lines for snapshot in published)
                if uncertain
                else 0
            ),
            applied=False,
            rollback_attempted=rollback_attempted,
            rollback_succeeded=rollback_succeeded,
            errors=(
                self._error_info("apply", None, original),
                *rollback_errors,
            ),
        )
