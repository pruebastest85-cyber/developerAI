import unittest
from unittest.mock import patch

from tools.internet_search import InternetSearchTool


class InternetSearchTests(unittest.TestCase):
    class DummyLogger:
        def log(self, *args, **kwargs):
            return None

    def _execute_payload(self, payload, *, structured=True, query="python"):
        tool = InternetSearchTool(
            endpoint="https://example.test",
            logger=self.DummyLogger(),
        )
        with patch("tools.internet_search.urlopen") as mock_urlopen:
            mock_response = unittest.mock.MagicMock()
            mock_response.__enter__.return_value.read.return_value = payload.encode("utf-8")
            mock_urlopen.return_value = mock_response
            return tool.execute({"query": query}, structured=structured)

    def test_execute_historical_returns_original_success_dict(self):
        payload = '{"results": [{"title": "Demo", "url": "https://demo.test", "content": "hola"}]}'
        result = self._execute_payload(payload, structured=False)

        self.assertIsInstance(result, dict)
        self.assertNotIn("status", result)
        self.assertEqual(result["query"], "python")
        self.assertEqual(result["results"][0]["title"], "Demo")

    def test_execute_structured_returns_tool_result(self):
        payload = '{"results": [{"title": "Demo", "url": "https://demo.test", "content": "hola"}]}'
        result = self._execute_payload(payload)

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.data["source"], "searxng")
        self.assertEqual(result.data["results"][0]["url"], "https://demo.test")

    def test_invalid_json_returns_failed_tool_result(self):
        result = self._execute_payload("not-json")

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.metadata["exception_type"], "JSONDecodeError")
        self.assertIsNone(result.data)

    def test_valid_json_requires_object_root(self):
        for payload in ("[]", '"text"'):
            with self.subTest(payload=payload):
                result = self._execute_payload(payload)
                self.assertEqual(result.status, "failed")
                self.assertIn("Formato externo inesperado", result.error)

    def test_invalid_internal_schema_is_failed(self):
        for payload in (
            '{"results": {}}',
            '{"results": ["bad"]}',
            '{"results": [{"title": 1}]}',
        ):
            with self.subTest(payload=payload):
                result = self._execute_payload(payload)
                self.assertEqual(result.status, "failed")
                self.assertIn("Formato externo inesperado", result.error)

    def test_empty_query_is_explicit_failure(self):
        tool = InternetSearchTool(
            endpoint="https://example.test",
            logger=self.DummyLogger(),
        )
        result = tool.execute({"query": "  "}, structured=True)

        self.assertEqual(result.status, "failed")
        self.assertIn("consulta", result.error)


if __name__ == "__main__":
    unittest.main()
