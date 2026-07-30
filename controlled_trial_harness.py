"""JSON-safe evidence persistence for manually controlled local-model trials.

The live operator remains the sole session authority.  Persisted evidence can
restore an equivalent public view, but it cannot recreate approvals, tokens,
or an executable session after the owning process exits.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType

from brain.programming_operator import ProgrammingOperatorView


class TrialEvidenceError(ValueError):
    """Closed, non-sensitive failure at the trial evidence boundary."""

    __slots__ = ("code", "category")

    def __init__(self, code="invalid_trial_evidence", category="evidence"):
        self.code = (
            code
            if code in {
                "invalid_trial_evidence",
                "cyclic_trial_evidence",
                "trial_evidence_write_failed",
            }
            else "invalid_trial_evidence"
        )
        allowed_categories = {
            "evidence", "session_id", "state", "plan_id",
            "approval_request_id", "error_code", "workflow_runtime_id",
            "correction_applications", "presentation", "plan", "approval",
            "tests", "diff", "write",
        }
        self.category = (
            category if category in allowed_categories else "evidence"
        )
        super().__init__(
            f"La evidencia temporal no es válida: {self.category}."
        )


def to_json_safe(value, active=None):
    """Copy supported public values into deterministic JSON-native structures."""
    active = set() if active is None else active
    if value is None or type(value) in {str, bool, int}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise TrialEvidenceError()
        return value

    value_id = id(value)
    if value_id in active:
        raise TrialEvidenceError("cyclic_trial_evidence") from None
    active.add(value_id)
    try:
        if isinstance(value, Mapping):
            result = {}
            for key, item in value.items():
                if type(key) is not str:
                    raise TrialEvidenceError()
                result[key] = to_json_safe(item, active)
            return result
        if (
            isinstance(value, Sequence)
            and not isinstance(value, (str, bytes, bytearray))
        ):
            return [to_json_safe(item, active) for item in value]
    except TrialEvidenceError:
        raise
    except BaseException:
        raise TrialEvidenceError() from None
    finally:
        active.remove(value_id)
    raise TrialEvidenceError() from None


def view_to_evidence(view):
    if type(view) is not ProgrammingOperatorView:
        raise TrialEvidenceError()
    return {
        name: to_json_safe(getattr(view, name))
        for name in view.__dataclass_fields__
    }


def _freeze_public(value):
    if type(value) is dict:
        return MappingProxyType(
            {key: _freeze_public(item) for key, item in value.items()}
        )
    if type(value) is list:
        return tuple(_freeze_public(item) for item in value)
    return value


_STATES = frozenset({
    "idle", "pending_plan", "running", "awaiting_approval",
    "awaiting_correction", "completed", "failed", "rejected", "cancelled",
})
_PLAN_FIELDS = frozenset({
    "id", "tool", "action", "goal", "depends_on", "arguments",
})
_APPROVAL_FIELDS = frozenset({
    "request_id", "tool", "action", "arguments",
})
_TEST_FIELDS = frozenset({
    "scope", "status", "tests_run", "passed", "failures", "errors", "skipped",
})


def _fail(category):
    raise TrialEvidenceError(category=category) from None


def _required_string(value, category, *, allow_empty=False):
    if type(value) is not str or not allow_empty and not value:
        _fail(category)


def _optional_string(value, category):
    if value is not None:
        _required_string(value, category)


def _json_value(value, category):
    if value is None or type(value) in {str, bool, int}:
        return
    if type(value) is float:
        if not math.isfinite(value):
            _fail(category)
        return
    if type(value) is list:
        for item in value:
            _json_value(item, category)
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                _fail(category)
            _json_value(item, category)
        return
    _fail(category)


def _validate_plan(plan):
    if type(plan) is not list:
        _fail("plan")
    for step in plan:
        if type(step) is not dict or set(step) != _PLAN_FIELDS:
            _fail("plan")
        for name in ("id", "tool", "action", "goal"):
            _required_string(step[name], "plan")
        if (
            type(step["depends_on"]) is not list
            or any(type(item) is not str or not item for item in step["depends_on"])
        ):
            _fail("plan")
        if type(step["arguments"]) not in {dict, list}:
            _fail("plan")
        _json_value(step["arguments"], "plan")


def _validate_approval(approval):
    if approval is None:
        return
    if type(approval) is not dict or set(approval) != _APPROVAL_FIELDS:
        _fail("approval")
    for name in ("request_id", "tool", "action"):
        _required_string(approval[name], "approval")
    if type(approval["arguments"]) not in {dict, list}:
        _fail("approval")
    _json_value(approval["arguments"], "approval")


def _validate_tests(tests):
    if type(tests) is not list:
        _fail("tests")
    for result in tests:
        if type(result) is not dict or set(result) != _TEST_FIELDS:
            _fail("tests")
        for name in ("scope", "status"):
            _required_string(result[name], "tests")
        for name in (
            "tests_run", "passed", "failures", "errors", "skipped",
        ):
            value = result[name]
            if type(value) is not int or value < 0:
                _fail("tests")


def _validate_evidence(evidence):
    expected = set(ProgrammingOperatorView.__dataclass_fields__)
    if type(evidence) is not dict or set(evidence) != expected:
        _fail("evidence")
    _required_string(evidence["session_id"], "session_id")
    if type(evidence["state"]) is not str or evidence["state"] not in _STATES:
        _fail("state")
    for name in (
        "plan_id", "approval_request_id", "error_code", "workflow_runtime_id",
    ):
        _optional_string(evidence[name], name)
    applications = evidence["correction_applications"]
    if type(applications) is not int or applications < 0:
        _fail("correction_applications")
    _required_string(
        evidence["presentation"], "presentation", allow_empty=True
    )
    _validate_plan(evidence["plan"])
    _validate_approval(evidence["approval"])
    _validate_tests(evidence["tests"])
    _required_string(evidence["diff"], "diff", allow_empty=True)


def evidence_to_view(evidence):
    try:
        _validate_evidence(evidence)
    except TrialEvidenceError:
        raise
    except BaseException:
        _fail("evidence")
    safe = evidence
    try:
        return ProgrammingOperatorView(
            session_id=safe["session_id"],
            state=safe["state"],
            plan_id=safe["plan_id"],
            approval_request_id=safe["approval_request_id"],
            error_code=safe["error_code"],
            workflow_runtime_id=safe["workflow_runtime_id"],
            correction_applications=safe["correction_applications"],
            presentation=safe["presentation"],
            plan=tuple(_freeze_public(item) for item in safe["plan"]),
            approval=(
                _freeze_public(safe["approval"])
                if safe["approval"] is not None
                else None
            ),
            tests=tuple(_freeze_public(item) for item in safe["tests"]),
            diff=safe["diff"],
        )
    except (KeyError, TypeError, ValueError):
        raise TrialEvidenceError() from None


class ControlledTrialHarness:
    """Persist public evidence while continuing only the live operator session."""

    def __init__(self, operator, evidence_path):
        if not callable(getattr(operator, "execute", None)):
            raise TrialEvidenceError()
        self._operator = operator
        self._evidence_path = Path(evidence_path)

    def execute(self, command):
        if type(command) is not str or not command:
            raise TrialEvidenceError()
        view = self._operator.execute(command)
        self.persist(view)
        return view

    def persist(self, view):
        evidence = view_to_evidence(view)
        serialized = json.dumps(
            evidence,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        temporary = self._evidence_path.with_suffix(
            self._evidence_path.suffix + ".tmp"
        )
        try:
            temporary.write_text(serialized, encoding="utf-8")
            temporary.replace(self._evidence_path)
        except OSError:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise TrialEvidenceError(
                "trial_evidence_write_failed", category="write"
            ) from None

    def restore(self):
        try:
            text = self._evidence_path.read_text(encoding="utf-8")

            def pairs(pairs_value):
                result = {}
                for key, value in pairs_value:
                    if key in result:
                        raise TrialEvidenceError()
                    result[key] = value
                return result

            evidence = json.loads(
                text,
                object_pairs_hook=pairs,
                parse_constant=lambda _value: (_ for _ in ()).throw(
                    TrialEvidenceError()
                ),
            )
        except TrialEvidenceError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise TrialEvidenceError() from None
        return evidence_to_view(evidence)
