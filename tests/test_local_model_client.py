import json
import unittest

from brain.local_model_client import (
    LocalModelClient, ModelMessage, ModelResponseMetadata,
    StructuredModelRequest, StructuredModelResponse,
)
from brain.local_model_config import LocalModelConfig
from brain.model_errors import (
    IncompatibleOpenAIResponseError, InvalidJsonError,
    MalformedHttpResponseError, MissingChoicesError, MissingContentError,
    MissingMessageError, ModelConfigurationError, SchemaValidationError,
    TrailingContentError,
)
from brain.model_transport import TransportResponse
from brain.structured_json import StructuredOutputSchema


class FakeTransport:
    def __init__(self, body):
        self.body = body
        self.requests = []

    def send(self, request):
        self.requests.append(request)
        return TransportResponse(200, (("Content-Type", "application/json"),), self.body)


def schema():
    return StructuredOutputSchema("answer", {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
        "additionalProperties": False,
    })


def envelope(content='{"answer":"yes"}', **updates):
    value = {
        "id": "req-1", "model": "qwen-local",
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    }
    value.update(updates)
    return json.dumps(value).encode()


class LocalModelClientTests(unittest.TestCase):
    def make(self, body=None, **config_changes):
        values = dict(
            provider="lm_studio", base_url="http://localhost:1234/v1",
            model="qwen", api_key="top-secret",
        )
        values.update(config_changes)
        transport = FakeTransport(body or envelope())
        clock = iter([10.0, 10.25]).__next__
        return LocalModelClient(LocalModelConfig(**values), transport, clock), transport

    def request(self, **changes):
        values = dict(
            messages=(ModelMessage("system", "Return JSON"), ModelMessage("user", "go")),
            output_schema=schema(),
        )
        values.update(changes)
        return StructuredModelRequest(**values)

    def test_success_validates_and_returns_deeply_immutable_data(self):
        client, _ = self.make()
        result = client.complete(self.request())
        self.assertEqual(result.data["answer"], "yes")
        with self.assertRaises(TypeError):
            result.data["answer"] = "changed"
        copy = result.to_dict()
        copy["data"]["answer"] = "changed"
        self.assertEqual(result.data["answer"], "yes")
        self.assertEqual(result.metadata.duration_seconds, .25)
        json.dumps(result.to_dict(), allow_nan=False)

    def test_request_and_response_repr_redact_schema_and_generated_data(self):
        marker = "model-super-secret"
        secret_schema = StructuredOutputSchema("redacted", {
            "type": "string", "enum": [marker],
        })
        request = StructuredModelRequest(
            (ModelMessage("user", "prompt-secret"),), secret_schema
        )
        metadata = self.valid_metadata()
        response = StructuredModelResponse({"value": marker}, metadata)
        self.assertNotIn(marker, repr(secret_schema))
        self.assertNotIn(marker, repr(request))
        self.assertNotIn("prompt-secret", repr(request))
        self.assertNotIn(marker, repr(response))

    def test_serializes_exact_openai_request_without_external_dependency(self):
        client, transport = self.make()
        client.complete(self.request(max_output_tokens=17, temperature=.2))
        sent = transport.requests[0]
        payload = json.loads(sent.body)
        self.assertEqual(sent.method, "POST")
        self.assertEqual(sent.url, "http://localhost:1234/v1/chat/completions")
        self.assertEqual(payload["model"], "qwen")
        self.assertEqual(payload["max_tokens"], 17)
        self.assertEqual(payload["response_format"]["type"], "json_schema")
        headers = dict(sent.headers)
        self.assertEqual(headers["Authorization"], "Bearer top-secret")
        self.assertEqual(headers["Accept-Encoding"], "identity")

    def test_json_object_mode_uses_minimal_response_format(self):
        client, transport = self.make(structured_format="json_object")
        client.complete(self.request())
        payload = json.loads(transport.requests[0].body)
        self.assertEqual(payload["response_format"], {"type": "json_object"})

    def test_authorization_header_is_absent_without_credential(self):
        client, transport = self.make(api_key=None)
        client.complete(self.request())
        self.assertNotIn("Authorization", dict(transport.requests[0].headers))

    def test_missing_envelope_components_have_distinct_errors(self):
        cases = [
            ({}, MissingChoicesError),
            ({"choices": []}, MissingChoicesError),
            ({"choices": [{}]}, MissingMessageError),
            ({"choices": [{"message": {}}]}, MissingContentError),
            ({"choices": [{"message": {"content": None}}]}, MissingContentError),
            ({"choices": "bad"}, IncompatibleOpenAIResponseError),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                client, _ = self.make(json.dumps(value).encode())
                with self.assertRaises(expected):
                    client.complete(self.request())

    def test_malformed_http_json_is_not_treated_as_generated_content(self):
        client, _ = self.make(b"not json")
        with self.assertRaises(MalformedHttpResponseError):
            client.complete(self.request())

    def test_invalid_envelope_and_content_do_not_survive_in_error_graph(self):
        markers = [
            ("http-envelope-super-secret", False),
            ("generated-content-super-secret", True),
        ]
        for marker, inside_envelope in markers:
            with self.subTest(marker=marker):
                body = envelope(marker) if inside_envelope else marker.encode()
                client, _ = self.make(body)
                with self.assertRaises(Exception) as caught:
                    client.complete(self.request())
                pending = [caught.exception]
                visited = set()
                exposed = []
                while pending:
                    error = pending.pop()
                    if error is None or id(error) in visited:
                        continue
                    visited.add(id(error))
                    exposed.extend([
                        str(error), repr(error), repr(error.args),
                        repr(getattr(error, "__dict__", {})),
                    ])
                    pending.extend([error.__cause__, error.__context__])
                self.assertNotIn(marker, " ".join(exposed))
                self.assertIsNone(caught.exception.__cause__)
                self.assertIsNone(caught.exception.__context__)

    def test_generated_json_is_strict_and_schema_validated(self):
        cases = [
            ("not json", InvalidJsonError),
            ('{"answer":"yes"} trailing', TrailingContentError),
            ('{"other":"yes"}', SchemaValidationError),
        ]
        for content, expected in cases:
            with self.subTest(content=content):
                client, _ = self.make(envelope(content))
                with self.assertRaises(expected):
                    client.complete(self.request())

    def test_generated_content_byte_limit_is_enforced(self):
        client, _ = self.make(envelope('{"answer":"long"}'), max_content_bytes=5)
        with self.assertRaises(Exception) as caught:
            client.complete(self.request())
        self.assertEqual(caught.exception.code, "generated_content_too_large")

    def test_untrusted_metadata_is_sanitized(self):
        body = envelope(model="bad model / secret", id="x\nheader")
        client, _ = self.make(body)
        metadata = client.complete(self.request()).metadata
        self.assertIsNone(metadata.reported_model)
        self.assertIsNone(metadata.request_id)

    @staticmethod
    def valid_metadata(**changes):
        values = dict(
            provider="lm_studio", requested_model="qwen",
            reported_model="qwen-local", request_id="req-1",
            input_tokens=1, output_tokens=2, total_tokens=3,
            finish_reason="stop", duration_seconds=.1,
            endpoint_id="lm_studio@localhost:1234",
            structured_format="json_schema",
        )
        values.update(changes)
        return ModelResponseMetadata(**values)

    def test_metadata_rejects_invalid_direct_construction(self):
        cases = [
            {"provider": "unknown"},
            {"provider": []},
            {"requested_model": ""},
            {"reported_model": "bad model"},
            {"request_id": "x" * 129},
            {"input_tokens": True},
            {"output_tokens": -1},
            {"total_tokens": 1.5},
            {"finish_reason": "bad reason"},
            {"duration_seconds": True},
            {"duration_seconds": -1},
            {"duration_seconds": float("nan")},
            {"duration_seconds": float("inf")},
            {"endpoint_id": ""},
            {"structured_format": "auto"},
            {"structured_format": []},
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                with self.assertRaises(ModelConfigurationError):
                    self.valid_metadata(**changes)

    def test_metadata_accepts_only_canonical_local_endpoint_ids(self):
        valid = [
            "lm_studio@localhost:1234",
            "lm_studio@127.0.0.1:1234",
            "lm_studio@[::1]:1234",
            "lm_studio@host.docker.internal:1234",
        ]
        for endpoint_id in valid:
            with self.subTest(endpoint_id=endpoint_id):
                metadata = self.valid_metadata(endpoint_id=endpoint_id)
                self.assertEqual(metadata.endpoint_id, endpoint_id)

        marker = "metadata-super-secret"
        invalid = [
            f"lm_studio@user:{marker}@localhost:1234",
            "lm_studio@example.com:1234",
            f"lm_studio@localhost:1234?token={marker}",
            f"lm_studio@localhost:1234#{marker}",
            "lm_studio@localhost:1234/path",
            "https://localhost:1234",
            "lm_studio@localhost:0",
            "lm_studio@localhost:65536",
            "lm_studio@localhost:",
            "lm_studio@local\nhost:1234",
            "openai_compatible@localhost:1234",
            "lm_studio@" + "a" * 300 + ":1234",
        ]
        for endpoint_id in invalid:
            with self.subTest(endpoint_id=repr(endpoint_id)):
                with self.assertRaises(ModelConfigurationError) as caught:
                    self.valid_metadata(endpoint_id=endpoint_id)
                error = caught.exception
                exposed = " ".join([
                    str(error), repr(error), repr(error.args),
                    repr(error.__dict__), repr(error.__cause__),
                    repr(error.__context__),
                ])
                self.assertNotIn(marker, exposed)
                self.assertNotIn(endpoint_id, exposed)

    def test_config_endpoint_id_is_accepted_by_metadata_contract(self):
        for base_url in [
            "http://localhost:1234/v1", "http://127.0.0.1:1234/v1",
            "http://[::1]:1234/v1",
            "http://host.docker.internal:1234/v1",
        ]:
            with self.subTest(base_url=base_url):
                config = LocalModelConfig("lm_studio", base_url, "qwen")
                metadata = self.valid_metadata(endpoint_id=config.endpoint_id)
                self.assertEqual(metadata.endpoint_id, config.endpoint_id)

    def test_response_rejects_invalid_metadata_and_non_json_data(self):
        metadata = self.valid_metadata()
        circular = []
        circular.append(circular)
        for data in [
            (1, 2), {1: "bad"}, {"value": float("nan")},
            {"value": object()}, circular,
        ]:
            with self.subTest(data=repr(data)):
                with self.assertRaises(ModelConfigurationError):
                    StructuredModelResponse(data, metadata)
        with self.assertRaises(ModelConfigurationError):
            StructuredModelResponse({}, object())

    def test_valid_direct_response_serializes_to_independent_json(self):
        response = StructuredModelResponse(
            {"items": [1, {"ok": True}]}, self.valid_metadata()
        )
        first = response.to_dict()
        json.dumps(first, allow_nan=False)
        first["data"]["items"][1]["ok"] = False
        self.assertTrue(response.to_dict()["data"]["items"][1]["ok"])

    def test_request_limits_are_checked_before_transport(self):
        client, transport = self.make(max_output_tokens=10)
        with self.assertRaises(ModelConfigurationError):
            client.complete(self.request(max_output_tokens=11))
        self.assertEqual(transport.requests, [])

    def test_message_count_is_bounded(self):
        messages = tuple(ModelMessage("user", "x") for _ in range(65))
        with self.assertRaises(ModelConfigurationError):
            StructuredModelRequest(messages, schema())

    def test_prompt_byte_limit_is_checked_before_transport(self):
        client, transport = self.make(max_prompt_bytes=10)
        with self.assertRaises(ModelConfigurationError) as caught:
            client.complete(self.request())
        self.assertEqual(caught.exception.code, "prompt_too_large")
        self.assertEqual(transport.requests, [])

    def test_secrets_do_not_appear_in_configuration_or_errors(self):
        client, _ = self.make()
        self.assertNotIn("top-secret", repr(client.config))
        with self.assertRaises(ModelConfigurationError) as caught:
            client.complete(self.request(max_output_tokens=0))
        self.assertNotIn("top-secret", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
