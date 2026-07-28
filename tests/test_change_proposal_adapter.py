import copy
import tempfile
import unittest
from pathlib import Path

from brain.change_proposal import (
    ChangeProposal,
    FileChange,
    ProposalBudget,
    TestSpec,
)
from brain.change_proposal_adapter import (
    ChangeProposalAdaptationError,
    ChangeProposalAdapter,
)


def valid_arguments():
    return {
        "changes": [
            {
                "path": "src/new.py",
                "operation": "create",
                "new_content": "print('ok')\n",
                "expected_sha256": None,
                "justification": "add entry point",
            }
        ],
        "tests": [
            {
                "scope": "focused",
                "targets": ["tests.test_demo.DemoTests.test_ok"],
            },
            {"scope": "full", "targets": []},
        ],
        "justification": "implement requested behavior",
        "risks": ["creates one source file"],
        "budget": {
            "modified_files": 1,
            "new_files": 1,
            "write_bytes": 12,
            "changed_lines": 1,
        },
    }


class ChangeProposalAdapterTests(unittest.TestCase):
    def setUp(self):
        self.adapter = ChangeProposalAdapter()

    def test_builds_exact_existing_domain_objects(self):
        proposal = self.adapter.adapt(valid_arguments())

        self.assertIsInstance(proposal, ChangeProposal)
        self.assertIsInstance(proposal.changes[0], FileChange)
        self.assertIsInstance(proposal.tests[0], TestSpec)
        self.assertIsInstance(proposal.budget, ProposalBudget)
        self.assertEqual(proposal.changes[0].path, "src/new.py")
        self.assertEqual(proposal.tests[0].targets, (
            "tests.test_demo.DemoTests.test_ok",
        ))
        self.assertEqual(
            proposal.budget,
            ProposalBudget(1, 1, 12, 1),
        )

    def test_rejects_unknown_root_key(self):
        arguments = valid_arguments()
        arguments["extra"] = True
        with self.assertRaisesRegex(
            ChangeProposalAdaptationError,
            "Claves desconocidas en proposal",
        ):
            self.adapter.adapt(arguments)

    def test_rejects_unknown_nested_keys(self):
        cases = (
            ("changes", 0, "command"),
            ("tests", 0, "timeout"),
            ("budget", None, "currency"),
        )
        for section, index, key in cases:
            with self.subTest(section=section, key=key):
                arguments = valid_arguments()
                target = (
                    arguments[section]
                    if index is None
                    else arguments[section][index]
                )
                target[key] = "unexpected"
                with self.assertRaisesRegex(
                    ChangeProposalAdaptationError,
                    "Claves desconocidas",
                ):
                    self.adapter.adapt(arguments)

    def test_rejects_invalid_container_and_scalar_types_without_coercion(self):
        cases = (
            ("changes", "not-a-sequence"),
            ("tests", {"scope": "full"}),
            ("risks", "not-a-sequence"),
            ("justification", 7),
            ("budget", []),
        )
        for key, value in cases:
            with self.subTest(key=key):
                arguments = valid_arguments()
                arguments[key] = value
                with self.assertRaises(ChangeProposalAdaptationError):
                    self.adapter.adapt(arguments)

        arguments = valid_arguments()
        arguments["budget"]["modified_files"] = True
        with self.assertRaisesRegex(
            ChangeProposalAdaptationError,
            "modified_files debe ser un entero",
        ):
            self.adapter.adapt(arguments)

        arguments = valid_arguments()
        arguments["tests"][0]["targets"] = {
            "tests.test_demo.DemoTests.test_ok": True
        }
        with self.assertRaisesRegex(
            ChangeProposalAdaptationError,
            r"proposal\.tests\[0\]\.targets debe ser una lista o tupla",
        ):
            self.adapter.adapt(arguments)

    def test_rejects_unsupported_file_operation(self):
        arguments = valid_arguments()
        arguments["changes"][0]["operation"] = "delete"
        with self.assertRaisesRegex(
            ChangeProposalAdaptationError,
            "operation debe ser uno de",
        ):
            self.adapter.adapt(arguments)

    def test_rejects_arbitrary_test_commands_and_invalid_specs(self):
        arguments = valid_arguments()
        arguments["tests"][0] = {
            "scope": "focused",
            "command": ["python", "-c", "danger"],
        }
        with self.assertRaisesRegex(
            ChangeProposalAdaptationError,
            "Claves desconocidas",
        ):
            self.adapter.adapt(arguments)

        arguments = valid_arguments()
        arguments["tests"][0] = {"scope": "shell", "targets": []}
        with self.assertRaisesRegex(
            ChangeProposalAdaptationError,
            "scope debe ser uno de",
        ):
            self.adapter.adapt(arguments)

    def test_rejects_statically_inconsistent_budget(self):
        for field in (
            "modified_files",
            "new_files",
            "write_bytes",
            "changed_lines",
        ):
            with self.subTest(field=field):
                arguments = valid_arguments()
                arguments["budget"][field] += 1
                with self.assertRaisesRegex(
                    ChangeProposalAdaptationError,
                    "Presupuesto declarativo inconsistente",
                ):
                    self.adapter.adapt(arguments)

    def test_accepts_already_resolved_plain_values(self):
        arguments = valid_arguments()
        arguments["changes"][0]["path"] = "resolved/value.py"
        arguments["tests"][0]["targets"] = (
            "tests.test_resolved.ResolvedTests.test_value",
        )

        proposal = self.adapter.adapt(arguments)

        self.assertEqual(proposal.changes[0].path, "resolved/value.py")
        self.assertEqual(
            proposal.tests[0].targets,
            ("tests.test_resolved.ResolvedTests.test_value",),
        )

    def test_does_not_mutate_nested_input(self):
        arguments = valid_arguments()
        original = copy.deepcopy(arguments)

        self.adapter.adapt(arguments)

        self.assertEqual(arguments, original)

    def test_adaptation_has_no_filesystem_or_execution_effects(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / "marker.txt"
            marker.write_text("unchanged", encoding="utf-8")
            before = tuple(root.iterdir())

            proposal = self.adapter.adapt(valid_arguments())

            self.assertIsInstance(proposal, ChangeProposal)
            self.assertEqual(tuple(root.iterdir()), before)
            self.assertEqual(marker.read_text(encoding="utf-8"), "unchanged")

    def test_replace_defers_filesystem_dependent_line_budget(self):
        arguments = valid_arguments()
        arguments["changes"][0] = {
            "path": "existing.py",
            "operation": "replace",
            "new_content": "new\ncontent\n",
            "expected_sha256": "0" * 64,
        }
        arguments["budget"].update(
            new_files=0,
            write_bytes=12,
            changed_lines=99,
        )

        proposal = self.adapter.adapt(arguments)

        self.assertEqual(proposal.changes[0].operation, "replace")
        self.assertEqual(proposal.budget.changed_lines, 99)


if __name__ == "__main__":
    unittest.main()
