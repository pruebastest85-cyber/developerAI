from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from brain.change_proposal import (
    ChangeProposal,
    ChangeProposalStructureError,
    FileChange,
    ProposalBudget,
    TestSpec,
)


class ChangeProposalAdaptationError(ValueError):
    """Declarative workflow arguments do not match the proposal contract."""


_PROPOSAL_KEYS = frozenset(
    {"changes", "tests", "justification", "risks", "budget"}
)
_CHANGE_KEYS = frozenset(
    {
        "path",
        "operation",
        "new_content",
        "expected_sha256",
        "justification",
    }
)
_REQUIRED_CHANGE_KEYS = frozenset(
    {"path", "operation", "new_content", "expected_sha256"}
)
_TEST_KEYS = frozenset({"scope", "targets"})
_BUDGET_KEYS = frozenset(
    {"modified_files", "new_files", "write_bytes", "changed_lines"}
)


class ChangeProposalAdapter:
    """Build the existing immutable proposal domain from declarative values.

    This adapter is intentionally pure: it performs no workspace resolution.
    Filesystem-dependent preconditions and the exact line budget for replacements
    remain the responsibility of ``ChangeProposalValidator``.
    """

    def adapt(self, arguments: Mapping[str, Any]) -> ChangeProposal:
        root = self._mapping(arguments, "proposal")
        self._exact_keys(root, _PROPOSAL_KEYS, _PROPOSAL_KEYS, "proposal")

        changes = tuple(
            self._adapt_change(item, index)
            for index, item in enumerate(
                self._sequence(root["changes"], "proposal.changes")
            )
        )
        tests = tuple(
            self._adapt_test(item, index)
            for index, item in enumerate(
                self._sequence(root["tests"], "proposal.tests")
            )
        )
        risks = tuple(self._sequence(root["risks"], "proposal.risks"))
        budget = self._adapt_budget(root["budget"])

        try:
            proposal = ChangeProposal(
                changes=changes,
                tests=tests,
                justification=root["justification"],
                risks=risks,
                budget=budget,
            )
            self._validate_static_budget(proposal)
        except (ChangeProposalStructureError, UnicodeEncodeError) as exc:
            raise ChangeProposalAdaptationError(str(exc)) from exc
        return proposal

    def _adapt_change(self, value: Any, index: int) -> FileChange:
        label = f"proposal.changes[{index}]"
        item = self._mapping(value, label)
        self._exact_keys(
            item,
            _CHANGE_KEYS,
            _REQUIRED_CHANGE_KEYS,
            label,
        )
        try:
            return FileChange(
                path=item["path"],
                operation=item["operation"],
                new_content=item["new_content"],
                expected_sha256=item["expected_sha256"],
                justification=item.get("justification", ""),
            )
        except ChangeProposalStructureError as exc:
            raise ChangeProposalAdaptationError(str(exc)) from exc

    def _adapt_test(self, value: Any, index: int) -> TestSpec:
        label = f"proposal.tests[{index}]"
        item = self._mapping(value, label)
        self._exact_keys(item, _TEST_KEYS, {"scope"}, label)
        targets = (
            self._sequence(item["targets"], f"{label}.targets")
            if "targets" in item
            else ()
        )
        try:
            return TestSpec(
                scope=item["scope"],
                targets=targets,
            )
        except ChangeProposalStructureError as exc:
            raise ChangeProposalAdaptationError(str(exc)) from exc

    def _adapt_budget(self, value: Any) -> ProposalBudget:
        item = self._mapping(value, "proposal.budget")
        self._exact_keys(
            item,
            _BUDGET_KEYS,
            _BUDGET_KEYS,
            "proposal.budget",
        )
        try:
            return ProposalBudget(
                modified_files=item["modified_files"],
                new_files=item["new_files"],
                write_bytes=item["write_bytes"],
                changed_lines=item["changed_lines"],
            )
        except ChangeProposalStructureError as exc:
            raise ChangeProposalAdaptationError(str(exc)) from exc

    @staticmethod
    def _mapping(value: Any, label: str) -> Mapping[str, Any]:
        if not isinstance(value, Mapping):
            raise ChangeProposalAdaptationError(f"{label} debe ser un mapping")
        if any(not isinstance(key, str) for key in value):
            raise ChangeProposalAdaptationError(
                f"{label} solo admite claves de texto"
            )
        return value

    @staticmethod
    def _sequence(value: Any, label: str) -> tuple[Any, ...]:
        if not isinstance(value, (list, tuple)):
            raise ChangeProposalAdaptationError(
                f"{label} debe ser una lista o tupla"
            )
        return tuple(value)

    @staticmethod
    def _exact_keys(
        value: Mapping[str, Any],
        allowed: frozenset[str] | set[str],
        required: frozenset[str] | set[str],
        label: str,
    ) -> None:
        supplied = set(value)
        unknown = supplied.difference(allowed)
        missing = set(required).difference(supplied)
        if unknown:
            raise ChangeProposalAdaptationError(
                f"Claves desconocidas en {label}: {sorted(unknown)}"
            )
        if missing:
            raise ChangeProposalAdaptationError(
                f"Faltan claves en {label}: {sorted(missing)}"
            )

    @staticmethod
    def _validate_static_budget(proposal: ChangeProposal) -> None:
        calculated = {
            "modified_files": len(proposal.changes),
            "new_files": sum(
                change.operation == "create" for change in proposal.changes
            ),
            "write_bytes": sum(
                len(change.new_content.encode("utf-8"))
                for change in proposal.changes
            ),
        }
        declared = proposal.budget.canonical_dict()
        mismatches = {
            name: (declared[name], value)
            for name, value in calculated.items()
            if declared[name] != value
        }

        if all(change.operation == "create" for change in proposal.changes):
            changed_lines = sum(
                len(change.new_content.splitlines())
                for change in proposal.changes
            )
            if declared["changed_lines"] != changed_lines:
                mismatches["changed_lines"] = (
                    declared["changed_lines"],
                    changed_lines,
                )

        if mismatches:
            details = ", ".join(
                f"{name}=declarado:{declared_value}/calculado:{calculated_value}"
                for name, (declared_value, calculated_value) in sorted(
                    mismatches.items()
                )
            )
            raise ChangeProposalAdaptationError(
                f"Presupuesto declarativo inconsistente: {details}"
            )
