import json
import os
from datetime import datetime, timezone
from pathlib import Path

from tools.tool_result import ToolResult


def _safe_json_value(value, active_ids=None):
    """Return a JSON-compatible logging value without trusting user objects."""
    if active_ids is None:
        active_ids = set()

    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, os.PathLike):
        path_value = os.fspath(value)
        if isinstance(path_value, bytes):
            return path_value.decode("utf-8", errors="replace")
        return path_value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")

    is_container = isinstance(value, (dict, list, tuple, set, frozenset))
    value_id = id(value)
    if is_container:
        if value_id in active_ids:
            return "<circular-reference>"
        active_ids.add(value_id)

    try:
        if isinstance(value, dict):
            converted = {}
            for key, nested in value.items():
                safe_key = _safe_json_value(key, active_ids)
                if not isinstance(safe_key, (str, int, float, bool)) and safe_key is not None:
                    safe_key = json.dumps(
                        safe_key, ensure_ascii=False, sort_keys=True, default=str
                    )
                converted[str(safe_key)] = _safe_json_value(nested, active_ids)
            return converted
        if isinstance(value, (list, tuple)):
            return [_safe_json_value(item, active_ids) for item in value]
        if isinstance(value, (set, frozenset)):
            converted = [_safe_json_value(item, active_ids) for item in value]
            return sorted(
                converted,
                key=lambda item: json.dumps(
                    item, ensure_ascii=False, sort_keys=True, default=str
                ),
            )
    finally:
        if is_container:
            active_ids.remove(value_id)

    try:
        rendered = str(value)
    except Exception:
        rendered = ""
    if not rendered or rendered.startswith("<") and " at 0x" in rendered:
        return f"<{type(value).__module__}.{type(value).__qualname__}>"
    return rendered

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
        entry = {}
        try:
            entry = {
                "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "tool": tool_name,
                "params": {} if params is None else params,
                "result": "" if result is None else result,
            }
            if isinstance(result, ToolResult):
                result = result.to_dict()
            entry = {
                "timestamp": entry["timestamp"],
                "tool": _safe_json_value(tool_name),
                "params": _safe_json_value({} if params is None else params),
                "result": _safe_json_value("" if result is None else result),
            }
            history = _safe_json_value(self._read())
            history.append(entry)
            self.log_file.write_text(
                json.dumps(history, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except BaseException:
            return entry
        return entry

    def read(self):
        return self._read()
