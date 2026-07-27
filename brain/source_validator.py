from urllib.parse import urlparse


class SourceValidator:
    def __init__(self):
        self.trust_rules = {
            "python.org": 100,
            "docs.python.org": 100,
            "github.com": 90,
            "developer.mozilla.org": 90,
            "stackoverflow.com": 70,
            "wikipedia.org": 70,
            "medium.com": 40,
            "blog": 30,
            "reddit.com": 35,
            "youtube.com": 25,
        }

    def score(self, item):
        if not isinstance(item, dict):
            return 0

        url = str(item.get("url") or "").strip().lower()
        title = str(item.get("title") or item.get("name") or "").lower()
        snippet = str(item.get("snippet") or item.get("content") or "").lower()

        score = 0
        parsed = urlparse(url)
        hostname = parsed.netloc.lower()

        for domain, value in self.trust_rules.items():
            if domain in hostname or domain in title or domain in snippet:
                score = max(score, value)

        if "documentation" in title or "docs" in title:
            score = max(score, 95)
        if "official" in title or "official" in snippet:
            score = max(score, 85)
        if "error" in title or "error" in snippet:
            score += 5
        if "python" in title or "python" in snippet:
            score += 3

        return min(score, 100)

    def enrich(self, items):
        return [{**item, "source_score": self.score(item)} for item in items]
