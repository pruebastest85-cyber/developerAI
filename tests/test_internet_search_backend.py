import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.internet_search import InternetSearchTool


class InternetSearchBackendTests(unittest.TestCase):
    def test_execute_returns_structured_payload_and_uses_settings(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            settings_path = Path(tmpdir) / "settings.json"
            settings_path.write_text(json.dumps({"search_endpoint": "https://example.test/search"}), encoding="utf-8")

            with patch("tools.internet_search.urlopen") as mock_urlopen:
                mock_response = unittest.mock.MagicMock()
                payload = json.dumps({
                    "query": "fastapi",
                    "results": [
                        {"title": "FastAPI docs", "url": "https://docs.example/fastapi", "content": "Docs for FastAPI"}
                    ]
                })
                mock_response.__enter__.return_value.read.return_value = payload.encode("utf-8")
                mock_urlopen.return_value = mock_response

                tool = InternetSearchTool(settings_path=settings_path)
                result = tool.execute({"query": "fastapi"})

                self.assertEqual(result["query"], "fastapi")
                self.assertEqual(result["source"], "searxng")
                self.assertEqual(len(result["results"]), 1)
                self.assertEqual(result["results"][0]["title"], "FastAPI docs")

    def test_local_endpoint_is_normalized_to_searxng_search_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            settings_path = Path(tmpdir) / "settings.json"
            settings_path.write_text(json.dumps({"search_endpoint": "http://localhost:8080"}), encoding="utf-8")

            with patch("tools.internet_search.urlopen") as mock_urlopen:
                mock_response = unittest.mock.MagicMock()
                mock_response.__enter__.return_value.read.return_value = b'{"results": []}'
                mock_urlopen.return_value = mock_response

                tool = InternetSearchTool(settings_path=settings_path)
                tool.execute({"query": "python"})

                request = mock_urlopen.call_args.args[0]
                self.assertEqual(request.full_url, "http://localhost:8080/search?q=python&format=json")


if __name__ == "__main__":
    unittest.main()
