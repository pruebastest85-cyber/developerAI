from brain.source_validator import SourceValidator


class ContextRanker:
    def __init__(self, max_items=6, max_chars=1800):
        self.max_items = max_items
        self.max_chars = max_chars
        self.source_validator = SourceValidator()

    def rank(self, items):
        ranked = []
        for item in items:
            if not isinstance(item, dict):
                continue
            priority = self._score_item(item)
            ranked.append({**item, "priority": priority})

        ranked = self.source_validator.enrich(ranked)
        for entry in ranked:
            entry["priority"] = entry.get("priority", 0) + (entry.get("source_score", 0) // 10)

        ranked.sort(key=lambda entry: entry.get("priority", 0), reverse=True)
        ranked = ranked[: self.max_items]

        return self._trim_to_limit(ranked)

    def _score_item(self, item):
        source = str(item.get("source", "")).lower()
        title = str(item.get("title", "") or item.get("name", "")).lower()
        snippet = str(item.get("snippet", "") or item.get("content", "") or "").lower()

        score = 0
        if source in {"project", "memory", "history", "user"}:
            score += 5
        if source in {"docs", "documentation", "official", "project"}:
            score += 4
        if source in {"searxng", "internet", "web"}:
            score += 2
        if "error" in title or "error" in snippet:
            score += 3
        if "python" in title or "python" in snippet:
            score += 2
        if item.get("priority") is not None:
            score += int(item["priority"])
        score += int(item.get("source_score", 0) // 10)
        return score

    def _trim_to_limit(self, ranked):
        text = ""
        for entry in ranked:
            line = self._render_entry(entry)
            if len(text) + len(line) > self.max_chars:
                break
            text += line + "\n"
        return [entry for entry in ranked if self._render_entry(entry) in text]

    def _render_entry(self, entry):
        title = entry.get("title") or entry.get("name") or entry.get("source") or "item"
        snippet = entry.get("snippet") or entry.get("content") or ""
        source = entry.get("source", "unknown")
        priority = entry.get("priority", 0)
        return f"[{priority}] {source}: {title} | {snippet}".strip()
