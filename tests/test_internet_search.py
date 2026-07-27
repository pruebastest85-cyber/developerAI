import unittest
from pathlib import Path
from unittest.mock import patch

from tools.internet_search import InternetSearchTool


class InternetSearchTests(unittest.TestCase):
    def test_execute_returns_results_from_payload(self):
        tool = InternetSearchTool(endpoint="https://example.test")
        payload = '{"results": [{"title": "Demo", "url": "https://demo.test", "content": "hola"}]}'

        with patch("tools.internet_search.urlopen") as mock_urlopen:
            mock_response = unittest.mock.MagicMock()
            mock_response.__enter__.return_value.read.return_value = payload.encode("utf-8")
            mock_urlopen.return_value = mock_response

            result = tool.execute({"query": "python"})

        self.assertEqual(result["query"], "python")
        self.assertEqual(result["source"], "searxng")
        self.assertEqual(result["results"][0]["title"], "Demo")
        self.assertEqual(result["results"][0]["url"], "https://demo.test")


if __name__ == "__main__":
    unittest.main()
