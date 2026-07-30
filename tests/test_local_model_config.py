import unittest

from brain.local_model_config import LocalModelConfig
from brain.model_errors import (
    EndpointPolicyError, LocalModelError, ModelConfigurationError,
)


class LocalModelConfigTests(unittest.TestCase):
    def make(self, **changes):
        values = {
            "provider": "lm_studio",
            "base_url": "http://localhost:1234/v1",
            "model": "qwen",
        }
        values.update(changes)
        return LocalModelConfig(**values)

    def test_accepts_and_normalizes_supported_endpoints(self):
        cases = {
            "http://localhost:1234/v1/": "http://localhost:1234/v1",
            "http://127.0.0.1/v1": "http://127.0.0.1:80/v1",
            "http://[::1]:1234/v1": "http://[::1]:1234/v1",
            "http://host.docker.internal:1234/v1": "http://host.docker.internal:1234/v1",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(self.make(base_url=source).base_url, expected)

    def test_endpoint_id_is_canonical_for_every_allowed_host(self):
        cases = {
            "http://localhost:1234/v1": "lm_studio@localhost:1234",
            "http://127.0.0.1:1234/v1": "lm_studio@127.0.0.1:1234",
            "http://[::1]:1234/v1": "lm_studio@[::1]:1234",
            "http://host.docker.internal:1234/v1":
                "lm_studio@host.docker.internal:1234",
        }
        for base_url, expected in cases.items():
            with self.subTest(base_url=base_url):
                self.assertEqual(self.make(base_url=base_url).endpoint_id, expected)

    def test_rejects_unsafe_endpoint_variants(self):
        values = [
            "https://localhost:1234/v1", "http://example.com/v1",
            "http://localhost:1234/v2", "http://user:pass@localhost:1234/v1",
            "http://localhost:1234/v1?q=1", "http://localhost:1234/v1#x",
        ]
        for value in values:
            with self.subTest(value=value):
                with self.assertRaises(EndpointPolicyError):
                    self.make(base_url=value)

    def test_base_path_allows_only_v1_and_one_optional_slash(self):
        for path in ("/v1", "/v1/"):
            with self.subTest(path=path):
                self.make(base_url=f"http://localhost:1234{path}")
        for path in (
            "/v1//", "/v1///", "/v1////", "/v1/chat/completions", "/other",
        ):
            with self.subTest(path=path):
                with self.assertRaises(EndpointPolicyError):
                    self.make(base_url=f"http://localhost:1234{path}")

    def test_rejects_invalid_scalar_limits(self):
        for name, value in [
            ("connect_timeout_seconds", 0), ("read_timeout_seconds", float("inf")),
            ("max_http_body_bytes", True), ("max_content_bytes", 0),
            ("max_prompt_bytes", -1), ("max_json_depth", 0),
            ("max_output_tokens", 0), ("temperature", float("nan")),
        ]:
            with self.subTest(name=name):
                with self.assertRaises(ModelConfigurationError):
                    self.make(**{name: value})

    def test_rejects_unknown_provider_format_and_temperature_range(self):
        for changes in [
            {"provider": "remote"},
            {"structured_format": "auto"},
            {"structured_format": "json_object"},
            {"temperature": -0.1},
            {"temperature": 2.1},
        ]:
            with self.subTest(changes=changes):
                with self.assertRaises(ModelConfigurationError):
                    self.make(**changes)

    def test_rejects_blank_or_padded_identifiers(self):
        for name, value in [("model", " "), ("model", " qwen"),
                            ("provider", "lm_studio "),
                            ("structured_format", " json_schema")]:
            with self.subTest(name=name):
                with self.assertRaises(ModelConfigurationError):
                    self.make(**{name: value})

    def test_api_key_is_absent_from_repr_equality_and_serialization(self):
        first = self.make(api_key="secret-a")
        second = self.make(api_key="secret-b")
        self.assertEqual(first, second)
        self.assertNotIn("secret-a", repr(first))
        self.assertNotIn("api_key", first.to_dict())

    def test_api_key_rejects_unsafe_header_values_without_retaining_them(self):
        unsafe = [
            "secret\r", "secret\n", "secret\r\nInjected: yes", "secret\x00",
            "secret\x1f", "secret\x7f", "snowman-\u2603", "x" * 513,
        ]
        for marker in unsafe:
            with self.subTest(marker=repr(marker)):
                with self.assertRaises(ModelConfigurationError) as caught:
                    self.make(api_key=marker)
                error = caught.exception
                exposed = " ".join([
                    str(error), repr(error), repr(error.args),
                    repr(error.__cause__), repr(error.__context__),
                    repr(error.__dict__),
                ])
                self.assertNotIn(marker, exposed)
                self.assertIsNone(error.__cause__)
                self.assertIsNone(error.__context__)
        self.assertEqual(self.make(api_key="valid-token_123").api_key,
                         "valid-token_123")

    def test_public_errors_discard_arbitrary_messages_and_unsafe_endpoints(self):
        marker = "super-secret"
        with self.assertRaises(TypeError) as caught:
            LocalModelError(marker)
        self.assertNotIn(marker, str(caught.exception))
        for endpoint in [
            f"http://user:{marker}@localhost:1234/v1",
            f"http://localhost:1234/v1?token={marker}",
            f"http://localhost:1234/v1#{marker}",
        ]:
            error = LocalModelError(endpoint=endpoint)
            self.assertIsNone(error.endpoint)
            self.assertNotIn(marker, repr(error))
            self.assertNotIn(marker, str(error))
            self.assertNotIn(marker, repr(error.args))
        error = LocalModelError(code=marker)
        self.assertEqual(error.code, "local_model_error")
        self.assertNotIn(marker, repr(error.__dict__))
        self.assertEqual(LocalModelError(code=[]).code, "local_model_error")

    def test_from_env_reads_only_supported_variables(self):
        source = {
            "DEVELOPERAI_MODEL_PROVIDER": "openai_compatible",
            "DEVELOPERAI_MODEL_BASE_URL": "http://127.0.0.1:9999/v1",
            "DEVELOPERAI_MODEL_NAME": "local",
            "DEVELOPERAI_MODEL_API_KEY": "secret",
            "DEVELOPERAI_MODEL_CONNECT_TIMEOUT": "2.5",
            "DEVELOPERAI_MODEL_READ_TIMEOUT": "9",
            "DEVELOPERAI_MODEL_STRUCTURED_OUTPUT_MODE": "prompt_json",
            "UNRELATED": "unchanged",
        }
        config = LocalModelConfig.from_env(source)
        self.assertEqual(config.model, "local")
        self.assertEqual(config.connect_timeout_seconds, 2.5)
        self.assertEqual(config.structured_output_mode, "prompt_json")
        self.assertEqual(source["UNRELATED"], "unchanged")

    def test_structured_output_mode_defaults_to_json_schema(self):
        config = LocalModelConfig.from_env({
            "DEVELOPERAI_MODEL_NAME": "qwen",
        })
        self.assertEqual(config.structured_format, "json_schema")
        self.assertEqual(config.structured_output_mode, "json_schema")
        self.assertEqual(config.to_dict()["structured_format"], "json_schema")

    def test_invalid_environment_number_has_typed_error(self):
        with self.assertRaises(ModelConfigurationError) as caught:
            LocalModelConfig.from_env({
                "DEVELOPERAI_MODEL_NAME": "qwen",
                "DEVELOPERAI_MODEL_CONNECT_TIMEOUT": "not-a-number",
            })
        self.assertEqual(caught.exception.code, "invalid_environment")

    def test_unknown_environment_output_mode_is_rejected(self):
        with self.assertRaises(ModelConfigurationError) as caught:
            LocalModelConfig.from_env({
                "DEVELOPERAI_MODEL_NAME": "qwen",
                "DEVELOPERAI_MODEL_STRUCTURED_OUTPUT_MODE": "unknown",
            })
        self.assertEqual(
            caught.exception.code,
            "unsupported_structured_format",
        )


if __name__ == "__main__":
    unittest.main()
