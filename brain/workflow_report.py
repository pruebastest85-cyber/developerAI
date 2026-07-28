from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from typing import Any


WORKFLOW_REPORT_STATUSES = frozenset(
    {"running", "awaiting_approval", "awaiting_correction", "completed", "failed", "cancelled"}
)


def _tuple(value):
    return value if isinstance(value, tuple) else tuple(value)

def _require(value, expected, name):
    if not isinstance(value, expected):
        raise TypeError(f"{name} debe ser {expected.__name__}")


def _require_items(values, expected, name):
    if any(not isinstance(value, expected) for value in values):
        raise TypeError(f"{name} contiene un valor no válido")

def _require_int(value, name):
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} debe ser int")


def _primitive(value: Any) -> Any:
    if is_dataclass(value):
        return {field.name: _primitive(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, dict):
        return {str(key): _primitive(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_primitive(item) for item in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise TypeError(f"Valor no serializable en WorkflowReport: {type(value).__name__}")


class _ReportModel:
    def to_dict(self) -> dict[str, Any]:
        return _primitive(self)


@dataclass(frozen=True)
class WorkflowStepReport(_ReportModel):
    step_id: str
    tool: str
    action: str
    goal: str
    status: str
    required: bool
    attempts: int
    message: str = ""
    error: str | None = None
    retryable: bool = False

    def __post_init__(self) -> None:
        for name in ("step_id", "tool", "action", "goal", "status", "message"):
            _require(getattr(self, name), str, name)
        if self.error is not None:
            _require(self.error, str, "error")
        _require(self.required, bool, "required")
        _require(self.retryable, bool, "retryable")
        _require_int(self.attempts, "attempts")


@dataclass(frozen=True)
class ChangedFileReport(_ReportModel):
    path: str
    kind: str
    insertions: int | None = 0
    deletions: int | None = 0
    binary: bool = False
    diff_included: bool = True
    omitted_reason: str | None = None

    def __post_init__(self) -> None:
        _require(self.path, str, "path")
        if self.kind not in {"created", "modified"}:
            raise ValueError("kind solo admite created o modified")
        for name in ("insertions", "deletions"):
            if getattr(self, name) is not None:
                _require_int(getattr(self, name), name)
        _require(self.binary, bool, "binary")
        _require(self.diff_included, bool, "diff_included")
        if self.omitted_reason is not None:
            _require(self.omitted_reason, str, "omitted_reason")


@dataclass(frozen=True)
class ChangeReport(_ReportModel):
    files: tuple[ChangedFileReport, ...] = ()
    insertions: int = 0
    deletions: int = 0
    binary_files: tuple[str, ...] = ()
    omitted_files: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "files", _tuple(self.files))
        object.__setattr__(self, "binary_files", _tuple(self.binary_files))
        object.__setattr__(self, "omitted_files", _tuple(self.omitted_files))
        _require_items(self.files, ChangedFileReport, "files")
        _require_items(self.binary_files, str, "binary_files")
        _require_items(self.omitted_files, str, "omitted_files")
        _require_int(self.insertions, "insertions")
        _require_int(self.deletions, "deletions")


@dataclass(frozen=True)
class TestRunReport(_ReportModel):
    scope: str
    status: str
    targets: tuple[str, ...] = ()
    tests_run: int = 0
    passed: int = 0
    failures: int = 0
    errors: int = 0
    skipped: int = 0
    failed_test_ids: tuple[str, ...] = ()
    error_test_ids: tuple[str, ...] = ()
    message: str = ""
    error: str | None = None

    def __post_init__(self) -> None:
        for name in ("targets", "failed_test_ids", "error_test_ids"):
            object.__setattr__(self, name, _tuple(getattr(self, name)))
            _require_items(getattr(self, name), str, name)
        for name in ("scope", "status", "message"):
            _require(getattr(self, name), str, name)
        if self.error is not None:
            _require(self.error, str, "error")
        for name in ("tests_run", "passed", "failures", "errors", "skipped"):
            _require_int(getattr(self, name), name)


@dataclass(frozen=True)
class CorrectionReport(_ReportModel):
    runtime_id: str
    status: str
    proposal_ids: tuple[str, ...] = ()
    applied_proposal_ids: tuple[str, ...] = ()
    correction_iterations: int = 0
    modified_files: tuple[str, ...] = ()
    new_files: tuple[str, ...] = ()
    terminal_reason: str | None = None

    def __post_init__(self) -> None:
        for name in ("proposal_ids", "applied_proposal_ids", "modified_files", "new_files"):
            object.__setattr__(self, name, _tuple(getattr(self, name)))
            _require_items(getattr(self, name), str, name)
        for name in ("runtime_id", "status"):
            _require(getattr(self, name), str, name)
        if self.terminal_reason is not None:
            _require(self.terminal_reason, str, "terminal_reason")
        _require_int(self.correction_iterations, "correction_iterations")


@dataclass(frozen=True)
class ApprovalReport(_ReportModel):
    status: str = "not_required"
    step_id: str | None = None
    request_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("step_id", "request_id"):
            if getattr(self, name) is not None:
                _require(getattr(self, name), str, name)
        if self.status not in {"not_required", "pending", "approved", "denied", "cancelled"}:
            raise ValueError("Estado de aprobación no válido")


@dataclass(frozen=True)
class LimitReport(_ReportModel):
    max_correction_iterations: int
    correction_iterations: int
    max_modified_files: int
    modified_files: int
    max_total_change_bytes: int
    total_change_bytes: int
    max_changed_lines: int
    changed_lines: int
    reached: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "reached", _tuple(self.reached))
        _require_items(self.reached, str, "reached")
        for name in (
            "max_correction_iterations", "correction_iterations",
            "max_modified_files", "modified_files", "max_total_change_bytes",
            "total_change_bytes", "max_changed_lines", "changed_lines",
        ):
            _require_int(getattr(self, name), name)


@dataclass(frozen=True)
class DiffSnapshot(_ReportModel):
    available: bool
    text: str = ""
    files: tuple[ChangedFileReport, ...] = ()
    insertions: int = 0
    deletions: int = 0
    binary_files: tuple[str, ...] = ()
    truncated: bool = False
    omitted_paths: tuple[str, ...] = ()
    error_code: str | None = None
    error_message: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "files", _tuple(self.files))
        object.__setattr__(self, "binary_files", _tuple(self.binary_files))
        object.__setattr__(self, "omitted_paths", _tuple(self.omitted_paths))
        _require_items(self.files, ChangedFileReport, "files")
        _require_items(self.binary_files, str, "binary_files")
        _require_items(self.omitted_paths, str, "omitted_paths")
        _require(self.available, bool, "available")
        _require(self.text, str, "text")
        _require(self.truncated, bool, "truncated")
        _require_int(self.insertions, "insertions")
        _require_int(self.deletions, "deletions")
        for name in ("error_code", "error_message"):
            if getattr(self, name) is not None:
                _require(getattr(self, name), str, name)


@dataclass(frozen=True)
class WorkflowReport(_ReportModel):
    workflow_id: str
    goal: str
    status: str
    steps: tuple[WorkflowStepReport, ...]
    changes: ChangeReport
    tests: tuple[TestRunReport, ...] = ()
    corrections: CorrectionReport | None = None
    approval: ApprovalReport | None = None
    limits: LimitReport | None = None
    diff: DiffSnapshot | None = None
    terminal_error: str | None = None
    automatic_commit_performed: bool = False
    automatic_push_performed: bool = False

    def __post_init__(self) -> None:
        _require(self.workflow_id, str, "workflow_id")
        _require(self.goal, str, "goal")
        if self.status not in WORKFLOW_REPORT_STATUSES:
            raise ValueError("Estado general de workflow no válido")
        object.__setattr__(self, "steps", _tuple(self.steps))
        object.__setattr__(self, "tests", _tuple(self.tests))
        _require_items(self.steps, WorkflowStepReport, "steps")
        _require_items(self.tests, TestRunReport, "tests")
        _require(self.changes, ChangeReport, "changes")
        for value, expected, name in (
            (self.corrections, CorrectionReport, "corrections"),
            (self.approval, ApprovalReport, "approval"),
            (self.limits, LimitReport, "limits"),
            (self.diff, DiffSnapshot, "diff"),
        ):
            if value is not None:
                _require(value, expected, name)
        if self.terminal_error is not None:
            _require(self.terminal_error, str, "terminal_error")
        _require(self.automatic_commit_performed, bool, "automatic_commit_performed")
        _require(self.automatic_push_performed, bool, "automatic_push_performed")

    @property
    def is_terminal(self) -> bool:
        return self.status in {"completed", "failed", "cancelled"}

    @property
    def is_resumable(self) -> bool:
        return self.status in {"awaiting_approval", "awaiting_correction"}
