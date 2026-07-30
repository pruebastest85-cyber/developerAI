import json
import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType
from unittest.mock import patch

from brain.programming_operator import ProgrammingOperatorView
from controlled_trial_harness import (
    ControlledTrialHarness,
    TrialEvidenceError,
    evidence_to_view,
    to_json_safe,
    view_to_evidence,
)


def view(**changes):
    values = {
        "session_id": "session-1",
        "state": "pending_plan",
        "plan_id": "mp1_plan",
        "approval_request_id": None,
        "error_code": None,
        "workflow_runtime_id": None,
        "correction_applications": 0,
        "presentation": "Plan listo",
        "plan": (
            MappingProxyType({
                "id": "inspect",
                "tool": "code_reader",
                "action": "read_file",
                "goal": "Inspect behavior",
                "depends_on": (),
                "arguments": MappingProxyType({"path": "behavior.py"}),
            }),
        ),
        "approval": None,
        "tests": (),
        "diff": "",
    }
    values.update(changes)
    return ProgrammingOperatorView(**values)


class ScriptedOperator:
    def __init__(self, *views):
        self.views = list(views)
        self.commands = []
        self.transport_calls = 0

    def execute(self, command):
        self.commands.append(command)
        return self.views.pop(0)


class ControlledTrialHarnessTests(unittest.TestCase):
    def test_mapping_proxy_is_copied_to_dict(self):
        original = MappingProxyType({"state": "pending_plan"})
        self.assertEqual(to_json_safe(original), {"state": "pending_plan"})

    def test_deep_mappings_tuples_and_lists_are_json_safe(self):
        original = MappingProxyType({
            "nested": (MappingProxyType({"items": [2, 1]}),),
        })
        self.assertEqual(to_json_safe(original), {
            "nested": [{"items": [2, 1]}],
        })

    def test_identity_state_and_human_approval_are_exact(self):
        original = view(
            state="awaiting_approval",
            approval_request_id="request-1",
            approval=MappingProxyType({
                "request_id": "request-1",
                "tool": "patch_generator",
                "action": "generate_patch",
                "arguments": MappingProxyType({"path": "behavior.py"}),
            }),
        )
        evidence = view_to_evidence(original)
        self.assertEqual(evidence["plan_id"], "mp1_plan")
        self.assertEqual(evidence["state"], "awaiting_approval")
        self.assertEqual(
            evidence["approval"]["request_id"],
            "request-1",
        )

    def test_conversion_does_not_mutate_original(self):
        arguments = {"path": "behavior.py"}
        original = MappingProxyType({"arguments": arguments})
        converted = to_json_safe(original)
        converted["arguments"]["path"] = "changed.py"
        self.assertEqual(arguments, {"path": "behavior.py"})

    def test_result_is_accepted_by_json_dumps(self):
        json.dumps(view_to_evidence(view()), allow_nan=False)

    def test_view_round_trip_restores_equivalent_public_session(self):
        original = view()
        restored = evidence_to_view(view_to_evidence(original))
        self.assertEqual(restored.session_id, original.session_id)
        self.assertEqual(restored.plan_id, original.plan_id)
        self.assertEqual(restored.state, original.state)
        self.assertEqual(dict(restored.plan[0]), dict(original.plan[0]))

    def test_regression_mapping_proxy_after_plan_approval_persists(self):
        pending = view(
            state="awaiting_approval",
            workflow_runtime_id="runtime-1",
            approval_request_id="request-1",
            approval=MappingProxyType({
                "request_id": "request-1",
                "tool": "patch_generator",
                "action": "generate_patch",
                "arguments": MappingProxyType({
                    "files": (("behavior.py", "replace"),),
                }),
            }),
        )
        operator = ScriptedOperator(pending)
        with tempfile.TemporaryDirectory() as directory:
            harness = ControlledTrialHarness(
                operator, Path(directory) / "evidence.json"
            )
            returned = harness.execute("aprobar-plan mp1_plan")
            restored = harness.restore()
        self.assertIs(returned, pending)
        self.assertEqual(restored.session_id, "session-1")
        self.assertEqual(restored.workflow_runtime_id, "runtime-1")
        self.assertEqual(
            restored.approval["arguments"]["files"][0],
            ("behavior.py", "replace"),
        )
        self.assertEqual(operator.commands, ["aprobar-plan mp1_plan"])

    def test_live_session_continues_after_first_approval(self):
        operator = ScriptedOperator(
            view(state="awaiting_approval", approval_request_id="request-1"),
            view(state="completed", plan_id="mp1_plan"),
        )
        with tempfile.TemporaryDirectory() as directory:
            harness = ControlledTrialHarness(
                operator, Path(directory) / "evidence.json"
            )
            harness.execute("aprobar-plan mp1_plan")
            completed = harness.execute("aprobar request-1")
            restored = harness.restore()
        self.assertEqual(completed.state, "completed")
        self.assertEqual(restored.state, "completed")
        self.assertEqual(len(operator.commands), 2)

    def test_no_correction_path_completes_without_extra_command(self):
        operator = ScriptedOperator(view(state="completed"))
        with tempfile.TemporaryDirectory() as directory:
            harness = ControlledTrialHarness(
                operator, Path(directory) / "evidence.json"
            )
            harness.execute("aprobar-plan mp1_plan")
        self.assertEqual(operator.commands, ["aprobar-plan mp1_plan"])
        self.assertEqual(operator.transport_calls, 0)

    def test_corrective_second_pause_is_preserved_and_continues(self):
        correction = view(
            state="awaiting_approval",
            approval_request_id="correction-1",
            correction_applications=0,
            approval=MappingProxyType({
                "request_id": "correction-1",
                "tool": "correction_workflow",
                "action": "apply_correction",
                "arguments": MappingProxyType({
                    "files": (("behavior.py", "replace"),),
                }),
            }),
        )
        completed = view(
            state="completed",
            correction_applications=1,
        )
        operator = ScriptedOperator(correction, completed)
        with tempfile.TemporaryDirectory() as directory:
            harness = ControlledTrialHarness(
                operator, Path(directory) / "evidence.json"
            )
            harness.execute("aprobar-plan mp1_plan")
            paused = harness.restore()
            harness.execute("aprobar correction-1")
            final = harness.restore()
        self.assertEqual(paused.approval_request_id, "correction-1")
        self.assertEqual(final.correction_applications, 1)
        self.assertEqual(operator.transport_calls, 0)

    def test_unknown_non_finite_and_non_string_keys_are_rejected(self):
        for value in (
            object(),
            float("nan"),
            {1: "value"},
            {"unexpected"},
            frozenset({"unexpected"}),
        ):
            with self.subTest(type=type(value).__name__):
                with self.assertRaises(TrialEvidenceError):
                    to_json_safe(value)

    def test_cycles_are_rejected_with_closed_error(self):
        circular = []
        circular.append(circular)
        with self.assertRaises(TrialEvidenceError) as caught:
            to_json_safe(circular)
        self.assertEqual(caught.exception.code, "cyclic_trial_evidence")
        self.assertIsNone(caught.exception.__cause__)

    def test_errors_do_not_retain_unknown_values(self):
        marker = "SECRET_UNKNOWN_VALUE"

        class Unknown:
            def __repr__(self):
                return marker

        with self.assertRaises(TrialEvidenceError) as caught:
            to_json_safe(Unknown())
        exposed = " ".join([
            str(caught.exception),
            repr(caught.exception),
            repr(caught.exception.args),
            repr(caught.exception.__dict__),
        ])
        self.assertNotIn(marker, exposed)

    def test_failed_conversion_does_not_replace_previous_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.json"
            harness = ControlledTrialHarness(
                ScriptedOperator(), path
            )
            harness.persist(view())
            previous = path.read_bytes()
            with self.assertRaises(TrialEvidenceError):
                harness.persist(view(plan=(MappingProxyType({"bad": object()}),)))
            self.assertEqual(path.read_bytes(), previous)

    def test_representative_round_trip_preserves_all_public_fields(self):
        original = view(
            state="awaiting_approval",
            approval_request_id="approval-7",
            error_code="test_failed",
            workflow_runtime_id="runtime-9",
            correction_applications=2,
            approval=MappingProxyType({
                "request_id": "approval-7",
                "tool": "patch_generator",
                "action": "generate_patch",
                "arguments": MappingProxyType({
                    "files": (("behavior.py", "replace"),),
                }),
            }),
            tests=(MappingProxyType({
                "scope": "focused",
                "status": "failed",
                "tests_run": 1,
                "passed": 0,
                "failures": 1,
                "errors": 0,
                "skipped": 0,
            }),),
            diff="- return 1\n+ return 2\n",
        )
        restored = evidence_to_view(view_to_evidence(original))
        self.assertEqual(view_to_evidence(restored), view_to_evidence(original))
        self.assertIsInstance(restored.plan[0], MappingProxyType)
        self.assertIsInstance(restored.approval, MappingProxyType)
        self.assertIsInstance(restored.tests[0], MappingProxyType)

    def test_all_public_states_and_optional_identifiers_round_trip(self):
        states = (
            "idle", "pending_plan", "running", "awaiting_approval",
            "awaiting_correction", "completed", "failed", "rejected",
            "cancelled",
        )
        for state in states:
            with self.subTest(state=state):
                self.assertEqual(
                    evidence_to_view(view_to_evidence(view(state=state))).state,
                    state,
                )
        for field in (
            "plan_id", "approval_request_id", "error_code",
            "workflow_runtime_id",
        ):
            for value in (None, "identifier"):
                with self.subTest(field=field, value=value):
                    restored = evidence_to_view(
                        view_to_evidence(view(**{field: value}))
                    )
                    self.assertEqual(getattr(restored, field), value)

    def test_restored_view_has_no_execution_authority(self):
        restored = evidence_to_view(view_to_evidence(view()))
        self.assertFalse(hasattr(restored, "execute"))
        self.assertFalse(hasattr(restored, "approve"))

    def test_invalid_top_level_types_are_rejected_without_coercion(self):
        invalid = {
            "session_id": [],
            "state": 7,
            "correction_applications": "1",
            "presentation": [],
            "plan": "not-a-plan",
            "approval": [],
            "tests": {},
            "diff": [],
        }
        base = view_to_evidence(view())
        for field, value in invalid.items():
            with self.subTest(field=field):
                evidence = dict(base)
                evidence[field] = value
                with self.assertRaises(TrialEvidenceError) as caught:
                    evidence_to_view(evidence)
                self.assertEqual(caught.exception.category, field)

    def test_invalid_state_counts_and_optional_ids_are_rejected(self):
        base = view_to_evidence(view())
        cases = [
            ("state", "future_state"),
            ("correction_applications", True),
            ("correction_applications", -1),
        ]
        for field in (
            "plan_id", "approval_request_id", "error_code",
            "workflow_runtime_id",
        ):
            cases.extend((field, value) for value in ("", [], 1, False))
        for field, value in cases:
            with self.subTest(field=field, value=value):
                evidence = dict(base)
                evidence[field] = value
                with self.assertRaises(TrialEvidenceError) as caught:
                    evidence_to_view(evidence)
                self.assertEqual(caught.exception.category, field)

    def test_missing_and_additional_top_level_fields_are_rejected(self):
        base = view_to_evidence(view())
        missing = dict(base)
        missing.pop("state")
        for evidence in (missing, dict(base, unexpected="value")):
            with self.assertRaises(TrialEvidenceError) as caught:
                evidence_to_view(evidence)
            self.assertEqual(caught.exception.category, "evidence")

    def test_nested_plan_approval_and_test_shapes_are_closed(self):
        base = view_to_evidence(view())
        step = dict(base["plan"][0])
        invalid_plans = []
        for field, value in (
            ("id", None), ("tool", 1), ("action", ""), ("goal", []),
            ("depends_on", {}), ("depends_on", [""]),
            ("arguments", "path"), ("arguments", {1: "value"}),
            ("arguments", {"value": object()}),
        ):
            candidate = dict(step)
            candidate[field] = value
            invalid_plans.append([candidate])
        invalid_plans.extend((
            [dict(step, unexpected=True)],
            [{"id": "missing-fields"}],
            [None],
        ))
        for plan in invalid_plans:
            with self.subTest(section="plan"):
                evidence = dict(base)
                evidence["plan"] = plan
                with self.assertRaises(TrialEvidenceError) as caught:
                    evidence_to_view(evidence)
                self.assertEqual(caught.exception.category, "plan")

        approval = {
            "request_id": "request-1",
            "tool": "patch_generator",
            "action": "generate_patch",
            "arguments": {"path": "behavior.py"},
        }
        invalid_approvals = (
            dict(approval, request_id=None),
            dict(approval, tool=1),
            dict(approval, action=""),
            dict(approval, arguments="path"),
            dict(approval, arguments={1: "value"}),
            dict(approval, unexpected=True),
            {"request_id": "missing-fields"},
        )
        for candidate in invalid_approvals:
            with self.subTest(section="approval"):
                evidence = dict(base)
                evidence["approval"] = candidate
                with self.assertRaises(TrialEvidenceError) as caught:
                    evidence_to_view(evidence)
                self.assertEqual(caught.exception.category, "approval")

        result = {
            "scope": "focused", "status": "ok", "tests_run": 1,
            "passed": 1, "failures": 0, "errors": 0, "skipped": 0,
        }
        invalid_results = (
            dict(result, scope=None),
            dict(result, status=""),
            dict(result, tests_run=True),
            dict(result, passed=-1),
            dict(result, failures="0"),
            dict(result, unexpected=True),
            {"scope": "missing-fields"},
            None,
        )
        for candidate in invalid_results:
            with self.subTest(section="tests"):
                evidence = dict(base)
                evidence["tests"] = [candidate]
                with self.assertRaises(TrialEvidenceError) as caught:
                    evidence_to_view(evidence)
                self.assertEqual(caught.exception.category, "tests")

    def test_validation_error_is_closed_and_never_coerces_sensitive_value(self):
        marker = "SECRET_REJECTED_VALUE"

        class Sensitive:
            def __str__(self):
                return marker

            def __repr__(self):
                return marker

        evidence = view_to_evidence(view())
        evidence["session_id"] = Sensitive()
        with self.assertRaises(TrialEvidenceError) as caught:
            evidence_to_view(evidence)
        exposed = " ".join((
            str(caught.exception),
            repr(caught.exception),
            repr(caught.exception.args),
            repr(caught.exception.__dict__),
        ))
        self.assertEqual(caught.exception.category, "session_id")
        self.assertNotIn(marker, exposed)

    def test_restoration_rejects_non_json_containers_instead_of_coercing(self):
        base = view_to_evidence(view())
        cases = (
            ("evidence", MappingProxyType(base)),
            ("plan", tuple(base["plan"])),
            ("plan", [dict(base["plan"][0], depends_on=())]),
            ("plan", [dict(
                base["plan"][0],
                arguments=MappingProxyType({"path": "behavior.py"}),
            )]),
        )
        for category, replacement in cases:
            with self.subTest(category=category):
                evidence = replacement
                if category != "evidence":
                    evidence = dict(base)
                    evidence[category] = replacement
                with self.assertRaises(TrialEvidenceError) as caught:
                    evidence_to_view(evidence)
                self.assertEqual(caught.exception.category, category)

    def test_partial_write_is_removed_and_final_is_not_created(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.json"
            temporary = path.with_suffix(".json.tmp")
            harness = ControlledTrialHarness(ScriptedOperator(), path)
            original_write = Path.write_text

            def partial_then_fail(target, data, **kwargs):
                original_write(target, data[:7], **kwargs)
                raise OSError("SECRET_WRITE_FAILURE")

            with patch.object(Path, "write_text", partial_then_fail):
                with self.assertRaises(TrialEvidenceError) as caught:
                    harness.persist(view())
            self.assertEqual(caught.exception.code, "trial_evidence_write_failed")
            self.assertEqual(caught.exception.category, "write")
            self.assertFalse(path.exists())
            self.assertFalse(temporary.exists())
            self.assertNotIn("SECRET_WRITE_FAILURE", str(caught.exception))

    def test_failed_overwrite_preserves_previous_bytes_and_removes_temp(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.json"
            temporary = path.with_suffix(".json.tmp")
            harness = ControlledTrialHarness(ScriptedOperator(), path)
            harness.persist(view())
            previous = path.read_bytes()
            original_write = Path.write_text

            def partial_then_fail(target, data, **kwargs):
                original_write(target, data[:5], **kwargs)
                raise OSError("write failed")

            with patch.object(Path, "write_text", partial_then_fail):
                with self.assertRaises(TrialEvidenceError):
                    harness.persist(view(state="completed"))
            self.assertEqual(path.read_bytes(), previous)
            self.assertFalse(temporary.exists())

    def test_cleanup_targets_only_exact_temp_and_preserves_unrelated_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.json"
            temporary = path.with_suffix(".json.tmp")
            unrelated = Path(directory) / "unrelated.tmp"
            unrelated.write_text("keep", encoding="utf-8")
            harness = ControlledTrialHarness(ScriptedOperator(), path)
            original_write = Path.write_text
            original_unlink = Path.unlink
            unlinked = []

            def partial_then_fail(target, data, **kwargs):
                original_write(target, data[:3], **kwargs)
                raise OSError("write failed")

            def record_unlink(target, *args, **kwargs):
                unlinked.append(target)
                return original_unlink(target, *args, **kwargs)

            with patch.object(Path, "write_text", partial_then_fail):
                with patch.object(Path, "unlink", record_unlink):
                    with self.assertRaises(TrialEvidenceError):
                        harness.persist(view())
            self.assertEqual(unlinked, [temporary])
            self.assertEqual(unrelated.read_text(encoding="utf-8"), "keep")

    def test_cleanup_failure_remains_closed_and_does_not_replace_write_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.json"
            temporary = path.with_suffix(".json.tmp")
            harness = ControlledTrialHarness(ScriptedOperator(), path)
            original_write = Path.write_text

            def partial_then_fail(target, data, **kwargs):
                original_write(target, data[:3], **kwargs)
                raise OSError("SECRET_PRIMARY")

            with patch.object(Path, "write_text", partial_then_fail):
                with patch.object(
                    Path, "unlink", side_effect=OSError("SECRET_CLEANUP")
                ):
                    with self.assertRaises(TrialEvidenceError) as caught:
                        harness.persist(view())
            exposed = " ".join((
                str(caught.exception),
                repr(caught.exception.args),
                repr(caught.exception.__dict__),
            ))
            self.assertEqual(caught.exception.code, "trial_evidence_write_failed")
            self.assertEqual(caught.exception.category, "write")
            self.assertNotIn("SECRET_PRIMARY", exposed)
            self.assertNotIn("SECRET_CLEANUP", exposed)
            temporary.unlink(missing_ok=True)

    def test_all_harness_paths_make_zero_transport_calls(self):
        operator = ScriptedOperator(view())
        with tempfile.TemporaryDirectory() as directory:
            harness = ControlledTrialHarness(
                operator, Path(directory) / "evidence.json"
            )
            harness.execute("programar: fixture")
            harness.restore()
        self.assertEqual(operator.transport_calls, 0)


if __name__ == "__main__":
    unittest.main()
