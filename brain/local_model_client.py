from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field

from brain.local_model_config import LocalModelConfig, is_valid_endpoint_id
from brain.model_errors import (
    IncompatibleOpenAIResponseError, MalformedHttpResponseError,
    MissingChoicesError, MissingContentError, MissingMessageError,
    ModelConfigurationError, StructuredOutputError,
)
from brain.model_transport import HttpClientTransport, TransportRequest
from brain.structured_json import (
    StructuredOutputSchema, freeze_json, strict_json_loads, thaw_json,
)

MAX_MESSAGES = 64
MAX_MODEL_NAME_LENGTH = 256
MAX_METADATA_TEXT_LENGTH = 128
PROMPT_JSON_INSTRUCTION = (
    "Return exclusively one JSON document that validates against the JSON "
    "Schema below. Do not use Markdown, code fences, comments, prefixes, "
    "suffixes, explanations, or any text outside the JSON document. "
    "Include every required field and do not include additional properties "
    "that the schema does not permit. "
    "JSON Schema:"
)


def _safe_identifier(value):
    if not isinstance(value, str) or not 0 < len(value) <= MAX_METADATA_TEXT_LENGTH:
        return None
    if any(not (char.isascii() and (char.isalnum() or char in "-_.:")) for char in value):
        return None
    return value


def _safe_text(value, max_length):
    return (
        isinstance(value, str)
        and 0 < len(value) <= max_length
        and all(character.isprintable() for character in value)
    )


def _valid_token_count(value):
    return (
        value is None
        or isinstance(value, int)
        and not isinstance(value, bool)
        and value >= 0
    )


def _validate_json_data(value, active=None):
    active = set() if active is None else active
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ModelConfigurationError(code="invalid_response_data")
        return
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ModelConfigurationError(code="invalid_response_data")
        value_id = id(value)
        if value_id in active:
            raise ModelConfigurationError(code="invalid_response_data")
        active.add(value_id)
        try:
            for item in value.values():
                _validate_json_data(item, active)
        finally:
            active.remove(value_id)
        return
    if isinstance(value, list):
        value_id = id(value)
        if value_id in active:
            raise ModelConfigurationError(code="invalid_response_data")
        active.add(value_id)
        try:
            for item in value:
                _validate_json_data(item, active)
        finally:
            active.remove(value_id)
        return
    raise ModelConfigurationError(code="invalid_response_data")


@dataclass(frozen=True)
class ModelMessage:
    role: str
    content: str = field(repr=False)

    def __post_init__(self):
        if not isinstance(self.role, str) or self.role not in {
            "system", "user", "assistant"
        }:
            raise ModelConfigurationError(code="invalid_message_role")
        if not isinstance(self.content, str):
            raise ModelConfigurationError(code="invalid_message_content")


@dataclass(frozen=True)
class StructuredModelRequest:
    messages: tuple[ModelMessage, ...]
    output_schema: StructuredOutputSchema = field(repr=False)
    max_output_tokens: int | None = None
    temperature: float | None = None

    def __post_init__(self):
        if (not isinstance(self.messages, tuple) or not self.messages
                or len(self.messages) > MAX_MESSAGES):
            raise ModelConfigurationError(code="invalid_messages")
        if any(not isinstance(item, ModelMessage) for item in self.messages):
            raise ModelConfigurationError(code="invalid_messages")
        if not isinstance(self.output_schema, StructuredOutputSchema):
            raise ModelConfigurationError(code="invalid_output_schema")


@dataclass(frozen=True)
class ModelResponseMetadata:
    provider: str
    requested_model: str
    reported_model: str | None
    request_id: str | None
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    finish_reason: str | None
    duration_seconds: float
    endpoint_id: str
    structured_format: str

    def __post_init__(self):
        if (
            not isinstance(self.provider, str)
            or self.provider not in {"lm_studio", "openai_compatible"}
        ):
            raise ModelConfigurationError(code="invalid_response_metadata")
        if not _safe_text(self.requested_model, MAX_MODEL_NAME_LENGTH):
            raise ModelConfigurationError(code="invalid_response_metadata")
        for value in (self.reported_model, self.request_id, self.finish_reason):
            if value is not None and _safe_identifier(value) is None:
                raise ModelConfigurationError(code="invalid_response_metadata")
        for value in (self.input_tokens, self.output_tokens, self.total_tokens):
            if not _valid_token_count(value):
                raise ModelConfigurationError(code="invalid_response_metadata")
        if (
            isinstance(self.duration_seconds, bool)
            or not isinstance(self.duration_seconds, (int, float))
            or not math.isfinite(self.duration_seconds)
            or self.duration_seconds < 0
        ):
            raise ModelConfigurationError(code="invalid_response_metadata")
        if (
            not is_valid_endpoint_id(self.endpoint_id, self.provider)
            or not isinstance(self.structured_format, str)
            or self.structured_format not in {"json_schema", "prompt_json"}
        ):
            raise ModelConfigurationError(code="invalid_response_metadata")
        object.__setattr__(self, "duration_seconds", float(self.duration_seconds))

    def to_dict(self):
        return {
            "provider": self.provider,
            "requested_model": self.requested_model,
            "reported_model": self.reported_model,
            "request_id": self.request_id,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "finish_reason": self.finish_reason,
            "duration_seconds": self.duration_seconds,
            "endpoint_id": self.endpoint_id,
            "structured_format": self.structured_format,
        }


@dataclass(frozen=True)
class StructuredModelResponse:
    data: object = field(repr=False)
    metadata: ModelResponseMetadata

    def __post_init__(self):
        if not isinstance(self.metadata, ModelResponseMetadata):
            raise ModelConfigurationError(code="invalid_response_metadata")
        _validate_json_data(self.data)
        object.__setattr__(self, "data", freeze_json(self.data))

    def to_dict(self):
        return {"data": thaw_json(self.data), "metadata": self.metadata.to_dict()}


class LocalModelClient:
    """Isolated client: structured data in, validated structured data out."""

    def __init__(self, config: LocalModelConfig, transport=None, clock=None):
        if not isinstance(config, LocalModelConfig):
            raise ModelConfigurationError()
        self.config = config
        self.transport = transport or HttpClientTransport()
        self._clock = clock or time.monotonic

    @staticmethod
    def _token(value):
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

    def _request_values(self, request, config=None):
        config = self.config if config is None else config
        if not isinstance(request, StructuredModelRequest):
            raise ModelConfigurationError(code="invalid_request")
        tokens = request.max_output_tokens
        if tokens is None:
            tokens = config.max_output_tokens
        if isinstance(tokens, bool) or not isinstance(tokens, int) or not 0 < tokens <= config.max_output_tokens:
            raise ModelConfigurationError(code="invalid_max_output_tokens")
        temperature = config.temperature if request.temperature is None else request.temperature
        if isinstance(temperature, bool) or not isinstance(temperature, (int, float)):
            raise ModelConfigurationError(code="invalid_temperature")
        if not math.isfinite(temperature) or not 0 <= temperature <= 2:
            raise ModelConfigurationError(code="invalid_temperature")
        return tokens, float(temperature)

    @staticmethod
    def _prompt_json_messages(request, schema):
        schema_text = json.dumps(
            schema,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        instruction = f"{PROMPT_JSON_INSTRUCTION}\n{schema_text}"
        messages = [
            {"role": item.role, "content": item.content}
            for item in request.messages
        ]
        for message in messages:
            if message["role"] == "system":
                message["content"] = f"{message['content']}\n\n{instruction}"
                return messages
        if len(messages) >= MAX_MESSAGES:
            raise ModelConfigurationError(code="invalid_messages")
        return [{"role": "system", "content": instruction}, *messages]

    def _request_payload(self, request, config=None):
        config = self.config if config is None else config
        tokens, temperature = self._request_values(request, config)
        schema = request.output_schema.to_openai_schema()
        if config.structured_format == "json_schema":
            messages = [
                {"role": item.role, "content": item.content}
                for item in request.messages
            ]
            response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": request.output_schema.name,
                    "strict": True,
                    "schema": schema,
                },
            }
        else:
            messages = self._prompt_json_messages(request, schema)
            response_format = None
        payload = {
            "model": config.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": tokens,
        }
        if response_format is not None:
            payload["response_format"] = response_format
        return payload

    def _serialized_request_body(self, request, config=None):
        payload = self._request_payload(request, config)
        try:
            return json.dumps(
                payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False
            ).encode("utf-8")
        except (TypeError, ValueError):
            raise ModelConfigurationError(
                code="request_serialization_failed"
            ) from None

    def complete(self, request: StructuredModelRequest) -> StructuredModelResponse:
        config = self.config
        body = self._serialized_request_body(request, config)
        if len(body) > config.max_prompt_bytes:
            raise ModelConfigurationError(code="prompt_too_large")
        headers = [
            ("Accept", "application/json"),
            ("Accept-Encoding", "identity"),
            ("Content-Type", "application/json"),
            ("Host", url_host_header(config.base_url)),
        ]
        if config.api_key:
            headers.append(("Authorization", f"Bearer {config.api_key}"))
        transport_request = TransportRequest(
            method="POST",
            url=f"{config.base_url}/chat/completions",
            headers=tuple(headers),
            body=body,
            connect_timeout_seconds=config.connect_timeout_seconds,
            read_timeout_seconds=config.read_timeout_seconds,
            max_response_bytes=config.max_http_body_bytes,
        )
        started = self._clock()
        response = self.transport.send(transport_request)
        duration = max(0.0, self._clock() - started)
        malformed_envelope = False
        try:
            text = response.body.decode("utf-8")
            envelope = strict_json_loads(
                text, max_bytes=config.max_http_body_bytes,
                max_depth=config.max_json_depth,
            )
        except (UnicodeError, StructuredOutputError):
            malformed_envelope = True
        if malformed_envelope:
            raise MalformedHttpResponseError() from None
        if not isinstance(envelope, dict):
            raise IncompatibleOpenAIResponseError()
        choices = envelope.get("choices")
        if not isinstance(choices, list):
            if choices is None:
                raise MissingChoicesError()
            raise IncompatibleOpenAIResponseError()
        if not choices:
            raise MissingChoicesError()
        choice = choices[0]
        if not isinstance(choice, dict):
            raise IncompatibleOpenAIResponseError()
        if "message" not in choice:
            raise MissingMessageError()
        message = choice["message"]
        if not isinstance(message, dict):
            raise IncompatibleOpenAIResponseError()
        if "content" not in message:
            raise MissingContentError()
        content = message["content"]
        if not isinstance(content, str):
            raise MissingContentError()
        data = strict_json_loads(
            content, max_bytes=config.max_content_bytes,
            max_depth=config.max_json_depth,
        )
        request.output_schema.validate(data)
        usage = envelope.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        metadata = ModelResponseMetadata(
            provider=config.provider,
            requested_model=config.model,
            reported_model=_safe_identifier(envelope.get("model")),
            request_id=_safe_identifier(envelope.get("id")),
            input_tokens=self._token(usage.get("prompt_tokens", usage.get("input_tokens"))),
            output_tokens=self._token(usage.get("completion_tokens", usage.get("output_tokens"))),
            total_tokens=self._token(usage.get("total_tokens")),
            finish_reason=_safe_identifier(choice.get("finish_reason")),
            duration_seconds=duration,
            endpoint_id=config.endpoint_id,
            structured_format=config.structured_format,
        )
        return StructuredModelResponse(data, metadata)


def url_host_header(base_url):
    from urllib.parse import urlsplit
    parsed = urlsplit(base_url)
    host = parsed.hostname or ""
    rendered = f"[{host}]" if ":" in host else host
    return f"{rendered}:{parsed.port or 80}"
