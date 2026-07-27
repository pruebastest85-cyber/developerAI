import json
from pathlib import Path
from urllib.parse import quote_plus
from urllib.request import Request, urlopen

from tools.action_logger import ActionLogger
from tools.base_tool import Tool


class InternetSearchTool(Tool):
    name = "internet_search"
    description = "Busca información actualizada en internet"
    requires_confirmation = False
    risk = "low"

    def __init__(self, endpoint=None, logger=None, settings_path=None):
        self.settings_path = Path(settings_path or "config/settings.json")
        self.endpoint = endpoint or self._load_endpoint_from_settings() or "https://searx.be/search"
        self.logger = logger or ActionLogger()

    def execute(self, args=None):
        query = args.get("query", "") if isinstance(args, dict) else str(args or "")
        if not query:
            raise ValueError("Se requiere una consulta para buscar en internet")

        url = self._build_url(query)
        request = Request(url, headers={"User-Agent": "DeveloperAI/1.0"})
        with urlopen(request, timeout=10) as response:
            payload = response.read().decode("utf-8")

        result = self._parse_result(payload, query)
        self.logger.log(self.name, params={"query": query, "endpoint": self.endpoint}, result=result)
        return result

    def _build_url(self, query):
        encoded = quote_plus(query)
        base = self.endpoint.rstrip("/")
        if base.endswith("/search"):
            return f"{base}?q={encoded}&format=json"
        if base.endswith("/search"):
            return f"{base}?q={encoded}&format=json"
        if "/search" not in base and not base.endswith("/search"):
            return f"{base}/search?q={encoded}&format=json"
        return f"{base}?q={encoded}&format=json"

    def _load_endpoint_from_settings(self):
        if not self.settings_path.exists():
            return None
        try:
            data = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        endpoint = data.get("search_endpoint") or data.get("SEARCH_ENDPOINT")
        return endpoint if isinstance(endpoint, str) and endpoint.strip() else None

    def _parse_result(self, payload, query):
        try:
            data = json.loads(payload)
            results = data.get("results", [])
            return {
                "query": query,
                "source": "searxng",
                "results": [{
                    "title": item.get("title", ""),
                    "url": item.get("url", ""),
                    "snippet": item.get("content", "")
                } for item in results[:5]]
            }
        except Exception:
            return {
                "query": query,
                "source": "searxng",
                "results": [{"title": "error", "url": "", "snippet": payload[:200]}],
            }
