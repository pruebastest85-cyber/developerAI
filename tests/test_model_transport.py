import socket
import unittest

from brain.model_errors import (
    EndpointPolicyError, MalformedHttpResponseError, ModelConnectionError,
    ModelHttpStatusError, ModelRedirectError, ModelResponseTooLargeError,
    ModelTimeoutError,
)
from brain.model_transport import (
    HttpClientTransport, TransportRequest, TransportResponse,
)


class FakeResponse:
    def __init__(self, status=200, headers=(), body=b"{}"):
        self.status = status
        self._headers = headers
        self._body = body
        self._offset = 0
        self.read_calls = 0

    def getheaders(self):
        return self._headers

    def read(self, size):
        self.read_calls += 1
        chunk = self._body[self._offset:self._offset + size]
        self._offset += len(chunk)
        return chunk


class FakeSocket:
    def __init__(self):
        self.timeout = None

    def settimeout(self, value):
        self.timeout = value


class FakeConnection:
    def __init__(self, response=None, connect_error=None, response_error=None):
        self.response = response or FakeResponse()
        self.connect_error = connect_error
        self.response_error = response_error
        self.sock = FakeSocket()
        self.sent = None
        self.closed = False

    def connect(self):
        if self.connect_error:
            raise self.connect_error

    def request(self, method, path, body=None, headers=None):
        self.sent = (method, path, body, headers)

    def getresponse(self):
        if self.response_error:
            raise self.response_error
        return self.response

    def close(self):
        self.closed = True


class TransportTests(unittest.TestCase):
    def request(self, **changes):
        values = dict(
            method="POST", url="http://localhost:1234/v1/chat/completions",
            headers=(("Host", "localhost:1234"),), body=b"{}",
            connect_timeout_seconds=2, read_timeout_seconds=3,
            max_response_bytes=100,
        )
        values.update(changes)
        return TransportRequest(**values)

    def transport(self, connection, addresses=None):
        resolver = lambda *args, **kwargs: addresses or [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 1234))
        ]
        return HttpClientTransport(
            resolver=resolver,
            connection_factory=lambda address, port, timeout: connection,
        )

    def assert_rejected_before_connection(self, request):
        calls = []
        transport = HttpClientTransport(
            resolver=lambda *args, **kwargs: calls.append(args) or ["127.0.0.1"],
            connection_factory=lambda *args: self.fail("must not connect"),
        )
        with self.assertRaises(EndpointPolicyError):
            transport.send(request)
        self.assertEqual(calls, [])

    def test_sends_exact_request_to_pinned_address_and_closes(self):
        connection = FakeConnection(FakeResponse(headers=(("Content-Length", "2"),)))
        result = self.transport(connection).send(self.request())
        self.assertEqual(result.body, b"{}")
        self.assertEqual(connection.sent[:3], ("POST", "/v1/chat/completions", b"{}"))
        self.assertEqual(connection.sock.timeout, 3)
        self.assertTrue(connection.closed)

    def test_rejects_any_disallowed_dns_answer(self):
        answers = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 1234)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 1234)),
        ]
        with self.assertRaises(EndpointPolicyError):
            self.transport(FakeConnection(), answers).send(self.request())

    def test_docker_host_accepts_private_address(self):
        connection = FakeConnection()
        request = self.request(
            url="http://host.docker.internal:1234/v1/chat/completions",
            headers=(("Host", "host.docker.internal:1234"),),
        )
        result = self.transport(connection, ["192.168.65.2"]).send(request)
        self.assertEqual(result.status, 200)

    def test_transport_contract_rejects_unsafe_requests_before_resolution(self):
        cases = [
            self.request(method="GET"),
            self.request(method="PUT"),
            self.request(url="http://localhost:1234/v1/other"),
            self.request(url="http://localhost:1234/v1/chat/completions?q=x"),
            self.request(url="http://localhost:1234/v1/chat/completions#x"),
            self.request(url="http://user:pass@localhost:1234/v1/chat/completions"),
            self.request(headers=()),
            self.request(headers=(("Host", "localhost:1234"),
                                  ("host", "localhost:1234"))),
            self.request(headers=(("Host", "127.0.0.1:1234"),)),
            self.request(url="https://localhost:1234/v1/chat/completions"),
            self.request(connect_timeout_seconds=True),
            self.request(read_timeout_seconds=float("inf")),
            self.request(max_response_bytes=0),
        ]
        for request in cases:
            with self.subTest(request=repr(request)):
                self.assert_rejected_before_connection(request)

    def test_unknown_logical_host_is_rejected_even_when_private(self):
        request = self.request(
            url="http://unapproved.internal:1234/v1/chat/completions",
            headers=(("Host", "unapproved.internal:1234"),),
        )
        self.assert_rejected_before_connection(request)

    def test_ipv4_and_ipv6_connect_to_validated_ip_with_one_resolution(self):
        cases = [
            ("http://127.0.0.1:1234/v1/chat/completions",
             "127.0.0.1:1234", "127.0.0.1"),
            ("http://[::1]:1234/v1/chat/completions", "[::1]:1234", "::1"),
        ]
        for url, host_header, address in cases:
            with self.subTest(url=url):
                resolution_calls = []
                factory_calls = []
                connection = FakeConnection()
                transport = HttpClientTransport(
                    resolver=lambda *args, **kwargs: (
                        resolution_calls.append((args, kwargs)) or [address]
                    ),
                    connection_factory=lambda *args: (
                        factory_calls.append(args) or connection
                    ),
                )
                transport.send(self.request(
                    url=url, headers=(("Host", host_header),)
                ))
                self.assertEqual(len(resolution_calls), 1)
                self.assertEqual(factory_calls, [(address, 1234, 2)])

    def test_transport_distinguishes_absent_valid_and_invalid_ports(self):
        accepted = [
            ("http://localhost/v1/chat/completions", "localhost:80", 80),
            ("http://localhost:1/v1/chat/completions", "localhost:1", 1),
            ("http://localhost:65535/v1/chat/completions",
             "localhost:65535", 65535),
        ]
        for url, host_header, expected_port in accepted:
            with self.subTest(url=url):
                calls = []
                connection = FakeConnection()
                transport = HttpClientTransport(
                    resolver=lambda host, port, **kwargs: (
                        calls.append(("resolve", host, port)) or ["127.0.0.1"]
                    ),
                    connection_factory=lambda address, port, timeout: (
                        calls.append(("connect", address, port)) or connection
                    ),
                )
                transport.send(self.request(
                    url=url, headers=(("Host", host_header),)
                ))
                self.assertEqual(calls[0], ("resolve", "localhost", expected_port))
                self.assertEqual(calls[1], ("connect", "127.0.0.1", expected_port))

        invalid = [
            ("http://localhost:0/v1/chat/completions", "localhost:0"),
            ("http://localhost:65536/v1/chat/completions", "localhost:65536"),
            ("http://localhost:/v1/chat/completions", "localhost:80"),
            ("http://localhost:not-a-number/v1/chat/completions",
             "localhost:not-a-number"),
        ]
        for url, host_header in invalid:
            with self.subTest(url=url):
                self.assert_rejected_before_connection(self.request(
                    url=url, headers=(("Host", host_header),)
                ))

    def test_invalid_port_error_does_not_retain_url_marker(self):
        marker = "port-super-secret"
        request = self.request(
            url=f"http://localhost:{marker}/v1/chat/completions",
            headers=(("Host", f"localhost:{marker}"),),
        )
        transport = HttpClientTransport(
            resolver=lambda *args, **kwargs: self.fail("must not resolve"),
            connection_factory=lambda *args: self.fail("must not connect"),
        )
        with self.assertRaises(EndpointPolicyError) as caught:
            transport.send(request)
        error = caught.exception
        exposed = " ".join([
            str(error), repr(error), repr(error.args), repr(error.__dict__),
            repr(error.__cause__), repr(error.__context__),
        ])
        self.assertNotIn(marker, exposed)
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)

    def test_docker_policy_rejects_global_but_accepts_link_local(self):
        request = self.request(
            url="http://host.docker.internal:1234/v1/chat/completions",
            headers=(("Host", "host.docker.internal:1234"),),
        )
        with self.assertRaises(EndpointPolicyError):
            self.transport(FakeConnection(), ["8.8.8.8"]).send(request)
        self.assertEqual(
            self.transport(FakeConnection(), ["169.254.1.2"]).send(request).status,
            200,
        )

    def test_connect_failures_are_typed(self):
        for error, expected in [
            (socket.timeout(), ModelTimeoutError),
            (OSError("down"), ModelConnectionError),
        ]:
            with self.subTest(error=type(error)):
                with self.assertRaises(expected):
                    self.transport(FakeConnection(connect_error=error)).send(self.request())

    def test_read_timeout_is_distinct(self):
        connection = FakeConnection(response_error=socket.timeout())
        with self.assertRaises(ModelTimeoutError) as caught:
            self.transport(connection).send(self.request())
        self.assertEqual(caught.exception.code, "read_timeout")

    def test_rejects_redirect_and_non_success_without_following(self):
        for status, expected in [(302, ModelRedirectError), (500, ModelHttpStatusError)]:
            with self.subTest(status=status):
                with self.assertRaises(expected):
                    self.transport(FakeConnection(FakeResponse(status=status))).send(self.request())

    def test_rejects_compression_and_bad_content_length(self):
        for headers in [
            (("Content-Encoding", "gzip"),),
            (("Content-Length", "bad"),),
            (("Content-Length", "-1"),),
        ]:
            with self.subTest(headers=headers):
                with self.assertRaises(MalformedHttpResponseError):
                    self.transport(FakeConnection(FakeResponse(headers=headers))).send(self.request())

    def test_rejects_duplicate_or_conflicting_content_length(self):
        for headers in [
            (("Content-Length", "2"), ("Content-Length", "2")),
            (("Content-Length", "2"), ("content-length", "3")),
        ]:
            with self.subTest(headers=headers):
                with self.assertRaises(MalformedHttpResponseError):
                    self.transport(FakeConnection(
                        FakeResponse(headers=headers)
                    )).send(self.request())

    def test_http_error_body_is_never_read(self):
        response = FakeResponse(status=500, body=b"server-secret")
        with self.assertRaises(ModelHttpStatusError):
            self.transport(FakeConnection(response)).send(self.request())
        self.assertEqual(response.read_calls, 0)

    def test_transport_errors_do_not_retain_internal_secret(self):
        marker = "transport-super-secret"
        connection = FakeConnection(response_error=ValueError(marker))
        with self.assertRaises(ModelConnectionError) as caught:
            self.transport(connection).send(self.request())
        error = caught.exception
        exposed = " ".join([
            str(error), repr(error), repr(error.args),
            repr(error.__cause__), repr(error.__context__),
        ])
        self.assertNotIn(marker, exposed)
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)

    def test_request_and_response_repr_redact_headers_and_bodies(self):
        marker = "header-super-secret"
        request = self.request(headers=(
            ("Host", "localhost:1234"), ("Authorization", marker),
        ), body=marker.encode())
        response = TransportResponse(200, (("Set-Cookie", marker),),
                                     marker.encode())
        self.assertNotIn(marker, repr(request))
        self.assertNotIn(marker, repr(response))

    def test_enforces_declared_and_streamed_body_limits(self):
        declared = FakeResponse(headers=(("Content-Length", "101"),))
        streamed = FakeResponse(body=b"x" * 101)
        for response in (declared, streamed):
            with self.subTest(response=response):
                with self.assertRaises(ModelResponseTooLargeError):
                    self.transport(FakeConnection(response)).send(self.request())


if __name__ == "__main__":
    unittest.main()
