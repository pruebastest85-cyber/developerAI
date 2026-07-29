from __future__ import annotations

import gc
import hashlib
import json
import math
import re
import threading
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from brain.model_plan import (
    SAFE_MODEL_OPERATION_CATALOG,
    ModelPlanAdapter,
    ModelPlanDecision,
    ModelPlanStep,
)
from brain.model_planning_service import ModelPlanningResult
from brain.workflow_plan import ResultRef, StepSpec, WorkflowPlan
from brain.workflow_runtime import WorkflowRuntimeState


MAPPING_PROXY_TYPE = type(MappingProxyType({}))
PLAN_ID_PATTERN = re.compile(r"^mp1_[0-9a-f]{64}$", re.ASCII)
PLAN_COMMAND_PATTERN = re.compile(
    r"^(aprobar-plan|rechazar-plan|cancelar-plan) (mp1_[0-9a-f]{64})$",
    re.ASCII,
)
IDENTITY_DOMAIN = "developer_ai.model_plan_review"
IDENTITY_VERSION = "1"
MAX_ARGUMENT_PREVIEW = 160
MAX_PRESENTATION_TEXT = 12_000
MAX_PREVIEW_DEPTH = 8
MAX_PREVIEW_ITEMS = 64
MAX_PREVIEW_SCALAR = 512
AUTHORITY_WARNING = (
    "Aprobar este plan autoriza iniciar su ejecución, pero no aprueba "
    "escrituras, comandos sensibles ni operaciones Git. Esas operaciones "
    "conservarán sus aprobaciones específicas."
)
SENSITIVE_KEY_PARTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "private_key",
    "pkey",
)
ERROR_MESSAGES = MappingProxyType(
    {
        "no_pending_plan": "No existe un plan del modelo pendiente.",
        "plan_id_mismatch": "El identificador no coincide con el plan pendiente.",
        "plan_already_consumed": "El plan ya fue consumido y no puede ejecutarse otra vez.",
        "plan_rejected": "El plan ya fue rechazado.",
        "plan_cancelled": "El plan ya fue cancelado.",
        "plan_revalidation_failed": "El plan pendiente no superó la revalidación.",
    }
)


class ModelPlanReviewError(ValueError):
    """Closed, non-sensitive failure at the global plan-review boundary."""

    __slots__ = ("code",)

    def __init__(self, code: str):
        safe_code = code if code in ERROR_MESSAGES else "plan_revalidation_failed"
        self.code = safe_code
        super().__init__(ERROR_MESSAGES[safe_code])


@dataclass(frozen=True)
class ModelPlanReviewStepView:
    id: str
    tool: str
    action: str
    arguments: tuple[tuple[str, str], ...]
    depends_on: tuple[str, ...]
    goal: str


@dataclass(frozen=True)
class ModelPlanReviewView:
    plan_id: str
    status: str
    goal: str
    step_count: int
    steps: tuple[ModelPlanReviewStepView, ...]
    text: str
    authority_warning: str = AUTHORITY_WARNING


@dataclass(frozen=True)
class ModelPlanReviewResult:
    plan_id: str
    status: str
    message: str
    runtime: WorkflowRuntimeState | None = None


@dataclass
class _PlanRecord:
    result: ModelPlanningResult
    plan_id: str
    trusted_mapping_ids: frozenset[int]
    goal: str
    steps: tuple[ModelPlanReviewStepView, ...]
    operations: tuple[str, ...]
    status: str = "pending"
    runtime: WorkflowRuntimeState | None = None
    execution_finished_logged: bool = False


def parse_model_plan_command(text: str) -> tuple[str, str] | None:
    if type(text) is not str:
        return None
    matched = PLAN_COMMAND_PATTERN.fullmatch(text.strip())
    if matched is None:
        return None
    action = {
        "aprobar-plan": "approve",
        "rechazar-plan": "reject",
        "cancelar-plan": "cancel",
    }[matched.group(1)]
    return action, matched.group(2)


class ModelPlanReviewController:
    """Hold, present and consume one model plan without granting tool authority."""

    def __init__(self, agent):
        self._agent = agent
        self._lock = threading.Lock()
        self._pending: _PlanRecord | None = None
        self._last_terminal: _PlanRecord | None = None

    def register(self, result: ModelPlanningResult) -> ModelPlanReviewView:
        if type(result) is not ModelPlanningResult:
            raise ModelPlanReviewError("plan_revalidation_failed")
        trusted_mapping_ids = _collect_trusted_mapping_ids(result)
        plan_id = model_plan_id(
            result,
            trusted_mapping_ids=trusted_mapping_ids,
        )
        steps = _build_step_views(result, trusted_mapping_ids)
        goal = result.decision.goal
        operations = tuple(
            f"{step.tool}/{step.action}" for step in result.workflow.steps
        )
        with self._lock:
            record = _PlanRecord(
                result=result,
                plan_id=plan_id,
                trusted_mapping_ids=trusted_mapping_ids,
                goal=goal,
                steps=steps,
                operations=operations,
            )
            if self._pending is not None:
                self._pending.status = "superseded"
                self._last_terminal = self._pending
            self._pending = record
            view = _build_view(record)
        self._log("model_plan_generated", record)
        return view

    def get_pending(self) -> ModelPlanReviewView | None:
        with self._lock:
            if self._pending is None:
                return None
            view = _build_view(self._pending)
            record = self._pending
        self._log("model_plan_presented", record)
        return view

    def render_pending(self) -> str:
        view = self.get_pending()
        if view is None:
            raise ModelPlanReviewError("no_pending_plan")
        return view.text

    def approve(self, plan_id: str) -> WorkflowRuntimeState:
        with self._lock:
            record = self._require_pending(plan_id)
            revalidation_failed = False
            try:
                decision, workflow = _revalidate(record)
            except (ModelPlanReviewError, TypeError, ValueError):
                revalidation_failed = True
                decision = None
                workflow = None
            if revalidation_failed:
                record.status = "revalidation_failed"
                self._pending = None
                self._last_terminal = record
                self._log("model_plan_revalidation_failed", record)
                raise ModelPlanReviewError("plan_revalidation_failed") from None
            record.status = "executing"
            self._pending = None
            self._last_terminal = record

        self._log("model_plan_approved", record)
        self._log("model_plan_execution_started", record)
        try:
            runtime = self._agent.execution_engine.run_workflow(
                workflow,
                goal=decision.goal,
                safe_logging=True,
            )
        except BaseException as exc:
            from brain.approval_controller import ApprovalRequiredError

            runtime = self._agent.execution_engine.last_workflow_runtime
            record.runtime = runtime
            if (
                runtime is not None
                and runtime.status == "awaiting_approval"
                and isinstance(exc, ApprovalRequiredError)
            ):
                record.status = "awaiting_operation_approval"
                self._attach_operational_observer(exc, record)
            else:
                record.status = "failed"
                self._finish_execution(record)
            raise

        record.runtime = runtime
        if runtime.status == "completed":
            record.status = "completed"
        elif runtime.status == "awaiting_approval":
            record.status = "awaiting_operation_approval"
        else:
            record.status = "failed"
        if record.status != "awaiting_operation_approval":
            self._finish_execution(record)
        return runtime

    def _attach_operational_observer(
        self,
        error,
        record: _PlanRecord,
    ) -> None:
        from brain.approval_controller import ApprovalRequiredError

        continuation = error.execute
        cancellation = error.on_cancel
        gate_lock = threading.Lock()
        claimed = False

        def claim_pause() -> bool:
            nonlocal claimed
            with gate_lock:
                if claimed:
                    return False
                claimed = True
                return True

        def continue_and_observe():
            if not claim_pause():
                return None
            try:
                result = continuation()
            except ApprovalRequiredError as next_error:
                record.runtime = self._agent.execution_engine.last_workflow_runtime
                record.status = "awaiting_operation_approval"
                self._attach_operational_observer(next_error, record)
                raise
            except BaseException:
                record.runtime = self._agent.execution_engine.last_workflow_runtime
                record.status = "failed"
                self._finish_execution(record)
                raise
            record.runtime = self._agent.execution_engine.last_workflow_runtime
            self._sync_terminal_status(record)
            if record.status != "awaiting_operation_approval":
                self._finish_execution(record)
            return result

        def cancel_and_observe(reason: str):
            if not claim_pause():
                return None
            try:
                if cancellation is not None:
                    return cancellation(reason)
                return None
            finally:
                record.runtime = self._agent.execution_engine.last_workflow_runtime
                self._sync_terminal_status(record)
                if record.status == "awaiting_operation_approval":
                    record.status = "cancelled"
                self._finish_execution(record)

        error.execute = continue_and_observe
        error.on_cancel = cancel_and_observe

    @staticmethod
    def _sync_terminal_status(record: _PlanRecord) -> None:
        runtime = record.runtime
        if runtime is None:
            record.status = "failed"
        elif runtime.status == "awaiting_approval":
            record.status = "awaiting_operation_approval"
        elif runtime.status in {"completed", "failed", "cancelled"}:
            record.status = runtime.status
        else:
            record.status = "failed"

    def _finish_execution(self, record: _PlanRecord) -> None:
        with self._lock:
            if record.execution_finished_logged:
                return
            record.execution_finished_logged = True
        self._log("model_plan_execution_finished", record)

    def reject(self, plan_id: str) -> ModelPlanReviewResult:
        with self._lock:
            record = self._require_pending(plan_id)
            record.status = "rejected"
            self._pending = None
            self._last_terminal = record
        self._log("model_plan_rejected", record)
        return ModelPlanReviewResult(
            plan_id=record.plan_id,
            status="rejected",
            message="El plan fue rechazado. No se inició ninguna ejecución.",
        )

    def cancel(self, plan_id: str) -> ModelPlanReviewResult:
        with self._lock:
            record = self._require_pending(plan_id)
            record.status = "cancelled"
            self._pending = None
            self._last_terminal = record
        self._log("model_plan_cancelled", record)
        return ModelPlanReviewResult(
            plan_id=record.plan_id,
            status="cancelled",
            message="El plan fue cancelado. No se inició ninguna ejecución.",
        )

    def _require_pending(self, plan_id: str) -> _PlanRecord:
        if type(plan_id) is not str or PLAN_ID_PATTERN.fullmatch(plan_id) is None:
            raise ModelPlanReviewError("plan_id_mismatch")
        if self._pending is not None:
            if plan_id != self._pending.plan_id:
                raise ModelPlanReviewError("plan_id_mismatch")
            if self._pending.status != "pending":
                raise ModelPlanReviewError("plan_already_consumed")
            return self._pending
        if self._last_terminal is not None and plan_id == self._last_terminal.plan_id:
            if self._last_terminal.status == "rejected":
                raise ModelPlanReviewError("plan_rejected")
            if self._last_terminal.status == "cancelled":
                raise ModelPlanReviewError("plan_cancelled")
            raise ModelPlanReviewError("plan_already_consumed")
        raise ModelPlanReviewError("no_pending_plan")

    def _log(self, event: str, record: _PlanRecord) -> None:
        self._agent.action_logger.log(
            "model_plan_review",
            params={
                "event": event,
                "plan_id": record.plan_id,
                "status": record.status,
                "step_count": len(record.steps),
                "operations": list(record.operations),
                "approval_domain": "model_plan",
            },
            result=record.status,
        )


def model_plan_id(
    result: ModelPlanningResult,
    *,
    trusted_mapping_ids: frozenset[int] | None = None,
) -> str:
    if trusted_mapping_ids is None:
        trusted_mapping_ids = _collect_trusted_mapping_ids(result)
    payload = _identity_payload(result, trusted_mapping_ids)
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return "mp1_" + hashlib.sha256(encoded).hexdigest()


def _identity_payload(
    result: ModelPlanningResult,
    trusted_mapping_ids: frozenset[int],
) -> dict[str, Any]:
    if type(result) is not ModelPlanningResult:
        raise ModelPlanReviewError("plan_revalidation_failed")
    decision = result.decision
    workflow = result.workflow
    if type(decision) is not ModelPlanDecision or type(workflow) is not WorkflowPlan:
        raise ModelPlanReviewError("plan_revalidation_failed")
    if type(workflow.steps) is not tuple or (
        workflow.allowed_tools is not None
        and type(workflow.allowed_tools) is not frozenset
    ) or any(type(step) is not StepSpec for step in workflow.steps):
        raise ModelPlanReviewError("plan_revalidation_failed")
    return {
        "domain": IDENTITY_DOMAIN,
        "identity_version": IDENTITY_VERSION,
        "decision": _decision_payload(decision, trusted_mapping_ids),
        "workflow": {
            "steps": [
                {
                    "id": step.id,
                    "tool": step.tool,
                    "action": step.action,
                    "args": _json_value(step.args, trusted_mapping_ids),
                    "bindings": _bindings_value(
                        step.bindings,
                        trusted_mapping_ids,
                    ),
                    "goal": step.goal,
                    "depends_on": list(step.depends_on),
                    "approval": step.approval,
                    "required": step.required,
                    "repeat_completed": step.repeat_completed,
                }
                for step in workflow.steps
            ],
            "allowed_tools": (
                None
                if workflow.allowed_tools is None
                else sorted(workflow.allowed_tools)
            ),
        },
    }


def _decision_payload(
    decision: ModelPlanDecision,
    trusted_mapping_ids: frozenset[int],
) -> dict[str, Any]:
    if type(decision) is not ModelPlanDecision or type(decision.steps) is not tuple:
        raise ModelPlanReviewError("plan_revalidation_failed")
    if any(type(step) is not ModelPlanStep for step in decision.steps):
        raise ModelPlanReviewError("plan_revalidation_failed")
    return {
        "schema_version": decision.schema_version,
        "goal": decision.goal,
        "completed": decision.completed,
        "steps": [
            {
                "id": step.id,
                "tool": step.tool,
                "action": step.action,
                "args": _json_value(step.args, trusted_mapping_ids),
                "goal": step.goal,
                "depends_on": list(step.depends_on),
            }
            for step in decision.steps
        ],
        "message": decision.message,
    }


def _decision_payload_for_reconstruction(
    decision: ModelPlanDecision,
    trusted_mapping_ids: frozenset[int],
) -> dict[str, Any]:
    payload = _decision_payload(decision, trusted_mapping_ids)
    for serialized, step in zip(payload["steps"], decision.steps):
        serialized["justification"] = step.justification
    return payload


def _revalidate(record: _PlanRecord) -> tuple[ModelPlanDecision, WorkflowPlan]:
    if model_plan_id(
        record.result,
        trusted_mapping_ids=record.trusted_mapping_ids,
    ) != record.plan_id:
        raise ModelPlanReviewError("plan_revalidation_failed")
    decision_payload = _decision_payload_for_reconstruction(
        record.result.decision,
        record.trusted_mapping_ids,
    )
    decision = ModelPlanDecision.from_mapping(
        decision_payload
    )
    workflow = ModelPlanAdapter(SAFE_MODEL_OPERATION_CATALOG).adapt(decision)
    workflow.validate()
    if (
        workflow.identity() != record.result.workflow.identity()
        or workflow.allowed_tools != record.result.workflow.allowed_tools
        or decision.goal != record.result.decision.goal
        or model_plan_id(
        ModelPlanningResult(
            decision=decision,
            workflow=workflow,
            metadata=record.result.metadata,
        ),
        trusted_mapping_ids=_collect_trusted_mapping_ids_from_parts(
            decision,
            workflow,
        ),
    )
        != record.plan_id
    ):
        raise ModelPlanReviewError("plan_revalidation_failed")
    return decision, workflow


def _build_view(record: _PlanRecord) -> ModelPlanReviewView:
    steps = record.steps
    lines = [
        f"Plan: {record.plan_id}",
        f"Estado: {record.status}",
        f"Objetivo: {record.goal}",
        f"Pasos: {len(steps)}",
        AUTHORITY_WARNING,
    ]
    for index, step in enumerate(steps, start=1):
        lines.extend(
            [
                "",
                f"{index}. {step.id}",
                f"   Herramienta: {step.tool}",
                f"   Acción: {step.action}",
                "   Argumentos: "
                + (
                    ", ".join(f"{name}={value}" for name, value in step.arguments)
                    if step.arguments
                    else "(ninguno)"
                ),
                "   Dependencias: "
                + (", ".join(step.depends_on) if step.depends_on else "(ninguna)"),
            ]
        )
    text = "\n".join(lines)
    if len(text) > MAX_PRESENTATION_TEXT:
        text = text[: MAX_PRESENTATION_TEXT - 15] + "\n[TRUNCATED]"
    return ModelPlanReviewView(
        plan_id=record.plan_id,
        status=record.status,
        goal=record.goal,
        step_count=len(steps),
        steps=steps,
        text=text,
    )


def _build_step_views(
    result: ModelPlanningResult,
    trusted_mapping_ids: frozenset[int],
) -> tuple[ModelPlanReviewStepView, ...]:
    return tuple(
        ModelPlanReviewStepView(
            id=step.id,
            tool=step.tool,
            action=step.action,
            arguments=tuple(
                (
                    name,
                    _argument_preview(
                        name,
                        value,
                        trusted_mapping_ids,
                    ),
                )
                for name, value in sorted(step.args.items())
            ),
            depends_on=tuple(step.depends_on),
            goal=step.goal,
        )
        for step in result.workflow.execution_order()
    )


def _argument_preview(
    name: str,
    value: Any,
    trusted_mapping_ids: frozenset[int],
) -> str:
    safe_value = _sanitize_preview_value(
        name,
        value,
        trusted_mapping_ids,
        depth=0,
        active=set(),
    )
    rendered = json.dumps(
        safe_value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    if len(rendered) > MAX_ARGUMENT_PREVIEW:
        return rendered[: MAX_ARGUMENT_PREVIEW - 15] + "[TRUNCATED]"
    return rendered


def _sanitize_preview_value(
    name: str | None,
    value: Any,
    trusted_mapping_ids: frozenset[int],
    *,
    depth: int,
    active: set[int],
) -> Any:
    folded = "" if name is None else name.casefold()
    if any(part in folded for part in SENSITIVE_KEY_PARTS):
        return "[REDACTED]"
    if folded == "new_content":
        if type(value) is not str:
            return "[CONTENT OMITTED]"
        try:
            size = len(value.encode("utf-8"))
        except UnicodeError:
            return "[CONTENT OMITTED]"
        return f"[CONTENT OMITTED: {size} UTF-8 bytes]"
    if depth > MAX_PREVIEW_DEPTH:
        return "[TRUNCATED]"
    if value is None or type(value) in {bool, int}:
        return value
    if type(value) is str:
        if len(value) > MAX_PREVIEW_SCALAR:
            return value[:MAX_PREVIEW_SCALAR] + "[TRUNCATED]"
        return value
    if type(value) is float:
        return value if math.isfinite(value) else "[UNSUPPORTED]"
    if type(value) is MAPPING_PROXY_TYPE:
        backing = _mapping_proxy_backing(value)
        if id(value) not in trusted_mapping_ids or backing is None:
            return "[UNSUPPORTED]"
        value = backing
    elif type(value) is not dict:
        if type(value) not in {list, tuple}:
            return "[UNSUPPORTED]"

    container_id = id(value)
    if container_id in active:
        return "[UNSUPPORTED]"
    active.add(container_id)
    try:
        if type(value) is dict:
            result = {}
            for index, (key, nested) in enumerate(value.items()):
                if index >= MAX_PREVIEW_ITEMS:
                    result["[TRUNCATED]"] = "[TRUNCATED]"
                    break
                if type(key) is not str:
                    return "[UNSUPPORTED]"
                result[key] = _sanitize_preview_value(
                    key,
                    nested,
                    trusted_mapping_ids,
                    depth=depth + 1,
                    active=active,
                )
            return result
        result = []
        for index, nested in enumerate(value):
            if index >= MAX_PREVIEW_ITEMS:
                result.append("[TRUNCATED]")
                break
            result.append(
                _sanitize_preview_value(
                    None,
                    nested,
                    trusted_mapping_ids,
                    depth=depth + 1,
                    active=active,
                )
            )
        return result
    finally:
        active.remove(container_id)


def _json_value(
    value: Any,
    trusted_mapping_ids: frozenset[int],
    *,
    active: set[int] | None = None,
) -> Any:
    if active is None:
        active = set()
    if value is None or type(value) in {str, bool, int}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ModelPlanReviewError("plan_revalidation_failed")
        return value
    if type(value) is MAPPING_PROXY_TYPE:
        backing = _mapping_proxy_backing(value)
        if id(value) not in trusted_mapping_ids or backing is None:
            raise ModelPlanReviewError("plan_revalidation_failed")
        value = backing
    if type(value) in {dict, MAPPING_PROXY_TYPE, list, tuple}:
        container_id = id(value)
        if container_id in active:
            raise ModelPlanReviewError("plan_revalidation_failed")
        active.add(container_id)
        try:
            if type(value) is dict:
                copied = {}
                for key, nested in value.items():
                    if type(key) is not str:
                        raise ModelPlanReviewError("plan_revalidation_failed")
                    copied[key] = _json_value(
                        nested,
                        trusted_mapping_ids,
                        active=active,
                    )
                return copied
            return [
                _json_value(item, trusted_mapping_ids, active=active)
                for item in value
            ]
        finally:
            active.remove(container_id)
    if type(value) is ResultRef:
        return {"step_id": value.step_id, "path": list(value.path)}
    raise ModelPlanReviewError("plan_revalidation_failed")


def _bindings_value(
    value: Any,
    trusted_mapping_ids: frozenset[int],
) -> dict[str, Any]:
    if type(value) not in {dict, MAPPING_PROXY_TYPE}:
        raise ModelPlanReviewError("plan_revalidation_failed")
    if (
        type(value) is MAPPING_PROXY_TYPE
        and id(value) not in trusted_mapping_ids
    ):
        raise ModelPlanReviewError("plan_revalidation_failed")
    if type(value) is MAPPING_PROXY_TYPE:
        backing = _mapping_proxy_backing(value)
        if backing is None:
            raise ModelPlanReviewError("plan_revalidation_failed")
        value = backing
    copied = {}
    for name, reference in value.items():
        if type(name) is not str or type(reference) is not ResultRef:
            raise ModelPlanReviewError("plan_revalidation_failed")
        if type(reference.path) is not tuple:
            raise ModelPlanReviewError("plan_revalidation_failed")
        copied[name] = {
            "step_id": reference.step_id,
            "path": [
                _json_value(segment, trusted_mapping_ids)
                for segment in reference.path
            ],
        }
    return copied


def _collect_trusted_mapping_ids(
    result: ModelPlanningResult,
) -> frozenset[int]:
    if type(result) is not ModelPlanningResult:
        raise ModelPlanReviewError("plan_revalidation_failed")
    return _collect_trusted_mapping_ids_from_parts(
        result.decision,
        result.workflow,
    )


def _collect_trusted_mapping_ids_from_parts(
    decision: ModelPlanDecision,
    workflow: WorkflowPlan,
) -> frozenset[int]:
    if type(decision) is not ModelPlanDecision or type(workflow) is not WorkflowPlan:
        raise ModelPlanReviewError("plan_revalidation_failed")
    mappings = []
    for step in decision.steps:
        if type(step) is not ModelPlanStep or type(step.args) is not MAPPING_PROXY_TYPE:
            raise ModelPlanReviewError("plan_revalidation_failed")
        mappings.append(step.args)
    for step in workflow.steps:
        if (
            type(step) is not StepSpec
            or type(step.args) is not MAPPING_PROXY_TYPE
            or type(step.bindings) is not MAPPING_PROXY_TYPE
        ):
            raise ModelPlanReviewError("plan_revalidation_failed")
        mappings.extend((step.args, step.bindings))
    trusted = set()
    pending = list(mappings)
    seen_containers = set()
    while pending:
        mapping = pending.pop()
        if id(mapping) in trusted:
            continue
        backing = _mapping_proxy_backing(mapping)
        if backing is None:
            raise ModelPlanReviewError("plan_revalidation_failed")
        trusted.add(id(mapping))
        _queue_nested_mapping_proxies(
            backing,
            pending,
            seen_containers,
        )
    return frozenset(trusted)


def _queue_nested_mapping_proxies(
    value: Any,
    pending: list,
    seen_containers: set[int],
) -> None:
    if type(value) is MAPPING_PROXY_TYPE:
        pending.append(value)
        return
    if type(value) not in {dict, list, tuple}:
        return
    container_id = id(value)
    if container_id in seen_containers:
        raise ModelPlanReviewError("plan_revalidation_failed")
    seen_containers.add(container_id)
    try:
        values = value.values() if type(value) is dict else value
        for nested in values:
            _queue_nested_mapping_proxies(
                nested,
                pending,
                seen_containers,
            )
    finally:
        seen_containers.remove(container_id)


def _mapping_proxy_backing(value: Any) -> dict[str, Any] | None:
    if type(value) is not MAPPING_PROXY_TYPE:
        return None
    try:
        referents = gc.get_referents(value)
    except BaseException:
        return None
    if len(referents) != 1 or type(referents[0]) is not dict:
        return None
    return referents[0]
