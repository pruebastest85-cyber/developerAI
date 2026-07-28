from __future__ import annotations

from urllib.parse import urlsplit


_SAFE_ENDPOINT_HOSTS = frozenset(
    {"localhost", "127.0.0.1", "::1", "host.docker.internal"}
)
_SAFE_CODES = frozenset({
    "additional_properties_must_be_false", "compressed_response",
    "connect_timeout", "connection_failed", "dns_resolution_failed",
    "duplicate_content_length", "duplicate_json_key",
    "generated_content_too_large", "host_header_rejected", "host_rejected",
    "http_body_too_large", "http_error", "incompatible_openai_response",
    "invalid_api_key", "invalid_body", "invalid_configuration",
    "invalid_connect_timeout_seconds", "invalid_content_length",
    "invalid_enum", "invalid_environment", "invalid_headers", "invalid_json",
    "invalid_max_content_bytes", "invalid_max_http_body_bytes",
    "invalid_max_json_depth", "invalid_max_output_tokens",
    "invalid_max_prompt_bytes", "invalid_message_content",
    "invalid_message_role", "invalid_messages", "invalid_model",
    "invalid_output_schema", "invalid_port", "invalid_properties",
    "invalid_property_name", "invalid_provider",
    "invalid_read_timeout_seconds", "invalid_request", "invalid_required",
    "invalid_response_data", "invalid_response_metadata", "invalid_schema",
    "invalid_schema_limits", "invalid_schema_name",
    "invalid_structured_format", "invalid_temperature",
    "invalid_transport_url", "json_depth_exceeded",
    "keyword_not_allowed_for_type", "local_model_error",
    "malformed_http_response", "method_rejected", "missing_choices",
    "missing_content", "missing_items", "missing_message",
    "non_finite_number", "non_string_json_key", "path_rejected",
    "prompt_too_large", "protocol_error", "read_timeout",
    "recursive_schema", "recursive_value", "redirect_rejected",
    "request_serialization_failed", "resolved_address_rejected",
    "response_failed", "schema_too_deep", "schema_validation_failed",
    "structured_output_error", "timeout", "trailing_content",
    "unknown_schema_keyword", "unsupported_json_value",
    "unsupported_provider", "unsupported_schema_type", "unsupported_scheme",
    "unsupported_structured_format", "url_credentials_rejected",
    "url_suffix_rejected",
})


def _safe_code(value, fallback):
    return value if isinstance(value, str) and value in _SAFE_CODES else fallback


def _safe_endpoint(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    host = parsed.hostname
    if (
        parsed.scheme != "http"
        or host is None
        or host.casefold() not in _SAFE_ENDPOINT_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port is None
        or not 1 <= port <= 65535
    ):
        return None
    host = host.casefold()
    rendered = f"[{host}]" if ":" in host else host
    return f"http://{rendered}:{port}"


class LocalModelError(Exception):
    """Base error that deliberately retains only non-sensitive diagnostics."""

    default_code = "local_model_error"
    default_message = "La operación del modelo local falló."

    def __init__(self, *, code=None, endpoint=None, http_status=None):
        self.code = _safe_code(code, self.default_code)
        self.endpoint = _safe_endpoint(endpoint)
        self.http_status = (
            http_status
            if isinstance(http_status, int)
            and not isinstance(http_status, bool)
            and 100 <= http_status <= 599
            else None
        )
        super().__init__(self.default_message)


class ModelConfigurationError(LocalModelError):
    default_code = "invalid_configuration"
    default_message = "La configuración del modelo local no es válida."


class EndpointPolicyError(ModelConfigurationError):
    default_code = "endpoint_rejected"
    default_message = "El endpoint del modelo local no está permitido."


class ModelConnectionError(LocalModelError):
    default_code = "connection_failed"
    default_message = "No se pudo conectar con el modelo local."


class ModelTimeoutError(LocalModelError):
    default_code = "timeout"
    default_message = "La solicitud al modelo local excedió el tiempo permitido."


class ModelRedirectError(LocalModelError):
    default_code = "redirect_rejected"
    default_message = "El servidor intentó redirigir la solicitud."


class ModelHttpStatusError(LocalModelError):
    default_code = "http_error"
    default_message = "El servidor devolvió un estado HTTP no exitoso."


class ModelResponseTooLargeError(LocalModelError):
    default_code = "http_body_too_large"
    default_message = "La respuesta HTTP excede el límite permitido."


class ModelProtocolError(LocalModelError):
    default_code = "protocol_error"
    default_message = "La respuesta no cumple el protocolo esperado."


class MalformedHttpResponseError(ModelProtocolError):
    default_code = "malformed_http_response"
    default_message = "La respuesta HTTP contiene un cuerpo incompatible."


class IncompatibleOpenAIResponseError(ModelProtocolError):
    default_code = "incompatible_openai_response"
    default_message = "La respuesta no tiene una estructura OpenAI compatible."


class MissingChoicesError(ModelProtocolError):
    default_code = "missing_choices"
    default_message = "La respuesta no contiene choices utilizables."


class MissingMessageError(ModelProtocolError):
    default_code = "missing_message"
    default_message = "La respuesta no contiene un message utilizable."


class MissingContentError(ModelProtocolError):
    default_code = "missing_content"
    default_message = "La respuesta no contiene content textual."


class StructuredOutputError(LocalModelError):
    default_code = "structured_output_error"
    default_message = "La salida estructurada no es válida."


class InvalidJsonError(StructuredOutputError):
    default_code = "invalid_json"
    default_message = "La salida no es un documento JSON válido."


class TrailingContentError(StructuredOutputError):
    default_code = "trailing_content"
    default_message = "La salida contiene texto adicional fuera del JSON."


class DuplicateJsonKeyError(StructuredOutputError):
    default_code = "duplicate_json_key"
    default_message = "La salida JSON contiene claves duplicadas."


class NonFiniteNumberError(StructuredOutputError):
    default_code = "non_finite_number"
    default_message = "La salida JSON contiene un número no finito."


class JsonDepthExceededError(StructuredOutputError):
    default_code = "json_depth_exceeded"
    default_message = "La salida JSON excede la profundidad permitida."


class GeneratedContentTooLargeError(StructuredOutputError):
    default_code = "generated_content_too_large"
    default_message = "El contenido generado excede el límite permitido."


class SchemaValidationError(StructuredOutputError):
    default_code = "schema_validation_failed"
    default_message = "La salida JSON no cumple el esquema requerido."
