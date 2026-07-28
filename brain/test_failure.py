from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from brain.change_proposal import TestSpec
from tools.tool_result import ToolResult


MAX_RELEVANT_LINES = 12
MAX_RELEVANT_CHARS = 2000


class TestFailureFingerprintError(ValueError):
    """A stable fingerprint cannot be built from the supplied test result."""


def _normalize_text(value: Any) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\\", "/")
    text = re.sub(r"\b0x[0-9a-fA-F]+\b", "<address>", text)
    text = re.sub(
        r"\b\d{4}-\d{2}-\d{2}[T ][0-9:.+-]+Z?\b",
        "<timestamp>",
        text,
    )
    text = re.sub(r"\bin\s+\d+(?:\.\d+)?s\b", "in <duration>", text)
    text = re.sub(r"\b\d+(?:\.\d+)?\s*(?:ms|seconds?|secs?)\b", "<duration>", text)
    text = re.sub(
        r"(?:[A-Za-z]:)?/(?:Users|home)/[^/\s]+/AppData/Local/Temp/[^/\s]+",
        "<temp>",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"(?:[A-Za-z]:)?/[^\s:]+/DeveloperAI", "<workspace>", text)
    text = re.sub(r"/tmp/(?:tmp)?[A-Za-z0-9_.-]+", "<temp>", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _stable_lines(*values: Any) -> list[str]:
    lines: list[str] = []
    for value in values:
        if value is None:
            continue
        for line in str(value).splitlines():
            normalized = _normalize_text(line)
            if normalized and normalized not in lines:
                lines.append(normalized)
            if len(lines) >= MAX_RELEVANT_LINES:
                break
        if len(lines) >= MAX_RELEVANT_LINES:
            break
    joined = "\n".join(lines)[:MAX_RELEVANT_CHARS]
    return joined.splitlines()


def failure_category(result: ToolResult) -> str:
    if not isinstance(result, ToolResult):
        raise TestFailureFingerprintError("result debe ser ToolResult")
    data = result.data if isinstance(result.data, dict) else {}
    reason = result.metadata.get("reason")
    if reason == "timeout" or data.get("timed_out") is True:
        return "timeout"
    if reason == "zero_tests" or (
        data.get("tests_run") == 0 and data.get("returncode") == 0
    ):
        return "zero_tests"
    if result.metadata.get("exception_type"):
        return "spawn_error"
    return "test_failure"


def failure_fingerprint(test_spec: TestSpec, tool_result: ToolResult) -> str:
    if not isinstance(test_spec, TestSpec):
        raise TestFailureFingerprintError("test_spec debe ser TestSpec")
    if not isinstance(tool_result, ToolResult):
        raise TestFailureFingerprintError("tool_result debe ser ToolResult")
    if tool_result.status == "ok":
        raise TestFailureFingerprintError(
            "No se genera fingerprint para un resultado ok"
        )
    data = tool_result.data if isinstance(tool_result.data, dict) else {}
    command_args = data.get("command_args")
    if isinstance(command_args, (list, tuple)) and all(
        isinstance(item, str) for item in command_args
    ):
        command = [_normalize_text(item) for item in command_args]
    else:
        command = [
            test_spec.scope,
            *test_spec.targets,
        ]

    failed_ids = sorted(
        str(item)
        for item in data.get("failed_test_ids", ())
        if isinstance(item, str)
    )
    error_ids = sorted(
        str(item)
        for item in data.get("error_test_ids", ())
        if isinstance(item, str)
    )
    exception_types = tool_result.metadata.get("exception_types")
    if exception_types is None:
        exception_types = [tool_result.metadata.get("exception_type")]
    elif isinstance(exception_types, str):
        exception_types = [exception_types]
    exception_types = sorted(
        str(item) for item in exception_types if isinstance(item, str) and item
    )

    payload = {
        "test_spec": {
            "scope": test_spec.scope,
            "targets": list(test_spec.targets),
        },
        "command": command,
        "category": failure_category(tool_result),
        "returncode": data.get("returncode"),
        "failed_test_ids": failed_ids,
        "error_test_ids": error_ids,
        "exception_types": exception_types,
        "message": _normalize_text(tool_result.error or tool_result.message),
        "relevant": _stable_lines(
            tool_result.error,
            tool_result.message,
            data.get("stderr"),
            data.get("stdout"),
        ),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
