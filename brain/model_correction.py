from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from types import MappingProxyType
from typing import Any

from brain.change_proposal import (
    ChangeProposal,
    ChangeProposalStructureError,
    FileChange,
    ProposalBudget,
    TestSpec,
)
from brain.change_validator import (
    ChangeProposalValidator,
    ValidatedChangeProposal,
    is_authentic_validated_proposal,
)
from brain.local_model_client import (
    LocalModelClient,
    ModelMessage,
    ModelResponseMetadata,
    StructuredModelRequest,
    StructuredModelResponse,
)
from brain.model_errors import LocalModelError
from brain.path_policy import PathPolicy, PathValidationError
from brain.structured_json import (
    StructuredOutputError,
    StructuredOutputSchema,
    strict_json_loads,
    thaw_json,
)
from brain.workflow_limits import WorkflowLimits


MODEL_CORRECTION_ERROR_MESSAGES = MappingProxyType(
    {
        "invalid_model_correction": "La corrección propuesta por el modelo no es válida.",
        "unsupported_correction_version": "La versión de la corrección no está soportada.",
        "correction_operation_not_allowed": "La corrección contiene una operación no permitida.",
        "correction_path_not_allowed": "La corrección contiene una ruta no permitida.",
        "correction_limit_exceeded": "La corrección excede los límites autorizados.",
        "duplicate_correction": "La corrección ya fue propuesta para este runtime.",
        "stale_correction_precondition": "La precondición de la corrección quedó obsoleta.",
        "correction_budget_exhausted": "Se agotó el presupuesto de correcciones del modelo.",
    }
)
_ROOT_FIELDS = frozenset({"schema_version", "summary", "changes", "risks"})
_CHANGE_FIELDS = frozenset(
    {"operation", "path", "new_content", "expected_sha256", "justification"}
)
_SHA256 = frozenset("0123456789abcdef")


class ModelCorrectionError(ValueError):
    """Closed, non-sensitive failure at the untrusted correction boundary."""

    __slots__ = ("code",)

    def __init__(self, code: str):
        safe = (
            code
            if code in MODEL_CORRECTION_ERROR_MESSAGES
            else "invalid_model_correction"
        )
        self.code = safe
        super().__init__(MODEL_CORRECTION_ERROR_MESSAGES[safe])


MODEL_CORRECTION_OUTPUT_SCHEMA = StructuredOutputSchema(
    "developer_ai_model_correction",
    {
        "type": "object",
        "properties": {
            "schema_version": {"type": "string", "enum": ["1"]},
            "summary": {"type": "string", "minLength": 1, "maxLength": 1000},
            "changes": {
                "type": "array",
                "minItems": 1,
                "maxItems": 5,
                "items": {
                    "type": "object",
                    "properties": {
                        "operation": {
                            "type": "string",
                            "enum": ["create", "replace"],
                        },
                        "path": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 512,
                        },
                        "new_content": {
                            "type": "string",
                            "maxLength": 256 * 1024,
                        },
                        "expected_sha256": {
                            "type": ["string", "null"],
                            "maxLength": 64,
                        },
                        "justification": {
                            "type": "string",
                            "maxLength": 500,
                        },
                    },
                    "required": [
                        "operation",
                        "path",
                        "new_content",
                        "expected_sha256",
                        "justification",
                    ],
                    "additionalProperties": False,
                },
            },
            "risks": {
                "type": "array",
                "minItems": 0,
                "maxItems": 8,
                "items": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 300,
                },
            },
        },
        "required": ["schema_version", "summary", "changes", "risks"],
        "additionalProperties": False,
    },
)


@dataclass(frozen=True)
class ModelCorrectionChangeDraft:
    operation: str
    path: str
    new_content: str = field(repr=False)
    expected_sha256: str | None
    justification: str

    def __post_init__(self) -> None:
        if type(self.operation) is not str or self.operation not in {
            "create",
            "replace",
        }:
            raise ModelCorrectionError("correction_operation_not_allowed")
        if type(self.path) is not str or not 1 <= len(self.path) <= 512:
            raise ModelCorrectionError("correction_path_not_allowed")
        if (
            type(self.new_content) is not str
            or len(self.new_content) > 256 * 1024
        ):
            raise ModelCorrectionError("invalid_model_correction")
        if type(self.justification) is not str or len(self.justification) > 500:
            raise ModelCorrectionError("invalid_model_correction")
        if self.operation == "create":
            if self.expected_sha256 is not None:
                raise ModelCorrectionError("invalid_model_correction")
        elif (
            type(self.expected_sha256) is not str
            or len(self.expected_sha256) != 64
            or any(character not in _SHA256 for character in self.expected_sha256)
        ):
            raise ModelCorrectionError("stale_correction_precondition")

    @classmethod
    def from_mapping(cls, value: Any) -> "ModelCorrectionChangeDraft":
        if type(value) is not dict or set(value) != _CHANGE_FIELDS:
            raise ModelCorrectionError("invalid_model_correction")
        try:
            return cls(
                operation=value["operation"],
                path=value["path"],
                new_content=value["new_content"],
                expected_sha256=value["expected_sha256"],
                justification=value["justification"],
            )
        except ModelCorrectionError:
            raise
        except BaseException:
            raise ModelCorrectionError("invalid_model_correction") from None

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "path": self.path,
            "new_content": self.new_content,
            "expected_sha256": self.expected_sha256,
            "justification": self.justification,
        }


@dataclass(frozen=True)
class ModelCorrectionProposalDraft:
    schema_version: str
    summary: str
    changes: tuple[ModelCorrectionChangeDraft, ...]
    risks: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.schema_version != "1":
            raise ModelCorrectionError("unsupported_correction_version")
        if type(self.summary) is not str or not 1 <= len(self.summary) <= 1000:
            raise ModelCorrectionError("invalid_model_correction")
        if (
            type(self.changes) is not tuple
            or not 1 <= len(self.changes) <= 5
            or any(type(item) is not ModelCorrectionChangeDraft for item in self.changes)
        ):
            raise ModelCorrectionError("invalid_model_correction")
        if (
            type(self.risks) is not tuple
            or len(self.risks) > 8
            or any(type(item) is not str or not 1 <= len(item) <= 300 for item in self.risks)
        ):
            raise ModelCorrectionError("invalid_model_correction")

    @classmethod
    def from_mapping(cls, value: Any) -> "ModelCorrectionProposalDraft":
        if type(value) is not dict or set(value) != _ROOT_FIELDS:
            raise ModelCorrectionError("invalid_model_correction")
        if value.get("schema_version") != "1":
            raise ModelCorrectionError("unsupported_correction_version")
        if type(value.get("changes")) is not list or type(value.get("risks")) is not list:
            raise ModelCorrectionError("invalid_model_correction")
        try:
            return cls(
                schema_version=value["schema_version"],
                summary=value["summary"],
                changes=tuple(
                    ModelCorrectionChangeDraft.from_mapping(item)
                    for item in value["changes"]
                ),
                risks=tuple(value["risks"]),
            )
        except ModelCorrectionError:
            raise
        except BaseException:
            raise ModelCorrectionError("invalid_model_correction") from None

    @classmethod
    def from_json(
        cls,
        text: str,
        *,
        max_bytes: int = 512 * 1024,
        max_depth: int = 8,
    ) -> "ModelCorrectionProposalDraft":
        try:
            value = strict_json_loads(
                text,
                max_bytes=max_bytes,
                max_depth=max_depth,
            )
        except (StructuredOutputError, UnicodeError):
            raise ModelCorrectionError("invalid_model_correction") from None
        return cls.from_mapping(value)

    @property
    def draft_id(self) -> str:
        payload = {
            "schema_version": self.schema_version,
            "summary": self.summary,
            "changes": [item.canonical_dict() for item in self.changes],
            "risks": list(self.risks),
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return "mc1_" + hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class ModelCorrectionSource:
    path: str
    current_content: str = field(repr=False)
    current_sha256: str

    def __post_init__(self) -> None:
        if (
            type(self.path) is not str
            or not 1 <= len(self.path) <= 512
            or type(self.current_content) is not str
            or len(self.current_content) > 256 * 1024
            or type(self.current_sha256) is not str
            or len(self.current_sha256) != 64
            or any(character not in _SHA256 for character in self.current_sha256)
        ):
            raise ModelCorrectionError("invalid_model_correction")


@dataclass(frozen=True)
class ModelCorrectionContext:
    session_id: str
    runtime_id: str
    step_id: str
    goal: str
    failure_code: str
    remaining_files: int
    remaining_bytes: int
    remaining_lines: int
    sources: tuple[ModelCorrectionSource, ...] = ()
    failed_test_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("session_id", "runtime_id", "step_id", "goal", "failure_code"):
            if type(getattr(self, name)) is not str or not getattr(self, name):
                raise ModelCorrectionError("invalid_model_correction")
        for name in ("remaining_files", "remaining_bytes", "remaining_lines"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ModelCorrectionError("invalid_model_correction")
        if (
            type(self.sources) is not tuple
            or len(self.sources) > 5
            or any(type(item) is not ModelCorrectionSource for item in self.sources)
            or sum(
                len(item.current_content.encode("utf-8"))
                for item in self.sources
            )
            > 256 * 1024
            or type(self.failed_test_ids) is not tuple
            or len(self.failed_test_ids) > 32
            or any(
                type(item) is not str or not 1 <= len(item) <= 512
                for item in self.failed_test_ids
            )
        ):
            raise ModelCorrectionError("correction_limit_exceeded")

    def prompt_payload(self) -> dict[str, Any]:
        return {
            "goal": self.goal[:1000],
            "failed_step": self.step_id,
            "failure_code": self.failure_code[:128],
            "remaining_budget": {
                "files": self.remaining_files,
                "bytes": self.remaining_bytes,
                "lines": self.remaining_lines,
            },
            "authorized_sources": [
                {
                    "path": item.path,
                    "current_content": item.current_content,
                    "current_sha256": item.current_sha256,
                }
                for item in self.sources
            ],
            "failed_test_ids": list(self.failed_test_ids),
        }


@dataclass(frozen=True)
class ModelCorrectionGenerationPolicy:
    max_generations: int = 3
    max_invalid_drafts: int = 2
    max_rejected_proposals: int = 2
    max_test_executions_per_application: int = 6

    def __post_init__(self) -> None:
        for value in vars(self).values():
            if type(value) is not int or value <= 0:
                raise ModelCorrectionError("invalid_model_correction")


@dataclass(frozen=True)
class ModelCorrectionGenerationResult:
    draft: ModelCorrectionProposalDraft
    metadata: ModelResponseMetadata

    def __post_init__(self) -> None:
        if type(self.draft) is not ModelCorrectionProposalDraft:
            raise ModelCorrectionError("invalid_model_correction")
        if type(self.metadata) is not ModelResponseMetadata:
            raise ModelCorrectionError("invalid_model_correction")


_SYSTEM_PROMPT = (
    "Devuelve únicamente el JSON del esquema solicitado. Es un borrador de "
    "corrección sin autoridad. No incluyas aprobaciones, presupuestos, tests, "
    "herramientas, comandos, rutas base, commit, push, tokens ni estado runtime. "
    "Solo puedes proponer create o replace; la arquitectura validará rutas, "
    "precondiciones, límites y aprobación antes de cualquier escritura."
)


class ModelCorrectionService:
    """Generate only an immutable draft; this service has no execution authority.

    Direct invocation can only perform the configured model request and construct
    a DTO.  It cannot bind approval, execute tools or tests, apply files, or
    authorize a workflow.  ``ControlledProgrammingSession`` owns the mandatory
    ``awaiting_correction`` and explicit-approval gates.
    """

    def __init__(self, model_client: LocalModelClient):
        if type(model_client) is not LocalModelClient:
            raise ModelCorrectionError("invalid_model_correction")
        self._model_client = model_client

    def propose(
        self,
        context: ModelCorrectionContext,
    ) -> ModelCorrectionGenerationResult:
        if type(context) is not ModelCorrectionContext:
            raise ModelCorrectionError("invalid_model_correction")
        request = StructuredModelRequest(
            messages=(
                ModelMessage("system", _SYSTEM_PROMPT),
                ModelMessage(
                    "user",
                    json.dumps(
                        context.prompt_payload(),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                ),
            ),
            output_schema=MODEL_CORRECTION_OUTPUT_SCHEMA,
            temperature=0.0,
        )
        try:
            response = self._model_client.complete(request)
        except LocalModelError:
            raise ModelCorrectionError("invalid_model_correction") from None
        if type(response) is not StructuredModelResponse:
            raise ModelCorrectionError("invalid_model_correction")
        try:
            draft = ModelCorrectionProposalDraft.from_mapping(
                thaw_json(response.data)
            )
        except ModelCorrectionError:
            raise
        except BaseException:
            raise ModelCorrectionError("invalid_model_correction") from None
        return ModelCorrectionGenerationResult(draft, response.metadata)


@dataclass(frozen=True)
class AdaptedModelCorrection:
    draft_id: str
    proposal: ChangeProposal
    validated: ValidatedChangeProposal

    def __post_init__(self) -> None:
        if (
            type(self.draft_id) is not str
            or not self.draft_id.startswith("mc1_")
            or type(self.proposal) is not ChangeProposal
            or type(self.validated) is not ValidatedChangeProposal
            or not is_authentic_validated_proposal(self.validated)
            or self.validated.proposal is not self.proposal
            or self.validated.proposal_id != self.proposal.proposal_id
        ):
            raise ModelCorrectionError("invalid_model_correction")


class ModelCorrectionAdapter:
    """Convert a draft with trusted workspace, limits and tests, without writing."""

    def __init__(self, workspace, *, limits: WorkflowLimits | None = None):
        self.workspace = Path(workspace or ".").resolve()
        self.limits = limits or WorkflowLimits()
        self.path_policy = PathPolicy(self.workspace)
        self.validator = ChangeProposalValidator(self.workspace, self.limits)

    def adapt(
        self,
        draft: ModelCorrectionProposalDraft,
        *,
        tests: tuple[TestSpec, ...],
    ) -> AdaptedModelCorrection:
        if type(draft) is not ModelCorrectionProposalDraft:
            raise ModelCorrectionError("invalid_model_correction")
        trusted_tests = self._trusted_tests(tests)
        try:
            changes = tuple(
                FileChange(
                    path=item.path,
                    operation=item.operation,
                    new_content=item.new_content,
                    expected_sha256=item.expected_sha256,
                    justification=item.justification,
                )
                for item in draft.changes
            )
            budget = self._calculate_budget(changes)
            proposal = ChangeProposal(
                changes=changes,
                tests=trusted_tests,
                justification=draft.summary,
                risks=draft.risks,
                budget=budget,
            )
            validated = self.validator.validate(proposal)
        except PathValidationError:
            raise ModelCorrectionError("correction_path_not_allowed") from None
        except ModelCorrectionError:
            raise
        except ChangeProposalStructureError:
            raise ModelCorrectionError("invalid_model_correction") from None
        except UnicodeError:
            raise ModelCorrectionError("invalid_model_correction") from None
        except ValueError as exc:
            name = type(exc).__name__
            if "Path" in name:
                code = "correction_path_not_allowed"
            elif "Precondition" in name:
                code = "stale_correction_precondition"
            elif "Limit" in name or "Budget" in name:
                code = "correction_limit_exceeded"
            else:
                code = "invalid_model_correction"
            raise ModelCorrectionError(code) from None
        return AdaptedModelCorrection(draft.draft_id, proposal, validated)

    def _calculate_budget(
        self,
        changes: tuple[FileChange, ...],
    ) -> ProposalBudget:
        write_bytes = 0
        changed_lines = 0
        new_files = 0
        for change in changes:
            resolved = self.path_policy.resolve_for_write(change.path)
            new_bytes = change.new_content.encode("utf-8")
            write_bytes += len(new_bytes)
            if change.operation == "create":
                new_files += 1
                changed_lines += len(change.new_content.splitlines())
                continue
            try:
                with resolved.absolute.open("rb") as handle:
                    current_bytes = handle.read(
                        self.limits.max_read_bytes_per_file + 1
                    )
                if len(current_bytes) > self.limits.max_read_bytes_per_file:
                    raise ModelCorrectionError("correction_limit_exceeded")
                current = current_bytes.decode("utf-8")
            except (OSError, UnicodeError):
                raise ModelCorrectionError("stale_correction_precondition") from None
            if hashlib.sha256(current_bytes).hexdigest() != change.expected_sha256:
                raise ModelCorrectionError("stale_correction_precondition")
            matcher = SequenceMatcher(
                None,
                current.splitlines(),
                change.new_content.splitlines(),
                autojunk=False,
            )
            changed_lines += sum(
                max(old_end - old_start, new_end - new_start)
                for tag, old_start, old_end, new_start, new_end
                in matcher.get_opcodes()
                if tag != "equal"
            )
        return ProposalBudget(
            modified_files=len(changes),
            new_files=new_files,
            write_bytes=write_bytes,
            changed_lines=changed_lines,
        )

    @staticmethod
    def _trusted_tests(tests: tuple[TestSpec, ...]) -> tuple[TestSpec, ...]:
        if type(tests) is not tuple or any(type(item) is not TestSpec for item in tests):
            raise ModelCorrectionError("invalid_model_correction")
        focused = tuple(item for item in tests if item.scope == "focused")
        return (*focused, TestSpec("full"))
