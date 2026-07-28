from __future__ import annotations

import http.client
import ipaddress
import math
import socket
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlsplit

from brain.model_errors import (
    EndpointPolicyError, MalformedHttpResponseError, ModelConnectionError,
    ModelHttpStatusError, ModelRedirectError, ModelResponseTooLargeError,
    ModelTimeoutError,
)
from brain.local_model_config import ALLOWED_HOSTS


@dataclass(frozen=True)
class TransportRequest:
    method: str
    url: str = field(repr=False)
    headers: tuple[tuple[str, str], ...] = field(repr=False)
    body: bytes = field(repr=False)
    connect_timeout_seconds: float = 5.0
    read_timeout_seconds: float = 120.0
    max_response_bytes: int = 2 * 1024 * 1024


@dataclass(frozen=True)
class TransportResponse:
    status: int
    headers: tuple[tuple[str, str], ...] = field(repr=False)
    body: bytes = field(repr=False)


class ModelTransport(Protocol):
    def send(self, request: TransportRequest) -> TransportResponse: ...


class HttpClientTransport:
    """Small HTTP transport with DNS pinning and no redirect handling."""

    def __init__(self, resolver=None, connection_factory=None):
        self._resolver = resolver or socket.getaddrinfo
        self._connection_factory = connection_factory or self._new_connection

    @staticmethod
    def _new_connection(address, port, timeout):
        return http.client.HTTPConnection(address, port, timeout=timeout)

    @staticmethod
    def _allowed_address(host, address):
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return False
        if (ip.is_unspecified or ip.is_multicast
                or ip.is_reserved and not ip.is_loopback):
            return False
        if host in {"localhost", "127.0.0.1", "::1"}:
            return ip.is_loopback
        return ip.is_loopback or ip.is_private or ip.is_link_local

    def _resolve(self, host, port):
        resolution_failed = False
        try:
            answers = self._resolver(host, port, type=socket.SOCK_STREAM)
        except (OSError, socket.gaierror):
            resolution_failed = True
        if resolution_failed:
            raise ModelConnectionError(code="dns_resolution_failed") from None
        addresses = []
        for answer in answers:
            address = answer if isinstance(answer, str) else answer[4][0]
            if not self._allowed_address(host, address):
                raise EndpointPolicyError(code="resolved_address_rejected")
            if address not in addresses:
                addresses.append(address)
        if not addresses:
            raise ModelConnectionError(code="dns_resolution_failed")
        return addresses[0]

    def _connect(self, address, port, timeout):
        connection = None
        failure = None
        try:
            connection = self._connection_factory(address, port, timeout)
            connection.connect()
        except socket.timeout:
            failure = "connect_timeout"
        except OSError:
            failure = "connection_failed"
        return connection, failure

    @staticmethod
    def _expected_host_header(host, port):
        rendered = f"[{host}]" if ":" in host else host
        return f"{rendered}:{port}"

    @classmethod
    def _validate_request(cls, request):
        if not isinstance(request, TransportRequest) or request.method != "POST":
            raise EndpointPolicyError(code="method_rejected")
        invalid_url = False
        try:
            parsed = urlsplit(request.url)
            parsed_port = parsed.port
        except (TypeError, ValueError):
            invalid_url = True
        if invalid_url:
            raise EndpointPolicyError(code="invalid_transport_url") from None
        authority = parsed.netloc.rsplit("@", 1)[-1]
        if authority.startswith("["):
            closing = authority.find("]")
            suffix = authority[closing + 1:] if closing >= 0 else ""
            explicit_port = suffix.startswith(":")
            empty_port = suffix == ":"
        else:
            explicit_port = ":" in authority
            empty_port = explicit_port and not authority.rsplit(":", 1)[1]
        if empty_port or explicit_port and parsed_port is None:
            raise EndpointPolicyError(code="invalid_port")
        port = 80 if parsed_port is None else parsed_port
        if not 1 <= port <= 65535:
            raise EndpointPolicyError(code="invalid_port")
        host = parsed.hostname
        if parsed.scheme != "http":
            raise EndpointPolicyError(code="unsupported_scheme")
        if host is None or host.casefold() not in ALLOWED_HOSTS:
            raise EndpointPolicyError(code="host_rejected")
        if parsed.username is not None or parsed.password is not None:
            raise EndpointPolicyError(code="url_credentials_rejected")
        if parsed.query or parsed.fragment:
            raise EndpointPolicyError(code="url_suffix_rejected")
        if parsed.path != "/v1/chat/completions":
            raise EndpointPolicyError(code="path_rejected")
        if not isinstance(request.headers, tuple):
            raise EndpointPolicyError(code="invalid_headers")
        host_values = []
        for header in request.headers:
            if (
                not isinstance(header, tuple)
                or len(header) != 2
                or not all(isinstance(part, str) for part in header)
            ):
                raise EndpointPolicyError(code="invalid_headers")
            name, value = header
            if (
                not name
                or any(not (char.isascii() and (char.isalnum() or char == "-"))
                       for char in name)
                or not value
                or any(ord(char) > 255 or not char.isprintable() for char in value)
            ):
                raise EndpointPolicyError(code="invalid_headers")
            if name.casefold() == "host":
                host_values.append(value.casefold())
        expected = cls._expected_host_header(host.casefold(), port).casefold()
        if len(host_values) != 1 or host_values[0] != expected:
            raise EndpointPolicyError(code="host_header_rejected")
        if not isinstance(request.body, bytes):
            raise EndpointPolicyError(code="invalid_body")
        for value in (
            request.connect_timeout_seconds, request.read_timeout_seconds
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise EndpointPolicyError(code="invalid_request")
        if (
            isinstance(request.max_response_bytes, bool)
            or not isinstance(request.max_response_bytes, int)
            or request.max_response_bytes <= 0
        ):
            raise EndpointPolicyError(code="invalid_request")
        return parsed, host.casefold(), port

    def send(self, request):
        parsed, host, port = self._validate_request(request)
        address = self._resolve(host, port)
        connection, connection_failure = self._connect(
            address, port, request.connect_timeout_seconds
        )
        if connection_failure == "connect_timeout":
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
            raise ModelTimeoutError(code="connect_timeout") from None
        if connection_failure:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
            raise ModelConnectionError() from None
        try:
            if getattr(connection, "sock", None) is not None:
                connection.sock.settimeout(request.read_timeout_seconds)
            response_failure = None
            try:
                connection.request(
                    request.method, parsed.path or "/", body=request.body,
                    headers=dict(request.headers),
                )
                response = connection.getresponse()
                status = int(response.status)
                if 300 <= status < 400:
                    raise ModelRedirectError(http_status=status)
                if not 200 <= status < 300:
                    raise ModelHttpStatusError(http_status=status)
                headers = tuple((str(k), str(v)) for k, v in response.getheaders())
                lowered = {}
                for key, value in headers:
                    lowered.setdefault(key.casefold(), []).append(value)
                if len(lowered.get("content-length", ())) > 1:
                    raise MalformedHttpResponseError(
                        code="duplicate_content_length"
                    )
                encoding_values = lowered.get("content-encoding", ("identity",))
                if len(encoding_values) != 1:
                    raise MalformedHttpResponseError(code="compressed_response")
                encoding = encoding_values[0].casefold()
                if encoding not in {"", "identity"}:
                    raise MalformedHttpResponseError(code="compressed_response")
                lengths = lowered.get("content-length")
                length = lengths[0] if lengths else None
                if length is not None:
                    invalid_length = False
                    try:
                        declared = int(length)
                    except ValueError:
                        invalid_length = True
                    if invalid_length or declared < 0:
                        raise MalformedHttpResponseError(
                            code="invalid_content_length"
                        )
                    if declared > request.max_response_bytes:
                        raise ModelResponseTooLargeError()
                chunks = []
                size = 0
                while True:
                    chunk = response.read(min(65536, request.max_response_bytes - size + 1))
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > request.max_response_bytes:
                        raise ModelResponseTooLargeError()
                    chunks.append(chunk)
                return TransportResponse(status, headers, b"".join(chunks))
            except socket.timeout:
                response_failure = "read_timeout"
            except (ModelRedirectError, ModelHttpStatusError,
                    ModelResponseTooLargeError, MalformedHttpResponseError):
                raise
            except (OSError, ValueError, UnicodeError):
                response_failure = "response_failed"
            if response_failure == "read_timeout":
                raise ModelTimeoutError(code="read_timeout") from None
            if response_failure:
                raise ModelConnectionError(code="response_failed") from None
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
