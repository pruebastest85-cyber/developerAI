from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional


UUID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)
APPROVAL_VERBS = {"aprobar", "approve", "rechazar", "reject", "cancelar", "cancel"}
MODEL_PLAN_APPROVAL_VERBS = {
    "aprobar-plan", "rechazar-plan", "cancelar-plan",
}
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


@dataclass
class ApprovalCommand:
    action: str
    request_id: str


@dataclass
class PendingOperation:
    request_id: str
    tool_name: str
    action_name: str
    important_args: Dict[str, Any]
    execute: Callable[[], Any]
    description: str
    force_approval: bool = False
    on_cancel: Callable[[str], Any] | None = None
    on_request: Callable[[str], Any] | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class ApprovalRequired:
    request_id: str
    tool_name: str
    action_name: str
    important_args: Dict[str, Any]
    message: str


@dataclass
class ApprovalResult:
    status: str
    request_id: str
    message: str
    result: Any = None


class ApprovalRequiredError(PermissionError):
    def __init__(
        self,
        tool_name,
        action_name,
        important_args,
        execute,
        message,
        force_approval=False,
        on_cancel=None,
        on_request=None,
    ):
        super().__init__(message)
        self.tool_name = tool_name
        self.action_name = action_name
        self.important_args = important_args or {}
        self.execute = execute
        self.message = message
        self.force_approval = bool(force_approval)
        self.on_cancel = on_cancel
        self.on_request = on_request


def parse_approval_command(text: str) -> Optional[ApprovalCommand]:
    parts = text.strip().split()
    if len(parts) != 2:
        return None

    verb = parts[0].lower()
    request_id = parts[1]
    if not UUID_PATTERN.fullmatch(request_id):
        return None
    if request_id.lower() != request_id:
        return None

    mapping = {
        "aprobar": "approve",
        "approve": "approve",
        "rechazar": "reject",
        "reject": "reject",
        "cancelar": "cancel",
        "cancel": "cancel",
    }
    action = mapping.get(verb)
    if action is None:
        return None
    return ApprovalCommand(action=action, request_id=request_id)


def sanitize_important_args(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized = {}
        for key, nested_value in value.items():
            key_text = str(key).lower()
            if any(part in key_text for part in SENSITIVE_KEY_PARTS):
                sanitized[key] = "[REDACTED]"
            else:
                sanitized[key] = sanitize_important_args(nested_value)
        return sanitized
    if isinstance(value, list):
        return [sanitize_important_args(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_important_args(item) for item in value]
    return value


class ApprovalController:
    def __init__(self, agent):
        self.agent = agent
        self._pending_operations: Dict[str, PendingOperation] = {}

    def _risk_for(self, tool_name: str, action_name: str) -> str:
        risk = self.agent.permission_manager.get_risk_level(tool_name, action_name=action_name)
        return str(risk).upper()

    def _build_pending_message(self, pending: PendingOperation) -> str:
        sanitized_args = sanitize_important_args(pending.important_args)
        risk = self._risk_for(pending.tool_name, pending.action_name)
        lines = [
            "Se requiere aprobación.",
            "",
            f"Solicitud: {pending.request_id}",
            f"Herramienta: {pending.tool_name}",
            f"Acción: {pending.action_name}",
            f"Riesgo: {risk}",
            f"Objetivo: {pending.description}",
            f"Argumentos: {sanitized_args}",
            "",
            f"Para aprobar: aprobar {pending.request_id}",
            f"Para rechazar: rechazar {pending.request_id}",
            f"Para cancelar: cancelar {pending.request_id}",
        ]
        return "\n".join(lines)

    def _default_description(self, tool_name: str, action_name: str, important_args: Dict[str, Any]) -> str:
        args = sanitize_important_args(copy.deepcopy(important_args or {}))
        if "path" in args:
            return f"Modificar archivo {args['path']}"
        if "target" in args:
            return f"Operar sobre {args['target']}"
        if "query" in args:
            return f"Buscar usando query {args['query']}"
        if "command" in args:
            return f"Ejecutar comando {args['command']}"
        return f"Ejecutar {tool_name}.{action_name}"

    def request_operation(
        self,
        tool_name,
        action_name,
        important_args,
        execute,
        description=None,
        force_approval=False,
        on_cancel=None,
        on_request=None,
    ):
        if self._pending_operations:
            existing = next(iter(self._pending_operations.values()))
            return ApprovalResult(
                status="failed",
                request_id=existing.request_id,
                message=(
                    "Ya existe una solicitud pendiente. Resuélvela antes de crear otra: "
                    + existing.request_id
                ),
            )

        request = self.agent.create_operation_approval_request(
            tool_name,
            action_name,
            important_args=important_args,
            force=force_approval,
        )
        if not request or "request_id" not in request:
            return ApprovalResult(
                status="failed",
                request_id="",
                message="No se pudo crear una solicitud de aprobación para la operación solicitada.",
            )

        request_id = request["request_id"]
        pending = PendingOperation(
            request_id=request_id,
            tool_name=tool_name,
            action_name=action_name,
            important_args=copy.deepcopy(important_args or {}),
            execute=execute,
            description=description or self._default_description(tool_name, action_name, important_args or {}),
            force_approval=force_approval,
            on_cancel=on_cancel,
            on_request=on_request,
        )
        self._pending_operations[request_id] = pending
        if pending.on_request is not None:
            pending.on_request(request_id)
        return ApprovalRequired(
            request_id=request_id,
            tool_name=tool_name,
            action_name=action_name,
            important_args=sanitize_important_args(copy.deepcopy(important_args or {})),
            message=self._build_pending_message(pending),
        )

    def approve(self, request_id):
        pending = self._pending_operations.get(request_id)
        if pending is None:
            return ApprovalResult(
                status="not_found",
                request_id=request_id,
                message="No existe una solicitud pendiente con ese identificador.",
            )

        try:
            approval_token = self.agent.permission_manager.grant_approval(request_id)
            if not approval_token:
                return ApprovalResult(
                    status="cancelled",
                    request_id=request_id,
                    message="La solicitud ya no es válida o fue consumida.",
                )

            if not callable(pending.execute):
                return ApprovalResult(
                    status="failed",
                    request_id=request_id,
                    message="La operación pendiente no es ejecutable.",
                )

            result = self.agent.execute_tool(
                pending.tool_name,
                pending.execute,
                action_name=pending.action_name,
                important_args=pending.important_args,
                approval_token=approval_token,
                require_approval=pending.force_approval,
            )
            return ApprovalResult(
                status="approved",
                request_id=request_id,
                message="Operación aprobada y ejecutada correctamente.",
                result=result,
            )
        except ApprovalRequiredError as exc:
            if exc.on_request is None and exc.on_cancel is None:
                return ApprovalResult(
                    status="failed",
                    request_id=request_id,
                    message="La operación aprobada falló durante la ejecución.",
                )
            self._pending_operations.pop(request_id, None)
            requested = self.request_operation(
                tool_name=exc.tool_name,
                action_name=exc.action_name,
                important_args=exc.important_args,
                execute=exc.execute,
                description=f"{exc.tool_name}.{exc.action_name}",
                force_approval=exc.force_approval,
                on_cancel=exc.on_cancel,
                on_request=exc.on_request,
            )
            return ApprovalResult(
                status="awaiting_approval",
                request_id=getattr(requested, "request_id", ""),
                message=requested.message,
                result=requested,
            )
        except Exception:
            return ApprovalResult(
                status="failed",
                request_id=request_id,
                message="La operación aprobada falló durante la ejecución.",
            )
        finally:
            self._pending_operations.pop(request_id, None)

    def reject(self, request_id):
        pending = self._pending_operations.pop(request_id, None)
        if pending is None:
            return ApprovalResult(
                status="not_found",
                request_id=request_id,
                message="No existe una solicitud pendiente con ese identificador.",
            )

        self.agent.permission_manager.cancel_approval_request(request_id)
        if pending.on_cancel is not None:
            pending.on_cancel("rejected")
        return ApprovalResult(
            status="rejected",
            request_id=request_id,
            message="La solicitud fue rechazada. No se ejecutó ninguna operación.",
        )

    def cancel(self, request_id):
        pending = self._pending_operations.pop(request_id, None)
        if pending is None:
            return ApprovalResult(
                status="not_found",
                request_id=request_id,
                message="No existe una solicitud pendiente con ese identificador.",
            )

        self.agent.permission_manager.cancel_approval_request(request_id)
        if pending.on_cancel is not None:
            pending.on_cancel("cancelled")
        return ApprovalResult(
            status="cancelled",
            request_id=request_id,
            message="La solicitud fue cancelada. No se ejecutó ninguna operación.",
        )

    def get_pending(self, request_id=None):
        if request_id is None:
            return list(self._pending_operations.values())
        return self._pending_operations.get(request_id)


class ConversationalController:
    def __init__(self, agent, approval_controller=None):
        self.agent = agent
        self.approval_controller = approval_controller or ApprovalController(agent)

    def process_message(self, text: str) -> str:
        get_session = getattr(self.agent, "get_programming_session", None)
        if callable(get_session):
            session = get_session()
            if session.should_handle_command(text):
                return self.agent.respond(text)

        parts = text.strip().split()
        if parts and parts[0].lower() in MODEL_PLAN_APPROVAL_VERBS:
            from brain.model_plan_review import (
                ModelPlanReviewError,
                parse_model_plan_command,
            )

            command = parse_model_plan_command(text)
            if command is None:
                return (
                    "Comando de plan inválido. Usa: "
                    "aprobar-plan|rechazar-plan|cancelar-plan <plan_id exacto>"
                )
            action, plan_id = command
            try:
                if action == "approve":
                    runtime = self.agent.approve_model_plan(plan_id)
                    return f"Plan aprobado. Estado del workflow: {runtime.status}"
                if action == "reject":
                    return self.agent.reject_model_plan(plan_id).message
                return self.agent.cancel_model_plan(plan_id).message
            except ApprovalRequiredError as exc:
                return self._request_operation(exc)
            except ModelPlanReviewError as exc:
                return str(exc)

        if parts and parts[0].lower() in APPROVAL_VERBS:
            command = parse_approval_command(text)
            if command is None:
                return "Comando de aprobación inválido. Usa: aprobar|rechazar|cancelar <request_id exacto>"

            if command.action == "approve":
                result = self.approval_controller.approve(command.request_id)
            elif command.action == "reject":
                result = self.approval_controller.reject(command.request_id)
            else:
                result = self.approval_controller.cancel(command.request_id)

            if result.status == "approved":
                return result.message + "\nResultado:\n" + str(result.result)
            return result.message

        command = parse_approval_command(text)
        if command:
            if command.action == "approve":
                result = self.approval_controller.approve(command.request_id)
            elif command.action == "reject":
                result = self.approval_controller.reject(command.request_id)
            else:
                result = self.approval_controller.cancel(command.request_id)

            if result.status == "approved":
                return result.message + "\nResultado:\n" + str(result.result)
            return result.message

        try:
            return self.agent.respond(text)
        except ApprovalRequiredError as exc:
            return self._request_operation(exc)

    def _request_operation(self, error: ApprovalRequiredError) -> str:
        requested = self.approval_controller.request_operation(
            tool_name=error.tool_name,
            action_name=error.action_name,
            important_args=error.important_args,
            execute=error.execute,
            description=f"{error.tool_name}.{error.action_name}",
            force_approval=error.force_approval,
            on_cancel=error.on_cancel,
            on_request=error.on_request,
        )
        return requested.message
