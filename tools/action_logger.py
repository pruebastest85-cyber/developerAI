import json
from datetime import datetime, timezone
from pathlib import Path

class ActionLogger:
    def __init__(self, log_file=None):
        self.log_file = Path(log_file or "logs/agent_actions.json")
        self.log_file.parent.mkdir(parents=True, exist_ok=True)

    def _read(self):
        if not self.log_file.exists():
            return []

        try:
            text = self.log_file.read_text(encoding="utf-8")
            if not text.strip():
                return []
            payload = json.loads(text)
            if isinstance(payload, list):
                return payload
        except json.JSONDecodeError:
            pass

        return []

    def log(self, tool_name, params=None, result=None):
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "tool": tool_name,
            "params": params or {},
            "result": result or "",
        }
        history = self._read()
        history.append(entry)
        self.log_file.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
        return entry

    def read(self):
        return self._read()
