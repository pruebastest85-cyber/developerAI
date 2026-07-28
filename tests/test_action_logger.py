import json
import tempfile
import unittest
import warnings
from datetime import datetime, timezone
from os import PathLike
from pathlib import Path
from unittest import mock

from tools.action_logger import ActionLogger
from tools.tool_result import ToolResult


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

    def test_log_serializes_tool_result(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "actions.json"
            logger = ActionLogger(log_file=log_file)

            entry = logger.log(
                "demo",
                result=ToolResult.failure("demo", error="boom"),
            )

            self.assertEqual(entry["result"]["status"], "failed")
            self.assertEqual(entry["result"]["error"], "boom")
            self.assertEqual(
                json.loads(log_file.read_text(encoding="utf-8"))[0]["result"]["tool_name"],
                "demo",
            )

    def test_log_defensively_serializes_non_json_and_circular_values(self):
        class CustomValue:
            def __str__(self):
                return "custom-value"

        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "actions.json"
            logger = ActionLogger(log_file=log_file)
            circular = []
            circular.append(circular)
            result = ToolResult.success(
                "demo",
                data={
                    "path": Path("nested/file.txt"),
                    "bytes": b"hola\xff",
                    "custom": CustomValue(),
                    "nested": [{"cycle": circular}],
                },
                metadata={"choices": {3, 1, 2}},
            )

            entry = logger.log(
                "demo",
                params={Path("key"): (b"value",)},
                result=result,
            )
            stored = json.loads(log_file.read_text(encoding="utf-8"))[0]

            self.assertEqual(stored["result"]["data"]["path"], "nested\\file.txt")
            self.assertEqual(stored["result"]["metadata"]["choices"], [1, 2, 3])
            self.assertEqual(stored["result"]["data"]["custom"], "custom-value")
            self.assertEqual(
                stored["result"]["data"]["nested"][0]["cycle"][0],
                "<circular-reference>",
            )
            self.assertIn("hola", stored["result"]["data"]["bytes"])
            self.assertEqual(entry["result"]["status"], "ok")

    def test_conversion_failure_is_absorbed_without_writing_partial_entry(self):
        class BrokenDict(dict):
            def items(self):
                raise RuntimeError("broken traversal")

        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "actions.json"
            original = '[{"tool": "existing"}]'
            log_file.write_text(original, encoding="utf-8")
            logger = ActionLogger(log_file=log_file)

            entry = logger.log("demo", params=BrokenDict({"value": 1}), result="ok")
            operation_continued = True

            self.assertTrue(operation_continued)
            self.assertEqual(entry["tool"], "demo")
            self.assertEqual(log_file.read_text(encoding="utf-8"), original)

    def test_pathlike_str_bytes_and_failure_are_defensive(self):
        class StringPath(PathLike):
            def __fspath__(self):
                return "nested/string.txt"

        class BytesPath(PathLike):
            def __fspath__(self):
                return b"nested/bytes-\xff.txt"

        class BrokenPath(PathLike):
            def __fspath__(self):
                raise RuntimeError("broken fspath")

        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "actions.json"
            logger = ActionLogger(log_file=log_file)
            result = ToolResult.success(
                "demo",
                data={"paths": [StringPath(), BytesPath()]},
                metadata={"nested": {"path": StringPath()}},
            )

            logger.log("demo", result=result)
            stored = json.loads(log_file.read_text(encoding="utf-8"))[0]

            self.assertEqual(
                stored["result"]["data"]["paths"][0],
                "nested/string.txt",
            )
            self.assertEqual(
                stored["result"]["data"]["paths"][1],
                "nested/bytes-\ufffd.txt",
            )
            self.assertEqual(
                stored["result"]["metadata"]["nested"]["path"],
                "nested/string.txt",
            )

            before = log_file.read_text(encoding="utf-8")
            entry = logger.log("demo", result={"path": BrokenPath()})
            self.assertEqual(entry["tool"], "demo")
            self.assertEqual(log_file.read_text(encoding="utf-8"), before)

    def test_shared_value_is_not_mistaken_for_cycle(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "actions.json"
            logger = ActionLogger(log_file=log_file)
            shared = {"value": [1, 2]}

            logger.log("demo", result={"first": shared, "second": shared})
            stored = json.loads(log_file.read_text(encoding="utf-8"))[0]["result"]

            self.assertEqual(stored["first"], {"value": [1, 2]})
            self.assertEqual(stored["second"], {"value": [1, 2]})

    def test_heterogeneous_set_is_serialized_deterministically(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "actions.json"
            logger = ActionLogger(log_file=log_file)
            values = {1, "one", (2, "two")}

            logger.log("demo", result={"values": values})
            first = json.loads(log_file.read_text(encoding="utf-8"))[0]["result"]["values"]
            log_file.unlink()
            logger.log("demo", result={"values": values})
            second = json.loads(log_file.read_text(encoding="utf-8"))[0]["result"]["values"]

            self.assertEqual(first, second)
            self.assertEqual(len(first), 3)

    def test_write_failure_is_absorbed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "actions.json"
            logger = ActionLogger(log_file=log_file)

            with mock.patch.object(
                Path,
                "write_text",
                side_effect=OSError("disk unavailable"),
            ):
                entry = logger.log("demo", result={"value": 1})

            self.assertEqual(entry["tool"], "demo")
            self.assertFalse(log_file.exists())


if __name__ == "__main__":
    unittest.main()
