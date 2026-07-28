import json
from pathlib import Path
from urllib.parse import quote_plus
from urllib.request import Request, urlopen

from tools.action_logger import ActionLogger
from tools.base_tool import Tool
from tools.tool_result import ToolResult, legacy_tool_value


class InternetSearchTool(Tool):
    name = "internet_search"
    description = "Busca información actualizada en internet"
    requires_confirmation = False
    risk = "low"

    def __init__(self, endpoint=None, logger=None, settings_path=None):
        self.settings_path = Path(settings_path or "config/settings.json")
        self.endpoint = endpoint or self._load_endpoint_from_settings() or "https://searx.be/search"
        self.logger = logger or ActionLogger()

    def execute(self, args=None, structured=False):
        query = args.get("query", "") if isinstance(args, dict) else str(args or "")
        if not isinstance(query, str) or not query.strip():
            result = ToolResult.failure(
                self.name,
                error="Se requiere una consulta para buscar en internet",
            )
            return result if structured else legacy_tool_value(result)
        query = query.strip()

        try:
            url = self._build_url(query)
            request = Request(url, headers={"User-Agent": "DeveloperAI/1.0"})
            with urlopen(request, timeout=10) as response:
                payload = response.read().decode("utf-8")
            data = self._parse_result(payload, query)
            result = ToolResult.success(
                self.name,
                data=data,
                message=f"Se encontraron {len(data['results'])} resultados.",
            )
        except (OSError, UnicodeError, json.JSONDecodeError, UnexpectedSearchFormat) as exc:
            result = ToolResult.failure(
                self.name,
                error=(
                    f"Formato externo inesperado: {exc}"
                    if isinstance(exc, UnexpectedSearchFormat)
                    else str(exc)
                ),
                metadata={"exception_type": type(exc).__name__, "query": query},
                retryable=isinstance(exc, OSError),
            )

        self.logger.log(
            self.name,
            params={"query": query, "endpoint": self.endpoint},
            result=result,
        )
        return result if structured else legacy_tool_value(result)

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
        data = json.loads(payload)
        if not isinstance(data, dict):
            raise UnexpectedSearchFormat("la raíz JSON debe ser un objeto")
        results = data.get("results", [])
        if not isinstance(results, list):
            raise UnexpectedSearchFormat("results debe ser una lista")
        normalized_results = []
        for item in results[:5]:
            if not isinstance(item, dict):
                raise UnexpectedSearchFormat("cada resultado debe ser un objeto")
            fields = {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("content", ""),
            }
            if any(not isinstance(value, str) for value in fields.values()):
                raise UnexpectedSearchFormat(
                    "title, url y content deben ser cadenas"
                )
            normalized_results.append(fields)
        return {
            "query": query,
            "source": "searxng",
            "results": normalized_results,
        }


class UnexpectedSearchFormat(ValueError):
    """The remote search service returned valid JSON with an unusable schema."""
