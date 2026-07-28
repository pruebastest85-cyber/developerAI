from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Literal, Tuple, Type


ToolStatus = Literal["ok", "failed", "partial"]
NonePolicy = Literal["ok", "failed", "passthrough"]
VALID_STATUSES = frozenset({"ok", "failed", "partial"})
UNHANDLED = object()


@dataclass(frozen=True)
class ToolResult:
    status: ToolStatus
    tool_name: str
    data: Any = None
    message: str = ""
    error: str | None = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    retryable: bool = False

    def __post_init__(self) -> None:
        if self.status not in VALID_STATUSES:
            raise ValueError(f"Estado de herramienta no válido: {self.status}")
        if not isinstance(self.tool_name, str) or not self.tool_name.strip():
            raise ValueError("tool_name debe ser una cadena no vacía")
        if not isinstance(self.message, str):
            raise TypeError("message debe ser una cadena")
        if self.error is not None and not isinstance(self.error, str):
            raise TypeError("error debe ser una cadena o None")
        if not isinstance(self.metadata, dict):
            raise TypeError("metadata debe ser un diccionario")
        if not isinstance(self.retryable, bool):
            raise TypeError("retryable debe ser bool")
        if self.status == "ok" and (self.error is not None or self.retryable):
            raise ValueError("Un resultado ok no puede contener error ni ser retryable")
        if self.status == "failed" and not (self.error or self.message):
            raise ValueError("Un resultado failed requiere error o message")
        if self.status == "partial" and self.data is None and not self.message:
            raise ValueError("Un resultado partial requiere data o message")
        if self.retryable and self.status not in {"failed", "partial"}:
            raise ValueError("retryable solo es válido para failed o partial")
        object.__setattr__(self, "metadata", copy.deepcopy(self.metadata))

    @classmethod
    def success(
        cls,
        tool_name: str,
        data: Any = None,
        message: str = "",
        metadata: Dict[str, Any] | None = None,
    ) -> "ToolResult":
        return cls(
            status="ok",
            tool_name=tool_name,
            data=data,
            message=message,
            metadata=metadata or {},
        )

    @classmethod
    def failure(
        cls,
        tool_name: str,
        *,
        error: str | None = None,
        message: str = "",
        data: Any = None,
        metadata: Dict[str, Any] | None = None,
        retryable: bool = False,
    ) -> "ToolResult":
        return cls(
            status="failed",
            tool_name=tool_name,
            data=data,
            message=message,
            error=error,
            metadata=metadata or {},
            retryable=retryable,
        )

    @classmethod
    def incomplete(
        cls,
        tool_name: str,
        *,
        data: Any = None,
        message: str = "",
        error: str | None = None,
        metadata: Dict[str, Any] | None = None,
        retryable: bool = False,
    ) -> "ToolResult":
        return cls(
            status="partial",
            tool_name=tool_name,
            data=data,
            message=message,
            error=error,
            metadata=metadata or {},
            retryable=retryable,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "tool_name": self.tool_name,
            "data": self.data,
            "message": self.message,
            "error": self.error,
            "metadata": copy.deepcopy(self.metadata),
            "retryable": self.retryable,
        }


def normalize_tool_result(
    value: Any,
    *,
    tool_name: str,
    none_policy: NonePolicy = "ok",
) -> ToolResult | None:
    """Normalize one historical tool return without interpreting human text.

    ``passthrough`` is reserved for existing routing sentinels where ``None``
    means "not handled"; real tool executions should select ``ok`` or ``failed``.
    """
    if isinstance(value, ToolResult):
        if value.tool_name != tool_name:
            raise ValueError(
                f"ToolResult pertenece a {value.tool_name}, no a {tool_name}"
            )
        return value

    if value is None:
        if none_policy == "passthrough":
            return None
        if none_policy == "failed":
            return ToolResult.failure(
                tool_name,
                error="La herramienta no devolvió un resultado",
            )
        if none_policy != "ok":
            raise ValueError(f"Política de None no válida: {none_policy}")
        return ToolResult.success(tool_name)

    if isinstance(value, dict) and isinstance(value.get("ok"), bool):
        if value["ok"]:
            return ToolResult.success(tool_name, data=value)
        error = str(value.get("stderr") or value.get("error") or "La herramienta falló")
        return ToolResult.failure(tool_name, error=error, data=value)

    return ToolResult.success(tool_name, data=value)


def execute_and_normalize(
    tool_name: str,
    action: Callable[[], Any],
    *,
    none_policy: NonePolicy = "ok",
    operational_exceptions: Tuple[Type[Exception], ...] = (),
    retryable: bool = False,
) -> ToolResult | None:
    """Execute and normalize, converting only explicitly declared exceptions."""
    from brain.approval_controller import ApprovalRequiredError

    if not isinstance(operational_exceptions, tuple):
        raise TypeError("operational_exceptions debe ser una tupla")
    for exception_type in operational_exceptions:
        if (
            not isinstance(exception_type, type)
            or not issubclass(exception_type, Exception)
            or exception_type in {Exception, BaseException}
        ):
            raise ValueError(
                "Las excepciones operativas deben ser subclases concretas de Exception"
            )

    try:
        value = action()
    except ApprovalRequiredError:
        raise
    except operational_exceptions as exc:
        return ToolResult.failure(
            tool_name,
            error=str(exc),
            metadata={"exception_type": type(exc).__name__},
            retryable=retryable,
        )
    return normalize_tool_result(
        value,
        tool_name=tool_name,
        none_policy=none_policy,
    )


def legacy_tool_value(result: ToolResult | None) -> Any:
    """Present results through historical interfaces.

    Exact raw-value compatibility is retained for successful results. Failed
    and partial results keep their explicit status and complete structured
    payload so they cannot be mistaken for successful raw data.
    """
    if result is None:
        return None
    if result.status == "ok":
        if result.data is not None:
            return result.data
        if result.message:
            return result.message
        return None
    return result.to_dict()


def present_tool_result(result: Any) -> Any:
    """Convert a structured result into a user-facing value without repr."""
    if not isinstance(result, ToolResult):
        return result

    message = result.message.strip()
    error = result.error.strip() if result.error else ""
    if message and error:
        if message == error:
            return message
        return f"{message}\n\nError: {error}"
    if message:
        return message
    if error:
        return error
    if result.data is not None:
        return result.data
    return ""
