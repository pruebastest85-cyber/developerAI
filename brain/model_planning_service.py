from __future__ import annotations

import json
from dataclasses import dataclass
from types import MappingProxyType

from brain.local_model_client import (
    LocalModelClient,
    ModelMessage,
    ModelResponseMetadata,
    StructuredModelRequest,
    StructuredModelResponse,
)
from brain.model_plan import (
    MODEL_PLAN_OUTPUT_SCHEMA,
    SAFE_MODEL_OPERATION_CATALOG,
    ModelPlanAdapter,
    ModelPlanDecision,
)
from brain.structured_json import thaw_json
from brain.workflow_plan import WorkflowPlan


MAX_MODEL_PLANNING_REQUEST_BYTES = 256 * 1024
MODEL_PLANNING_SYSTEM_PROMPT = (
    "Devuelve únicamente un objeto JSON que cumpla el esquema solicitado. "
    "La salida es una propuesta declarativa para revisión, no una orden de "
    "ejecución. Usa solo las herramientas, acciones y argumentos presentados "
    "por el esquema. No incluyas aprobaciones, tokens, bindings, resultados, "
    "estado runtime, políticas, permisos ni instrucciones de ejecución. La "
    "arquitectura validará después la semántica y conservará toda autoridad."
)
_ERROR_MESSAGES = MappingProxyType(
    {
        "invalid_request": "La solicitud de planificación no es válida.",
        "service_unavailable": "El servicio de planificación no está disponible.",
        "invalid_model_response": "La respuesta del modelo no es utilizable.",
    }
)


class ModelPlanningServiceError(ValueError):
    """Public, closed and non-sensitive planning-service failure."""

    __slots__ = ("code",)

    def __init__(self, code: str):
        safe_code = code if code in _ERROR_MESSAGES else "invalid_model_response"
        self.code = safe_code
        super().__init__(_ERROR_MESSAGES[safe_code])


@dataclass(frozen=True)
class ModelPlanningResult:
    """Canonical validated decision, workflow and sanitized model metadata."""

    decision: ModelPlanDecision
    workflow: WorkflowPlan
    metadata: ModelResponseMetadata

    def __post_init__(self) -> None:
        if (
            type(self.decision) is not ModelPlanDecision
            or type(self.workflow) is not WorkflowPlan
            or type(self.metadata) is not ModelResponseMetadata
        ):
            raise ModelPlanningServiceError("invalid_model_response")


class ModelPlanningService:
    """Turn one structured local-model response into a review-only workflow."""

    def __init__(
        self,
        model_client,
        adapter: ModelPlanAdapter | None = None,
    ):
        if type(model_client) is not LocalModelClient:
            raise ModelPlanningServiceError("service_unavailable")
        if adapter is not None and type(adapter) is not ModelPlanAdapter:
            raise ModelPlanningServiceError("service_unavailable")
        self._model_client = model_client
        self._adapter = adapter or ModelPlanAdapter(
            SAFE_MODEL_OPERATION_CATALOG
        )
        self._max_request_bytes = MAX_MODEL_PLANNING_REQUEST_BYTES

    def plan(self, user_request: str) -> ModelPlanningResult:
        if type(user_request) is not str or not user_request:
            raise ModelPlanningServiceError("invalid_request")
        invalid_encoding = False
        try:
            request_size = len(user_request.encode("utf-8"))
        except UnicodeError:
            invalid_encoding = True
            request_size = 0
        if (
            invalid_encoding
            or request_size > self._max_request_bytes
        ):
            raise ModelPlanningServiceError("invalid_request") from None

        request = StructuredModelRequest(
            messages=(
                ModelMessage("system", MODEL_PLANNING_SYSTEM_PROMPT),
                ModelMessage("user", user_request),
            ),
            output_schema=MODEL_PLAN_OUTPUT_SCHEMA,
            temperature=0.0,
        )
        if (
            len(_serialized_request_body(self._model_client, request))
            > self._model_client.config.max_prompt_bytes
        ):
            raise ModelPlanningServiceError("invalid_request") from None
        response = self._model_client.complete(request)
        if type(response) is not StructuredModelResponse:
            raise ModelPlanningServiceError("invalid_model_response")

        payload = thaw_json(response.data)
        decision = ModelPlanDecision.from_mapping(payload)
        workflow = self._adapter.adapt(decision)
        return ModelPlanningResult(
            decision=decision,
            workflow=workflow,
            metadata=response.metadata,
        )


def _serialized_request_body(
    model_client: LocalModelClient,
    request: StructuredModelRequest,
) -> bytes:
    """Serialize exactly the body whose size LocalModelClient enforces."""
    tokens, temperature = model_client._request_values(request)
    if model_client.config.structured_format == "json_schema":
        response_format = {
            "type": "json_schema",
            "json_schema": {
                "name": request.output_schema.name,
                "strict": True,
                "schema": request.output_schema.to_openai_schema(),
            },
        }
    else:
        response_format = {"type": "json_object"}
    payload = {
        "model": model_client.config.model,
        "messages": [
            {"role": item.role, "content": item.content}
            for item in request.messages
        ],
        "temperature": temperature,
        "max_tokens": tokens,
        "response_format": response_format,
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
