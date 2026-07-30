from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from brain.model_errors import EndpointPolicyError, ModelConfigurationError


ALLOWED_PROVIDERS = frozenset({"lm_studio", "openai_compatible"})
ALLOWED_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "host.docker.internal"})
ALLOWED_STRUCTURED_FORMATS = frozenset({"json_schema", "prompt_json"})
MAX_API_KEY_LENGTH = 512
MAX_ENDPOINT_ID_LENGTH = 256


def canonical_endpoint_id(provider, host, port):
    rendered_host = f"[{host}]" if ":" in host else host
    return f"{provider}@{rendered_host}:{port}"


def is_valid_endpoint_id(value, expected_provider=None):
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= MAX_ENDPOINT_ID_LENGTH
        or any(not character.isprintable() for character in value)
        or value.count("@") != 1
    ):
        return False
    provider, authority = value.split("@", 1)
    if (
        provider not in ALLOWED_PROVIDERS
        or expected_provider is not None
        and provider != expected_provider
        or any(
        character in authority for character in "/?#"
        )
    ):
        return False
    try:
        parsed = urlsplit(f"http://{authority}")
        port = parsed.port
    except ValueError:
        return False
    host = parsed.hostname
    if (
        host is None
        or host.casefold() not in ALLOWED_HOSTS
        or port is None
        or not 1 <= port <= 65535
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        return False
    return value == canonical_endpoint_id(
        provider, host.casefold(), port
    )


@dataclass(frozen=True)
class LocalModelConfig:
    provider: str
    base_url: str
    model: str
    connect_timeout_seconds: float = 5.0
    read_timeout_seconds: float = 120.0
    max_http_body_bytes: int = 2 * 1024 * 1024
    max_content_bytes: int = 512 * 1024
    max_prompt_bytes: int = 1024 * 1024
    max_json_depth: int = 32
    max_output_tokens: int = 4096
    temperature: float = 0.0
    structured_format: str = "json_schema"
    api_key: str | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        for name in ("provider", "base_url", "model", "structured_format"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ModelConfigurationError()
            if value != value.strip():
                raise ModelConfigurationError(code=f"invalid_{name}")
        if self.provider not in ALLOWED_PROVIDERS:
            raise ModelConfigurationError(code="unsupported_provider")
        if self.structured_format not in ALLOWED_STRUCTURED_FORMATS:
            raise ModelConfigurationError(code="unsupported_structured_format")
        if self.api_key is not None:
            valid_key = (
                isinstance(self.api_key, str)
                and 0 < len(self.api_key) <= MAX_API_KEY_LENGTH
                and all(
                    ord(character) <= 255 and character.isprintable()
                    for character in self.api_key
                )
            )
            if not valid_key:
                raise ModelConfigurationError(code="invalid_api_key") from None
        for name in ("connect_timeout_seconds", "read_timeout_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ModelConfigurationError(code=f"invalid_{name}")
            if not math.isfinite(value) or value <= 0:
                raise ModelConfigurationError(code=f"invalid_{name}")
        for name in (
            "max_http_body_bytes", "max_content_bytes", "max_prompt_bytes",
            "max_json_depth", "max_output_tokens",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ModelConfigurationError(code=f"invalid_{name}")
        if isinstance(self.temperature, bool) or not isinstance(
            self.temperature, (int, float)
        ):
            raise ModelConfigurationError(code="invalid_temperature")
        if not math.isfinite(self.temperature) or not 0 <= self.temperature <= 2:
            raise ModelConfigurationError(code="invalid_temperature")

        parsed = urlsplit(self.base_url)
        invalid_port = False
        try:
            port = parsed.port
        except ValueError:
            invalid_port = True
        if invalid_port:
            raise EndpointPolicyError(code="invalid_port") from None
        if parsed.scheme != "http":
            raise EndpointPolicyError(code="unsupported_scheme")
        if parsed.username is not None or parsed.password is not None:
            raise EndpointPolicyError(code="url_credentials_rejected")
        if parsed.query or parsed.fragment:
            raise EndpointPolicyError(code="url_suffix_rejected")
        if parsed.hostname is None or parsed.hostname.casefold() not in ALLOWED_HOSTS:
            raise EndpointPolicyError(code="host_rejected")
        if parsed.path not in {"/v1", "/v1/"}:
            raise EndpointPolicyError(code="path_rejected")
        if port is not None and not 1 <= port <= 65535:
            raise EndpointPolicyError(code="invalid_port")
        host = parsed.hostname.casefold()
        rendered_host = f"[{host}]" if ":" in host else host
        normalized = f"http://{rendered_host}:{port or 80}/v1"
        object.__setattr__(self, "base_url", normalized)
        object.__setattr__(self, "connect_timeout_seconds", float(self.connect_timeout_seconds))
        object.__setattr__(self, "read_timeout_seconds", float(self.read_timeout_seconds))
        object.__setattr__(self, "temperature", float(self.temperature))

    @property
    def endpoint_id(self) -> str:
        parsed = urlsplit(self.base_url)
        return canonical_endpoint_id(
            self.provider, parsed.hostname, parsed.port
        )

    @property
    def structured_output_mode(self) -> str:
        """Explicit structured-output policy, retaining the historical field."""
        return self.structured_format

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "base_url": self.base_url,
            "model": self.model,
            "connect_timeout_seconds": self.connect_timeout_seconds,
            "read_timeout_seconds": self.read_timeout_seconds,
            "max_http_body_bytes": self.max_http_body_bytes,
            "max_content_bytes": self.max_content_bytes,
            "max_prompt_bytes": self.max_prompt_bytes,
            "max_json_depth": self.max_json_depth,
            "max_output_tokens": self.max_output_tokens,
            "temperature": self.temperature,
            "structured_format": self.structured_format,
        }

    @classmethod
    def from_env(cls, environ=None) -> "LocalModelConfig":
        values = os.environ if environ is None else environ
        try:
            return cls(
                provider=values.get("DEVELOPERAI_MODEL_PROVIDER", "lm_studio"),
                base_url=values.get(
                    "DEVELOPERAI_MODEL_BASE_URL", "http://localhost:1234/v1"
                ),
                model=values.get("DEVELOPERAI_MODEL_NAME", ""),
                api_key=values.get("DEVELOPERAI_MODEL_API_KEY") or None,
                connect_timeout_seconds=float(values.get(
                    "DEVELOPERAI_MODEL_CONNECT_TIMEOUT", "5"
                )),
                read_timeout_seconds=float(values.get(
                    "DEVELOPERAI_MODEL_READ_TIMEOUT", "120"
                )),
                structured_format=values.get(
                    "DEVELOPERAI_MODEL_STRUCTURED_OUTPUT_MODE", "json_schema"
                ),
            )
        except (TypeError, ValueError) as exc:
            raise ModelConfigurationError(code="invalid_environment") from exc
