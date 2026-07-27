import json
import tempfile
import unittest
import warnings
from datetime import datetime, timezone
from pathlib import Path

from tools.action_logger import ActionLogger


class ActionLoggerTests(unittest.TestCase):
    def test_log_writes_entry_to_json_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "actions.json"
            logger = ActionLogger(log_file=log_file)
            with warnings.catch_warnings(record=True) as captured:
                warnings.simplefilter("always")
                entry = logger.log("demo_tool", params={"query": "hola"}, result="ok")

            self.assertIn("tool", entry)
            self.assertEqual(entry["tool"], "demo_tool")
            self.assertEqual(json.loads(log_file.read_text(encoding="utf-8"))[0]["tool"], "demo_tool")

            timestamp = entry["timestamp"]
            self.assertTrue(timestamp.endswith("Z"))
            parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            self.assertEqual(parsed.tzinfo, timezone.utc)
            self.assertFalse(any(issubclass(w.category, DeprecationWarning) for w in captured))


if __name__ == "__main__":
    unittest.main()
